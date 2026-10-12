"""Timezone metadata must preserve existing accounts, sessions, and grants."""

from datetime import datetime, timedelta, timezone

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select


def test_timezone_migration_preserves_existing_rows(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'timezone-migration.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    migration_config = Config("alembic.ini")
    command.upgrade(migration_config, "0004")
    database_engine = create_engine(database_url)
    metadata = MetaData()
    users = Table("users", metadata, autoload_with=database_engine)
    sessions = Table("web_sessions", metadata, autoload_with=database_engine)
    grants = Table("sandbox_access_tokens", metadata, autoload_with=database_engine)
    timestamp = datetime.now(timezone.utc)
    with database_engine.begin() as connection:
        connection.execute(users.insert().values(
            user_id="a" * 32, google_sub="sub", email="user@example.com",
            connected_at=timestamp, updated_at=timestamp, credentials=b"grant", scopes="[]",
        ))
        connection.execute(sessions.insert().values(
            id="b" * 32, user_id="a" * 32, session_token_hash="c" * 64,
            csrf_token="csrf", expires_at=timestamp + timedelta(days=1),
        ))
        connection.execute(grants.insert().values(
            id="d" * 32, user_id="a" * 32, token_hash="e" * 64,
            conversation_id="f" * 32, container_id="container", created_at=timestamp,
            expires_at=timestamp + timedelta(hours=1),
        ))
        original_rows = [connection.execute(select(table)).all() for table in (users, sessions, grants)]
    for _ in range(2):
        command.upgrade(migration_config, "0005")
        timezone_column = next(
            column for column in inspect(database_engine).get_columns("users")
            if column["name"] == "timezone"
        )
        assert timezone_column["nullable"]
        assert timezone_column["type"].length == 64
        current_users = Table("users", MetaData(), autoload_with=database_engine)
        with database_engine.begin() as connection:
            assert connection.execute(select(current_users.c.timezone)).scalar_one() is None
            assert [connection.execute(select(table)).all() for table in (users, sessions, grants)] == original_rows
            connection.execute(current_users.update().values(timezone="America/New_York"))
        command.downgrade(migration_config, "0004")
        assert "timezone" not in {column["name"] for column in inspect(database_engine).get_columns("users")}
        with database_engine.connect() as connection:
            assert [connection.execute(select(table)).all() for table in (users, sessions, grants)] == original_rows
    command.upgrade(migration_config, "0005")
    database_engine.dispose()
