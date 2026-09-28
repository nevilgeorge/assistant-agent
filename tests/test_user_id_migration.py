"""The schema change must retain existing Google grants and signed-in sessions."""
from datetime import datetime, timezone

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text


def test_existing_accounts_and_sessions_survive_user_id_migration(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0001")
    engine = create_engine(url)
    timestamp = datetime.now(timezone.utc)
    with engine.begin() as db:
        db.execute(text("INSERT INTO users (sub, email, connected_at, updated_at, credentials, scopes) VALUES (:sub, :email, :date, :date, :credentials, :scopes)"), {"sub": "google-123", "email": "user@example.com", "date": timestamp, "credentials": b"encrypted-grant", "scopes": '["openid"]'})
        db.execute(text("INSERT INTO web_sessions (id_hash, user_sub, csrf_token, expires_at) VALUES (:hash, :sub, :csrf, :date)"), {"hash": "session-hash", "sub": "google-123", "csrf": "csrf", "date": timestamp})
    command.upgrade(config, "head")
    with engine.connect() as db:
        user = db.execute(text("SELECT user_id, google_sub, email, credentials FROM users")).mappings().one()
        session = db.execute(text("SELECT user_id FROM web_sessions WHERE session_token_hash = 'session-hash'")).scalar_one()
        assert len(user["user_id"]) == 32
        assert user["google_sub"] == "google-123"
        assert user["email"] == "user@example.com"
        assert user["credentials"] == b"encrypted-grant"
        assert session == user["user_id"]
    command.downgrade(config, "0001")
    with engine.connect() as db:
        assert db.execute(text("SELECT sub FROM users")).scalar_one() == "google-123"
        assert db.execute(text("SELECT user_sub FROM web_sessions")).scalar_one() == "google-123"
