"""Add conversation-bound sandbox access token grants.

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sandbox_access_tokens",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("conversation_id", sa.String(32), nullable=False),
        sa.Column("container_id", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sandbox_access_tokens_token_hash", "sandbox_access_tokens", ["token_hash"], unique=True
    )
    op.create_index("ix_sandbox_access_tokens_user_id", "sandbox_access_tokens", ["user_id"])
    op.create_index(
        "ix_sandbox_access_tokens_conversation_id", "sandbox_access_tokens", ["conversation_id"]
    )


def downgrade() -> None:
    op.drop_table("sandbox_access_tokens")
