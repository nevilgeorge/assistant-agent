import asyncio
import threading
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials
from fastapi import Request
from sqlalchemy.exc import IntegrityError

from assistant_agent.database import Base, WebSession, utcnow
from assistant_agent.chat import ChatError


@pytest.fixture
async def web(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'web.db'}")
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("APP_ENV", "development")
    from assistant_agent import config, web as module
    from test_chat import FakeProcess, FakeSandbox

    config.get_settings.cache_clear()
    monkeypatch.setattr(module, "Sandbox", FakeSandbox)
    # Production applies migrations before lifespan startup and grant invalidation.
    setup_engine = module.make_async_engine(config.get_settings().database_url)
    try:
        async with setup_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await setup_engine.dispose()
    application = module.create_app()
    clients = []
    try:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(application.router.lifespan_context(application))
            state = application.state
            monkeypatch.setattr(state.conversation_manager, "process_factory", FakeProcess)
            try:
                yield SimpleNamespace(
                    app=application,
                    module=module,
                    web_store=state.web_store,
                    conversation_manager=state.conversation_manager,
                    settings=state.settings,
                    COOKIE=module.COOKIE,
                    clients=clients,
                )
            finally:
                for client in clients:
                    await client.aclose()
    finally:
        config.get_settings.cache_clear()


def client_for(web):
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app), base_url="http://localhost:8000"
    )
    web.clients.append(client)
    return client


def credentials():
    return Credentials(
        token="access",
        refresh_token="refresh",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client-id",
        client_secret="client-secret",
        scopes=[
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
        ],
    )


async def connect(web, monkeypatch, sub, email):
    monkeypatch.setattr(
        web.module,
        "authorization_url",
        lambda nonce: (
            f"https://accounts.google.com/?state=state-{sub}",
            f"state-{sub}",
            "verifier",
        ),
    )
    monkeypatch.setattr(web.module, "exchange_code", lambda *args, **kwargs: credentials())
    monkeypatch.setattr(web.module, "account_identity", lambda creds, expected_nonce: (email, sub))
    client = client_for(web)
    start = await client.get("/auth/google/start", follow_redirects=False)
    assert start.status_code == 302
    callback = await client.get(
        f"/auth/google/callback?state=state-{sub}&code=code", follow_redirects=False
    )
    assert callback.status_code == 303
    return client


async def test_user_isolation_and_csrf(web, monkeypatch):
    a = await connect(web, monkeypatch, "sub-a", "a@example.com")
    b = await connect(web, monkeypatch, "sub-b", "b@example.com")
    monkeypatch.setattr(web.web_store, "load_refreshed", AsyncMock(return_value=credentials()))
    page = (await a.get("/")).text
    assert "a@example.com" in page and "b@example.com" not in page
    assert (await a.post("/disconnect", data={"csrf_token": "wrong"})).status_code == 400
    assert await web.web_store.user_by_google_sub("sub-a") is not None
    assert (await b.post("/disconnect", data={"csrf_token": "wrong"})).status_code == 400
    assert await web.web_store.user_by_google_sub("sub-a") is not None
    assert (await client_for(web).post("/disconnect", data={})).status_code == 401
    session = await web.web_store.session(a.cookies.get(web.COOKIE))
    monkeypatch.setattr(web.module, "revoke", lambda creds: True)
    assert (
        await a.post("/disconnect", data={"csrf_token": session.csrf_token}, follow_redirects=False)
    ).status_code == 303
    assert (
        await web.web_store.user_by_google_sub("sub-a") is None
        and await web.web_store.user_by_google_sub("sub-b") is not None
    )


async def test_state_consumed_and_nonce_passed(web, monkeypatch):
    monkeypatch.setattr(
        web.module,
        "authorization_url",
        lambda nonce: ("https://google.test", "expected", "verifier"),
    )
    client = client_for(web)
    await client.get("/auth/google/start", follow_redirects=False)
    assert (await client.get("/auth/google/callback?state=wrong&code=code")).status_code == 400
    assert (await client.get("/auth/google/callback?state=expected&code=code")).status_code == 400
    await client.get("/auth/google/start", follow_redirects=False)
    observed = {}
    monkeypatch.setattr(web.module, "exchange_code", lambda *args, **kwargs: credentials())

    def identity(creds, expected_nonce):
        observed["nonce"] = expected_nonce
        return ("a@example.com", "sub-a")

    monkeypatch.setattr(web.module, "account_identity", identity)
    assert (
        await client.get("/auth/google/callback?state=expected&code=code", follow_redirects=False)
    ).status_code == 303
    assert observed["nonce"]


