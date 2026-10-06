"""Conversation admission, startup, and background lifecycle work."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Awaitable
from typing import TYPE_CHECKING

from assistant_agent import sandbox

from . import constants
from .chat_error import ChatError
from .claude_process import ClaudeProcess
from .conversation import Conversation

if TYPE_CHECKING:
    from assistant_agent.sandbox_access import SandboxAccessService

logger = logging.getLogger(__name__)


async def cleanup_orphans(service: sandbox.Sandbox) -> None:
    """Retire groups left by an earlier app instance (single-worker deployment)."""
    await service.reconcile()



class ConversationManager:
    """Own loop-confined conversations, shielded submissions, and background cleanup."""

    def __init__(
        self, process_factory=ClaudeProcess, service: sandbox.Sandbox | None = None,
        access_service: SandboxAccessService | None = None,
    ) -> None:
        """Initialize conversation limits, task tracking, and sandbox ownership."""
        self.process_factory = process_factory
        self.sandbox_service = service if service is not None else sandbox.Sandbox()
        self.owns_sandbox_service = service is None
        self.access_service = access_service
        self.needs_access_invalidation = access_service is not None
        self.conversations = {}
        self.blocked_users: set[str] = set()
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
            async with self.cleanup_lock:
                await self._invalidate_access()
        except Exception:
            logger.warning("Conversation access unavailable during startup invalidation")
        try:
            await cleanup_orphans(self.sandbox_service)
            self.needs_cleanup = False
        except Exception:
            self.needs_cleanup = True
            logger.warning("Sandbox unavailable during chat startup cleanup")
        self.sweeper = asyncio.create_task(self._sweep())

    async def _invalidate_access(self) -> None:
        """Gate launches until grants from the previous app instance are invalidated."""
        if self.needs_access_invalidation and self.access_service is not None:
            await self.access_service.invalidate_outstanding()
            self.needs_access_invalidation = False

    def resolve_live_assignment(
        self, user_id: str, conversation_id: str, container_id: str,
    ) -> sandbox.SandboxHandle | None:
        """Resolve only an existing, ready assignment using trusted grant identities."""
        conversation = self.conversations.get(user_id)
        if (
            self.stopped or conversation is None or conversation.conversation_id != conversation_id
            or conversation.is_starting or conversation.has_failed or conversation.is_retired
            or conversation.access_expired() or conversation.claude_process is None
            or conversation.claude_process.is_closed
        ):
            return None
        sandbox_handle = conversation.sandbox_handle
        if (
            sandbox_handle is None or sandbox_handle.user_id != user_id
            or sandbox_handle.conversation_id != conversation_id
            or sandbox_handle.container_id != container_id
        ):
            return None
        return sandbox_handle

    async def _sweep(self) -> None:
        """Periodically retire conversations that exceed the idle timeout."""
        while True:
            await asyncio.sleep(30)
            await self.sweep_once()

    async def sweep_once(self) -> None:
        """Remove idle conversations and track their process cleanup."""
        if self.access_service is not None:
            try:
                async with self.cleanup_lock:
                    await self._invalidate_access()
                await self.access_service.retry_failed_revocations()
            except Exception:
                logger.warning("Conversation access recovery retry failed")
        try:
            await self.sandbox_service.retry_cleanup()
        except Exception:
            logger.warning("Sandbox cleanup retry failed")
        expired = []
        for user, conversation in list(self.conversations.items()):
            if conversation.access_expired() or (
                not conversation.active_turn_id
                and not conversation.is_starting
                and time.monotonic() - conversation.last_activity_monotonic >= self.idle_seconds
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
        if self.stopped or user in self.blocked_users:
            raise ChatError("The assistant is unavailable.", 503)
        if user not in self.conversations:
            if len(self.conversations) >= self.max_conversations:
                raise ChatError("All conversation slots are busy. Try again later.", 503)
            self.conversations[user] = Conversation(
                user, self.sandbox_service, self._track, self.access_service,
            )
        return self.conversations[user]

    def block_user(self, user: str) -> None:
        """Synchronously deny admission before disconnect cleanup begins."""
        self.blocked_users.add(user)
        conversation = self.conversations.get(user)
        if conversation is not None:
            conversation.retire("Account disconnected.")

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
        if conversation_id and conversation_id != conversation.conversation_id:
            raise ChatError("Conversation reset. Reload before sending.")
        if conversation.access_expired():
            conversation.retire("Conversation access expired. Start a new conversation.")
            conversation.request_close()
            raise ChatError("Conversation access expired. Start a new conversation.")
        if conversation.has_failed:
            raise ChatError("Start a new conversation before sending.")
        if conversation.active_turn_id or conversation.is_starting:
            raise ChatError("A response is already in progress.")
        if conversation.transcript_size_bytes + len(prompt.encode()) > constants.PROTOCOL_LIMIT:
            raise ChatError("Conversation limit reached. Start a new conversation.")
        process = conversation.claude_process
        if process:
            result = self._begin_turn(conversation, prompt)
        else:
            conversation.is_starting = True
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
                    await self._invalidate_access()
                    if self.needs_cleanup:
                        await cleanup_orphans(self.sandbox_service)
                        self.needs_cleanup = False
                conversation.sandbox_handle = await self.sandbox_service.allocate(
                    conversation.user_id, conversation.conversation_id
                )
                if conversation.is_retired or self.stopped:
                    raise ChatError("The assistant conversation ended. Start a new conversation.", 503)
                if self.access_service is not None:
                    issued_access = await self.access_service.issue(
                        conversation.user_id, conversation.conversation_id, conversation.sandbox_handle.container_id,
                    )
                    conversation.access_token_id = issued_access.token_id
                    conversation.access_token_expires_at = issued_access.expires_at
                    del issued_access
                if conversation.is_retired or self.stopped or conversation.access_expired():
                    raise ChatError("The assistant conversation ended. Start a new conversation.", 503)
                async with asyncio.timeout(constants.STARTUP_SECONDS):
                    process_options = (
                        {"before_close": conversation.revoke_access}
                        if self.access_service is not None else {}
                    )
                    process = await self.process_factory.create(
                        conversation.conversation_id, conversation.receive, conversation.fail, self.sandbox_service,
                        conversation.sandbox_handle, **process_options,
                    )
            except Exception:
                conversation.has_failed = True
                raise ChatError(
                    "The assistant is unavailable. Start a new conversation.", 503
                ) from None
            conversation.claude_process = process
            if conversation.has_failed or conversation.is_retired or self.stopped or conversation.access_expired():
                raise ChatError("The assistant conversation ended. Start a new conversation.", 503)
            result = self._begin_turn(conversation, prompt)
            begun = True
            return process, result
        finally:
            conversation.is_starting = False
            if not begun:
                conversation.claude_process = process
                conversation.request_close()
                if conversation.cleanup_task:
                    await asyncio.shield(conversation.cleanup_task)

    def _begin_turn(self, conversation: Conversation, prompt: str) -> dict:
        """Record the prompt, emit turn start, and arm the turn deadline."""
        conversation.active_turn_id = uuid.uuid4().hex
        conversation.has_streamed_turn_text = False
        conversation.turn_started_monotonic = time.monotonic()
        logger.info("Claude conversation %s started turn", conversation.conversation_id)
        conversation.transcript_size_bytes += len(prompt.encode())
        conversation.transcript_messages.extend(
            [
                dict(role="user", text=prompt, turn_id=conversation.active_turn_id),
                dict(role="assistant", text="", turn_id=conversation.active_turn_id),
            ]
        )
        conversation.emit("turn_start", text=prompt)
        turn_id = conversation.active_turn_id
        conversation.turn_timeout_handle = asyncio.get_running_loop().call_later(
            sandbox.REQUEST_TIMEOUT_SECONDS, self._timeout, conversation, turn_id
        )
        return dict(conversation_id=conversation.conversation_id, turn_id=turn_id)

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
