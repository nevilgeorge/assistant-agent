"""Bounded, atomic materialization of Gmail content into a session input mount."""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, TypeVar

from .gmail_service import BinaryContent, EmailContent, GmailError, GmailService
from .sandbox_access import AuthorizedSandboxContext, SandboxAuthorizationError

T = TypeVar("T")
Reauthorize = Callable[[], Awaitable[AuthorizedSandboxContext]]


@dataclass(frozen=True)
class SessionFileLimits:
    messages: int = 50
    binary_bytes: int = 32 * 1024 * 1024
    call_bytes: int = 64 * 1024 * 1024
    conversation_bytes: int = 256 * 1024 * 1024
    scheduling_seconds: float = 120
    workers: int = 4

    def __post_init__(self) -> None:
        for name in ("messages", "binary_bytes", "call_bytes", "conversation_bytes", "workers"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.scheduling_seconds, bool)
            or not isinstance(self.scheduling_seconds, (int, float))
            or not math.isfinite(self.scheduling_seconds)
            or self.scheduling_seconds < 0
        ):
            raise ValueError("scheduling_seconds must be finite and nonnegative")


@dataclass
class DownloadItem:
    position: int
    message_id: str
    attachment_id: str | None = None
    success: bool = False
    path: str | None = None
    bytes: int = 0
    mime_type: str | None = None
    error_code: str | None = None
    retryable: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class DownloadResult:
    items: list[DownloadItem]
    total_bytes: int = 0


@dataclass
class _ConversationFiles:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set[asyncio.Task[object]] = field(default_factory=set)
    published_bytes: int = 0


def _open_directory(path: Path) -> int:
    """Walk without following any symlink, including ancestors of the mount."""
    if not path.is_absolute() or ".." in path.parts:
        raise OSError("invalid directory")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _serialize(content: EmailContent | BinaryContent, format: str) -> tuple[bytes, list[str]]:
    if isinstance(content, BinaryContent):
        return content.data, []
    warnings = (["truncated"] if content.truncated else []) + [
        error.code for error in content.errors
    ]
    if format == "json":
        return json.dumps(
            asdict(content), ensure_ascii=False, separators=(",", ":")
        ).encode(), warnings
    lines = [f"Message-ID: {content.message_id}", f"Thread-ID: {content.thread_id}"]
    headers = {name.lower(): value for name, value in content.headers.items()}
    lines += [
        f"{name.title()}: {headers.get(name, '')}"
        for name in ("from", "to", "cc", "subject", "date")
    ]
    lines += [f"WARNING: {warning}" for warning in warnings]
    return ("\n".join(lines) + "\n\n" + content.body).encode(), warnings


