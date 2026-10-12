"""Daily-log preparation must protect paths and retain stalled worker admission."""

import asyncio
import os
import stat
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from assistant_agent import daily_logs
from assistant_agent.config import SandboxSettings
from assistant_agent.daily_logs import DailyLogError, DailyLogService

USER_ID = "a" * 32
NOW = datetime(2026, 10, 13, 2, 30, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path: Path) -> SandboxSettings:
    root = tmp_path.resolve()
    return SandboxSettings(
        app_user_data_root=root / "app-user-data",
        host_user_data_root=root / "host-user-data",
        app_input_root=root / "app-input",
        host_input_root=root / "host-input",
    )


async def test_mapping_captured_time_and_existing_content(settings):
    clock = Mock(return_value=datetime(2026, 10, 14, tzinfo=UTC))
    service = DailyLogService(settings, clock=clock)
    captured = service.current_user_time("America/New_York")
    assert captured.isoformat() == "2026-10-13T20:00:00-04:00"
    daily_log = await service.prepare(USER_ID, "America/New_York", NOW)
    assert daily_log.local_date == date(2026, 10, 12)
    assert daily_log.host_daily_logs_path == settings.host_user_data_root / USER_ID / "daily-logs"
    assert daily_log.app_daily_logs_path == settings.app_user_data_root / USER_ID / "daily-logs"
    assert not settings.host_user_data_root.exists()
    assert clock.call_count == 1
    daily_file = daily_log.app_daily_logs_path / "2026-10-12.md"
    assert daily_file.read_text() == "# 2026-10-12 (America/New_York)\n"
    daily_file.write_text("preserved content")
    daily_file.chmod(0o640)
    daily_file.parent.chmod(0o750)
    daily_file.parent.parent.chmod(0o700)
    assert await service.ensure_today(USER_ID, "America/New_York", NOW) == daily_log
    assert daily_file.read_text() == "preserved content"
    assert stat.S_IMODE(daily_file.stat().st_mode) == 0o640
    assert stat.S_IMODE(daily_file.parent.stat().st_mode) == 0o750
    assert stat.S_IMODE(daily_file.parent.parent.stat().st_mode) == 0o700
    await service.aclose()
    await service.aclose()


async def test_creation_modes_and_clock_not_used(settings):
    service = DailyLogService(settings, clock=Mock(side_effect=AssertionError("clock read")))
    daily_log = await service.prepare(USER_ID, "UTC", NOW)
    for directory in (daily_log.app_daily_logs_path, daily_log.app_daily_logs_path.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o777
    assert stat.S_IMODE((daily_log.app_daily_logs_path / "2026-10-13.md").stat().st_mode) == 0o666
    await service.aclose()


@pytest.mark.parametrize("user_id", ["", "A" * 32, "a" * 31, "g" * 32, "../" + "a" * 29, None])
async def test_invalid_user_ids(settings, user_id):
    with pytest.raises(ValueError):
        await DailyLogService(settings).prepare(user_id, "UTC", NOW)
    assert not settings.app_user_data_root.exists()


@pytest.mark.parametrize("timezone_name", ["", "Invalid/Timezone", "../UTC", None])
async def test_invalid_timezones(settings, timezone_name):
    service = DailyLogService(settings)
    with pytest.raises(ValueError):
        await service.prepare(USER_ID, timezone_name, NOW)
    with pytest.raises(ValueError):
        service.current_user_time(timezone_name)


@pytest.mark.parametrize("timestamp", [datetime(2026, 10, 12), None, "2026-10-12"])
async def test_invalid_timestamps(settings, timestamp):
    service = DailyLogService(settings, clock=lambda: timestamp)
    with pytest.raises(ValueError):
        await service.ensure_today(USER_ID, "UTC", timestamp)
    with pytest.raises(ValueError):
        service.current_user_time("UTC")


@pytest.mark.parametrize("workers", [0, -1, True, 1.5, "4"])
def test_invalid_workers(workers):
    with pytest.raises(ValueError):
        DailyLogService(workers=workers)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "30"])
def test_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        DailyLogService(timeout_seconds=timeout)


