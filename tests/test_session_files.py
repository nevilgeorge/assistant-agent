"""Session materialization must preserve content and drain retirement races."""

import asyncio
import json
import stat
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from assistant_agent.gmail_service import BinaryContent, EmailContent, GmailError, ItemError
from assistant_agent.sandbox_access import AuthorizedSandboxContext, SandboxAuthorizationError
from assistant_agent.session_files import SessionFileLimits, SessionFilesService


@pytest.fixture
def context(tmp_path: Path) -> AuthorizedSandboxContext:
    return AuthorizedSandboxContext("user", "conversation", "container", tmp_path, tmp_path)


@pytest.fixture
def gmail_service() -> AsyncMock:
    service = AsyncMock()
    service.get_email.return_value = EmailContent(
        message_id="id",
        thread_id="thread",
        headers={"From": "sender@example.com"},
        body="body café",
        truncated=True,
        errors=[ItemError("id", "part_missing")],
    )
    return service


def published_file(context: AuthorizedSandboxContext, path: str) -> Path:
    return context.app_input_path / path.removeprefix("/input/")


async def test_normalized_formats_and_duplicates(context, gmail_service):
    service = SessionFilesService(gmail_service)
    auth = AsyncMock(return_value=context)
    result = await service.download_emails(context, ["../../bad", "../../bad"], reauthorize=auth)
    assert all(item.success for item in result.items)
    assert len({item.path for item in result.items}) == 2
    for item in result.items:
        output = published_file(context, item.path)
        assert "WARNING: truncated" in output.read_text()
        assert "WARNING: part_missing" in output.read_text()
        assert "From: sender@example.com" in output.read_text()
        assert stat.S_IMODE(output.stat().st_mode) == 0o644
        assert stat.S_IMODE(output.parent.stat().st_mode) == 0o755
    assert result.total_bytes == sum(item.bytes for item in result.items)
    normalized = await service.download_emails(context, ["id"], "json", reauthorize=auth)
    assert (
        json.loads(published_file(context, normalized.items[0].path).read_text())["body"]
        == "body café"
    )
    assert gmail_service.get_email.await_count == 3
    assert not list(context.app_input_path.glob(".gmail-stage-*"))


async def test_binary_exact_and_generic_names(context, gmail_service):
    data = b"\x00\xff\r\noriginal"
    gmail_service.get_email.return_value = BinaryContent("id", data)
    gmail_service.get_attachment.return_value = BinaryContent("id", data, "attachment")
    service = SessionFilesService(gmail_service)
    auth = AsyncMock(return_value=context)
    raw = await service.download_emails(context, ["id"], "eml", reauthorize=auth)
    attachment = await service.download_attachment(context, "id", "../original", reauthorize=auth)
    assert published_file(context, raw.items[0].path).read_bytes() == data
    assert published_file(context, attachment.path).read_bytes() == data
    assert attachment.path.endswith("/001.bin")
    assert attachment.mime_type == "application/octet-stream"


async def test_partial_errors_and_quota(context, gmail_service):
    gmail_service.get_email.side_effect = [
        GmailError("not_found"),
        BinaryContent("id", b"123"),
        BinaryContent("id", b"12"),
    ]
    service = SessionFilesService(gmail_service, limits=SessionFileLimits(call_bytes=4))
    result = await service.download_emails(
        context, ["missing", "id", "id"], "eml", reauthorize=AsyncMock(return_value=context)
    )
    assert [item.error_code for item in result.items] == ["not_found", None, "quota_exceeded"]
    assert result.total_bytes == 3
    gmail_service.get_email.side_effect = None
    gmail_service.get_email.return_value = BinaryContent("id", b"12345")
    failed = await service.download_emails(
        context, ["id"], "eml", reauthorize=AsyncMock(return_value=context)
    )
    assert failed.items[0].error_code == "quota_exceeded"
    assert len(list(context.app_input_path.iterdir())) == 1


async def test_retirement_cancels_queued_and_fetching(context, gmail_service):
    started = asyncio.Event()

    async def fetching(*args):
        started.set()
        await asyncio.Event().wait()

    gmail_service.get_email.side_effect = fetching
    service = SessionFilesService(gmail_service)
    auth = AsyncMock(return_value=context)
    first = asyncio.create_task(service.download_emails(context, ["id"], reauthorize=auth))
    await started.wait()
    second = asyncio.create_task(service.download_emails(context, ["id"], reauthorize=auth))
    await asyncio.sleep(0)
    await service.retire(context.conversation_id)
    assert first.cancelled() and second.cancelled()
    assert not list(context.app_input_path.iterdir())
    with pytest.raises(SandboxAuthorizationError):
        await service.download_emails(context, ["id"], reauthorize=auth)
    await service.retire(context.conversation_id)


async def test_revocation_before_publish_cleans_staging(context, gmail_service):
    service = SessionFilesService(gmail_service)
    with pytest.raises(SandboxAuthorizationError):
        await service.download_emails(
            context, ["id"], reauthorize=AsyncMock(side_effect=SandboxAuthorizationError())
        )
    assert not list(context.app_input_path.iterdir())


