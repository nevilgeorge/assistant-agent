"""Live-only Claude conversations, owned independently of HTTP connections."""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import shlex
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiodocker.stream import Stream

from assistant_agent import sandbox

logger = logging.getLogger(__name__)
STARTUP_SECONDS = 15
TEARDOWN_SECONDS = 10
PROTOCOL_LIMIT = 2 * 1024 * 1024


class ChatError(RuntimeError):
    """A user-facing chat error with the HTTP status returned by the web API."""

    def __init__(self, message, status=409):
        """Store the user-facing message and HTTP status."""
        super().__init__(message)
        self.status = status


class ClaudeProcess:
    """One process group and aiodocker stream; Claude protocol stays in the app."""

    argv = [
        "claude",
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--no-session-persistence",
        "--restricted",
        "--tools",
        "",
        "--disallowedTools",
        "mcp__*",
    ]

    def __init__(self, service: sandbox.Sandbox) -> None:
        """Initialize process resources without starting an exec."""
        self.service = service
        self.closed = False
        self.stream: Stream | None = None
        self.reader: asyncio.Task | None = None
        self.teardown: asyncio.Task | None = None
        self.write_lock = asyncio.Lock()

    @classmethod
    async def create(
        cls, token: str, receive: Callable, failed: Callable, service: sandbox.Sandbox
    ) -> ClaudeProcess:
        """Start Claude, attach its stream, and wait for process readiness."""
        self = cls(service)
        self.path = f"/tmp/assistant-chat-{token}.pid"
        try:
            async with asyncio.timeout(STARTUP_SECONDS):
                inner = f"echo $$ > {shlex.quote(self.path)}; exec {shlex.join(cls.argv)}"
                self.execution = await service.exec(
                    f"exec setsid --wait bash -lc {shlex.quote(inner)}", stdin=True
                )
                self.stream = self.execution.start()
                # start() is lazy. Attach before checking the PID file.
                await self.stream.__aenter__()
                ready = await service.run(
                    f"for i in {{1..100}}; do test -s {shlex.quote(self.path)} && exit 0; "
                    "sleep 0.05; done; exit 1",
                    timeout=7,
                )
                if not ready.ok:
                    raise sandbox.SandboxError("Claude process did not initialize")
                self.reader = asyncio.create_task(self._read(receive, failed))
                return self
        except BaseException:
            await self.close()
            raise

    async def send(self, prompt: str) -> None:
        """Write one JSON prompt while keeping stdin open for later turns."""
        message = {"type": "user", "message": {"role": "user", "content": prompt}}
        async with self.write_lock:
            if self.closed:
                raise RuntimeError("Claude process closed")
            await self.stream.write_in((json.dumps(message) + "\n").encode())

    async def _read(self, receive: Callable, failed: Callable) -> None:
        """Parse stdout JSON events, drain stderr, and report transport failures."""
        decoder = codecs.getincrementaldecoder("utf-8")()
        pending = ""
        try:
            while not self.closed:
                message = await self.stream.read_out()
                if message is None:
                    raise EOFError("Claude stream closed")
                if message.stream not in (1, 2):
                    raise ValueError("Invalid Docker stream ID")
                if message.stream == 2:
                    continue
                pending += decoder.decode(message.data)
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if len(line) > PROTOCOL_LIMIT:
                        raise ValueError("Claude protocol line exceeds limit")
                    if line.strip():
                        receive(json.loads(line))
                if len(pending) > PROTOCOL_LIMIT:
                    raise ValueError("Claude protocol line exceeds limit")
        except Exception:
            if not self.closed:
                logger.warning("Claude transport failed")
                failed("The assistant conversation ended. Start a new conversation.")
                self.request_close()

    def request_close(self) -> asyncio.Task:
        """Schedule teardown once without waiting for it to finish."""
        if self.teardown is None:
            self.closed = True
            self.teardown = asyncio.create_task(self._close())
        return self.teardown

    async def close(self) -> None:
        """Wait for shared teardown without cancelling it if the caller disconnects."""
        await asyncio.shield(self.request_close())

    async def _close(self) -> None:
        """Terminate the process group, close the stream, and join the reader."""
        try:
            async with asyncio.timeout(TEARDOWN_SECONDS):
                await self.service.run(
                    f"p=$(cat {shlex.quote(self.path)} 2>/dev/null); "
                    'case "$p" in ""|*[!0-9]*) exit 0;; esac; '
                    'kill -TERM -- "-$p" 2>/dev/null; sleep 0.2; '
                    'kill -KILL -- "-$p" 2>/dev/null; '
                    f"rm -f {shlex.quote(self.path)}",
                    timeout=TEARDOWN_SECONDS,
                )
        except Exception:
            logger.warning("Could not terminate Claude process group")
        finally:
            try:
                if self.stream is not None:
                    async with asyncio.timeout(TEARDOWN_SECONDS):
                        await self.stream.close()
            finally:
                if self.reader:
                    self.reader.cancel()
                    await asyncio.gather(self.reader, return_exceptions=True)


