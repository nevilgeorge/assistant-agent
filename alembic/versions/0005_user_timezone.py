"""Persist the user's primary Calendar timezone.

Revision ID: 0005
Revises: 0004
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("timezone", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "timezone")