async def test_session_expiry_and_encrypted_credentials(web, monkeypatch):
    client = await connect(web, monkeypatch, "sub-a", "a@example.com")
    user = await web.web_store.user_by_google_sub("sub-a")
    assert len(user.user_id) == 32 and user.user_id != user.google_sub
    assert b"refresh" not in user.credentials
    assert (await web.web_store.load_credentials(user.user_id)).refresh_token == "refresh"
    token = client.cookies.get(web.COOKIE)
    from assistant_agent.web_store import WebStore

    reloaded = WebStore(factory=web.web_store.factory, key=web.settings.credential_encryption_key)
    assert (await reloaded.session(token)).user_id == user.user_id
    assert (await reloaded.load_credentials(user.user_id)).refresh_token == "refresh"
    async with web.web_store.factory.begin() as db:
        row = await db.get(WebSession, (await web.web_store.session(token)).id)
        row.expires_at = utcnow() - timedelta(seconds=1)
    assert await web.web_store.session(token) is None
    assert "a@example.com" not in (await client.get("/")).text


async def test_refresh_persists_new_token(web, monkeypatch):
    user_id = await web.web_store.save_credentials("sub-a", "a@example.com", credentials())
    from google.oauth2.credentials import Credentials as GoogleCredentials

    monkeypatch.setattr(GoogleCredentials, "valid", property(lambda self: False))

    def refresh(self, request):
        self.token = "new-access"

    monkeypatch.setattr(GoogleCredentials, "refresh", refresh)
    assert (await web.web_store.load_refreshed(user_id)).token == "new-access"
    assert (await web.web_store.load_credentials(user_id)).token == "new-access"


async def test_reconnect_preserves_internal_user_id(web):
    user_id = await web.web_store.save_credentials(
        "stable-google-sub", "old@example.com", credentials()
    )
    again = await web.web_store.save_credentials("stable-google-sub", "new@example.com", credentials())
    assert again == user_id
    assert (await web.web_store.user(user_id)).email == "new@example.com"
    assert (await web.web_store.user_by_google_sub("stable-google-sub")).user_id == user_id


async def test_session_id_and_token_rotation(web):
    from assistant_agent.web_store import token_hash

    (old_token, original) = await web.web_store.new_session()
    assert len(original.id) == 32
    assert original.session_token_hash == token_hash(old_token)
    assert original.id != original.session_token_hash
    original.oauth_state = "saved-state"
    await web.web_store.save_session(original)
    assert (await web.web_store.session(old_token)).oauth_state == "saved-state"
    user_id = await web.web_store.save_credentials("sub-a", "a@example.com", credentials())
    (new_token, rotated) = await web.web_store.rotate(old_token, user_id)
    assert rotated.id != original.id
    assert rotated.session_token_hash == token_hash(new_token)
    assert await web.web_store.session(old_token) is None
    assert (await web.web_store.session(new_token)).user_id == user_id
    async with web.web_store.factory() as db:
        assert await db.get(WebSession, original.id) is None
        assert (await db.get(WebSession, rotated.id)).session_token_hash == token_hash(new_token)


async def test_verified_identity_and_nonce(web, monkeypatch):
    import assistant_agent.google_oauth as oauth

    monkeypatch.setattr(
        oauth.id_token,
        "verify_oauth2_token",
        lambda *args, **kwargs: {
            "email": "a@example.com",
            "email_verified": True,
            "sub": "stable-sub",
            "nonce": "expected",
        },
    )

    class Token:
        id_token = "signed-id-token"

    assert oauth.account_identity(Token(), expected_nonce="expected") == (
        "a@example.com",
        "stable-sub",
    )
    with pytest.raises(ValueError, match="nonce"):
        oauth.account_identity(Token(), expected_nonce="wrong")
    monkeypatch.setattr(
        oauth.id_token,
        "verify_oauth2_token",
        lambda *args, **kwargs: {
            "email": "a@example.com",
            "email_verified": False,
            "sub": "stable-sub",
            "nonce": "expected",
        },
    )
    with pytest.raises(ValueError, match="not verified"):
        oauth.account_identity(Token(), expected_nonce="expected")