@pytest.mark.parametrize("location", ["ancestor", "root", "user", "daily-logs", "file"])
async def test_rejects_symlinks(settings, location):
    root = settings.app_user_data_root
    target = root.parent / "target"
    target.mkdir()
    if location == "ancestor":
        ancestor = root.parent / "link"
        ancestor.symlink_to(target, target_is_directory=True)
        settings = SandboxSettings(app_user_data_root=ancestor / "nested")
    elif location == "root":
        root.symlink_to(target, target_is_directory=True)
    else:
        root.mkdir()
        if location == "user":
            (root / USER_ID).symlink_to(target, target_is_directory=True)
        else:
            (root / USER_ID).mkdir()
            if location == "daily-logs":
                (root / USER_ID / "daily-logs").symlink_to(target, target_is_directory=True)
            else:
                (root / USER_ID / "daily-logs").mkdir()
                destination = target / "protected"
                destination.write_text("unchanged")
                (root / USER_ID / "daily-logs" / "2026-10-13.md").symlink_to(destination)
    service = DailyLogService(settings)
    with pytest.raises(DailyLogError):
        await service.prepare(USER_ID, "UTC", NOW)
    assert sorted(path.name for path in target.iterdir()) == (["protected"] if location == "file" else [])
    if location == "file":
        assert destination.read_text() == "unchanged"
    await service.aclose()


@pytest.mark.parametrize("kind", ["directory", "fifo"])
async def test_rejects_nonregular_dated_files(settings, kind):
    log_directory = settings.app_user_data_root / USER_ID / "daily-logs"
    log_directory.mkdir(parents=True)
    daily_file = log_directory / "2026-10-13.md"
    if kind == "directory":
        daily_file.mkdir()
    else:
        os.mkfifo(daily_file)
    service = DailyLogService(settings)
    with pytest.raises(DailyLogError):
        await service.prepare(USER_ID, "UTC", NOW)
    await service.aclose()


async def test_chmod_permission_error_tolerated(settings, monkeypatch):
    monkeypatch.setattr(os, "fchmod", Mock(side_effect=PermissionError))
    service = DailyLogService(settings)
    daily_log = await service.prepare(USER_ID, "UTC", NOW)
    assert (daily_log.app_daily_logs_path / "2026-10-13.md").read_text().startswith("# 2026-10-13")
    assert os.fchmod.call_count == 3
    await service.aclose()


async def test_other_chmod_failure_fails_preparation(settings, monkeypatch):
    monkeypatch.setattr(os, "fchmod", Mock(side_effect=OSError))
    service = DailyLogService(settings)
    with pytest.raises(DailyLogError):
        await service.prepare(USER_ID, "UTC", NOW)
    await service.aclose()


async def test_warmup_exact_window_errors_and_ensure_skips(settings, monkeypatch, caplog):
    log_directory = settings.app_user_data_root / USER_ID / "daily-logs"
    log_directory.mkdir(parents=True)
    (log_directory / "2026-10-12.md").write_text("PRIVATE CONTENT")
    original_open = daily_logs._open_regular_file
    opened = []

    def record_open(descriptor, filename):
        opened.append(filename)
        if filename == "2026-10-11.md":
            raise PermissionError("PRIVATE CONTENT")
        return original_open(descriptor, filename)

    monkeypatch.setattr(daily_logs, "_open_regular_file", record_open)
    monkeypatch.setattr(os, "listdir", Mock(side_effect=AssertionError("directory scan")))
    monkeypatch.setattr(os, "scandir", Mock(side_effect=AssertionError("directory scan")))
    service = DailyLogService(settings)
    await service.prepare(USER_ID, "UTC", NOW)
    assert opened == [f"2026-10-{day:02d}.md" for day in range(12, 5, -1)]
    assert "warm-up read failed" in caplog.text
    assert "PRIVATE CONTENT" not in caplog.text
    opened.clear()
    await service.ensure_today(USER_ID, "UTC", NOW)
    assert opened == ["2026-10-13.md"]
    opened.clear()
    await service.ensure_today(USER_ID, "UTC", datetime(2026, 10, 14, tzinfo=UTC))
    assert opened == []
    await service.aclose()


class BlockedWorker:
    """Keep a filesystem worker running until the test explicitly releases it."""

    def __init__(self, monkeypatch, *, fail=False):
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        original_prepare = DailyLogService._prepare

        def blocked_prepare(daily_log, warmup):
            self.started.set()
            try:
                if not self.release.wait(5):
                    raise AssertionError("test failed to release worker")
                if fail:
                    raise OSError("PRIVATE CONTENT")
                return original_prepare(daily_log, warmup)
            finally:
                self.finished.set()

        monkeypatch.setattr(DailyLogService, "_prepare", staticmethod(blocked_prepare))

    async def wait_started(self):
        assert await asyncio.to_thread(self.started.wait, 2)


