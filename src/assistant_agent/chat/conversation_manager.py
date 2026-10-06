"""Conversation admission, startup, and background lifecycle work."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Awaitable

from assistant_agent import sandbox

from . import constants
from .chat_error import ChatError
from .claude_process import ClaudeProcess
from .conversation import Conversation

logger = logging.getLogger(__name__)


async def cleanup_orphans(service: sandbox.Sandbox) -> None:
    """Retire groups left by an earlier app instance (single-worker deployment)."""
    await service.reconcile()



class ConversationManager:
    """Own loop-confined conversations, shielded submissions, and background cleanup."""

    def __init__(
        self, process_factory=ClaudeProcess, service: sandbox.Sandbox | None = None
    ) -> None:
        """Initialize conversation limits, task tracking, and sandbox ownership."""
        self.process_factory = process_factory
        self.sandbox_service = service if service is not None else sandbox.Sandbox()
        self.owns_sandbox_service = service is None
        self.conversations = {}
        self.cleanup_lock = asyncio.Lock()
        self.needs_cleanup = True
        self.stopped = False
        self.sweeper = None
        self.background_tasks: set[asyncio.Task] = set()
        self.teardown: asyncio.Task | None = None
        self.max_conversations = int(os.getenv("CHAT_MAX_SESSIONS", "4"))
        self.idle_seconds = int(os.getenv("CHAT_IDLE_SECONDS", "1800"))
        if self.max_conversations < 1 or self.idle_seconds < 1:
            raise ValueError("Chat capacity and idle timeout must be positive")

    async def start(self) -> None:
        """Attempt orphan cleanup and start the idle conversation sweeper."""
        try:
            await cleanup_orphans(self.sandbox_service)
            self.needs_cleanup = False
        except Exception:
            self.needs_cleanup = True
            logger.warning("Sandbox unavailable during chat startup cleanup")
        self.sweeper = asyncio.create_task(self._sweep())

    async def _sweep(self) -> None:
        """Periodically retire conversations that exceed the idle timeout."""
        while True:
            await asyncio.sleep(30)
            await self.sweep_once()

    async def sweep_once(self) -> None:
        """Remove idle conversations and track their process cleanup."""
        try:
            await self.sandbox_service.retry_cleanup()
        except Exception:
            logger.warning("Sandbox cleanup retry failed")
        expired = []
        for user, conversation in list(self.conversations.items()):
            if (
                not conversation.active
                and not conversation.starting
                and time.monotonic() - conversation.last_used >= self.idle_seconds
            ):
                self.conversations.pop(user)
                conversation.retire("Conversation expired after inactivity.")
                expired.append(conversation)
        tasks = [
            self._track(conversation.close("Conversation expired after inactivity."))
            for conversation in expired
        ]
        if tasks:
            await asyncio.shield(asyncio.gather(*tasks))

    def get(self, user: str) -> Conversation:
        """Return or create a user conversation within the capacity limit."""
        if self.stopped:
            raise ChatError("The assistant is unavailable.", 503)
        if user not in self.conversations:
            if len(self.conversations) >= self.max_conversations:
                raise ChatError("All conversation slots are busy. Try again later.", 503)
            self.conversations[user] = Conversation(user, self.sandbox_service, self._track)
        return self.conversations[user]

    async def submit(self, user: str, prompt: str, conversation_id: str | None = None) -> dict:
        # Own accepted work independently of an HTTP request's cancellation.
        """Submit a prompt as tracked work that survives request cancellation."""
        if self.stopped:
            raise ChatError("The assistant is unavailable.", 503)
        task = self._track(self._submit(user, prompt, conversation_id))
        return await asyncio.shield(task)

    def _track(self, coroutine: Awaitable) -> asyncio.Task:
        """Own a background task until it completes."""
        task = asyncio.create_task(coroutine)
        self.background_tasks.add(task)
        task.add_done_callback(self._background_task_done)
        return task

    def _background_task_done(self, task: asyncio.Task) -> None:
        """Remove completed work and retrieve any unobserved exception."""
        self.background_tasks.discard(task)
        if not task.cancelled():
            task.exception()  # retrieve failures even when the request disconnected

    async def _submit(self, user: str, prompt: str, conversation_id: str | None) -> dict:
        """Validate the prompt, begin a turn, and send it to the process."""
        conversation = self.get(user)
        if conversation_id and conversation_id != conversation.id:
            raise ChatError("Conversation reset. Reload before sending.")
        if conversation.failed:
            raise ChatError("Start a new conversation before sending.")
        if conversation.active or conversation.starting:
            raise ChatError("A response is already in progress.")
        if conversation.bytes + len(prompt.encode()) > constants.PROTOCOL_LIMIT:
            raise ChatError("Conversation limit reached. Start a new conversation.")
        process = conversation.process
        if process:
            result = self._begin_turn(conversation, prompt)
        else:
            conversation.starting = True
            process, result = await self._spawn(conversation, prompt)
        try:
            async with asyncio.timeout(sandbox.REQUEST_TIMEOUT_SECONDS):
                await process.send(prompt)
        except Exception:
            conversation.fail("The assistant conversation ended. Start a new conversation.")
        return result

    async def _spawn(self, conversation: Conversation, prompt: str) -> tuple[ClaudeProcess, dict]:
        """Start a reserved conversation process and close it if already retired."""
        process = None
        begun = False
        try:
            try:
                async with self.cleanup_lock:
                    if self.needs_cleanup:
                        await cleanup_orphans(self.sandbox_service)
                        self.needs_cleanup = False
                conversation.handle = await self.sandbox_service.allocate(
                    conversation.user, conversation.id
                )
                if conversation.retired or self.stopped:
                    raise ChatError("The assistant conversation ended. Start a new conversation.", 503)
                async with asyncio.timeout(constants.STARTUP_SECONDS):
                    process = await self.process_factory.create(
                        conversation.id, conversation.receive, conversation.fail, self.sandbox_service,
                        conversation.handle,
                    )
            except Exception:
                conversation.failed = True
                raise ChatError(
                    "The assistant is unavailable. Start a new conversation.", 503
                ) from None
            conversation.process = process
            if conversation.failed or self.stopped:
                raise ChatError("The assistant conversation ended. Start a new conversation.", 503)
            result = self._begin_turn(conversation, prompt)
            begun = True
            return process, result
        finally:
            conversation.starting = False
            if not begun:
                conversation.process = process
                conversation.request_close()
                if conversation.teardown:
                    await asyncio.shield(conversation.teardown)

    def _begin_turn(self, conversation: Conversation, prompt: str) -> dict:
        """Record the prompt, emit turn start, and arm the turn deadline."""
        conversation.active = uuid.uuid4().hex
        conversation.text_streamed = False
        conversation.turn_started = time.monotonic()
        logger.info("Claude conversation %s started turn", conversation.id)
        conversation.bytes += len(prompt.encode())
        conversation.transcript.extend(
            [
                dict(role="user", text=prompt, turn_id=conversation.active),
                dict(role="assistant", text="", turn_id=conversation.active),
            ]
        )
        conversation.emit("turn_start", text=prompt)
        turn_id = conversation.active
        conversation.timer = asyncio.get_running_loop().call_later(
            sandbox.REQUEST_TIMEOUT_SECONDS, self._timeout, conversation, turn_id
        )
        return dict(conversation_id=conversation.id, turn_id=turn_id)

    def _timeout(self, conversation: Conversation, turn_id: str) -> None:
        """Fail a turn only if its deadline still belongs to the active turn."""
        conversation.fail("The assistant timed out. Start a new conversation.", turn_id=turn_id)

    async def reset(self, user: str) -> None:
        """Track conversation reset independently of request cancellation."""
        task = self._track(self._reset(user))
        await asyncio.shield(task)

    async def _reset(self, user: str) -> None:
        """Remove a user conversation and wait for its process cleanup."""
        conversation = self.conversations.pop(user, None)
        if conversation:
            await conversation.close()

    async def close(self) -> None:
        """Retire all conversations and wait for shared manager teardown."""
        if self.teardown is None:
            self.stopped = True
            conversations = list(self.conversations.values())
            self.conversations.clear()
            for conversation in conversations:
                conversation.retire()
            self.teardown = asyncio.create_task(self._close(conversations))
        await asyncio.shield(self.teardown)

    async def _close(self, conversations: list[Conversation]) -> None:
        """Join background work and process cleanup before releasing owned clients."""
        try:
            if self.sweeper:
                self.sweeper.cancel()
                await asyncio.gather(self.sweeper, return_exceptions=True)
            results = await asyncio.gather(
                *(conversation.close() for conversation in conversations), return_exceptions=True
            )
            if self.background_tasks:
                await asyncio.gather(*list(self.background_tasks), return_exceptions=True)
            # Startup may attach a process after the first pass retired the conversation.
            results += await asyncio.gather(
                *(conversation.close() for conversation in conversations), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        finally:
            if self.owns_sandbox_service:
                await self.sandbox_service.close()