async def test_message_requires_auth_and_csrf_and_returns_sandbox_response(web, monkeypatch):
    calls = []
    monkeypatch.setattr(
        web.conversation_manager,
        "submit",
        AsyncMock(side_effect=lambda user, prompt, conversation_id: calls.append(prompt)
                  or {"conversation_id": "chat", "turn_id": "turn"}),
    )
    anonymous = client_for(web)
    assert (await anonymous.post("/api/message", json={"message": "Hi"})).status_code == 401
    client = await connect(web, monkeypatch, "sub-a", "a@example.com")
    assert (await client.post("/api/message", json={"message": "Hi"})).status_code == 400
    csrf = (await web.web_store.session(client.cookies.get(web.COOKIE))).csrf_token
    headers = {"X-CSRF-Token": csrf}
    assert (
        await client.post("/api/message", json={"message": "  "}, headers=headers)
    ).status_code == 400
    assert (
        await client.post("/api/message", json={"message": "x" * 4001}, headers=headers)
    ).status_code == 422
    response = await client.post("/api/message", json={"message": "Hi"}, headers=headers)
    assert response.status_code == 202
    assert response.json() == {"conversation_id": "chat", "turn_id": "turn"}
    assert calls == ["Hi"]


async def test_message_reports_sandbox_failures_without_exposing_details(web, monkeypatch):
    client = await connect(web, monkeypatch, "sub-a", "a@example.com")
    headers = {"X-CSRF-Token": (await web.web_store.session(client.cookies.get(web.COOKIE))).csrf_token}

    async def unavailable(*args):
        raise ChatError("The assistant is unavailable.", 503)

    monkeypatch.setattr(web.conversation_manager, "submit", unavailable)
    failed = await client.post("/api/message", json={"message": "Hi"}, headers=headers)
    assert failed.status_code == 503


async def test_chat_routes_use_overridden_store_and_conversation_manager(web, monkeypatch):
    from assistant_agent.web_dependencies import get_conversation_manager, get_web_store

    user_id = "injected-user"
    injected_store = SimpleNamespace(
        session=AsyncMock(return_value=SimpleNamespace(user_id=user_id, csrf_token="injected-csrf")),
        user=AsyncMock(return_value=SimpleNamespace(user_id=user_id)),
    )
    conversation = web.conversation_manager.get(user_id)
    injected_manager = SimpleNamespace(
        submit=AsyncMock(return_value={"conversation_id": conversation.conversation_id, "turn_id": "injected-turn"}),
        get=lambda user: conversation,
        reset=AsyncMock(),
    )
    web.app.dependency_overrides[get_web_store] = lambda: injected_store
    web.app.dependency_overrides[get_conversation_manager] = lambda: injected_manager
    monkeypatch.delattr(web.app.state, "web_store")
    monkeypatch.delattr(web.app.state, "conversation_manager")
    client = client_for(web)

    response = await client.post(
        "/api/message",
        json={"message": "Injected prompt"},
        headers={"X-CSRF-Token": "injected-csrf"},
    )
    assert response.status_code == 202
    assert response.json()["turn_id"] == "injected-turn"
    injected_manager.submit.assert_awaited_once_with(user_id, "Injected prompt", None)
    snapshot = await client.get("/api/conversation")
    assert snapshot.json()["conversation_id"] == conversation.conversation_id
    stream = await client.get("/api/conversation/stream?conversation_id=old")
    assert '"type": "reload"' in stream.text
    reset = await client.post(
        "/api/conversation/reset", headers={"X-CSRF-Token": "injected-csrf"}
    )
    assert reset.status_code == 200
    injected_manager.reset.assert_awaited_once_with(user_id)
    assert injected_store.session.await_count == 4


