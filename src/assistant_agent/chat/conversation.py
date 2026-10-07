"""In-memory conversation state, replay, and assignment cleanup."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from assistant_agent import sandbox

from . import debug_logging
from .claude_process import ClaudeProcess

if TYPE_CHECKING:
    from assistant_agent.sandbox_access import SandboxAccessService
    from assistant_agent.session_files import SessionFilesService

logger = logging.getLogger(__name__)


class Conversation:
    """Keep a live conversation's transcript, turn state, and bounded event replay.

    Normalize Claude output into sequenced events for browser streaming, retain
    partial responses on failure, and track the active turn's deadline. All state
    belongs to the event loop. State is not persisted
    and the conversation's lifetime is independent of browser connections.
    """

    def __init__(
        self,
        user_id: str,
        sandbox_service: sandbox.Sandbox,
        track_background_task: Callable[[Awaitable[None]], asyncio.Task[None]],
        sandbox_access_service: SandboxAccessService | None = None,
        session_files_service: SessionFilesService | None = None,
    ) -> None:
        """Initialize an empty conversation and its turn and replay state."""
        self.user_id: str = user_id
        self.sandbox_service: sandbox.Sandbox = sandbox_service
        self.track_background_task: Callable[[Awaitable[None]], asyncio.Task[None]] = (
            track_background_task
        )
        self.sandbox_handle: sandbox.SandboxHandle | None = None
        self.conversation_id: str = uuid.uuid4().hex
        self.transcript_messages: list[dict[str, str]] = []
        self.replay_events: deque[dict[str, Any]] = deque(maxlen=2048)
        self.event_sequence: int = 0
        self.active_turn_id: str | None = None
        self.is_starting: bool = False
        self.has_failed: bool = False
        self.claude_process: ClaudeProcess | None = None
        self.debug_stream_enabled: bool = (
            os.getenv("CLAUDE_DEBUG_STREAM", "") in {"true", "1"}
            and os.getenv("APP_ENV", "").strip() != "production"
        )
        self.turn_timeout_handle: asyncio.TimerHandle | None = None
        self.last_activity_monotonic: float = time.monotonic()
        self.transcript_size_bytes: int = 0
        self.has_streamed_turn_text: bool = False
        self.turn_started_monotonic: float | None = None
        self.cleanup_task: asyncio.Task[None] | None = None
        self.is_retired: bool = False
        self.sandbox_access_service: SandboxAccessService | None = sandbox_access_service
        self.session_files_service: SessionFilesService | None = session_files_service
        self.access_token_id: str | None = None
        self.access_token_expires_at: datetime | None = None
        self.gmail_status: Literal["pending", "ready", "unavailable"] = "pending"

    def set_gmail_status(self, status: Literal["pending", "ready", "unavailable"]) -> None:
        """Publish resolved Gmail availability for live clients and reconnects."""
        if self.is_retired or self.has_failed or status == self.gmail_status:
            return
        self.gmail_status = status
        self.emit("gmail_status", gmail_status=status)

    def access_expired(self) -> bool:
        """Check the fixed grant deadline without extending it between turns."""
        return (
            self.access_token_expires_at is not None
            and datetime.now(UTC) >= self.access_token_expires_at
        )

    async def revoke_access(self) -> None:
        """Deny assignment access, revoke grants, and drain file work before cleanup."""
        self.access_token_id = None
        try:
            if self.sandbox_access_service is not None:
                await self.sandbox_access_service.revoke_conversation(self.conversation_id)
        except Exception:
            logger.warning("Conversation access revocation pending for %s", self.conversation_id)
        finally:
            if self.session_files_service is not None:
                await self.session_files_service.retire(self.conversation_id)

    def emit(self, kind: str, **data: Any) -> None:
        """Add a sequenced event to the bounded replay buffer."""
        self.event_sequence += 1
        event = dict(
            type=kind, conversation_id=self.conversation_id, turn_id=self.active_turn_id, sequence=self.event_sequence, **data
        )
        self.replay_events.append(event)

    def snapshot(self) -> dict:
        """Copy the transcript and current turn state for API responses."""
        return dict(
            conversation_id=self.conversation_id,
            transcript=[dict(m) for m in self.transcript_messages],
            sequence=self.event_sequence,
            active_turn=self.active_turn_id,
            failed=self.has_failed,
            gmail_status=self.gmail_status,
        )

    def _fail(self, message: str) -> None:
        """Mark the conversation failed, cancel its timer, and emit a failure event."""
        if self.has_failed:
            return
        self.has_failed = True
        logger.info("Claude conversation %s failed", self.conversation_id)
        if self.turn_timeout_handle:
            self.turn_timeout_handle.cancel()
        self.emit("turn_failure", message=message)
        self.active_turn_id = None

    def fail(self, message: str, turn_id: str | None = None) -> None:
        """Fail the current turn and request cleanup, ignoring stale deadlines."""
        if self.has_failed or (turn_id is not None and self.active_turn_id != turn_id):
            return
        self._fail(message)
        self.request_close()

    def receive(self, message: dict) -> None:
        """Handle output for an active turn and request cleanup on failure."""
        if not self.active_turn_id or self.has_failed:
            return
        if self.debug_stream_enabled and message.get("type") in {"assistant", "user"}:
            message_payload = message.get("message")
            if isinstance(message_payload, dict):
                self._debug_log_content(message["type"], message_payload.get("content", []))
        self._receive(message)
        process = self.claude_process if self.has_failed else None
        if process:
            self.request_close()

    def _debug_log_content(self, kind: str, content: Any) -> None:
        """Log completed content before UI filtering with the active turn's tags."""
        process = self.claude_process
        if process is None or not self.debug_stream_enabled:
            return
        debug_logging.emit_debug_record({
            "conversation_id": self.conversation_id,
            "container_id": process.sandbox_handle.container_id,
            "turn_id": self.active_turn_id,
            "stream": "stdout",
            "event": {"type": kind, "content": content},
        })

    def _receive(self, message: dict) -> None:
        """Translate Claude messages into text deltas and turn results."""
        kind = message.get("type")
        if kind == "stream_event":
            event = message.get("event", {})
            delta = event.get("delta", {})
            if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                self.append_assistant_text(delta.get("text", ""))
                self.has_streamed_turn_text = True
        elif kind == "assistant" and not self.has_streamed_turn_text:
            for block in message.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    self.append_assistant_text(block.get("text", ""))
        elif kind == "result":
            if message.get("is_error") or message.get("subtype") != "success":
                self._fail("The assistant could not complete the turn. Start a new conversation.")
                return
            if not self.transcript_messages[-1]["text"] and message.get("result"):
                self._debug_log_content("assistant", [{"type": "text", "text": message["result"]}])
                self.append_assistant_text(message["result"])
            if self.has_failed:
                return
            self.turn_timeout_handle.cancel()
            logger.info(
                "Claude conversation %s completed turn in %.2fs",
                self.conversation_id,
                time.monotonic() - self.turn_started_monotonic,
            )
            self.emit("turn_completion")
            self.active_turn_id = None
            self.last_activity_monotonic = time.monotonic()

    def append_assistant_text(self, text: str) -> None:
        """Append assistant text and emit a delta within the transcript limit."""
        if self.has_failed:
            return
        size = len(text.encode())
        if self.transcript_size_bytes + size > 2 * 1024 * 1024:
            self._fail("Conversation limit reached. Start a new conversation.")
            return
        self.transcript_size_bytes += size
        self.transcript_messages[-1]["text"] += text
        self.emit("assistant_delta", text=text)

    def retire(self, reason: str = "Conversation reset.") -> ClaudeProcess | None:
        """End the conversation once and return its process for cleanup."""
        if self.is_retired:
            return self.claude_process
        self.is_retired = True
        if self.turn_timeout_handle:
            self.turn_timeout_handle.cancel()
        self.has_failed = True
        self.emit("conversation_reset", message=reason)
        logger.info("Claude conversation %s retired", self.conversation_id)
        self.active_turn_id = None
        return self.claude_process

    def request_close(self) -> None:
        """Schedule process cleanup once without blocking state updates."""
        if not self.is_starting and self.cleanup_task is None:
            self.cleanup_task = self.track_background_task(self._close())

    async def _close(self) -> None:
        """Remove the assignment even when process termination fails."""
        await self.revoke_access()
        try:
            if self.claude_process:
                await self.claude_process.close()
        except Exception:
            logger.warning("Could not close conversation process")
        finally:
            if self.sandbox_handle:
                try:
                    await self.sandbox_service.destroy(self.sandbox_handle)
                except Exception:
                    logger.warning("Sandbox removal pending for conversation %s", self.conversation_id)

    async def close(self, reason: str = "Conversation reset.") -> None:
        """Retire the conversation and wait for its process cleanup."""
        self.retire(reason)
        if self.is_starting:
            # Startup owns final cleanup and repeats revocation after any in-flight issue.
            await self.revoke_access()
        self.request_close()
        if self.cleanup_task:
            await asyncio.shield(self.cleanup_task)