async def test_symlink_mount_rejected(context, gmail_service, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    linked = AuthorizedSandboxContext("user", "conversation", "container", link, link)
    with pytest.raises(GmailError, match="file_error"):
        await SessionFilesService(gmail_service).download_emails(
            linked, ["id"], reauthorize=AsyncMock(return_value=linked)
        )
    gmail_service.get_email.assert_not_awaited()


async def test_conversation_quota_serializes_calls(context, gmail_service):
    gmail_service.get_email.return_value = BinaryContent("id", b"123")
    service = SessionFilesService(gmail_service, limits=SessionFileLimits(conversation_bytes=5))
    auth = AsyncMock(return_value=context)
    results = await asyncio.gather(
        *(service.download_emails(context, ["id"], "eml", reauthorize=auth) for _ in range(2))
    )
    assert sum(result.total_bytes for result in results) == 3
    assert [result.items[0].success for result in results] == [True, False]


async def test_validation_and_binary_limit(context, gmail_service):
    service = SessionFilesService(gmail_service, limits=SessionFileLimits(binary_bytes=2))
    auth = AsyncMock(return_value=context)
    for ids in ([], ["id"] * 51, [""]):
        with pytest.raises(GmailError, match="invalid_arguments"):
            await service.download_emails(context, ids, reauthorize=auth)
    gmail_service.get_email.return_value = BinaryContent("id", b"123")
    result = await service.download_emails(context, ["id"], "eml", reauthorize=auth)
    assert result.items[0].error_code == "size_limit"
    assert not list(context.app_input_path.iterdir())


async def test_publication_failure_cleans_staging(context, gmail_service, monkeypatch):
    def failed_rename(*args, **kwargs):
        raise OSError("disk problem")

    monkeypatch.setattr("assistant_agent.session_files.os.rename", failed_rename)
    with pytest.raises(GmailError, match="file_error"):
        await SessionFilesService(gmail_service).download_emails(
            context, ["id"], reauthorize=AsyncMock(return_value=context)
        )
    assert not list(context.app_input_path.iterdir())


async def test_retirement_drains_worker_before_cleanup(context, gmail_service, monkeypatch):
    import threading

    worker_started = asyncio.Event()
    worker_release = threading.Event()
    loop = asyncio.get_running_loop()
    original_write = SessionFilesService._write

    def blocked_write(*args):
        loop.call_soon_threadsafe(worker_started.set)
        assert worker_release.wait(5)
        original_write(*args)

    monkeypatch.setattr(SessionFilesService, "_write", staticmethod(blocked_write))
    service = SessionFilesService(gmail_service)
    download = asyncio.create_task(
        service.download_emails(context, ["id"], reauthorize=AsyncMock(return_value=context))
    )
    await worker_started.wait()
    retirement = asyncio.create_task(service.retire(context.conversation_id))
    await asyncio.sleep(0)
    assert not retirement.done()
    worker_release.set()
    await retirement
    assert download.cancelled()
    assert not list(context.app_input_path.iterdir())


async def test_deadline_and_empty_binary(context, gmail_service):
    service = SessionFilesService(gmail_service, limits=SessionFileLimits(scheduling_seconds=0))
    result = await service.download_emails(
        context, ["id"], reauthorize=AsyncMock(return_value=context)
    )
    assert result.items[0].error_code == "deadline_exceeded"
    gmail_service.get_email.assert_not_awaited()
    gmail_service.get_email.return_value = BinaryContent("id", b"")
    empty = await SessionFilesService(gmail_service).download_emails(
        context, ["id"], "eml", reauthorize=AsyncMock(return_value=context)
    )
    assert empty.items[0].success
    assert published_file(context, empty.items[0].path).read_bytes() == b""


@pytest.mark.parametrize(
    "values",
    [
        {"workers": 0},
        {"messages": -1},
        {"binary_bytes": True},
        {"call_bytes": 1.5},
        {"conversation_bytes": 0},
        {"scheduling_seconds": -1},
        {"scheduling_seconds": float("nan")},
        {"scheduling_seconds": float("inf")},
    ],
)
def test_invalid_limits_fail_promptly(values):
    with pytest.raises(ValueError):
        SessionFileLimits(**values)


async def test_disk_write_failure_cleans_staging(context, gmail_service, monkeypatch):
    def failed_write(*args):
        raise OSError("private disk detail")

    monkeypatch.setattr(SessionFilesService, "_write", staticmethod(failed_write))
    with pytest.raises(GmailError, match="^file_error$"):
        await SessionFilesService(gmail_service).download_emails(
            context, ["id"], reauthorize=AsyncMock(return_value=context)
        )
    assert not list(context.app_input_path.iterdir())


async def test_repeated_cancellation_drains_cleanup_admission(context, gmail_service, monkeypatch):
    service = SessionFilesService(gmail_service, limits=SessionFileLimits(workers=1))
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    original_execute = service._execute_worker

    async def blocked_execute(function, *args):
        if function == service._remove_staging:
            cleanup_started.set()
            await cleanup_release.wait()
        return await original_execute(function, *args)

    monkeypatch.setattr(service, "_execute_worker", blocked_execute)
    download = asyncio.create_task(
        service.download_emails(
            context, ["id"], reauthorize=AsyncMock(side_effect=SandboxAuthorizationError())
        )
    )
    await cleanup_started.wait()
    download.cancel()
    await asyncio.sleep(0)
    download.cancel()
    await asyncio.sleep(0)
    assert not download.done()
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await download
    assert not list(context.app_input_path.iterdir())
