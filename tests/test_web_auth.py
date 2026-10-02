import importlib
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from google.oauth2.credentials import Credentials

from assistant_agent.database import WebSession, utcnow
from assistant_agent.sandbox import ExecResult, SandboxError


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'web.db'}")
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("APP_ENV", "development")
    import assistant_agent.config as config
    config.get_settings.cache_clear()
    module = importlib.import_module("assistant_agent.web")
    module = importlib.reload(module)
    from assistant_agent.database import Base
    Base.metadata.create_all(module.store.factory.kw['bind'])
    yield module
    config.get_settings.cache_clear()


def credentials():
    return Credentials(token="access", refresh_token="refresh", token_uri="https://oauth2.googleapis.com/token", client_id="client-id", client_secret="client-secret", scopes=["openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar.readonly"])


def connect(web, monkeypatch, sub, email):
    monkeypatch.setattr(web, "authorization_url", lambda nonce: (f"https://accounts.google.com/?state=state-{sub}", f"state-{sub}", "verifier"))
    monkeypatch.setattr(web, "exchange_code", lambda *args, **kwargs: credentials())
    monkeypatch.setattr(web, "account_identity", lambda creds, expected_nonce: (email, sub))
    client = TestClient(web.app)
    start = client.get("/auth/google/start", follow_redirects=False)
    assert start.status_code == 302
    callback = client.get(f"/auth/google/callback?state=state-{sub}&code=code", follow_redirects=False)
    assert callback.status_code == 303
    return client


def test_user_isolation_and_csrf(web, monkeypatch):
    a = connect(web, monkeypatch, "sub-a", "a@example.com")
    b = connect(web, monkeypatch, "sub-b", "b@example.com")
    monkeypatch.setattr(web.store, "load_refreshed", lambda user_id: credentials())
    page = a.get("/").text
    assert "a@example.com" in page and "b@example.com" not in page
    assert a.post("/disconnect", data={"csrf_token": "wrong"}).status_code == 400
    assert web.store.user_by_google_sub("sub-a") is not None
    assert b.post("/disconnect", data={"csrf_token": "wrong"}).status_code == 400
    assert web.store.user_by_google_sub("sub-a") is not None
    assert TestClient(web.app).post("/disconnect", data={}).status_code == 401
    session = web.store.session(a.cookies.get(web.COOKIE))
    monkeypatch.setattr(web, "revoke", lambda creds: True)
    assert a.post("/disconnect", data={"csrf_token": session.csrf_token}, follow_redirects=False).status_code == 303
    assert web.store.user_by_google_sub("sub-a") is None and web.store.user_by_google_sub("sub-b") is not None


def test_state_consumed_and_nonce_passed(web, monkeypatch):
    monkeypatch.setattr(web, "authorization_url", lambda nonce: ("https://google.test", "expected", "verifier"))
    client = TestClient(web.app)
    client.get("/auth/google/start", follow_redirects=False)
    assert client.get("/auth/google/callback?state=wrong&code=code").status_code == 400
    assert client.get("/auth/google/callback?state=expected&code=code").status_code == 400
    client.get("/auth/google/start", follow_redirects=False)
    observed = {}
    monkeypatch.setattr(web, "exchange_code", lambda *args, **kwargs: credentials())
    def identity(creds, expected_nonce):
        observed["nonce"] = expected_nonce
        return "a@example.com", "sub-a"
    monkeypatch.setattr(web, "account_identity", identity)
    assert client.get("/auth/google/callback?state=expected&code=code", follow_redirects=False).status_code == 303
    assert observed["nonce"]


def test_session_expiry_and_encrypted_credentials(web, monkeypatch):
    client = connect(web, monkeypatch, "sub-a", "a@example.com")
    user = web.store.user_by_google_sub("sub-a")
    assert len(user.user_id) == 32 and user.user_id != user.google_sub
    assert b"refresh" not in user.credentials
    assert web.store.load_credentials(user.user_id).refresh_token == "refresh"
    token = client.cookies.get(web.COOKIE)
    from assistant_agent.web_store import WebStore
    reloaded = WebStore(factory=web.store.factory, key=web.settings.credential_encryption_key)
    assert reloaded.session(token).user_id == user.user_id
    assert reloaded.load_credentials(user.user_id).refresh_token == "refresh"
    with web.store.factory.begin() as db:
        row = db.get(WebSession, web.store.session(token).id)
        row.expires_at = utcnow() - timedelta(seconds=1)
    assert web.store.session(token) is None
    assert "a@example.com" not in client.get("/").text


