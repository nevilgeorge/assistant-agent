"""Create private web users and sessions.

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("sub", sa.String(255), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("credentials", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", sa.String(2048), nullable=False),
    )
    op.create_table(
        "web_sessions",
        sa.Column("id_hash", sa.String(64), primary_key=True),
        sa.Column("user_sub", sa.String(255), sa.ForeignKey("users.sub", ondelete="SET NULL"), nullable=True),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("oauth_state", sa.String(255)),
        sa.Column("oauth_nonce", sa.String(255)),
        sa.Column("oauth_verifier", sa.String(255)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_web_sessions_expires_at", "web_sessions", ["expires_at"])


def downgrade():
    op.drop_index("ix_web_sessions_expires_at", table_name="web_sessions")
    op.drop_table("web_sessions")
    op.drop_table("users")
