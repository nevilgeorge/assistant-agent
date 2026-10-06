"""Claude process groups and streaming Docker attachments."""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import shlex
from collections.abc import Awaitable, Callable

from aiodocker.stream import Stream

from assistant_agent import sandbox

from . import constants

logger = logging.getLogger(__name__)


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

    def __init__(self, sandbox_service: sandbox.Sandbox, sandbox_handle: sandbox.SandboxHandle) -> None:
        """Initialize process resources without starting an exec."""
        self.sandbox_service = sandbox_service
        self.sandbox_handle = sandbox_handle
        self.is_closed = False
        self.docker_stream: Stream | None = None
        self.output_reader_task: asyncio.Task | None = None
        self.teardown_task: asyncio.Task | None = None
        self.stdin_write_lock = asyncio.Lock()
        self.before_close: Callable[[], Awaitable[None]] | None = None

    @classmethod
    async def create(
        cls, token: str, receive: Callable, failed: Callable, sandbox_service: sandbox.Sandbox,
        sandbox_handle: sandbox.SandboxHandle, *,
        before_close: Callable[[], Awaitable[None]] | None = None,
    ) -> ClaudeProcess:
        """Start Claude, attach its stream, and wait for process readiness."""
        self = cls(sandbox_service, sandbox_handle)
        self.before_close = before_close
        self.path = f"/tmp/assistant-chat-{token}.pid"
        try:
            async with asyncio.timeout(constants.STARTUP_SECONDS):
                inner = f"echo $$ > {shlex.quote(self.path)}; exec {shlex.join(cls.argv)}"
                self.execution = await sandbox_service.exec(
                    f"exec setsid --wait bash -lc {shlex.quote(inner)}", stdin=True,
                    name=sandbox_handle.container_id,
                    environment={"ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", "")},
                )
                self.docker_stream = self.execution.start()
                # start() is lazy. Attach before checking the PID file.
                await self.docker_stream.__aenter__()
                ready = await sandbox_service.run(
                    f"for i in {{1..100}}; do test -s {shlex.quote(self.path)} && exit 0; "
                    "sleep 0.05; done; exit 1",
                    timeout=7,
                    name=sandbox_handle.container_id,
                )
                if not ready.ok:
                    raise sandbox.SandboxError("Claude process did not initialize")
                self.output_reader_task = asyncio.create_task(self._read(receive, failed))
                return self
        except BaseException:
            await self.close()
            raise

    async def send(self, prompt: str) -> None:
        """Write one JSON prompt while keeping stdin open for later turns."""
        message = {"type": "user", "message": {"role": "user", "content": prompt}}
        async with self.stdin_write_lock:
            if self.is_closed:
                raise RuntimeError("Claude process closed")
            await self.docker_stream.write_in((json.dumps(message) + "\n").encode())

    async def _read(self, receive: Callable, failed: Callable) -> None:
        """Parse stdout JSON events, drain stderr, and report transport failures."""
        decoder = codecs.getincrementaldecoder("utf-8")()
        pending = ""
        try:
            while not self.is_closed:
                message = await self.docker_stream.read_out()
                if message is None:
                    raise EOFError("Claude stream closed")
                if message.stream not in (1, 2):
                    raise ValueError("Invalid Docker stream ID")
                if message.stream == 2:
                    continue
                pending += decoder.decode(message.data)
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if len(line) > constants.PROTOCOL_LIMIT:
                        raise ValueError("Claude protocol line exceeds limit")
                    if line.strip():
                        receive(json.loads(line))
                if len(pending) > constants.PROTOCOL_LIMIT:
                    raise ValueError("Claude protocol line exceeds limit")
        except Exception:
            if not self.is_closed:
                logger.warning("Claude transport failed")
                failed("The assistant conversation ended. Start a new conversation.")
                self.request_close()

    def request_close(self) -> asyncio.Task:
        """Schedule teardown once without waiting for it to finish."""
        if self.teardown_task is None:
            self.is_closed = True
            self.teardown_task = asyncio.create_task(self._close())
        return self.teardown_task

    async def close(self) -> None:
        """Wait for shared teardown without cancelling it if the caller disconnects."""
        await asyncio.shield(self.request_close())

    async def _close(self) -> None:
        """Terminate the process group, close the stream, and join the reader."""
        if self.before_close is not None:
            try:
                await self.before_close()
            except Exception:
                logger.warning("Conversation access revocation pending before process cleanup")
        try:
            async with asyncio.timeout(constants.TEARDOWN_SECONDS):
                await self.sandbox_service.run(
                    f"p=$(cat {shlex.quote(self.path)} 2>/dev/null); "
                    'case "$p" in ""|*[!0-9]*) exit 0;; esac; '
                    'kill -TERM -- "-$p" 2>/dev/null; sleep 0.2; '
                    'kill -KILL -- "-$p" 2>/dev/null; '
                    f"rm -f {shlex.quote(self.path)}",
                    timeout=constants.TEARDOWN_SECONDS,
                    name=self.sandbox_handle.container_id,
                )
        except Exception:
            logger.warning("Could not terminate Claude process group")
        finally:
            try:
                if self.docker_stream is not None:
                    async with asyncio.timeout(constants.TEARDOWN_SECONDS):
                        await self.docker_stream.close()
            finally:
                if self.output_reader_task:
                    self.output_reader_task.cancel()
                    await asyncio.gather(self.output_reader_task, return_exceptions=True)