class SessionFilesService:
    def __init__(
        self, gmail_service: GmailService, *, limits: SessionFileLimits | None = None
    ) -> None:
        self.gmail_service = gmail_service
        self.limits = limits or SessionFileLimits()
        self._conversations: dict[str, _ConversationFiles] = {}
        # Retain only conversation IDs until shutdown, preventing stale authorized
        # contexts from restarting work after retirement; tasks and quota are released.
        self._retired: set[str] = set()
        self._closed = False
        self._workers = asyncio.Semaphore(self.limits.workers)

    async def _execute_worker(self, function: Callable[..., T], *args: object) -> T:
        async with self._workers:
            return await asyncio.to_thread(function, *args)

    async def _worker(self, function: Callable[..., T], *args: object) -> T:
        # Shield admission too: repeated cancellation during final cleanup must
        # not abandon a cleanup waiting for a bounded worker slot.
        worker_task = asyncio.create_task(self._execute_worker(function, *args))
        try:
            return await asyncio.shield(worker_task)
        except asyncio.CancelledError:
            while not worker_task.done():
                try:
                    await asyncio.shield(worker_task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker_task.cancelled():
                worker_task.exception()
            raise

    async def retire(self, conversation_id: str) -> None:
        self._retired.add(conversation_id)
        state = self._conversations.get(conversation_id)
        if state:
            tasks = list(state.tasks)
            for task in tasks:
                if not task.cancelling():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._conversations.pop(conversation_id, None)

    async def aclose(self) -> None:
        self._closed = True
        await asyncio.gather(*(self.retire(key) for key in list(self._conversations)))
        self._retired.clear()

    async def download_emails(
        self,
        context: AuthorizedSandboxContext,
        message_ids: list[str],
        format: Literal["text", "json", "eml"] = "text",
        *,
        reauthorize: Reauthorize,
    ) -> DownloadResult:
        if format not in {"text", "json", "eml"} or not isinstance(message_ids, list):
            raise GmailError("invalid_arguments")
        if not 1 <= len(message_ids) <= self.limits.messages or any(
            not isinstance(message_id, str) or not message_id for message_id in message_ids
        ):
            raise GmailError("invalid_arguments")
        return await self._download(context, message_ids, format, None, reauthorize)

    async def download_attachment(
        self,
        context: AuthorizedSandboxContext,
        message_id: str,
        attachment_id: str,
        *,
        reauthorize: Reauthorize,
    ) -> DownloadItem:
        if not all(isinstance(value, str) and value for value in (message_id, attachment_id)):
            raise GmailError("invalid_arguments")
        result = await self._download(context, [message_id], "bin", attachment_id, reauthorize)
        return result.items[0]

    async def _download(
        self,
        context: AuthorizedSandboxContext,
        message_ids: list[str],
        format: str,
        attachment_id: str | None,
        reauthorize: Reauthorize,
    ) -> DownloadResult:
        conversation_id = context.conversation_id
        if self._closed or conversation_id in self._retired:
            raise SandboxAuthorizationError()
        state = self._conversations.setdefault(conversation_id, _ConversationFiles())
        task = asyncio.current_task()
        assert task is not None
        state.tasks.add(task)
        deadline = time.monotonic() + self.limits.scheduling_seconds
        try:
            async with state.lock:
                if conversation_id in self._retired:
                    raise SandboxAuthorizationError()
                return await self._materialize(
                    context, message_ids, format, attachment_id, reauthorize, state, deadline
                )
        finally:
            state.tasks.discard(task)

    async def _materialize(
        self,
        context: AuthorizedSandboxContext,
        message_ids: list[str],
        format: str,
        attachment_id: str | None,
        reauthorize: Reauthorize,
        state: _ConversationFiles,
        deadline: float,
    ) -> DownloadResult:
        call_id = uuid.uuid4().hex
        staging_name = f".gmail-stage-{call_id}"
        descriptor: int | None = None
        staging_created = False
        result = DownloadResult([])
        mime_type = {
            "text": "text/plain",
            "json": "application/json",
            "eml": "message/rfc822",
            "bin": "application/octet-stream",
        }[format]
        extension = "txt" if format == "text" else format
        try:
            # Opening is synchronous so cancellation cannot lose a just-opened descriptor.
            descriptor = _open_directory(context.app_input_path)
            os.mkdir(staging_name, mode=0o700, dir_fd=descriptor)
            staging_created = True
            for position, message_id in enumerate(message_ids):
                item = DownloadItem(position, message_id, attachment_id)
                result.items.append(item)
                if time.monotonic() >= deadline:
                    item.error_code, item.retryable = "deadline_exceeded", True
                    continue
                try:
                    if attachment_id is not None:
                        content = await self.gmail_service.get_attachment(
                            context.user_id, message_id, attachment_id
                        )
                    else:
                        content = await self.gmail_service.get_email(
                            context.user_id, message_id, "raw" if format == "eml" else "full"
                        )
                    data, item.warnings = await self._worker(_serialize, content, format)
                    if isinstance(content, BinaryContent) and len(data) > self.limits.binary_bytes:
                        raise GmailError("size_limit")
                    if (
                        result.total_bytes + len(data) > self.limits.call_bytes
                        or state.published_bytes + result.total_bytes + len(data)
                        > self.limits.conversation_bytes
                    ):
                        raise GmailError("quota_exceeded")
                    filename = f"{position + 1:03d}.{extension}"
                    await self._worker(self._write, descriptor, staging_name, filename, data)
                    item.success, item.path, item.bytes, item.mime_type = (
                        True,
                        f"/input/{call_id}/{filename}",
                        len(data),
                        mime_type,
                    )
                    result.total_bytes += len(data)
                except GmailError as error:
                    item.error_code, item.retryable = error.code, error.retryable
            if result.total_bytes or any(item.success for item in result.items):
                refreshed_context = await reauthorize()
                if refreshed_context != context or context.conversation_id in self._retired:
                    raise SandboxAuthorizationError()
                # No await between authorization, publication, and quota commit.
                os.chmod(staging_name, 0o755, dir_fd=descriptor, follow_symlinks=False)
                os.rename(staging_name, call_id, src_dir_fd=descriptor, dst_dir_fd=descriptor)
                staging_created = False
                state.published_bytes += result.total_bytes
            return result
        except OSError:
            raise GmailError("file_error", retryable=True) from None
        finally:
            if descriptor is not None:
                try:
                    if staging_created:
                        await self._worker(self._remove_staging, descriptor, staging_name)
                finally:
                    os.close(descriptor)

    @staticmethod
    def _write(descriptor: int, staging_name: str, filename: str, data: bytes) -> None:
        staging_descriptor = os.open(
            staging_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
        )
        try:
            file_descriptor = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=staging_descriptor,
            )
            with os.fdopen(file_descriptor, "wb") as output_file:
                output_file.write(data)
                os.fchmod(output_file.fileno(), 0o644)
        finally:
            os.close(staging_descriptor)

    @staticmethod
    def _remove_staging(descriptor: int, staging_name: str) -> None:
        shutil.rmtree(staging_name, dir_fd=descriptor)
