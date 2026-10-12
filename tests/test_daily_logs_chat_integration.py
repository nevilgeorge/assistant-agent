"""Exercise daily-log turns using real preparation, allocation, and Claude launch code."""

import shlex
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from assistant_agent.chat import ConversationManager
from assistant_agent.chat.claude_process import ClaudeProcess, GmailUnavailable
from assistant_agent.daily_logs import DailyLogService
from assistant_agent.sandbox import ExecResult
from test_claude_process import ControlStream
from test_sandbox_lifecycle import lifecycle  # noqa: F401


@pytest.mark.parametrize("gmail_fallback", [False, True])
async def test_daily_log_turns_launch_and_cleanup(lifecycle, tmp_path, monkeypatch, gmail_fallback):  # noqa: F811
    """Deliver one captured timestamp per turn and retain logs after retirement."""
    sandbox_service, _, _, configs = lifecycle
    sandbox_service.settings = replace(
        sandbox_service.settings,
        app_input_root=tmp_path.resolve() / "session-inputs",
        app_user_data_root=tmp_path.resolve() / "user-data",
        host_user_data_root=sandbox_service.settings.host_input_root.parent / "user-data",
    )
    captured_times = [
        datetime(2026, 10, 13, 3, 59, tzinfo=UTC),
        datetime(2026, 10, 13, 4, 1, tzinfo=UTC),
    ]
    clock = Mock(side_effect=captured_times)
    daily_log_service = DailyLogService(sandbox_service.settings, clock=clock)
    timezone_service = SimpleNamespace(resolve=AsyncMock(return_value="America/New_York"))
    streams = []

    async def create_exec(*args, **kwargs):
        stream = ControlStream()
        streams.append(stream)
        return SimpleNamespace(start=lambda: stream)

    monkeypatch.setattr(sandbox_service, "exec", AsyncMock(side_effect=create_exec))
    monkeypatch.setattr(sandbox_service, "run", AsyncMock(return_value=ExecResult(0, "")))
    access_service = None
    if gmail_fallback:
        access_service = SimpleNamespace(
            invalidate_outstanding=AsyncMock(),
            revoke_conversation=AsyncMock(),
            issue=AsyncMock(return_value=SimpleNamespace(
                token_id="token-id", raw_token="synthetic-grant",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )),
        )
        monkeypatch.setattr(
            ClaudeProcess, "discover_gmail", AsyncMock(side_effect=GmailUnavailable("discovery"))
        )
    conversation_manager = ConversationManager(
        service=sandbox_service, access_service=access_service,
        daily_log_service=daily_log_service, user_timezone_service=timezone_service,
    )
    user_id = uuid.uuid4().hex
    try:
        await conversation_manager.submit(user_id, "Remember blue")
        conversation = conversation_manager.get(user_id)
        handle = conversation.sandbox_handle
        daily_path = conversation.daily_log.app_daily_logs_path
        first_file = daily_path / "2026-10-12.md"
        assert first_file.read_text() == "# 2026-10-12 (America/New_York)\n"
        first_file.write_text(first_file.read_text() + "Blue is the remembered color.\n")
        assert configs[-1]["HostConfig"]["Binds"] == [
            f"{handle.host_input_path}:/input:ro",
            f"{handle.host_daily_logs_path}:/daily-logs",
        ]
        for execution_call in sandbox_service.exec.call_args_list:
            command = shlex.split(execution_call.args[0])[-1]
            assert "Read,Glob,Grep,Bash,Edit,Write" in command
            assert "--add-dir /daily-logs" in command
            assert "Read(//daily-logs/**)" in command
            assert "Edit(//daily-logs/**)" in command
        assert len(streams) == (2 if gmail_fallback else 1)
        if gmail_fallback:
            assert streams[0].closed
        assert streams[-1].writes[-1] == {
            "type": "user", "message": {
                "role": "user",
                "content": "Current user time: 2026-10-12T23:59:00-04:00\n\nRemember blue",
            },
        }
        conversation.receive({"type": "result", "subtype": "success", "result": "Recorded"})
        await conversation_manager.submit(user_id, "What was remembered?")
        assert (daily_path / "2026-10-13.md").read_text() == "# 2026-10-13 (America/New_York)\n"
        assert streams[-1].writes[-1]["message"]["content"] == (
            "Current user time: 2026-10-13T00:01:00-04:00\n\nWhat was remembered?"
        )
        assert [message["text"] for message in conversation.snapshot()["transcript"]
                if message["role"] == "user"] == ["Remember blue", "What was remembered?"]
        assert clock.call_count == 2
        timezone_service.resolve.assert_awaited_once_with(user_id)
        await conversation_manager.reset(user_id)
        assert not handle.app_input_path.exists()
        assert "Blue is the remembered color." in first_file.read_text()
        assert streams[-1].closed
    finally:
        await conversation_manager.close()
        await sandbox_service.close()
        await daily_log_service.aclose()