async def test_oauth_callback_uses_overridden_settings_and_worker(web, monkeypatch):
    from assistant_agent.web_dependencies import get_application_settings, get_google_worker

    monkeypatch.setattr(
        web.module, "authorization_url", lambda nonce: ("https://accounts.google.com", "state", "verifier")
    )
    client = client_for(web)
    assert (await client.get("/auth/google/start")).status_code == 302
    session = await web.web_store.session(client.cookies.get(web.COOKIE))
    nonce = session.oauth_nonce
    injected_settings = replace(web.settings, base_url="https://injected.example", app_env="production")
    injected_credentials = credentials()
    injected_worker = SimpleNamespace(
        run=AsyncMock(side_effect=[injected_credentials, ("injected@example.com", "injected-sub")])
    )
    web.app.dependency_overrides[get_application_settings] = lambda: injected_settings
    web.app.dependency_overrides[get_google_worker] = lambda: injected_worker
    monkeypatch.delattr(web.app.state, "settings")
    monkeypatch.delattr(web.app.state, "google_worker")

    response = await client.get("/auth/google/callback?state=state&code=code")
    assert response.status_code == 303
    assert "Secure" in response.headers["set-cookie"]
    assert injected_worker.run.await_args_list[0].args == (
        web.module.exchange_code,
        "https://injected.example/auth/google/callback?state=state&code=code",
    )
    assert injected_worker.run.await_args_list[1].args == (
        web.module.account_identity, injected_credentials
    )
    assert injected_worker.run.await_args_list[1].kwargs == {"expected_nonce": nonce}
    assert (await web.web_store.user_by_google_sub("injected-sub")).email == "injected@example.com"


async def test_conversation_snapshot_isolation_reset_and_disconnect(web, monkeypatch):
    a = await connect(web, monkeypatch, "sub-a", "a@example.com")
    b = await connect(web, monkeypatch, "sub-b", "b@example.com")
    first = (await a.get("/api/conversation")).json()
    assert (await b.get("/api/conversation")).json()["conversation_id"] != first["conversation_id"]
    assert (await client_for(web).get("/api/conversation")).status_code == 401
    assert (await a.post("/api/conversation/reset")).status_code == 400
    csrf = (await web.web_store.session(a.cookies.get(web.COOKIE))).csrf_token
    assert (
        await a.post("/api/conversation/reset", headers={"X-CSRF-Token": csrf})
    ).status_code == 200
    assert (await a.get("/api/conversation")).json()["conversation_id"] != first["conversation_id"]
    monkeypatch.setattr(web.module, "revoke", lambda creds: True)
    user = (await web.web_store.user_by_google_sub("sub-a")).user_id
    assert (
        await a.post("/disconnect", data={"csrf_token": csrf}, follow_redirects=False)
    ).status_code == 303
    assert user not in web.conversation_manager.conversations


async def test_stream_reloads_stale_conversation_and_enforces_auth(web, monkeypatch):
    anonymous = client_for(web)
    assert (await anonymous.get("/api/conversation/stream?conversation_id=old")).status_code == 401
    client = await connect(web, monkeypatch, "sub-a", "a@example.com")
    response = await client.get("/api/conversation/stream?conversation_id=old")
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type": "reload"' in response.text
    assert response.headers["x-accel-buffering"] == "no"


async def test_stream_replays_events_after_snapshot(web, monkeypatch):
    from test_chat import FakeProcess

    monkeypatch.setattr(web.conversation_manager, "process_factory", FakeProcess)
    client = await connect(web, monkeypatch, "sub-a", "a@example.com")
    snapshot = (await client.get("/api/conversation")).json()
    csrf = (await web.web_store.session(client.cookies.get(web.COOKIE))).csrf_token
    submitted = await client.post(
        "/api/message", json={"message": "Hi"}, headers={"X-CSRF-Token": csrf}
    )
    assert submitted.status_code == 202
    user = (await web.web_store.user_by_google_sub("sub-a")).user_id
    conversation = web.conversation_manager.get(user)
    conversation.claude_process.text("<script>alert(1)</script>")
    conversation.claude_process.finish()
    await conversation.close()
    response = await client.get(
        "/api/conversation/stream",
        params={"conversation_id": snapshot["conversation_id"], "after": snapshot["sequence"]},
    )
    assert '"type": "turn_start"' in response.text
    assert '"type": "assistant_delta"' in response.text
    assert '"type": "turn_completion"' in response.text
    assert '"type": "conversation_reset"' in response.text
    assert "id: 1" in response.text
    page = (await client.get("/")).text
    assert "renderMessage(content, content.dataset.text + data.text)" in page
    parser = await client.get("/static/vendor/markdown-it-14.1.0.min.js")
    assert parser.status_code == 200
    assert "markdown-it" in parser.text


