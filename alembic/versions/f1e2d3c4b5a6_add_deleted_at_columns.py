"""add_deleted_at_columns

Revision ID: f1e2d3c4b5a6
Revises: c3d4e5f6a7b8
Create Date: 2026-01-26 09:30:00.000000

Adds deleted_at columns to core tables for soft deletes.
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f1e2d3c4b5a6"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "ALTER TABLE experiments ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ"
    )
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS deleted_at")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS deleted_at")
    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS deleted_at")
