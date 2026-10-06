"""In-memory conversation state, replay, and assignment cleanup."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

from assistant_agent import sandbox

from .claude_process import ClaudeProcess

logger = logging.getLogger(__name__)


class Conversation:
    """Keep a live conversation's transcript, turn state, and bounded event replay.

    Normalize Claude output into sequenced events for browser streaming, retain
    partial responses on failure, and track the active turn's deadline. All state
    belongs to the event loop. State is not persisted
    and the conversation's lifetime is independent of browser connections.
    """

    def __init__(self, user: str, service: sandbox.Sandbox, track: Callable) -> None:
        """Initialize an empty conversation and its turn and replay state."""
        self.user = user
        self.service = service
        self.track = track
        self.handle: sandbox.SandboxHandle | None = None
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
        if not self.starting and self.teardown is None:
            self.teardown = self.track(self._close())

    async def _close(self) -> None:
        """Remove the assignment even when process termination fails."""
        try:
            if self.process:
                await self.process.close()
        except Exception:
            logger.warning("Could not close conversation process")
        finally:
            if self.handle:
                try:
                    await self.service.destroy(self.handle)
                except Exception:
                    logger.warning("Sandbox removal pending for conversation %s", self.id)

    async def close(self, reason: str = "Conversation reset.") -> None:
        """Retire the conversation and wait for its process cleanup."""
        self.retire(reason)
        self.request_close()
        if self.teardown:
            await asyncio.shield(self.teardown)