async def cleanup_orphans(service: sandbox.Sandbox) -> None:
    """Retire groups left by an earlier app instance (single-worker deployment)."""
    await service.run(
        "for f in /tmp/assistant-chat-*.pid; do "
        '[ -f "$f" ] || continue; p=$(cat "$f"); '
        'case "$p" in ""|*[!0-9]*) continue;; esac; '
        'if tr "\\0" " " < /proc/"$p"/cmdline 2>/dev/null | '
        "grep -q -- --input-format; then "
        'kill -KILL -- "-$p" 2>/dev/null; fi; rm -f "$f"; done',
        timeout=TEARDOWN_SECONDS,
    )


class Conversation:
    """Keep a live conversation's transcript, turn state, and bounded event replay.

    Normalize Claude output into sequenced events for browser streaming, retain
    partial responses on failure, and track the active turn's deadline. All state
    belongs to the event loop. State is not persisted
    and the conversation's lifetime is independent of browser connections.
    """

    def __init__(self) -> None:
        """Initialize an empty conversation and its turn and replay state."""
        self.id = uuid.uuid4().hex
        self.transcript = []
        self.events = deque(maxlen=2048)
        self.sequence = 0
        self.active = None
        self.starting = False
        self.failed = False
        self.process = None
        self.timer = None
        self.last_used = time.monotonic()
        self.bytes = 0
        self.text_streamed = False
        self.turn_started = None
        self.teardown: asyncio.Task | None = None
        self.retired = False

    def emit(self, kind: str, **data: Any) -> None:
        """Add a sequenced event to the bounded replay buffer."""
        self.sequence += 1
        event = dict(
            type=kind, conversation_id=self.id, turn_id=self.active, sequence=self.sequence, **data
        )
        self.events.append(event)

    def snapshot(self) -> dict:
        """Copy the transcript and current turn state for API responses."""
        return dict(
            conversation_id=self.id,
            transcript=[dict(m) for m in self.transcript],
            sequence=self.sequence,
            active_turn=self.active,
            failed=self.failed,
        )

    def _fail(self, message: str) -> None:
        """Mark the conversation failed, cancel its timer, and emit a failure event."""
        if self.failed:
            return
        self.failed = True
        logger.info("Claude conversation %s failed", self.id)
        if self.timer:
            self.timer.cancel()
        self.emit("turn_failure", message=message)
        self.active = None

    def fail(self, message: str, turn_id: str | None = None) -> None:
        """Fail the current turn and request cleanup, ignoring stale deadlines."""
        if self.failed or (turn_id is not None and self.active != turn_id):
            return
        self._fail(message)
        process = self.process
        if process:
            self.request_close()

    def receive(self, message: dict) -> None:
        """Handle output for an active turn and request cleanup on failure."""
        if not self.active or self.failed:
            return
        self._receive(message)
        process = self.process if self.failed else None
        if process:
            self.request_close()

    def _receive(self, message: dict) -> None:
        """Translate Claude messages into text deltas and turn results."""
        kind = message.get("type")
        if kind == "stream_event":
            event = message.get("event", {})
            delta = event.get("delta", {})
            if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                self.append(delta.get("text", ""))
                self.text_streamed = True
        elif kind == "assistant" and not self.text_streamed:
            for block in message.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    self.append(block.get("text", ""))
        elif kind == "result":
            if message.get("is_error") or message.get("subtype") != "success":
                self._fail("The assistant could not complete the turn. Start a new conversation.")
                return
            if not self.transcript[-1]["text"] and message.get("result"):
                self.append(message["result"])
            if self.failed:
                return
            self.timer.cancel()
            logger.info(
                "Claude conversation %s completed turn in %.2fs",
                self.id,
                time.monotonic() - self.turn_started,
            )
            self.emit("turn_completion")
            self.active = None
            self.last_used = time.monotonic()

    def append(self, text: str) -> None:
        """Append assistant text and emit a delta within the transcript limit."""
        if self.failed:
            return
        size = len(text.encode())
        if self.bytes + size > 2 * 1024 * 1024:
            self._fail("Conversation limit reached. Start a new conversation.")
            return
        self.bytes += size
        self.transcript[-1]["text"] += text
        self.emit("assistant_delta", text=text)

    def retire(self, reason: str = "Conversation reset.") -> ClaudeProcess | None:
        """End the conversation once and return its process for cleanup."""
        if self.retired:
            return self.process
        self.retired = True
        if self.timer:
            self.timer.cancel()
        self.failed = True
        self.emit("conversation_reset", message=reason)
        logger.info("Claude conversation %s retired", self.id)
        self.active = None
        return self.process

    def request_close(self) -> None:
        """Schedule process cleanup once without blocking state updates."""
        if self.process and self.teardown is None:
            self.teardown = asyncio.create_task(self.process.close())

    async def close(self, reason: str = "Conversation reset.") -> None:
        """Retire the conversation and wait for its process cleanup."""
        self.retire(reason)
        self.request_close()
        if self.teardown:
            await asyncio.shield(self.teardown)


