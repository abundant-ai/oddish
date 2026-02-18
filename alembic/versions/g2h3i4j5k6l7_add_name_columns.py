"""add_name_columns

Add `name` column to tasks and trials tables for human-readable names.
- Tasks: name is the task's display name (without UUID suffix)
- Trials: name is "{task_name}-{index}" (e.g., "my-task-0")

Backfills existing records by copying the id to name.

Revision ID: g2h3i4j5k6l7
Revises: c0a1b2c3d4e5
Create Date: 2026-02-01 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "g2h3i4j5k6l7"
down_revision: Union[str, Sequence[str], None] = "c0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add name column to tasks (nullable first for backfill)
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS name VARCHAR(255)")
    # Backfill: copy id to name for existing tasks
    op.execute("UPDATE tasks SET name = id WHERE name IS NULL")
    # Set NOT NULL constraint
    op.execute("ALTER TABLE tasks ALTER COLUMN name SET NOT NULL")

    # Add name column to trials (nullable first for backfill)
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS name VARCHAR(255)")
    # Backfill: copy id to name for existing trials
    op.execute("UPDATE trials SET name = id WHERE name IS NULL")
    # Set NOT NULL constraint
    op.execute("ALTER TABLE trials ALTER COLUMN name SET NOT NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS name")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS name")