@pytest.mark.parametrize("operation", ["database", "google", "chat"])
async def test_slow_operations_allow_health_checks_and_loop_progress(web, monkeypatch, operation):
    client = await connect(web, monkeypatch, "sub-a", "a@example.com")
    session = await web.web_store.session(client.cookies.get(web.COOKIE))
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def blocking(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        if not release.wait(5):
            raise TimeoutError("Test did not release SDK worker")
        return credentials() if operation == "google" else {"conversation_id": "chat"}

    if operation == "database":
        original = web.web_store.session

        async def slow_session(token):
            started.set()
            while not release.is_set():
                await asyncio.sleep(0)
            return await original(token)

        monkeypatch.setattr(web.web_store, "session", slow_session)
        request = client.get("/")
    elif operation == "google":
        await client.get("/auth/google/start", follow_redirects=False)
        monkeypatch.setattr(web.module, "exchange_code", blocking)
        request = client.get("/auth/google/callback?state=state-sub-a&code=code")
    else:
        async def slow_chat(*args):
            started.set()
            while not release.is_set():
                await asyncio.sleep(0)
            return {"conversation_id": "chat"}

        monkeypatch.setattr(web.conversation_manager, "submit", slow_chat)
        request = client.post(
            "/api/message", json={"message": "Hi"}, headers={"X-CSRF-Token": session.csrf_token}
        )
    task = asyncio.create_task(request)
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        # The request is still blocked while unrelated I/O and loop scheduling progress.
        for _ in range(3):
            await asyncio.sleep(0)
        assert not task.done()
        response = await asyncio.wait_for(client_for(web).get("/healthz"), timeout=2)
        assert response.status_code == 200
        assert not task.done()
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=2)


async def test_refresh_does_not_recreate_disconnected_user(web, monkeypatch):
    user_id = await web.web_store.save_credentials("sub-a", "a@example.com", credentials())
    monkeypatch.setattr(Credentials, "valid", property(lambda self: False))
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def refresh(self, request):
        loop.call_soon_threadsafe(started.set)
        if not release.wait(5):
            raise TimeoutError("Test did not release refresh")
        self.token = "new-token"

    monkeypatch.setattr(Credentials, "refresh", refresh)
    task = asyncio.create_task(web.web_store.load_refreshed(user_id))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert web.web_store.factory.kw["bind"].pool.checkedout() == 0
        await web.web_store.disconnect(user_id)
    finally:
        release.set()
    assert await asyncio.wait_for(task, timeout=2) is None
    assert await web.web_store.user_by_google_sub("sub-a") is None


async def test_failed_rotation_rolls_back_deletion(web, monkeypatch):
    old_token, _ = await web.web_store.new_session()
    conflicting_token, _ = await web.web_store.new_session()
    user_id = await web.web_store.save_credentials("sub-a", "a@example.com", credentials())
    monkeypatch.setattr(
        "assistant_agent.web_store.secrets.token_urlsafe", lambda size: conflicting_token
    )
    with pytest.raises(IntegrityError):
        await web.web_store.rotate(old_token, user_id)
    assert await web.web_store.session(old_token) is not None
    assert web.web_store.factory.kw["bind"].pool.checkedout() == 0


async def test_cancelled_transaction_rolls_back_and_releases_connection(web):
    started = asyncio.Event()

    async def transaction():
        async with web.web_store.factory.begin() as db:
            db.add(
                WebSession(
                    id="cancelled",
                    session_token_hash="hash",
                    csrf_token="csrf",
                    expires_at=utcnow() + timedelta(days=1),
                )
            )
            await db.flush()
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(transaction())
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with web.web_store.factory() as db:
        assert await db.get(WebSession, "cancelled") is None
    assert web.web_store.factory.kw["bind"].pool.checkedout() == 0


