"""Sandbox grants must preserve browser sessions and enforce ownership constraints."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from assistant_agent.database import SandboxAccessToken, User, WebSession


def make_migrated_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Engine, Config]:
    database_url = f"sqlite:///{tmp_path / 'sandbox-access.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    migration_config = Config("alembic.ini")
    command.upgrade(migration_config, "0003")
    database_engine = create_engine(database_url)

    @event.listens_for(database_engine, "connect")
    def enable_foreign_keys(database_connection, connection_record) -> None:
        database_connection.execute("PRAGMA foreign_keys=ON")

    return database_engine, migration_config


def test_sandbox_access_migration_preserves_accounts_and_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_engine, migration_config = make_migrated_engine(tmp_path, monkeypatch)
    timestamp = datetime.now(timezone.utc)
    with database_engine.begin() as database_connection:
        database_connection.execute(User.__table__.insert().values(
            user_id="a" * 32, google_sub="google-sub", email="user@example.com",
            credentials=b"encrypted-credentials", scopes="[]",
        ))
        database_connection.execute(WebSession.__table__.insert().values(
            id="b" * 32, session_token_hash="c" * 64, user_id="a" * 32,
            csrf_token="csrf", expires_at=timestamp,
        ))
        original_users = database_connection.execute(select(User.__table__)).all()
        original_sessions = database_connection.execute(select(WebSession.__table__)).all()

    command.upgrade(migration_config, "head")
    schema_inspector = inspect(database_engine)
    assert schema_inspector.get_pk_constraint("sandbox_access_tokens")["constrained_columns"] == ["id"]
    token_columns = {
        column["name"]: column
        for column in schema_inspector.get_columns("sandbox_access_tokens")
    }
    assert set(token_columns) == {
        "id", "token_hash", "user_id", "conversation_id", "container_id",
        "created_at", "expires_at", "revoked_at",
    }
    assert token_columns["revoked_at"]["nullable"]
    assert all(not column["nullable"] for name, column in token_columns.items() if name != "revoked_at")
    token_indexes = {
        index["name"]: index for index in schema_inspector.get_indexes("sandbox_access_tokens")
    }
    for indexed_field in ("user_id", "conversation_id", "token_hash"):
        assert token_indexes[f"ix_sandbox_access_tokens_{indexed_field}"]["column_names"] == [indexed_field]
    assert token_indexes["ix_sandbox_access_tokens_token_hash"]["unique"]
    token_foreign_key = schema_inspector.get_foreign_keys("sandbox_access_tokens")[0]
    assert token_foreign_key["constrained_columns"] == ["user_id"]
    assert token_foreign_key["referred_columns"] == ["user_id"]
    assert token_foreign_key["referred_table"] == "users"
    assert token_foreign_key["options"]["ondelete"] == "CASCADE"

    token_values = {
        "token_hash": "d" * 64, "user_id": "a" * 32, "conversation_id": "e" * 32,
        "container_id": "sandbox-container", "expires_at": timestamp + timedelta(hours=24),
    }
    with Session(database_engine) as database_session:
        sandbox_token = SandboxAccessToken(**token_values)
        database_session.add(sandbox_token)
        database_session.commit()
        assert len(sandbox_token.id) == 32
        assert int(sandbox_token.id, 16) >= 0
        assert sandbox_token.created_at is not None
        assert sandbox_token.revoked_at is None
    with pytest.raises(IntegrityError), database_engine.begin() as database_connection:
        database_connection.execute(SandboxAccessToken.__table__.insert().values(**token_values))
    with pytest.raises(IntegrityError), database_engine.begin() as database_connection:
        database_connection.execute(SandboxAccessToken.__table__.insert().values(
            **{**token_values, "token_hash": "f" * 64, "user_id": "missing-user"}
        ))

    command.downgrade(migration_config, "0003")
    assert "sandbox_access_tokens" not in inspect(database_engine).get_table_names()
    with database_engine.connect() as database_connection:
        assert database_connection.execute(select(User.__table__)).all() == original_users
        assert database_connection.execute(select(WebSession.__table__)).all() == original_sessions
    command.upgrade(migration_config, "head")
    assert "sandbox_access_tokens" in inspect(database_engine).get_table_names()
    database_engine.dispose()


def test_user_deletion_cascades_sandbox_grants_and_preserves_browser_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_engine, migration_config = make_migrated_engine(tmp_path, monkeypatch)
    command.upgrade(migration_config, "head")
    timestamp = datetime.now(timezone.utc)
    with database_engine.begin() as database_connection:
        database_connection.execute(User.__table__.insert().values(
            user_id="a" * 32, google_sub="google-sub", email="user@example.com",
            credentials=b"encrypted-credentials", scopes="[]",
        ))
        database_connection.execute(WebSession.__table__.insert().values(
            session_token_hash="b" * 64, user_id="a" * 32, csrf_token="csrf", expires_at=timestamp,
        ))
        database_connection.execute(SandboxAccessToken.__table__.insert().values(
            token_hash="c" * 64, user_id="a" * 32, conversation_id="d" * 32,
            container_id="sandbox-container", expires_at=timestamp + timedelta(hours=24),
        ))
        database_connection.execute(User.__table__.delete().where(User.user_id == "a" * 32))
        assert database_connection.execute(select(SandboxAccessToken.__table__)).all() == []
        assert database_connection.execute(select(WebSession.user_id)).scalar_one() is None
    database_engine.dispose()
