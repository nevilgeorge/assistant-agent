"""Read-only Gmail retrieval, with bounded workers and explicit incomplete results.

There are no filesystem or MCP operations here. User IDs are internal identities
supplied by trusted callers, never Gmail account selectors.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import html
import json
import math
import random
import secrets
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httplib2
from google.auth.exceptions import GoogleAuthError
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from assistant_agent.agent_kit.emails.build_index import extract_body
from assistant_agent.async_workers import AsyncWorker
from assistant_agent.web_store import WebStore

_BODY_TYPES = {"text/plain", "text/html", "text/calendar", "application/ics"}


def _is_attachment(part: dict[str, Any], headers: dict[str, str]) -> bool:
    disposition = headers.get("content-disposition", "").lower()
    mime = part.get("mimeType", "")
    body = part.get("body", {})
    return bool(
        part.get("filename")
        or disposition.startswith("attachment")
        or mime == "message/rfc822"
        or (
            mime not in _BODY_TYPES
            and not mime.startswith("multipart/")
            and (disposition.startswith("inline") or body.get("attachmentId") or body.get("data"))
        )
    )


@dataclass(frozen=True)
class GmailLimits:
    user_concurrency: int = 5
    app_concurrency: int = 4
    user_units_per_second: float = 80
    app_units_per_second: float = 400
    search_seconds: float = 15
    retrieval_seconds: float = 30
    network_seconds: float = 10
    attempts: int = 3
    body_bytes: int = 256 * 1024
    response_bytes: int = 1024 * 1024
    binary_bytes: int = 32 * 1024 * 1024
    thread_messages: int = 50
    mime_depth: int = 30
    mime_parts: int = 1000

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive")
            if (
                name
                not in {
                    "user_units_per_second",
                    "app_units_per_second",
                    "search_seconds",
                    "retrieval_seconds",
                    "network_seconds",
                }
                and type(value) is not int
            ):
                raise ValueError(f"{name} must be an integer")
        if min(self.user_units_per_second, self.app_units_per_second) < 40:
            raise ValueError("Quota bucket capacity must accommodate a thread request (40 units)")
        if self.response_bytes < 1024:
            raise ValueError("response_bytes must be at least 1024")


class GmailError(Exception):
    """Safe public error; never contains an upstream response or credentials."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass
class ItemError:
    item_id: str
    code: str
    retryable: bool = False


@dataclass
class Attachment:
    part_id: str
    attachment_id: str | None
    filename: str
    mime_type: str
    size: int
    inline: bool = False


@dataclass
class EmailPreview:
    message_id: str
    thread_id: str
    headers: dict[str, str] = field(default_factory=dict)
    snippet: str = ""
    truncated: bool = False


@dataclass
class EmailContent(EmailPreview):
    internal_date: str | None = None
    body: str = ""
    body_source: str = "none"
    attachments: list[Attachment] = field(default_factory=list)
    errors: list[ItemError] = field(default_factory=list)


@dataclass
class SearchResult:
    messages: list[EmailPreview]
    estimated_total: int | None
    cursor: str | None
    has_more: bool
    metadata_incomplete: bool
    errors: list[ItemError]
    truncated: bool = False


@dataclass
class ThreadResult:
    thread_id: str
    messages: list[EmailContent]
    message_ids: list[str]
    omitted_messages: int
    truncated: bool
    ids_truncated: bool = False
    errors: list[ItemError] = field(default_factory=list)


@dataclass
class BinaryContent:
    message_id: str
    data: bytes
    attachment_id: str | None = None
    thread_id: str | None = None


@dataclass
class _UserState:
    semaphore: asyncio.Semaphore
    refresh_lock: asyncio.Lock
    bucket: _Bucket


