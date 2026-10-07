"""Conversation-authenticated Gmail tools over stateless Streamable HTTP."""

from __future__ import annotations

import base64
import asyncio
import json
import time
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from collections import deque
from dataclasses import asdict, dataclass
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from mcp.server import MCPServer
from mcp.server.context import CallNext, ServerRequestContext
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from pydantic import Field, StrictInt
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .async_workers import AsyncWorker
from .gmail_service import BinaryContent, GmailError, GmailService
from .sandbox_access import (
    AuthorizedSandboxContext,
    LiveAssignmentResolver,
    SandboxAccessService,
    SandboxAuthorizationError,
)
from .session_files import Reauthorize, SessionFilesService


@dataclass(frozen=True)
class MCPLimits:
    """Bound HTTP request rates, payload sizes, and concurrent tool calls."""

    request_bytes: int = 64 * 1024
    response_bytes: int = 1024 * 1024
    requests_per_minute: int = 60
    conversation_calls: int = 2
    app_calls: int = 8

    def __post_init__(self) -> None:
        """Reject limits that are not positive integers."""
        if any(type(value) is not int or value <= 0 for value in asdict(self).values()):
            raise ValueError("MCP limits must be positive integers")


_ARGUMENTS = {
    "search_emails": {"query", "page_size", "cursor"},
    "get_email": {"message_id", "format"},
    "get_thread": {"thread_id"},
    "download_emails": {"message_ids", "format"},
    "download_attachment": {"message_id", "attachment_id"},
}
Identifier = Annotated[str, Field(strict=True, min_length=1, max_length=4096)]


def _bearer(headers: Mapping[str, str]) -> str | None:
    """Extract a bearer token, returning None for missing or malformed authorization."""
    value = headers.get("authorization", "")
    parts = value.split()
    return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None


async def _strict_arguments(context: ServerRequestContext, call_next: CallNext) -> Any:
    """Reject unknown tools and extra arguments before SDK handler dispatch."""
    if context.method == "tools/call" and isinstance(context.params, dict):
        tool_name = context.params.get("name")
        arguments = context.params.get("arguments", {})
        if not isinstance(tool_name, str) or tool_name not in _ARGUMENTS:
            raise MCPError(-32602, "Unknown tool")
        if not isinstance(arguments, dict) or arguments.keys() - _ARGUMENTS[tool_name]:
            raise MCPError(-32602, "Invalid tool arguments")
    return await call_next(context)