class ConversationManager:
    """Own loop-confined conversations, shielded submissions, and background cleanup."""

    def __init__(
        self, process_factory=ClaudeProcess, service: sandbox.Sandbox | None = None
    ) -> None:
        """Initialize conversation limits, task tracking, and sandbox ownership."""
        self.factory = process_factory
        self.service = service if service is not None else sandbox.Sandbox()
        self.owns_service = service is None
        self.conversations = {}
        self.cleanup_lock = asyncio.Lock()
        self.needs_cleanup = False
        self.stopped = False
        self.sweeper = None
        self.work: set[asyncio.Task] = set()
        self.teardown: asyncio.Task | None = None
        self.max_conversations = int(os.getenv("CHAT_MAX_SESSIONS", "4"))
        self.idle_seconds = int(os.getenv("CHAT_IDLE_SECONDS", "1800"))
        if self.max_conversations < 1 or self.idle_seconds < 1:
            raise ValueError("Chat capacity and idle timeout must be positive")

    async def start(self) -> None:
        """Attempt orphan cleanup and start the idle conversation sweeper."""
        try:
            await cleanup_orphans(self.service)
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
            self.conversations[user] = Conversation()
        return self.conversations[user]

    async def submit(self, user: str, prompt: str, conversation_id: str | None = None) -> dict:
        # Own accepted work independently of an HTTP request's cancellation.
        """Submit a prompt as tracked work that survives request cancellation."""
        if self.stopped:
            raise ChatError("The assistant is unavailable.", 503)
        task = self._track(self._submit(user, prompt, conversation_id))
        return await asyncio.shield(task)

    def _track(self, work: Awaitable) -> asyncio.Task:
        """Own a background task until it completes."""
        task = asyncio.create_task(work)
        self.work.add(task)
        task.add_done_callback(self._work_done)
        return task

    def _work_done(self, task: asyncio.Task) -> None:
        """Remove completed work and retrieve any unobserved exception."""
        self.work.discard(task)
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
        if conversation.bytes + len(prompt.encode()) > PROTOCOL_LIMIT:
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
                        await cleanup_orphans(self.service)
                        self.needs_cleanup = False
                async with asyncio.timeout(STARTUP_SECONDS):
                    process = await self.factory.create(
                        conversation.id, conversation.receive, conversation.fail, self.service
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
            if process and not begun:
                await process.close()

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
            if self.work:
                await asyncio.gather(*list(self.work), return_exceptions=True)
            # Startup may attach a process after the first pass retired the conversation.
            results += await asyncio.gather(
                *(conversation.close() for conversation in conversations), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
        finally:
            if self.owns_service:
                await self.service.close()
