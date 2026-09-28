"""Session IDs must preserve active sessions and retain unique token lookups."""
from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from assistant_agent.database import WebSession


def test_session_id_migration_preserves_sessions_and_constraints(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'sessions.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0002")
    engine = create_engine(url)
    timestamp = datetime.now(timezone.utc)
    with engine.begin() as db:
        db.execute(text("INSERT INTO users (user_id, google_sub, email, connected_at, updated_at, credentials, scopes) VALUES (:id, 'google-sub', 'user@example.com', :date, :date, :credentials, '[]')"), {"id": "a" * 32, "date": timestamp, "credentials": b"grant"})
        for token_hash, user_id in [("b" * 64, "a" * 32), ("c" * 64, None)]:
            db.execute(text("INSERT INTO web_sessions (id_hash, user_id, csrf_token, oauth_state, oauth_nonce, oauth_verifier, expires_at) VALUES (:hash, :user, 'csrf', 'state', 'nonce', 'verifier', :date)"), {"hash": token_hash, "user": user_id, "date": timestamp})

    command.upgrade(config, "head")
    schema = inspect(engine)
    assert schema.get_pk_constraint("web_sessions")["constrained_columns"] == ["id"]
    assert "id_hash" not in {column["name"] for column in schema.get_columns("web_sessions")}
    indexes = {index["name"]: index for index in schema.get_indexes("web_sessions")}
    assert indexes["ix_web_sessions_session_token_hash"]["column_names"] == ["session_token_hash"]
    assert indexes["ix_web_sessions_session_token_hash"]["unique"]
    assert "ix_web_sessions_expires_at" in indexes
    assert schema.get_foreign_keys("web_sessions")[0]["referred_table"] == "users"

    with engine.connect() as db:
        rows = db.execute(select(WebSession.__table__).order_by(WebSession.session_token_hash)).mappings().all()
    assert len(rows) == 2
    assert len({row["id"] for row in rows}) == 2
    for row in rows:
        assert len(row["id"]) == 32
        assert row["csrf_token"] == "csrf"
        assert row["oauth_state"] == "state"
        assert row["oauth_nonce"] == "nonce"
        assert row["oauth_verifier"] == "verifier"
        assert row["expires_at"].replace(tzinfo=timezone.utc) == timestamp
    assert rows[0]["user_id"] == "a" * 32
    assert rows[1]["user_id"] is None
    with pytest.raises(IntegrityError), engine.begin() as db:
        db.execute(WebSession.__table__.insert().values(**{**rows[0], "id": "d" * 32}))

    command.downgrade(config, "0002")
    schema = inspect(engine)
    assert schema.get_pk_constraint("web_sessions")["constrained_columns"] == ["id_hash"]
    assert {column["name"] for column in schema.get_columns("web_sessions")} == {
        "id_hash", "user_id", "csrf_token", "oauth_state", "oauth_nonce", "oauth_verifier", "expires_at",
    }
    with engine.connect() as db:
        restored = db.execute(text("SELECT id_hash, user_id, csrf_token, oauth_state, oauth_nonce, oauth_verifier, expires_at FROM web_sessions ORDER BY id_hash")).mappings().all()
    assert [row["id_hash"] for row in restored] == ["b" * 64, "c" * 64]
    for old, new in zip(restored, rows):
        for key in ("user_id", "csrf_token", "oauth_state", "oauth_nonce", "oauth_verifier"):
            assert old[key] == new[key]

    command.upgrade(config, "head")
    assert inspect(engine).get_pk_constraint("web_sessions")["constrained_columns"] == ["id"]