class GmailMCP:
    """Expose injected Gmail services through authenticated, bounded MCP tools."""

    def __init__(
        self,
        *,
        gmail_service: GmailService,
        session_files_service: SessionFilesService,
        access_service: SandboxAccessService,
        resolve_live_assignment: LiveAssignmentResolver,
        base_url: str,
        limits: MCPLimits | None = None,
    ) -> None:
        """Register tools and configure the HTTP transport using explicit dependencies."""
        self.gmail_service = gmail_service
        self.session_files_service = session_files_service
        self.access_service = access_service
        self.resolve_live_assignment = resolve_live_assignment
        self.limits = limits or MCPLimits()
        self.active_calls = 0
        self.conversation_calls: dict[str, int] = {}
        self.response_worker = AsyncWorker()
        self.requests: dict[str, deque[float]] = {}
        self.server = MCPServer("gmail", middleware=[_strict_arguments])
        self._register_tools()
        configured_url = urlsplit(base_url)
        allowed_hosts = ["app:8000", "localhost:*", "127.0.0.1:*", "[::1]:*"]
        if configured_url.netloc:
            allowed_hosts.append(configured_url.netloc)
        self.http_app = self.server.streamable_http_app(
            streamable_http_path="/gmail",
            stateless_http=True,
            json_response=True,
            max_request_body_size=self.limits.request_bytes,
            transport_security=TransportSecuritySettings(
                allowed_hosts=allowed_hosts,
                allowed_origins=[base_url],
            ),
        )
        self.http_app.add_middleware(BaseHTTPMiddleware, dispatch=self._authenticate_request)

    async def _authenticate_request(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Authenticate and rate-limit initialization, discovery, and tool requests."""
        try:
            identity = await self.access_service.authenticate(
                _bearer(request.headers), self.resolve_live_assignment
            )
        except SandboxAuthorizationError:
            return JSONResponse(
                {"error": "Sandbox access denied"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not self._admit_request(identity.conversation_id):
            return JSONResponse(
                {"error": "request_limit"}, status_code=429, headers={"Retry-After": "60"}
            )
        return await call_next(request)

    def _admit_request(self, conversation_id: str) -> bool:
        """Record a request if its conversation has capacity in the rolling minute."""
        now = time.monotonic()
        # Drop expired buckets so retired conversations do not accumulate forever.
        for expired_conversation_id, timestamps in list(self.requests.items()):
            while timestamps and timestamps[0] <= now - 60:
                timestamps.popleft()
            if not timestamps:
                self.requests.pop(expired_conversation_id)
        timestamps = self.requests.setdefault(conversation_id, deque())
        if len(timestamps) >= self.limits.requests_per_minute:
            return False
        timestamps.append(now)
        return True

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Own the SDK's AnyIO task group in one task throughout its lifetime."""
        ready = asyncio.Event()
        stop = asyncio.Event()

        async def manage() -> None:
            """Start the SDK session manager, signal readiness, and await shutdown."""
            async with self.server.session_manager.run():
                ready.set()
                await stop.wait()

        manager_task = asyncio.create_task(manage())
        ready_task = asyncio.create_task(ready.wait())
        try:
            await asyncio.wait({manager_task, ready_task}, return_when=asyncio.FIRST_COMPLETED)
            if manager_task.done():
                await manager_task
            yield
        finally:
            ready_task.cancel()
            await asyncio.gather(ready_task, return_exceptions=True)
            stop.set()
            await asyncio.shield(manager_task)

    def _output(self, result: Any) -> dict[str, Any]:
        """Serialize dataclass results and exact MIME bytes within the payload limit."""
        if isinstance(result, BinaryContent):
            if (len(result.data) + 2) // 3 * 4 > self.limits.response_bytes:
                raise ToolError('size_limit; use download_emails(format="eml")')
            output = {
                "message_id": result.message_id,
                "thread_id": result.thread_id,
                "encoding": "base64url",
                "bytes": len(result.data),
                "data": base64.urlsafe_b64encode(result.data).decode().rstrip("="),
            }
        else:
            output = asdict(result)
        if (
            len(json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode())
            > self.limits.response_bytes
        ):
            guidance = (
                '; use download_emails(format="eml")' if isinstance(result, BinaryContent) else ""
            )
            raise ToolError("size_limit" + guidance)
        return output

    async def _run(
        self,
        tool_name: str,
        context: Context,
        operation: Callable[[AuthorizedSandboxContext, Reauthorize], Awaitable[Any]],
    ) -> dict[str, Any]:
        """Authorize a tool call, bound concurrency, and return safe results or errors."""
        token = _bearer(context.headers or {})

        async def reauthorize() -> AuthorizedSandboxContext:
            """Revalidate the live grant and permission for this tool."""
            return await self.access_service.authorize(
                token, tool_name, self.resolve_live_assignment
            )

        try:
            identity = await reauthorize()
        except SandboxAuthorizationError:
            raise ToolError("Sandbox access denied") from None
        conversation_id = identity.conversation_id
        active = self.conversation_calls.get(conversation_id, 0)
        if active >= self.limits.conversation_calls or self.active_calls >= self.limits.app_calls:
            raise ToolError("concurrency_limit; retry later")
        self.active_calls += 1
        self.conversation_calls[conversation_id] = active + 1
        try:
            result = await operation(identity, reauthorize)
            return await self.response_worker.run(self._output, result)
        except GmailError as error:
            raise ToolError(f"{error.code}; retryable={error.retryable}") from None
        except SandboxAuthorizationError:
            raise ToolError("Sandbox access denied") from None
        except ToolError:
            raise
        except Exception:
            raise ToolError("operation_failed") from None
        finally:
            self.active_calls -= 1
            remaining = self.conversation_calls[conversation_id] - 1
            if remaining:
                self.conversation_calls[conversation_id] = remaining
            else:
                self.conversation_calls.pop(conversation_id)

    def _register_tools(self) -> None:
        """Register five tools with validated inputs and trusted identity resolution."""
        @self.server.tool()
        async def search_emails(
            query: Annotated[str, Field(strict=True, max_length=4096)],
            context: Context,
            page_size: Annotated[StrictInt, Field(ge=1, le=50)] = 50,
            cursor: Annotated[str, Field(strict=True, min_length=1, max_length=24 * 1024)]
            | None = None,
        ) -> dict[str, Any]:
            """
            Search Gmail, one page at a time. Follow cursor; partial results are not exhaustive.
            Gmail search syntax, as used in Gmail’s search box.
            Examples: from:alice@example.com, subject:invoice, after:2026/01/01 before:2026/02/01, or has:attachment.
            Combine filters to narrow results; broaden the query if relevant messages may have been missed.
            """
            return await self._run(
                "search_emails",
                context,
                lambda identity, _: (
                    self.gmail_service.search_emails(identity.user_id, query, page_size, cursor)
                ),
            )

        @self.server.tool()
        async def get_email(
            message_id: Identifier,
            context: Context,
            format: Literal["full", "raw"] = "full",
        ) -> dict[str, Any]:
            """Fetch fresh email content without files. Large raw MIME requires an EML download."""
            return await self._run(
                "get_email",
                context,
                lambda identity, _: (
                    self.gmail_service.get_email(identity.user_id, message_id, format)
                ),
            )

        @self.server.tool()
        async def get_thread(thread_id: Identifier, context: Context) -> dict[str, Any]:
            """Fetch a bounded thread without files; use message IDs for omitted content."""
            return await self._run(
                "get_thread",
                context,
                lambda identity, _: (self.gmail_service.get_thread(identity.user_id, thread_id)),
            )

        @self.server.tool()
        async def download_emails(
            message_ids: Annotated[list[Identifier], Field(min_length=1, max_length=50)],
            context: Context,
            format: Literal["text", "json", "eml"] = "text",
        ) -> dict[str, Any]:
            """Fetch explicit IDs anew into /input. Local searches cover only downloaded files.

            Prior downloads are preserved; inspect individual failures and truncation warnings.
            """
            return await self._run(
                "download_emails",
                context,
                lambda identity, reauthorize: (
                    self.session_files_service.download_emails(
                        identity, message_ids, format, reauthorize=reauthorize
                    )
                ),
            )

        @self.server.tool()
        async def download_attachment(
            message_id: Identifier,
            attachment_id: Identifier,
            context: Context,
        ) -> dict[str, Any]:
            """Fetch one attachment anew into /input as a generated .bin file."""
            return await self._run(
                "download_attachment",
                context,
                lambda identity, reauthorize: (
                    self.session_files_service.download_attachment(
                        identity, message_id, attachment_id, reauthorize=reauthorize
                    )
                ),
            )