async def test_timeout_retains_user_and_capacity_until_completion(settings, monkeypatch):
    blocked = BlockedWorker(monkeypatch)
    service = DailyLogService(settings, workers=1, timeout_seconds=0.05)
    operation = asyncio.create_task(service.prepare(USER_ID, "UTC", NOW))
    try:
        await blocked.wait_started()
        with pytest.raises(DailyLogError, match="timed out"):
            await operation
        assert len(service._tasks) == 1
        for user_id in (USER_ID, "b" * 32):
            with pytest.raises(DailyLogError, match="admission"):
                await service.prepare(user_id, "UTC", NOW)
    finally:
        blocked.release.set()
        await asyncio.gather(*service._tasks.values(), return_exceptions=True)
    assert service._tasks == {}
    assert (await service.prepare("b" * 32, "UTC", NOW)).local_date == NOW.date()
    await service.aclose()


async def test_cancellation_same_user_exclusion_and_other_user_capacity(settings, monkeypatch):
    blocked = BlockedWorker(monkeypatch)
    service = DailyLogService(settings, workers=2)
    first = asyncio.create_task(service.prepare(USER_ID, "UTC", NOW))
    second = None
    try:
        await blocked.wait_started()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(DailyLogError, match="admission"):
            await service.prepare(USER_ID, "UTC", NOW)
        second = asyncio.create_task(service.prepare("b" * 32, "UTC", NOW))
        await asyncio.sleep(0)
        assert len(service._tasks) == 2
        with pytest.raises(DailyLogError, match="admission"):
            await service.prepare("c" * 32, "UTC", NOW)
    finally:
        blocked.release.set()
        if second:
            await second
        await service.aclose()
    assert service._tasks == {}


async def test_late_failure_is_retrieved_without_contents(settings, monkeypatch, caplog):
    blocked = BlockedWorker(monkeypatch, fail=True)
    service = DailyLogService(settings, timeout_seconds=0.05)
    operation = asyncio.create_task(service.prepare(USER_ID, "UTC", NOW))
    try:
        await blocked.wait_started()
        with pytest.raises(DailyLogError):
            await operation
    finally:
        blocked.release.set()
        await service.aclose()
    assert service._tasks == {}
    assert "filesystem operation failed" in caplog.text
    assert "PRIVATE CONTENT" not in caplog.text


async def test_shutdown_timeout_and_later_draining(settings, monkeypatch):
    blocked = BlockedWorker(monkeypatch)
    monkeypatch.setattr(daily_logs, "SHUTDOWN_TIMEOUT_SECONDS", 0.05)
    service = DailyLogService(settings)
    operation = asyncio.create_task(service.prepare(USER_ID, "UTC", NOW))
    try:
        await blocked.wait_started()
        with pytest.raises(TimeoutError, match="still running"):
            await service.aclose()
        assert len(service._tasks) == 1
        assert not blocked.finished.is_set()
        with pytest.raises(DailyLogError, match="admission"):
            await service.prepare("b" * 32, "UTC", NOW)
    finally:
        blocked.release.set()
        await operation
        await service.aclose()
    await service.aclose()
    assert service._tasks == {}


async def test_cancelled_close_retains_workers(settings, monkeypatch):
    blocked = BlockedWorker(monkeypatch)
    service = DailyLogService(settings)
    operation = asyncio.create_task(service.prepare(USER_ID, "UTC", NOW))
    try:
        await blocked.wait_started()
        closing = asyncio.create_task(service.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert len(service._tasks) == 1
    finally:
        blocked.release.set()
        await operation
        await service.aclose()


async def test_earliest_date_has_no_previous_dates(settings):
    service = DailyLogService(settings)
    daily_log = await service.prepare(USER_ID, "UTC", datetime.min.replace(tzinfo=UTC))
    assert daily_log.local_date == date.min
    assert (daily_log.app_daily_logs_path / "0001-01-01.md").read_text() == "# 0001-01-01 (UTC)\n"
    await service.aclose()


async def test_default_admission_is_four_workers(settings, monkeypatch):
    blocked = BlockedWorker(monkeypatch)
    service = DailyLogService(settings)
    operations = [
        asyncio.create_task(service.prepare(character * 32, "UTC", NOW))
        for character in "abcd"
    ]
    try:
        await blocked.wait_started()
        assert len(service._tasks) == 4
        with pytest.raises(DailyLogError, match="admission"):
            await service.prepare("e" * 32, "UTC", NOW)
    finally:
        blocked.release.set()
        await asyncio.gather(*operations)
        await service.aclose()
