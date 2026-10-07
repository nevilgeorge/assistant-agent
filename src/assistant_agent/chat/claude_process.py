"""Claude process groups and streaming Docker attachments."""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import shlex
import uuid
from typing import Any
from collections.abc import Awaitable, Callable

from aiodocker.stream import Stream

from assistant_agent import sandbox

from . import constants

logger = logging.getLogger(__name__)

GMAIL_TOOLS = frozenset({
    "mcp__gmail__search_emails", "mcp__gmail__get_email", "mcp__gmail__get_thread",
    "mcp__gmail__download_emails", "mcp__gmail__download_attachment",
})
LOCAL_APPROVALS = ["Read(//input/**)", "Bash(ls *)", "Bash(rg *)"]
DISCOVERY_SECONDS = 10
STATUS_POLL_SECONDS = 0.25


class GmailUnavailable(RuntimeError):
    """Gmail configuration or discovery failed; a chat-only replacement is allowed."""


class ClaudeProtocolError(RuntimeError):
    """The Claude transport or control protocol failed without exposing response contents."""


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
        "--append-system-prompt-file",
        "/workspace/CLAUDE.md",
        "--tools",
        "Read,Glob,Grep,Bash",
        "--add-dir",
        "/input",
        "--permission-mode",
        "dontAsk",
        "--mcp-config",
        "/run/assistant/mcp.json",
        "--strict-mcp-config",
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
        self.control_requests: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.transport_error: ClaudeProtocolError | None = None

    @classmethod
    async def create(
        cls, token: str, receive: Callable, failed: Callable, sandbox_service: sandbox.Sandbox,
        sandbox_handle: sandbox.SandboxHandle, *,
        before_close: Callable[[], Awaitable[None]] | None = None,
        mcp_token: str | None = None,
        gmail_enabled: bool = False,
    ) -> ClaudeProcess:
        """Start Claude, attach its stream, and wait for process readiness."""
        self = cls(sandbox_service, sandbox_handle)
        self.before_close = before_close
        self.path = f"/tmp/assistant-chat-{token}.pid"
        launch_argv = cls.argv + ["--allowedTools", *LOCAL_APPROVALS]
        if gmail_enabled:
            if not mcp_token:
                raise ValueError("Gmail launch requires an access grant")
            launch_argv += sorted(GMAIL_TOOLS)
        else:
            launch_argv += ["--disallowedTools", "mcp__*"]
        environment = {"ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
                       "MCP_TIMEOUT": "10000"}
        if gmail_enabled:
            environment["ASSISTANT_MCP_TOKEN"] = mcp_token
        mcp_token = None
        try:
            async with asyncio.timeout(constants.STARTUP_SECONDS):
                inner = f"echo $$ > {shlex.quote(self.path)}; exec {shlex.join(launch_argv)}"
                self.execution = await sandbox_service.exec(
                    f"exec setsid --wait bash -lc {shlex.quote(inner)}", stdin=True,
                    name=sandbox_handle.container_id,
                    environment=environment,
                )
                environment = {}
                self.docker_stream = self.execution.start()
                # start() is lazy. Attach before checking the PID file.
                await self.docker_stream.__aenter__()
                self.output_reader_task = asyncio.create_task(self._read(receive, failed))
                ready = await sandbox_service.run(
                    f"for i in {{1..100}}; do test -s {shlex.quote(self.path)} && exit 0; "
                    "sleep 0.05; done; exit 1",
                    timeout=7,
                    name=sandbox_handle.container_id,
                )
                if not ready.ok:
                    raise sandbox.SandboxError("Claude process did not initialize")
                if self.transport_error is not None:
                    raise self.transport_error
                return self
        except BaseException:
            environment = {}
            await self.close()
            raise

    async def _write(self, message: dict[str, Any]) -> None:
        """Serialize prompts and control requests onto the same stdin stream."""
        async with self.stdin_write_lock:
            if self.transport_error is not None:
                raise self.transport_error
            if self.is_closed or self.docker_stream is None:
                raise ClaudeProtocolError("Claude process closed")
            await self.docker_stream.write_in((json.dumps(message) + "\n").encode())

    async def send(self, prompt: str) -> None:
        """Write one JSON prompt while keeping stdin open for later turns."""
        await self._write({"type": "user", "message": {"role": "user", "content": prompt}})

    async def _control(self, subtype: str) -> dict[str, Any]:
        """Await a correlated control response without retaining sensitive response data."""
        request_id = uuid.uuid4().hex
        response_future = asyncio.get_running_loop().create_future()
        self.control_requests[request_id] = response_future
        try:
            await self._write({"type": "control_request", "request_id": request_id,
                               "request": {"subtype": subtype}})
            return await response_future
        finally:
            self.control_requests.pop(request_id, None)
            if response_future.done() and not response_future.cancelled():
                response_future.exception()
            else:
                response_future.cancel()

    async def discover_gmail(self) -> None:
        """Initialize Claude and require connected Gmail discovery before the first prompt."""
        try:
            async with asyncio.timeout(DISCOVERY_SECONDS):
                await self._control("initialize")
                while True:
                    status = await self._control("mcp_status")
                    servers = status.get("mcpServers")
                    if not isinstance(servers, list) or any(
                        not isinstance(server, dict)
                        or not isinstance(server.get("name"), str)
                        or not isinstance(server.get("status"), str)
                        for server in servers
                    ):
                        raise ClaudeProtocolError("Invalid MCP status response")
                    gmail = next((server for server in servers
                                  if isinstance(server, dict) and server.get("name") == "gmail"),
                                 None)
                    if gmail is not None:
                        state = gmail.get("status")
                        if state == "connected":
                            tools = gmail.get("tools")
                            if not isinstance(tools, list) or any(
                                not isinstance(tool, str)
                                and (not isinstance(tool, dict)
                                     or not isinstance(tool.get("name"), str))
                                for tool in tools
                            ):
                                raise ClaudeProtocolError("Invalid MCP tools response")
                            names = {tool.get("name") if isinstance(tool, dict) else tool
                                     for tool in tools}
                            advertised_names = {
                                f"mcp__gmail__{name}" if isinstance(name, str)
                                and not name.startswith("mcp__") else name
                                for name in names
                            }
                            if not GMAIL_TOOLS.issubset(advertised_names):
                                raise GmailUnavailable("gmail_missing_tools")
                            return
                        if state in {"failed", "needs-auth", "disabled"}:
                            raise GmailUnavailable("gmail_disconnected")
                    await asyncio.sleep(STATUS_POLL_SECONDS)
        except TimeoutError as exc:
            raise GmailUnavailable("gmail_discovery_timeout") from exc

    def _consume_control(self, message: dict[str, Any]) -> None:
        """Resolve control responses internally and discard uncorrelated responses."""
        response = message.get("response")
        if not isinstance(response, dict):
            raise ClaudeProtocolError("Invalid Claude control response")
        request_id = response.get("request_id")
        if not isinstance(request_id, str):
            raise ClaudeProtocolError("Invalid Claude control request ID")
        response_future = self.control_requests.get(request_id)
        if response_future is None or response_future.done():
            return
        if response.get("subtype") == "error":
            # Error strings and server descriptions may contain authentication headers.
            response_future.set_exception(ClaudeProtocolError("Claude control request failed"))
            return
        payload = response.get("response")
        if response.get("subtype") != "success" or not isinstance(payload, dict):
            raise ClaudeProtocolError("Invalid Claude control payload")
        response_future.set_result(payload)

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
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ClaudeProtocolError("Invalid Claude event")
                        if event.get("type") == "control_response":
                            self._consume_control(event)
                        elif event.get("type") in {"control_request", "control_cancel_request"}:
                            raise ClaudeProtocolError("Unexpected Claude control request")
                        else:
                            receive(event)
                if len(pending) > constants.PROTOCOL_LIMIT:
                    raise ValueError("Claude protocol line exceeds limit")
        except Exception:
            if not self.is_closed:
                logger.warning("Claude transport failed")
                self.transport_error = ClaudeProtocolError("Claude transport failed")
                for response_future in self.control_requests.values():
                    if not response_future.done():
                        response_future.set_exception(self.transport_error)
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
                result = await self.sandbox_service.run(
                    f"p=$(cat {shlex.quote(self.path)} 2>/dev/null); "
                    'case "$p" in ""|*[!0-9]*) exit 0;; esac; '
                    'kill -TERM -- "-$p" 2>/dev/null; sleep 0.2; '
                    'kill -KILL -- "-$p" 2>/dev/null; '
                    'for i in {1..20}; do kill -0 -- "-$p" 2>/dev/null || break; sleep 0.05; done; '
                    'kill -0 -- "-$p" 2>/dev/null && exit 1; '
                    f"rm -f {shlex.quote(self.path)}",
                    timeout=constants.TEARDOWN_SECONDS,
                    name=self.sandbox_handle.container_id,
                )
                if not result.ok:
                    raise sandbox.SandboxError("Claude process termination failed")
        except Exception as exc:
            logger.warning("Could not terminate Claude process group")
            raise sandbox.SandboxError("Claude process termination failed") from exc
        finally:
            for response_future in self.control_requests.values():
                if not response_future.done():
                    response_future.set_exception(ClaudeProtocolError("Claude process closed"))
            try:
                if self.docker_stream is not None:
                    async with asyncio.timeout(constants.TEARDOWN_SECONDS):
                        await self.docker_stream.close()
            finally:
                if self.output_reader_task:
                    self.output_reader_task.cancel()
                    await asyncio.gather(self.output_reader_task, return_exceptions=True)
