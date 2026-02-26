"""add_missing_updated_at_columns

Revision ID: k6l7m8n9o0p1
Revises: j5k6l7m8n9o0
Create Date: 2026-02-19 00:10:00.000000

Backfills legacy schemas that are missing Base.updated_at.
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "k6l7m8n9o0p1"
down_revision: Union[str, Sequence[str], None] = "j5k6l7m8n9o0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ensure_updated_at(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ")
    op.execute(
        f"""
        UPDATE {table}
        SET updated_at = COALESCE(updated_at, created_at, NOW())
        WHERE updated_at IS NULL
        """
    )
    op.execute(f"ALTER TABLE {table} ALTER COLUMN updated_at SET NOT NULL")


def upgrade() -> None:
    """Upgrade schema."""
    _ensure_updated_at("tasks")
    _ensure_updated_at("trials")
    _ensure_updated_at("experiments")


def downgrade() -> None:
    """Downgrade schema.

    Non-reversible safely: on legacy databases we cannot know whether these
    columns predated this migration.
    """
    pass