class _Bucket:
    """One-second burst, with independently configurable sustained unit rate."""

    def __init__(self, rate: float, clock: Callable[[], float]) -> None:
        self.rate = rate
        self.clock = clock
        self.tokens = rate
        self.updated = clock()
        self.cooldown = 0.0

    def delay(self, units: int) -> float:
        now = self.clock()
        self.tokens = min(self.rate, self.tokens + max(0, now - self.updated) * self.rate)
        self.updated = now
        return max(0, self.cooldown - now, (units - self.tokens) / self.rate)


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decoded(value: str, limit: int) -> bytes:
    if not isinstance(value, str):
        raise GmailError("malformed_content")
    if len(value) > ((limit + 2) // 3) * 4:
        raise GmailError("size_limit")
    try:
        data = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise GmailError("malformed_content") from None
    if _encoded(data) != value.rstrip("="):
        raise GmailError("malformed_content")
    if len(data) > limit:
        raise GmailError("size_limit")
    return data


def _size(value: Any) -> int:
    return len(json.dumps(asdict(value), ensure_ascii=False, separators=(",", ":")).encode())


def _clip(value: str, limit: int) -> str:
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _headers(part: dict[str, Any]) -> dict[str, str]:
    return {str(h["name"]).lower(): str(h["value"]) for h in part.get("headers", [])}


def _http_failure(exc: HttpError) -> tuple[GmailError, str | None, float]:
    status = exc.resp.status
    reasons: set[str] = set()
    try:
        reasons = {e.get("reason", "") for e in json.loads(exc.content)["error"].get("errors", [])}
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    quota = None
    if "rateLimitExceeded" in reasons or "dailyLimitExceeded" in reasons:
        quota = "app"
    elif status == 429 or "userRateLimitExceeded" in reasons:
        quota = "user"
    retryable = status in {429, 500, 502, 503, 504} or (
        status == 403
        and bool(reasons & {"rateLimitExceeded", "userRateLimitExceeded", "backendError"})
    )
    code = {401: "reconnect_required", 403: "permission_denied", 404: "not_found"}.get(
        status, "upstream_error"
    )
    if quota:
        code = "quota_exceeded"
    delay = 0.0
    retry_after = exc.resp.get("retry-after", "")
    try:
        delay = max(0, float(retry_after))
    except (ValueError, TypeError):
        try:
            delay = max(
                0, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
            )
        except (ValueError, TypeError, OverflowError):
            pass
    return GmailError(code, retryable=retryable), quota, delay


class GmailService:
    def __init__(
        self,
        store: WebStore,
        *,
        limits: GmailLimits | None = None,
        request: Callable[..., dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[..., Any] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.store = store
        self.worker: AsyncWorker = store.google_worker
        self.limits = limits or GmailLimits()
        self.clock, self.sleep, self.jitter = clock, sleep, jitter
        self._request = request or self._sdk_request
        self._users: dict[str, _UserState] = {}
        self._app_semaphore = asyncio.Semaphore(self.limits.app_concurrency)
        self._app_bucket = _Bucket(self.limits.app_units_per_second, clock)
        self._cursor_key = secrets.token_bytes(32)
        self._local = threading.local()
        self._transports: list[httplib2.Http] = []
        self._transport_lock = threading.Lock()
        self._operations = 0
        self._drained = asyncio.Event()
        self._drained.set()
        self._closed = False

    def _user(self, user_id: str) -> _UserState:
        if user_id not in self._users:
            self._users[user_id] = _UserState(
                asyncio.Semaphore(self.limits.user_concurrency),
                asyncio.Lock(),
                _Bucket(self.limits.user_units_per_second, self.clock),
            )
        return self._users[user_id]

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        if self._closed:
            raise GmailError("service_closed")
        self._operations += 1
        self._drained.clear()
        try:
            yield
        finally:
            self._operations -= 1
            if not self._operations:
                self._drained.set()

    async def aclose(self) -> None:
        self._closed = True
        await self._drained.wait()
        for transport in self._transports:
            transport.close()
        self._transports.clear()
        self._users.clear()

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise GmailError("deadline_exceeded", retryable=True)
        return remaining

    async def _acquire(self, semaphore: Any, deadline: float) -> None:
        remaining = self._remaining(deadline)
        try:
            await asyncio.wait_for(semaphore.acquire(), remaining)
        except TimeoutError:
            raise GmailError("deadline_exceeded", retryable=True) from None

    async def _pause(self, seconds: float, deadline: float) -> None:
        if seconds >= self._remaining(deadline):
            raise GmailError("deadline_exceeded", retryable=True)
        await self.sleep(seconds)
        self._remaining(deadline)

    async def _credentials(self, user_id: str, deadline: float) -> Credentials:
        lock = self._user(user_id).refresh_lock
        await self._acquire(lock, deadline)
        # A separate task survives caller cancellation until the refresh worker drains.
        task = asyncio.create_task(self.store.load_refreshed(user_id))
        try:
            creds = await self._drain(task)
        except GoogleAuthError:
            raise GmailError("reconnect_required") from None
        finally:
            lock.release()
        self._remaining(deadline)
        if creds is None or not creds.token:
            raise GmailError("reconnect_required")
        return Credentials(token=creds.token, scopes=creds.scopes)

    @staticmethod
    async def _drain(
        task: asyncio.Task[Any],
        on_cancel: Callable[[], None] | None = None,
    ) -> Any:
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                if cancelled:
                    raise asyncio.CancelledError
                return result
            except asyncio.CancelledError:
                if not cancelled and on_cancel is not None:
                    on_cancel()
                if task.done():
                    # Retrieve a possible exception without exposing it to a cancelled caller.
                    if not task.cancelled():
                        task.exception()
                    raise
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise

    def _quota(self, state: _UserState, units: int) -> float:
        delay = max(state.bucket.delay(units), self._app_bucket.delay(units))
        if delay <= 0:
            state.bucket.tokens -= units
            self._app_bucket.tokens -= units
        return delay

    async def _attempt(
        self,
        user_id: str,
        creds: Credentials,
        method: str,
        params: dict[str, Any],
        deadline: float,
    ) -> dict[str, Any]:
        state = self._user(user_id)
        await self._acquire(state.semaphore, deadline)
        try:
            while True:
                await self._acquire(self._app_semaphore, deadline)
                try:
                    delay = self._quota(state, {"list": 5, "thread": 40}.get(method, 20))
                    if delay <= 0:
                        cancelled = threading.Event()

                        def execute() -> dict[str, Any]:
                            if cancelled.is_set():
                                raise GmailError("cancelled")
                            timeout = min(self.limits.network_seconds, self._remaining(deadline))
                            return self._request(creds, method, params, timeout)

                        return await self._drain(
                            asyncio.create_task(self.worker.run(execute)),
                            on_cancel=cancelled.set,
                        )
                finally:
                    self._app_semaphore.release()
                # A throttled user must not reserve app-wide capacity while waiting.
                await self._pause(delay, deadline)
        finally:
            state.semaphore.release()

    async def _call(
        self,
        user_id: str,
        creds: Credentials,
        method: str,
        params: dict[str, Any],
        deadline: float,
    ) -> dict[str, Any]:
        for attempt in range(self.limits.attempts):
            try:
                return await self._attempt(user_id, creds, method, params, deadline)
            except HttpError as exc:
                error, quota, retry_after = _http_failure(exc)
            except (OSError, socket.timeout, httplib2.HttpLib2Error):
                error, quota, retry_after = GmailError("network_error", retryable=True), None, 0
            except GoogleAuthError:
                raise GmailError("reconnect_required") from None
            delay = max(2**attempt + self.jitter(), retry_after)
            if quota:
                bucket = self._app_bucket if quota == "app" else self._user(user_id).bucket
                bucket.cooldown = max(bucket.cooldown, self.clock() + delay)
            if not error.retryable or attempt + 1 == self.limits.attempts:
                raise error from None
            await self._pause(delay, deadline)
        raise AssertionError("unreachable")

    def _sdk_request(
        self,
        creds: Credentials,
        method: str,
        params: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        if not hasattr(self._local, "http"):
            self._local.http = httplib2.Http(timeout=timeout)
            with self._transport_lock:
                self._transports.append(self._local.http)
        http = self._local.http
        http.timeout = timeout
        # Existing pooled sockets must also receive the new deadline-derived timeout.
        for connection in http.connections.values():
            connection.timeout = timeout
            if connection.sock:
                connection.sock.settimeout(timeout)
        authorized = AuthorizedHttp(
            Credentials(token=creds.token, scopes=creds.scopes),
            http=http,
            max_refresh_attempts=0,
        )
        gmail = build(
            "gmail",
            "v1",
            http=authorized,
            cache_discovery=False,
            static_discovery=True,
            num_retries=0,
        )
        users = gmail.users()
        resource = users.threads() if method == "thread" else users.messages()
        if method == "attachment":
            resource = resource.attachments()
        operation = resource.list if method == "list" else resource.get
        return operation(userId="me", **params).execute(num_retries=0)

    async def _cpu(self, function: Callable[..., Any], *args: Any) -> Any:
        """MIME decoding and HTML parsing also use the bounded worker."""
        return await self._drain(asyncio.create_task(self.worker.run(function, *args)))

    @staticmethod
    def _text(data: str, limit: int, charset: str) -> tuple[str, int, bool]:
        raw = _decoded(data, limit)
        try:
            return raw.decode(charset), len(raw), False
        except (LookupError, UnicodeError):
            return raw.decode("utf-8", errors="replace"), len(raw), True

    def _cursor(self, user_id: str, query: str, token: str) -> str:
        payload = json.dumps([user_id, query, token], separators=(",", ":")).encode()
        return _encoded(payload) + "." + _encoded(hmac.digest(self._cursor_key, payload, "sha256"))

    def _page_token(self, user_id: str, query: str, cursor: str | None) -> str | None:
        if cursor is None:
            return None
        try:
            payload, signature = cursor.split(".")
            data = _decoded(payload, 16 * 1024)
            if not hmac.compare_digest(
                _decoded(signature, 32), hmac.digest(self._cursor_key, data, "sha256")
            ):
                raise ValueError
            cursor_user, cursor_query, token = json.loads(data)
            if (cursor_user, cursor_query) != (user_id, query) or not isinstance(token, str):
                raise ValueError
            return token
        except (ValueError, TypeError, GmailError):
            raise GmailError("invalid_cursor") from None

    def _preview(self, message: dict[str, Any]) -> EmailPreview:
        headers = _headers(message.get("payload", {}))
        selected = {
            k: _clip(headers[k], 4096) for k in ("from", "to", "subject", "date") if k in headers
        }
        snippet = html.unescape(message.get("snippet", ""))
        return EmailPreview(
            message["id"],
            message.get("threadId", ""),
            selected,
            snippet[:512],
            any(len(headers[k].encode()) > 4096 for k in selected) or len(snippet) > 512,
        )

    async def search_emails(
        self,
        user_id: str,
        query: str,
        page_size: int = 20,
        cursor: str | None = None,
    ) -> SearchResult:
        if type(page_size) is not int or not 1 <= page_size <= 50 or not isinstance(query, str):
            raise GmailError("invalid_arguments")
        if len(query.encode()) > 4096:
            raise GmailError("invalid_arguments")
        token = self._page_token(user_id, query, cursor)
        async with self._operation():
            deadline = self.clock() + self.limits.search_seconds
            creds = await self._credentials(user_id, deadline)
            params: dict[str, Any] = {"q": query, "maxResults": page_size}
            if token is not None:
                params["pageToken"] = token
            page = await self._call(user_id, creds, "list", params, deadline)
            items = page.get("messages", [])[:page_size]
            results = [EmailPreview(m["id"], m.get("threadId", "")) for m in items]
            errors: list[ItemError] = []
            queue = iter(enumerate(items))

            async def previews() -> None:
                for index, item in queue:
                    try:
                        message = await self._call(
                            user_id,
                            creds,
                            "message",
                            {
                                "id": item["id"],
                                "format": "metadata",
                                "metadataHeaders": ["From", "To", "Subject", "Date"],
                            },
                            deadline,
                        )
                        results[index] = self._preview(message)
                    except GmailError as exc:
                        errors.append(ItemError(item["id"], exc.code, exc.retryable))

            tasks = [
                asyncio.create_task(previews())
                for _ in range(min(len(items), self.limits.user_concurrency))
            ]
            if tasks:

                async def collect() -> None:
                    # Drain every worker even if an unexpected parsing error occurs.
                    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
                    for outcome in outcomes:
                        if isinstance(outcome, BaseException):
                            raise outcome

                def cancel_previews() -> None:
                    for task in tasks:
                        task.cancel()

                await self._drain(asyncio.create_task(collect()), on_cancel=cancel_previews)
            next_token = page.get("nextPageToken")
            errors.sort(key=lambda e: next(i for i, m in enumerate(items) if m["id"] == e.item_id))
            result = SearchResult(
                results,
                page.get("resultSizeEstimate"),
                self._cursor(user_id, query, next_token) if next_token else None,
                bool(next_token),
                bool(errors),
                errors,
            )
            # Preserve hit IDs and failures while shedding previews if necessary.
            for preview in reversed(results):
                if _size(result) <= self.limits.response_bytes:
                    break
                preview.headers.clear()
                preview.snippet = ""
                preview.truncated = result.truncated = True
            if _size(result) > self.limits.response_bytes:
                raise GmailError("size_limit")
            return result

    async def _normalize(
        self,
        user_id: str,
        creds: Credentials,
        message: dict[str, Any],
        deadline: float,
    ) -> EmailContent:
        preview = self._preview(message)
        result = EmailContent(**asdict(preview), internal_date=message.get("internalDate"))
        headers = _headers(message.get("payload", {}))
        for name in ("cc", "bcc", "reply-to", "message-id"):
            if name in headers:
                result.headers[name] = _clip(headers[name], 4096)
                result.truncated |= len(headers[name].encode()) > 4096
        pending = [(message.get("payload", {}), 0)]
        parts: list[dict[str, Any]] = []
        while pending:
            part, depth = pending.pop()
            if depth > self.limits.mime_depth or len(parts) >= self.limits.mime_parts:
                result.errors.append(ItemError(result.message_id, "mime_limit"))
                result.truncated = True
                if len(parts) >= self.limits.mime_parts:
                    break
                continue
            parts.append(part)
            if not _is_attachment(part, _headers(part)):
                pending.extend((child, depth + 1) for child in reversed(part.get("parts", [])))
        decoded_parts: list[dict[str, Any]] = []
        decoded_bytes = 0
        for part in parts:
            headers = _headers(part)
            body = part.get("body", {})
            disposition = headers.get("content-disposition", "").lower()
            if _is_attachment(part, headers):
                result.attachments.append(
                    Attachment(
                        part.get("partId", ""),
                        body.get("attachmentId"),
                        _clip(part.get("filename", ""), 4096),
                        part.get("mimeType", ""),
                        body.get("size", 0),
                        disposition.startswith("inline"),
                    )
                )
                continue
            if part.get("mimeType") not in _BODY_TYPES:
                continue
            try:
                if body.get("size", 0) > self.limits.binary_bytes:
                    raise GmailError("size_limit")
                data = body.get("data")
                if not data and body.get("attachmentId"):
                    fetched = await self._call(
                        user_id,
                        creds,
                        "attachment",
                        {
                            "messageId": result.message_id,
                            "id": body["attachmentId"],
                        },
                        deadline,
                    )
                    data = fetched.get("data", "")
                content_type = Message()
                content_type["content-type"] = headers.get("content-type", part.get("mimeType", ""))
                charset = content_type.get_content_charset() or "utf-8"
                text, byte_count, lossy = await self._cpu(
                    self._text,
                    data or "",
                    self.limits.binary_bytes - decoded_bytes,
                    charset,
                )
                decoded_bytes += byte_count
                if lossy:
                    result.errors.append(ItemError(part.get("partId", ""), "lossy_decoding"))
                decoded_parts.append({"mimeType": part["mimeType"], "body": {"data": text}})
            except GmailError as exc:
                result.errors.append(ItemError(part.get("partId", ""), exc.code, exc.retryable))
                result.truncated = True
        result.body, result.body_source = await self._cpu(extract_body, {"parts": decoded_parts})
        if len(result.body.encode()) > self.limits.body_bytes:
            result.body = _clip(result.body, self.limits.body_bytes)
            result.truncated = True
        await self._cpu(self._fit_message, result)
        return result

    def _trim_sequence(self, result: Any, name: str) -> None:
        """Retain the longest prefix that fits, without quadratic serialization."""
        original = getattr(result, name)
        low, high = 0, len(original)
        while low < high:
            middle = (low + high + 1) // 2
            setattr(result, name, original[:middle])
            if _size(result) <= self.limits.response_bytes:
                low = middle
            else:
                high = middle - 1
        setattr(result, name, original[:low])

    def _fit_message(self, result: EmailContent) -> None:
        if _size(result) <= self.limits.response_bytes:
            return
        result.truncated = True
        self._trim_sequence(result, "attachments")
        if _size(result) > self.limits.response_bytes:
            self._trim_sequence(result, "errors")
        while _size(result) > self.limits.response_bytes:
            if result.body:
                result.body = _clip(result.body, max(0, len(result.body.encode()) // 2))
            elif result.headers:
                result.headers.popitem()
            else:
                raise GmailError("size_limit")

    async def get_email(
        self,
        user_id: str,
        message_id: str,
        format: Literal["full", "raw"] = "full",
    ) -> EmailContent | BinaryContent:
        if format not in {"full", "raw"} or not isinstance(message_id, str) or not message_id:
            raise GmailError("invalid_arguments")
        async with self._operation():
            deadline = self.clock() + self.limits.retrieval_seconds
            creds = await self._credentials(user_id, deadline)
            message = await self._call(
                user_id, creds, "message", {"id": message_id, "format": format}, deadline
            )
            if format == "raw":
                if message.get("sizeEstimate", 0) > self.limits.binary_bytes:
                    raise GmailError("size_limit")
                return BinaryContent(
                    message_id,
                    await self._cpu(_decoded, message.get("raw", ""), self.limits.binary_bytes),
                    thread_id=message.get("threadId"),
                )
            return await self._normalize(user_id, creds, message, deadline)

    async def get_thread(self, user_id: str, thread_id: str) -> ThreadResult:
        if not isinstance(thread_id, str) or not thread_id:
            raise GmailError("invalid_arguments")
        async with self._operation():
            deadline = self.clock() + self.limits.retrieval_seconds
            creds = await self._credentials(user_id, deadline)
            thread = await self._call(
                user_id, creds, "thread", {"id": thread_id, "format": "full"}, deadline
            )
            messages = thread.get("messages", [])
            result = ThreadResult(thread_id, [], [m["id"] for m in messages], len(messages), False)
            if _size(result) > self.limits.response_bytes:
                result.ids_truncated = result.truncated = True
                await self._cpu(self._trim_sequence, result, "message_ids")
            for message in messages[: self.limits.thread_messages]:
                if self.clock() >= deadline:
                    result.errors.append(ItemError(message["id"], "deadline_exceeded", True))
                    break
                try:
                    content = await self._normalize(user_id, creds, message, deadline)
                except GmailError as exc:
                    result.errors.append(ItemError(message["id"], exc.code, exc.retryable))
                    continue
                result.messages.append(content)
                result.omitted_messages -= 1
                if _size(result) > self.limits.response_bytes:
                    result.messages.pop()
                    result.omitted_messages += 1
                    break
            result.truncated |= bool(result.omitted_messages) or any(
                m.truncated for m in result.messages
            )
            if _size(result) > self.limits.response_bytes:
                result.errors.clear()
                if result.message_ids and _size(result) > self.limits.response_bytes:
                    result.ids_truncated = result.truncated = True
                    await self._cpu(self._trim_sequence, result, "message_ids")
            if _size(result) > self.limits.response_bytes:
                raise GmailError("size_limit")
            return result

    async def get_attachment(
        self, user_id: str, message_id: str, attachment_id: str
    ) -> BinaryContent:
        if not all(isinstance(value, str) and value for value in (message_id, attachment_id)):
            raise GmailError("invalid_arguments")
        async with self._operation():
            deadline = self.clock() + self.limits.retrieval_seconds
            creds = await self._credentials(user_id, deadline)
            attachment = await self._call(
                user_id,
                creds,
                "attachment",
                {
                    "messageId": message_id,
                    "id": attachment_id,
                },
                deadline,
            )
            if attachment.get("size", 0) > self.limits.binary_bytes:
                raise GmailError("size_limit")
            return BinaryContent(
                message_id,
                await self._cpu(_decoded, attachment.get("data", ""), self.limits.binary_bytes),
                attachment_id,
            )
