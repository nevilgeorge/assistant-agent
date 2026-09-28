"""Use an internal user ID while retaining Google's subject as a unique identity.

Revision ID: 0002
Revises: 0001
"""
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    users = op.create_table(
        "users_v2",
        sa.Column("user_id", sa.String(32), primary_key=True),
        sa.Column("google_sub", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("credentials", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", sa.String(2048), nullable=False),
        sa.UniqueConstraint("google_sub", name="uq_users_google_sub"),
    )
    sessions = op.create_table(
        "web_sessions_v2",
        sa.Column("id_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users_v2.user_id", ondelete="SET NULL")),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("oauth_state", sa.String(255)),
        sa.Column("oauth_nonce", sa.String(255)),
        sa.Column("oauth_verifier", sa.String(255)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    bind = op.get_bind()
    prior_users = sa.Table("users", sa.MetaData(), autoload_with=bind)
    prior_sessions = sa.Table("web_sessions", sa.MetaData(), autoload_with=bind)
    ids = {}
    for old in bind.execute(sa.select(prior_users)).mappings():
        user_id = uuid4().hex
        ids[old["sub"]] = user_id
        bind.execute(users.insert().values(
            user_id=user_id, google_sub=old["sub"], email=old["email"],
            connected_at=old["connected_at"], updated_at=old["updated_at"],
            credentials=old["credentials"], scopes=old["scopes"],
        ))
    for old in bind.execute(sa.select(prior_sessions)).mappings():
        bind.execute(sessions.insert().values(
            id_hash=old["id_hash"], user_id=ids.get(old["user_sub"]),
            csrf_token=old["csrf_token"], oauth_state=old["oauth_state"],
            oauth_nonce=old["oauth_nonce"], oauth_verifier=old["oauth_verifier"],
            expires_at=old["expires_at"],
        ))
    op.drop_index("ix_web_sessions_expires_at", table_name="web_sessions")
    op.drop_table("web_sessions")
    op.drop_table("users")
    op.rename_table("users_v2", "users")
    op.rename_table("web_sessions_v2", "web_sessions")
    op.create_index("ix_web_sessions_expires_at", "web_sessions", ["expires_at"])


def downgrade():
    users = op.create_table(
        "users_old",
        sa.Column("sub", sa.String(255), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("credentials", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", sa.String(2048), nullable=False),
    )
    sessions = op.create_table(
        "web_sessions_old",
        sa.Column("id_hash", sa.String(64), primary_key=True),
        sa.Column("user_sub", sa.String(255), sa.ForeignKey("users_old.sub", ondelete="SET NULL")),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("oauth_state", sa.String(255)),
        sa.Column("oauth_nonce", sa.String(255)),
        sa.Column("oauth_verifier", sa.String(255)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    bind = op.get_bind()
    current_users = sa.Table("users", sa.MetaData(), autoload_with=bind)
    current_sessions = sa.Table("web_sessions", sa.MetaData(), autoload_with=bind)
    subs = {}
    for current in bind.execute(sa.select(current_users)).mappings():
        subs[current["user_id"]] = current["google_sub"]
        bind.execute(users.insert().values(
            sub=current["google_sub"], email=current["email"],
            connected_at=current["connected_at"], updated_at=current["updated_at"],
            credentials=current["credentials"], scopes=current["scopes"],
        ))
    for current in bind.execute(sa.select(current_sessions)).mappings():
        bind.execute(sessions.insert().values(
            id_hash=current["id_hash"], user_sub=subs.get(current["user_id"]),
            csrf_token=current["csrf_token"], oauth_state=current["oauth_state"],
            oauth_nonce=current["oauth_nonce"], oauth_verifier=current["oauth_verifier"],
            expires_at=current["expires_at"],
        ))
    op.drop_index("ix_web_sessions_expires_at", table_name="web_sessions")
    op.drop_table("web_sessions")
    op.drop_table("users")
    op.rename_table("users_old", "users")
    op.rename_table("web_sessions_old", "web_sessions")
    op.create_index("ix_web_sessions_expires_at", "web_sessions", ["expires_at"])
