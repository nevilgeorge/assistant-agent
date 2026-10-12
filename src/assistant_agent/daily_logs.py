"""Prepare persistent daily logs with bounded, cancellation-safe filesystem workers."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import SandboxSettings, get_sandbox_settings

logger = logging.getLogger(__name__)
WARMUP_DAYS = 7
SHUTDOWN_TIMEOUT_SECONDS = 30


class DailyLogError(RuntimeError):
    """Daily-log preparation failed or no worker can admit the operation."""


@dataclass(frozen=True)
class DailyLog:
    """One user's persistent directory and the captured local date."""

    host_daily_logs_path: Path
    app_daily_logs_path: Path
    local_date: date
    timezone_name: str


def _timezone(timezone_name: str) -> ZoneInfo:
    """Validate a timezone without falling back silently."""
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValueError("timezone_name must be a valid timezone")
    try:
        return ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("timezone_name must be a valid timezone") from error


def _local_time(timestamp: datetime, timezone: ZoneInfo) -> datetime:
    """Convert only aware timestamps to the requested timezone."""
    if not isinstance(timestamp, datetime) or timestamp.utcoffset() is None:
        raise ValueError("current_user_time must be an aware datetime")
    return timestamp.astimezone(timezone)


def _chmod_created(descriptor: int, mode: int) -> None:
    """Apply local-development permissions, tolerating mount-owned modes."""
    try:
        os.fchmod(descriptor, mode)
    except PermissionError:
        pass


def _open_child_directory(parent_descriptor: int, name: str, *, shared: bool) -> int:
    """Create or open a directory relative to its verified parent."""
    created = False
    try:
        os.mkdir(name, mode=0o777 if shared else 0o755, dir_fd=parent_descriptor)
        created = True
    except FileExistsError:
        pass
    descriptor = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_descriptor
    )
    try:
        if created and shared:
            _chmod_created(descriptor, 0o777)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_daily_logs_directory(path: Path) -> int:
    """Walk from the filesystem root without following any ancestor symlink."""
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, component in enumerate(path.parts[1:], start=1):
            next_descriptor = _open_child_directory(
                descriptor, component, shared=index >= len(path.parts) - 2
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_regular_file(directory_descriptor: int, filename: str) -> int:
    """Open an existing file without following links or blocking on a FIFO."""
    descriptor = os.open(
        filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_descriptor
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("dated daily log must be a regular file")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class DailyLogService:
    """Admit bounded workers and retain them until their actual I/O completes."""

    def __init__(
        self,
        settings: SandboxSettings | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        workers: int = 4,
        timeout_seconds: float = 30,
    ) -> None:
        if isinstance(workers, bool) or not isinstance(workers, int) or workers <= 0:
            raise ValueError("workers must be a positive integer")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        self.settings = settings or get_sandbox_settings()
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._workers = workers
        self._timeout_seconds = timeout_seconds
        self._tasks: dict[str, asyncio.Task[DailyLog]] = {}
        self._closed = False

    def current_user_time(self, timezone_name: str) -> datetime:
        """Capture the clock once and return an aware user-local timestamp."""
        timezone = _timezone(timezone_name)
        return _local_time(self._clock(), timezone)

    async def prepare(
        self, user_id: str, timezone_name: str, current_user_time: datetime
    ) -> DailyLog:
        """Ensure today's file exists and warm the preceding seven local dates."""
        return await self._run(user_id, timezone_name, current_user_time, warmup=True)

    async def ensure_today(
        self, user_id: str, timezone_name: str, current_user_time: datetime
    ) -> DailyLog:
        """Ensure today's file exists without warming older files."""
        return await self._run(user_id, timezone_name, current_user_time, warmup=False)

    async def _run(
        self, user_id: str, timezone_name: str, timestamp: datetime, *, warmup: bool
    ) -> DailyLog:
        """Validate and promptly admit work without creating an internal queue."""
        if not isinstance(user_id, str) or not re.fullmatch(r"[0-9a-f]{32}", user_id):
            raise ValueError("user_id must be 32 lowercase hexadecimal characters")
        local_date = _local_time(timestamp, _timezone(timezone_name)).date()
        daily_log = DailyLog(
            self.settings.host_user_data_root / user_id / "daily-logs", # join paths with /
            self.settings.app_user_data_root / user_id / "daily-logs",
            local_date,
            timezone_name,
        )
        if self._closed or user_id in self._tasks or len(self._tasks) >= self._workers:
            logger.warning("Daily-log operation admission rejected for user %s", user_id)
            raise DailyLogError("Daily-log worker admission unavailable")
        worker_task = asyncio.create_task(asyncio.to_thread(self._prepare, daily_log, warmup))
        self._tasks[user_id] = worker_task
        worker_task.add_done_callback(lambda task: self._worker_finished(user_id, task))
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await asyncio.shield(worker_task)
        except TimeoutError as error:
            logger.warning("Daily-log operation timed out for user %s", user_id)
            raise DailyLogError("Daily-log preparation timed out") from error
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise DailyLogError("Daily-log preparation failed") from error

    def _worker_finished(self, user_id: str, worker_task: asyncio.Task[DailyLog]) -> None:
        """Release admission only after completion and retrieve late failures."""
        self._tasks.pop(user_id, None)
        if not worker_task.cancelled():
            error = worker_task.exception()
            if error is not None:
                logger.warning(
                    "Daily-log filesystem operation failed for user %s (%s)",
                    user_id,
                    type(error).__name__,
                )

    async def aclose(self) -> None:
        """Stop admission and boundedly drain workers without cancelling threads."""
        self._closed = True
        if self._tasks:
            _, unfinished = await asyncio.wait(
                list(self._tasks.values()), timeout=SHUTDOWN_TIMEOUT_SECONDS
            )
            if unfinished:
                logger.warning("Daily-log shutdown timed out with %d workers", len(unfinished))
                raise TimeoutError("Daily-log workers still running")

    @staticmethod
    def _prepare(daily_log: DailyLog, warmup: bool) -> DailyLog:
        """Perform all filesystem operations inside one tracked worker."""
        directory_descriptor = _open_daily_logs_directory(daily_log.app_daily_logs_path)
        try:
            filename = f"{daily_log.local_date.isoformat()}.md"
            try:
                file_descriptor = os.open(
                    filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o666, dir_fd=directory_descriptor,
                )
            except FileExistsError:
                os.close(_open_regular_file(directory_descriptor, filename))
            else:
                # File doesn't exist, create it.
                with os.fdopen(file_descriptor, "wb") as daily_file:
                    daily_file.write(
                        f"# {daily_log.local_date.isoformat()} ({daily_log.timezone_name})\n".encode()
                    )
                    _chmod_created(daily_file.fileno(), 0o666)
            if warmup:
                available_dates = min(WARMUP_DAYS, daily_log.local_date.toordinal() - 1)
                # Download a bounded set of previous daily logs.
                for offset in range(1, available_dates + 1):
                    filename = f"{daily_log.local_date - timedelta(days=offset)}.md"
                    try:
                        file_descriptor = _open_regular_file(directory_descriptor, filename)
                        with os.fdopen(file_descriptor, "rb") as previous_file:
                            while previous_file.read(64 * 1024):
                                pass
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        logger.warning(
                            "Daily-log warm-up read failed for %s (%s)",
                            filename, type(error).__name__,
                        )
            return daily_log
        finally:
            os.close(directory_descriptor)
