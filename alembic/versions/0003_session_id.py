"""Give web sessions an internal ID and index their token hashes.

Revision ID: 0003
Revises: 0002
"""
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# Give SQLite's unnamed primary key a name for batch constraint operations.
NAMING_CONVENTION = {"pk": "pk_%(table_name)s"}


def primary_key_name():
    return sa.inspect(op.get_bind()).get_pk_constraint("web_sessions")["name"] or "pk_web_sessions"


def upgrade():
    op.add_column("web_sessions", sa.Column("id", sa.String(32), nullable=True))
    bind = op.get_bind()
    sessions = sa.Table("web_sessions", sa.MetaData(), autoload_with=bind)
    for token_hash in bind.execute(sa.select(sessions.c.id_hash)).scalars():
        bind.execute(sessions.update().where(sessions.c.id_hash == token_hash).values(id=uuid4().hex))
    with op.batch_alter_table("web_sessions", naming_convention=NAMING_CONVENTION) as batch:
        batch.drop_constraint(primary_key_name(), type_="primary")
        batch.alter_column("id_hash", new_column_name="session_token_hash", existing_type=sa.String(64), existing_nullable=False)
        batch.alter_column("id", existing_type=sa.String(32), nullable=False)
        batch.create_primary_key("pk_web_sessions", ["id"])
    op.create_index("ix_web_sessions_session_token_hash", "web_sessions", ["session_token_hash"], unique=True)


def downgrade():
    op.drop_index("ix_web_sessions_session_token_hash", table_name="web_sessions")
    with op.batch_alter_table("web_sessions", naming_convention=NAMING_CONVENTION) as batch:
        batch.drop_constraint(primary_key_name(), type_="primary")
        batch.alter_column("session_token_hash", new_column_name="id_hash", existing_type=sa.String(64), existing_nullable=False)
        batch.drop_column("id")
    # Reflect the renamed column before restoring its primary key in SQLite.
    with op.batch_alter_table("web_sessions", naming_convention=NAMING_CONVENTION) as batch:
        batch.create_primary_key("pk_web_sessions", ["id_hash"])