async def test_stream_closes_when_authentication_expires(web, monkeypatch):
    client = await connect(web, monkeypatch, "sub-a", "a@example.com")
    token = client.cookies.get(web.COOKIE)
    row = await web.web_store.session(token)
    conversation = web.conversation_manager.get(row.user_id)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/conversation/stream",
            "app": web.app,
            "headers": [(b"cookie", f"{web.COOKIE}={token}".encode())],
        }
    )
    monkeypatch.setattr(request, "is_disconnected", AsyncMock(return_value=False))
    monkeypatch.setattr(web.module, "HEARTBEAT_SECONDS", 0)
    response = await web.module.conversation_stream(
        request, conversation.conversation_id, web.web_store, web.conversation_manager
    )
    assert await anext(response.body_iterator) == ": heartbeat\n\n"
    async with web.web_store.factory.begin() as db:
        expired = await db.get(WebSession, row.id)
        expired.expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(StopAsyncIteration):
        await anext(response.body_iterator)
    assert web.web_store.factory.kw["bind"].pool.checkedout() == 0


async def test_stream_respects_last_event_id(web, monkeypatch):
    client = await connect(web, monkeypatch, "sub-a", "a@example.com")
    user = await web.web_store.user_by_google_sub("sub-a")
    conversation = web.conversation_manager.get(user.user_id)
    conversation.emit("assistant_delta", text="already-seen")
    await conversation.close()
    response = await client.get(
        "/api/conversation/stream",
        params={"conversation_id": conversation.conversation_id},
        headers={"Last-Event-ID": "1"},
    )
    assert "already-seen" not in response.text
    assert '"type": "conversation_reset"' in response.text
    assert "id: 2" in response.text
    invalid = await client.get(
        "/api/conversation/stream",
        params={"conversation_id": conversation.conversation_id},
        headers={"Last-Event-ID": "invalid"},
    )
    assert invalid.status_code == 400


async def test_failed_startup_closes_chat_and_disposes_engine(web, monkeypatch):
    close = threading.Event()
    dispose = AsyncMock()
    monkeypatch.setattr(
        web.module, "make_async_engine", lambda url: SimpleNamespace(dispose=dispose)
    )
    monkeypatch.setattr(web.module, "make_async_session_factory", lambda engine: web.web_store.factory)

    async def fail_start():
        raise RuntimeError("startup failed")

    manager = SimpleNamespace(start=fail_start, close=AsyncMock(side_effect=close.set))
    monkeypatch.setattr(web.module, "ConversationManager", lambda **kwargs: manager)
    application = web.module.create_app()
    with pytest.raises(RuntimeError, match="startup failed"):
        async with application.router.lifespan_context(application):
            pytest.fail("Failed startup must not yield")
    assert close.is_set()
    assert application.state.gmail_service._closed
    assert application.state.gmail_service.worker is application.state.google_worker
    dispose.assert_awaited_once()


async def test_shutdown_disposes_engine_even_if_chat_close_fails(web, monkeypatch):
    dispose = AsyncMock()
    monkeypatch.setattr(
        web.module, "make_async_engine", lambda url: SimpleNamespace(dispose=dispose)
    )
    monkeypatch.setattr(web.module, "make_async_session_factory", lambda engine: web.web_store.factory)

    async def fail_close():
        raise RuntimeError("cleanup failed")

    manager = SimpleNamespace(start=AsyncMock(), close=fail_close)
    monkeypatch.setattr(web.module, "ConversationManager", lambda **kwargs: manager)
    application = web.module.create_app()
    with pytest.raises(RuntimeError, match="cleanup failed"):
        async with application.router.lifespan_context(application):
            pass
    dispose.assert_awaited_once()
    assert application.state.gmail_service._closed


