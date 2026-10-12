"""Daily-log preparation and sandbox mounts share paths without sharing cleanup."""

import uuid
from dataclasses import replace
from datetime import datetime, timezone

from assistant_agent.daily_logs import DailyLogService
from test_sandbox_lifecycle import lifecycle  # noqa: F401 -- reuse the Docker-shaped fixture


async def test_prepared_daily_log_survives_sandbox_cleanup(lifecycle, tmp_path):  # noqa: F811
    """Mount the prepared host path and preserve app storage through cleanup."""
    sandbox_service, _, _, configs = lifecycle
    sandbox_service.settings = replace(
        sandbox_service.settings,
        app_input_root=tmp_path.resolve() / "session-inputs",
        host_user_data_root=sandbox_service.settings.host_input_root.parent / "user-data",
        app_user_data_root=tmp_path.resolve() / "user-data",
    )
    daily_log_service = DailyLogService(sandbox_service.settings)
    user_id = uuid.uuid4().hex
    captured_time = datetime(2026, 10, 12, 2, 30, tzinfo=timezone.utc)
    try:
        daily_log = await daily_log_service.prepare(
            user_id, "America/New_York", captured_time
        )
        daily_file = daily_log.app_daily_logs_path / "2026-10-11.md"
        daily_file.write_text(daily_file.read_text() + "Remember this entry.\n")
        preserved_content = daily_file.read_bytes()

        handle = await sandbox_service.allocate(
            user_id, uuid.uuid4().hex,
            host_daily_logs_path=daily_log.host_daily_logs_path,
        )
        assert handle.host_daily_logs_path == daily_log.host_daily_logs_path
        assert configs[-1]["HostConfig"]["Binds"] == [
            f"{handle.host_input_path}:/input:ro",
            f"{daily_log.host_daily_logs_path}:/daily-logs",
        ]
        await sandbox_service.destroy(handle)
        assert not handle.app_input_path.exists()
        assert daily_file.read_bytes() == preserved_content

        # Reconciliation may remove abandoned session inputs, including hex IDs.
        orphan_input = sandbox_service.settings.app_input_root / user_id
        orphan_input.mkdir()
        await sandbox_service.reconcile()
        assert not orphan_input.exists()
        assert daily_file.read_bytes() == preserved_content
    finally:
        await sandbox_service.close()
        await daily_log_service.aclose()
