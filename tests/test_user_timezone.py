"""Calendar timezone lookup, safe persistence, and fallback behavior."""

import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials

from assistant_agent import user_timezone as module
from assistant_agent.database import Base, make_async_engine, make_async_session_factory
from assistant_agent.user_timezone import UserTimezoneService, valid_timezone
from assistant_agent.web_store import WebStore


@pytest.mark.parametrize("name", ["UTC", "America/New_York", "Asia/Kolkata"])
def test_valid_timezone(name):
    assert valid_timezone(name) == name


@pytest.mark.parametrize("name", [None, "", "Bogus/Timezone", "../UTC", "/UTC", "a" * 65, 123])
def test_invalid_timezone(name):
    assert valid_timezone(name) is None


def test_packaged_timezone_data_without_system_search_path():
    result = subprocess.run(
        [sys.executable, "-c", "from zoneinfo import ZoneInfo; "
         "assert ZoneInfo('America/New_York').key == 'America/New_York'"],
        env={**os.environ, "PYTHONTZPATH": ""}, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("failure", [False, True])
def test_calendar_request_and_transport_cleanup(monkeypatch, failure):
    transport = Mock()
    http_factory = Mock(return_value=transport)
    authorize = Mock()
    calendar_service = Mock()
    calendar_service.calendars.return_value.get.return_value.execute.return_value = {
        "timeZone": "America/New_York"
    }
    if failure:
        calendar_service.calendars.return_value.get.return_value.execute.side_effect = OSError("bad")
    build = Mock(return_value=calendar_service)
    monkeypatch.setattr(module.httplib2, "Http", http_factory)
    monkeypatch.setattr(module, "AuthorizedHttp", authorize)
    monkeypatch.setattr(module, "build", build)
    credentials = Credentials(token="access", refresh_token="secret", scopes=["calendar"])
    if failure:
        with pytest.raises(OSError):
            module.fetch_primary_calendar_timezone(credentials)
    else:
        assert module.fetch_primary_calendar_timezone(credentials) == "America/New_York"
    http_factory.assert_called_once_with(timeout=5)
    token_credentials = authorize.call_args.args[0]
    assert token_credentials.token == "access"
    assert token_credentials.refresh_token is None
    assert authorize.call_args.kwargs == {"http": transport, "max_refresh_attempts": 0}
    build.assert_called_once_with(
        "calendar", "v3", http=authorize.return_value,
        cache_discovery=False, static_discovery=True, num_retries=0,
    )
    calendar_service.calendars.return_value.get.assert_called_once_with(calendarId="primary")
    calendar_service.calendars.return_value.get.return_value.execute.assert_called_once_with(
        num_retries=0
    )
    transport.close.assert_called_once()


def fake_store(timezone=None):
    return SimpleNamespace(
        user=AsyncMock(return_value=SimpleNamespace(timezone=timezone)),
        load_refreshed=AsyncMock(return_value=Credentials(token="token")),
        save_timezone=AsyncMock(), google_worker=SimpleNamespace(run=AsyncMock()),
    )


async def test_cache_avoids_network():
    store = fake_store("Europe/London")
    assert await UserTimezoneService(store).resolve("user") == "Europe/London"
    store.load_refreshed.assert_not_awaited()
    store.google_worker.run.assert_not_awaited()


async def test_lazy_fetch_persists_valid_timezone():
    store = fake_store("Bad/Timezone")
    store.google_worker.run.return_value = "Asia/Tokyo"
    assert await UserTimezoneService(store).resolve("user") == "Asia/Tokyo"
    store.save_timezone.assert_awaited_once_with("user", "Asia/Tokyo")
    store.google_worker.run.assert_awaited_once_with(
        module.fetch_primary_calendar_timezone, store.load_refreshed.return_value
    )


async def test_fallback_is_not_persisted_and_retries():
    store = fake_store()
    store.google_worker.run.side_effect = [None, "Europe/London"]
    service = UserTimezoneService(store)
    assert await service.resolve("user") == "UTC"
    store.save_timezone.assert_not_awaited()
    assert await service.resolve("user") == "Europe/London"
    assert store.google_worker.run.await_count == 2


@pytest.mark.parametrize("stage", ["user", "load_refreshed", "worker", "save_timezone"])
async def test_failures_fall_back_without_exposing_exception(stage, caplog):
    store = fake_store()
    store.google_worker.run.return_value = "Asia/Tokyo"
    operation = store.google_worker.run if stage == "worker" else getattr(store, stage)
    operation.side_effect = RuntimeError("secret token and raw Google response")
    assert await UserTimezoneService(store).resolve("user") == "UTC"
    assert "secret" not in caplog.text
    assert "resolution failed" in caplog.text


async def test_missing_user_or_credentials():
    store = fake_store()
    store.user.return_value = None
    service = UserTimezoneService(store)
    assert await service.resolve("user") == "UTC"
    store.load_refreshed.assert_not_awaited()
    store.user.return_value = SimpleNamespace(timezone=None)
    store.load_refreshed.return_value = None
    assert await service.resolve("user") == "UTC"
    store.google_worker.run.assert_not_awaited()
    store.save_timezone.assert_not_awaited()


async def test_cancellation_is_preserved():
    store = fake_store()
    store.google_worker.run.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await UserTimezoneService(store).resolve("user")
    store.save_timezone.assert_not_awaited()


async def test_save_timezone_updates_only_existing_user(tmp_path):
    engine = make_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'timezone.db'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        store = WebStore(make_async_session_factory(engine), Fernet.generate_key().decode())
        user_id = await store.save_credentials("sub", "user@example.com", Credentials(token="token"))
        original = await store.user(user_id)
        await store.save_timezone(user_id, "Asia/Tokyo")
        updated = await store.user(user_id)
        assert updated.timezone == "Asia/Tokyo"
        assert updated.updated_at >= original.updated_at
        assert updated.credentials == original.credentials
        assert updated.connected_at == original.connected_at
        await store.disconnect(user_id)
        await store.save_timezone(user_id, "UTC")
        assert await store.user(user_id) is None
        assert await store.user_by_google_sub("sub") is None
    finally:
        await engine.dispose()


@pytest.mark.parametrize("response", [{}, {"timeZone": "Invalid/Zone"}, {"timeZone": 123}, None, []])
def test_calendar_invalid_responses(monkeypatch, response):
    transport = Mock()
    calendar_service = Mock()
    calendar_service.calendars.return_value.get.return_value.execute.return_value = response
    monkeypatch.setattr(module.httplib2, "Http", Mock(return_value=transport))
    monkeypatch.setattr(module, "AuthorizedHttp", Mock())
    monkeypatch.setattr(module, "build", Mock(return_value=calendar_service))
    assert module.fetch_primary_calendar_timezone(Credentials(token="access")) is None
    transport.close.assert_called_once()


def test_calendar_closes_transport_when_build_fails(monkeypatch):
    transport = Mock()
    monkeypatch.setattr(module.httplib2, "Http", Mock(return_value=transport))
    monkeypatch.setattr(module, "AuthorizedHttp", Mock())
    monkeypatch.setattr(module, "build", Mock(side_effect=OSError("build failed")))
    with pytest.raises(OSError):
        module.fetch_primary_calendar_timezone(Credentials(token="access"))
    transport.close.assert_called_once()