async def test_postgres_async_driver_transactions_and_disposal(web):
    """Opt-in real-driver verification; all tables live in a temporary schema."""
    import os
    import uuid
    from sqlalchemy import text
    from sqlalchemy.schema import CreateSchema, DropSchema
    from assistant_agent.database import make_async_engine, make_async_session_factory
    from assistant_agent.web_store import WebStore

    url = os.getenv("ASYNC_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set ASYNC_TEST_DATABASE_URL for PostgreSQL integration")
    engine = make_async_engine(url)
    schema = "async_test_" + uuid.uuid4().hex
    scoped = engine.execution_options(schema_translate_map={None: schema})
    factory = make_async_session_factory(scoped)
    store = WebStore(factory, web.settings.credential_encryption_key)
    created = False
    try:
        assert engine.dialect.is_async
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
            created = True
        async with scoped.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        user_id = await store.save_credentials("postgres-sub", "pg@example.com", credentials())
        old_token, _ = await store.new_session()
        new_token, _ = await store.rotate(old_token, user_id)
        assert await store.session(old_token) is None
        assert (await store.session(new_token)).user_id == user_id
        assert (await store.load_credentials(user_id)).refresh_token == "refresh"
        with pytest.raises(RuntimeError, match="rollback"):
            async with factory.begin() as db:
                row = await db.get(WebSession, (await store.session(new_token)).id)
                row.csrf_token = "should-roll-back"
                await db.flush()
                raise RuntimeError("rollback")
        assert (await store.session(new_token)).csrf_token != "should-roll-back"

        # A real slow database query must yield to a second connection on the same loop.
        async with engine.connect() as slow, engine.connect() as fast:
            task = asyncio.create_task(slow.execute(text("SELECT pg_sleep(0.2)")))
            try:
                await asyncio.sleep(0)
                result = await asyncio.wait_for(fast.execute(text("SELECT 1")), timeout=2)
                assert result.scalar_one() == 1
                assert not task.done()
            finally:
                await task
        await store.disconnect(user_id)
        assert await store.user(user_id) is None
        assert await store.session(new_token) is None
        assert engine.pool.checkedout() == 0
    finally:
        try:
            if created:
                async with engine.begin() as connection:
                    await connection.execute(DropSchema(schema, cascade=True))
        finally:
            old_pool = engine.pool
            await engine.dispose()
            assert engine.pool is not old_pool


