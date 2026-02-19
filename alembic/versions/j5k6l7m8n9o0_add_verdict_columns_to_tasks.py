"""add_verdict_columns_to_tasks

Revision ID: j5k6l7m8n9o0
Revises: i4j5k6l7m8n9
Create Date: 2026-02-19 00:00:00.000000

Adds task-level verdict columns used by the analysis/verdict pipeline.
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "j5k6l7m8n9o0"
down_revision: Union[str, Sequence[str], None] = "i4j5k6l7m8n9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS verdict JSONB")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS verdict_status jobstatus")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS verdict_error TEXT")
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS verdict_started_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS verdict_finished_at TIMESTAMPTZ"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS verdict_finished_at")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS verdict_started_at")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS verdict_error")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS verdict_status")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS verdict")