def test_refresh_persists_new_token(web, monkeypatch):
    user_id = web.store.save_credentials("sub-a", "a@example.com", credentials())
    from google.oauth2.credentials import Credentials as GoogleCredentials
    monkeypatch.setattr(GoogleCredentials, "valid", property(lambda self: False))
    def refresh(self, request):
        self.token = "new-access"
    monkeypatch.setattr(GoogleCredentials, "refresh", refresh)
    assert web.store.load_refreshed(user_id).token == "new-access"
    assert web.store.load_credentials(user_id).token == "new-access"


def test_reconnect_preserves_internal_user_id(web):
    user_id = web.store.save_credentials("stable-google-sub", "old@example.com", credentials())
    again = web.store.save_credentials("stable-google-sub", "new@example.com", credentials())
    assert again == user_id
    assert web.store.user(user_id).email == "new@example.com"
    assert web.store.user_by_google_sub("stable-google-sub").user_id == user_id


def test_session_id_and_token_rotation(web):
    from assistant_agent.web_store import token_hash

    old_token, original = web.store.new_session()
    assert len(original.id) == 32
    assert original.session_token_hash == token_hash(old_token)
    assert original.id != original.session_token_hash
    original.oauth_state = "saved-state"
    web.store.save_session(original)
    assert web.store.session(old_token).oauth_state == "saved-state"

    user_id = web.store.save_credentials("sub-a", "a@example.com", credentials())
    new_token, rotated = web.store.rotate(old_token, user_id)
    assert rotated.id != original.id
    assert rotated.session_token_hash == token_hash(new_token)
    assert web.store.session(old_token) is None
    assert web.store.session(new_token).user_id == user_id
    with web.store.factory() as db:
        assert db.get(WebSession, original.id) is None
        assert db.get(WebSession, rotated.id).session_token_hash == token_hash(new_token)


def test_verified_identity_and_nonce(web, monkeypatch):
    import assistant_agent.google_oauth as oauth
    monkeypatch.setattr(oauth.id_token, "verify_oauth2_token", lambda *args, **kwargs: {"email": "a@example.com", "email_verified": True, "sub": "stable-sub", "nonce": "expected"})
    class Token:
        id_token = "signed-id-token"
    assert oauth.account_identity(Token(), expected_nonce="expected") == ("a@example.com", "stable-sub")
    with pytest.raises(ValueError, match="nonce"):
        oauth.account_identity(Token(), expected_nonce="wrong")
    monkeypatch.setattr(oauth.id_token, "verify_oauth2_token", lambda *args, **kwargs: {"email": "a@example.com", "email_verified": False, "sub": "stable-sub", "nonce": "expected"})
    with pytest.raises(ValueError, match="not verified"):
        oauth.account_identity(Token(), expected_nonce="expected")


def test_message_requires_auth_and_csrf_and_returns_sandbox_response(web, monkeypatch):
    calls = []
    monkeypatch.setattr(web.sandbox, "ask", lambda prompt: calls.append(prompt) or ExecResult(0, "Hello!\n"))
    anonymous = TestClient(web.app)
    assert anonymous.post("/api/message", json={"message": "Hi"}).status_code == 401
    client = connect(web, monkeypatch, "sub-a", "a@example.com")
    assert client.post("/api/message", json={"message": "Hi"}).status_code == 400
    csrf = web.store.session(client.cookies.get(web.COOKIE)).csrf_token
    headers = {"X-CSRF-Token": csrf}
    assert client.post("/api/message", json={"message": "  "}, headers=headers).status_code == 400
    assert client.post("/api/message", json={"message": "x" * 4001}, headers=headers).status_code == 422
    response = client.post("/api/message", json={"message": "Hi"}, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"response": "Hello!"}
    assert calls == ["Hi"]


def test_message_reports_sandbox_failures_without_exposing_details(web, monkeypatch):
    client = connect(web, monkeypatch, "sub-a", "a@example.com")
    headers = {"X-CSRF-Token": web.store.session(client.cookies.get(web.COOKIE)).csrf_token}
    monkeypatch.setattr(web.sandbox, "ask", lambda prompt: ExecResult(124, "private output"))
    assert client.post("/api/message", json={"message": "Hi"}, headers=headers).status_code == 504
    monkeypatch.setattr(web.sandbox, "ask", lambda prompt: ExecResult(1, "private output"))
    failed = client.post("/api/message", json={"message": "Hi"}, headers=headers)
    assert failed.status_code == 502 and "private output" not in failed.text
    def unavailable(prompt):
        raise SandboxError("private daemon detail")
    monkeypatch.setattr(web.sandbox, "ask", unavailable)
    failed = client.post("/api/message", json={"message": "Hi"}, headers=headers)
    assert failed.status_code == 503 and "private daemon detail" not in failed.text