async def test_disconnect_deletes_credentials_and_sessions_when_docker_cleanup_fails(
    web, monkeypatch
):
    client = await connect(web, monkeypatch, "sub-a", "a@example.com")
    token = client.cookies.get(web.COOKIE)
    session = await web.web_store.session(token)
    user_id = session.user_id
    await web.conversation_manager.submit(user_id, "one")
    handle = web.conversation_manager.sandbox_service.allocated[0]
    monkeypatch.setattr(web.module, "revoke", lambda creds: True)
    web.conversation_manager.sandbox_service.destroy = AsyncMock(side_effect=RuntimeError("Docker unavailable"))
    response = await client.post(
        "/disconnect", data={"csrf_token": session.csrf_token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert user_id not in web.conversation_manager.conversations
    assert await web.web_store.load_credentials(user_id) is None
    assert await web.web_store.session(token) is None
    assert await web.web_store.user_by_google_sub("sub-a") is None
    web.conversation_manager.sandbox_service.destroy.assert_awaited_once_with(handle)


async def test_sandbox_access_dependency_override_on_disconnect(web, monkeypatch):
    from assistant_agent.web_dependencies import get_sandbox_access_service

    client = await connect(web, monkeypatch, "access-user", "access@example.com")
    session = await web.web_store.session(client.cookies.get(web.COOKIE))
    injected_access_service = SimpleNamespace(revoke_user=AsyncMock(return_value=True))
    web.app.dependency_overrides[get_sandbox_access_service] = lambda: injected_access_service
    monkeypatch.delattr(web.app.state, "sandbox_access_service")
    monkeypatch.setattr(web.module, "revoke", lambda creds: True)
    response = await client.post("/disconnect", data={"csrf_token": session.csrf_token})
    assert response.status_code == 303
    injected_access_service.revoke_user.assert_awaited_once_with(session.user_id)


async def test_web_grant_is_private_and_reset_preserves_browser_session(web, monkeypatch, caplog):
    from sqlalchemy import select
    from assistant_agent.database import SandboxAccessToken
    from assistant_agent.sandbox_access import SandboxAuthorizationError
    from assistant_agent.web_store import token_hash

    client = await connect(web, monkeypatch, "grant-user", "grant@example.com")
    session_token = client.cookies.get(web.COOKIE)
    session = await web.web_store.session(session_token)
    access_service = web.app.state.sandbox_access_service
    original_issue = access_service.issue
    issued_tokens = []

    async def capture_issue(*identities):
        issued_access = await original_issue(*identities)
        issued_tokens.append(issued_access.raw_token)
        return issued_access

    monkeypatch.setattr(access_service, "issue", capture_issue)
    response = await client.post(
        "/api/message", json={"message": "Hello"},
        headers={"X-CSRF-Token": session.csrf_token},
    )
    assert response.status_code == 202
    raw_token, = issued_tokens
    conversation = web.conversation_manager.get(session.user_id)
    context = await access_service.authorize(
        raw_token, "search_emails", web.conversation_manager.resolve_live_assignment
    )
    assert context.user_id == session.user_id
    async with web.web_store.factory() as database_session:
        grant = await database_session.scalar(select(SandboxAccessToken))
        assert grant.token_hash == token_hash(raw_token)
        assert raw_token not in repr(grant.__dict__)
    assert raw_token not in repr(conversation.__dict__)
    assert raw_token not in response.text
    assert raw_token not in (await client.get("/api/conversation")).text
    assert raw_token not in caplog.text
    assert conversation.claude_process.sandbox_handle == conversation.sandbox_handle
    await web.conversation_manager.reset(session.user_id)
    with pytest.raises(SandboxAuthorizationError):
        await access_service.authorize(
            raw_token, "search_emails", web.conversation_manager.resolve_live_assignment
        )
    assert await web.web_store.session(session_token) is not None


async def test_cancelled_disconnect_blocks_launch_and_finishes_deletion(web, monkeypatch):
    client = await connect(web, monkeypatch, "cancel-user", "cancel@example.com")
    session_token = client.cookies.get(web.COOKIE)
    session = await web.web_store.session(session_token)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_reset = web.conversation_manager.reset

    async def delayed_reset(user_id):
        entered.set()
        await release.wait()
        await original_reset(user_id)

    monkeypatch.setattr(web.conversation_manager, "reset", delayed_reset)
    monkeypatch.setattr(web.module, "revoke", lambda creds: True)
    request_task = asyncio.create_task(
        client.post("/disconnect", data={"csrf_token": session.csrf_token})
    )
    await entered.wait()
    with pytest.raises(ChatError):
        await web.conversation_manager.submit(session.user_id, "Race")
    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    cleanup_tasks = list(web.app.state.account_cleanup_tasks)
    assert cleanup_tasks
    release.set()
    await asyncio.gather(*cleanup_tasks)
    assert await web.web_store.user(session.user_id) is None
    assert await web.web_store.session(session_token) is None


async def test_restart_invalidation_preserves_browser_sessions_even_without_docker(web, monkeypatch):
    from sqlalchemy import select
    from assistant_agent.database import SandboxAccessToken
    from test_chat import FakeSandbox

    client = await connect(web, monkeypatch, "restart-user", "restart@example.com")
    session_token = client.cookies.get(web.COOKIE)
    session = await web.web_store.session(session_token)
    issued_access = await web.app.state.sandbox_access_service.issue(
        session.user_id, "previous-conversation", "previous-container"
    )
    sandbox_service = FakeSandbox()
    sandbox_service.reconcile.side_effect = RuntimeError("Docker unavailable")
    monkeypatch.setattr(web.module, "Sandbox", lambda: sandbox_service)
    restarted_app = web.module.create_app()
    async with restarted_app.router.lifespan_context(restarted_app):
        assert restarted_app.state.conversation_manager.needs_cleanup
        assert not restarted_app.state.conversation_manager.needs_access_invalidation
        async with restarted_app.state.web_store.factory() as database_session:
            grant = await database_session.scalar(
                select(SandboxAccessToken).where(SandboxAccessToken.id == issued_access.token_id)
            )
            assert grant.revoked_at is not None
        assert await restarted_app.state.web_store.session(session_token) is not None
