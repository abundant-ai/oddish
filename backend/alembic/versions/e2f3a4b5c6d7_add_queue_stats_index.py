"""add_org_id_to_experiments_and_trials

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-01-23 10:00:00.000000

Adds org_id to experiments and trials tables for efficient org-scoped queries.
This denormalization eliminates expensive JOINs in dashboard queries.

Changes:
- experiments: add org_id column, change unique constraint from (name) to (org_id, name)
- trials: add org_id column, add composite index for queue stats
- Backfills org_id from tasks table
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add org_id to experiments and trials, backfill from tasks."""

    # =========================================================================
    # 1. Add org_id column to experiments
    # =========================================================================
    op.execute("ALTER TABLE experiments ADD COLUMN IF NOT EXISTS org_id VARCHAR(64)")

    # Backfill experiments.org_id from the first task's org_id
    op.execute(
        """
        UPDATE experiments e
        SET org_id = (
            SELECT t.org_id
            FROM tasks t
            WHERE t.experiment_id = e.id AND t.org_id IS NOT NULL
            LIMIT 1
        )
        WHERE e.org_id IS NULL
        """
    )

    # Drop old unique constraint on name (if exists)
    op.execute("DROP INDEX IF EXISTS ix_experiments_name")
    op.execute("ALTER TABLE experiments DROP CONSTRAINT IF EXISTS experiments_name_key")

    # Create new unique index on (org_id, name)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_experiments_org_name "
        "ON experiments (org_id, name)"
    )

    # Index for org_id lookups
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_experiments_org_id " "ON experiments (org_id)"
    )

    # =========================================================================
    # 2. Add org_id column to trials
    # =========================================================================
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS org_id VARCHAR(64)")

    # Backfill trials.org_id from tasks.org_id
    op.execute(
        """
        UPDATE trials tr
        SET org_id = (
            SELECT t.org_id
            FROM tasks t
            WHERE t.id = tr.task_id
        )
        WHERE tr.org_id IS NULL
        """
    )

    # Index for org_id lookups
    op.execute("CREATE INDEX IF NOT EXISTS idx_trials_org_id " "ON trials (org_id)")

    # Composite index for efficient queue stats (eliminates JOIN)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trials_org_provider_status "
        "ON trials (org_id, provider, status)"
    )

    # Drop the simpler provider_status index if it exists (superseded by above)
    op.execute("DROP INDEX IF EXISTS idx_trials_provider_status")


def downgrade() -> None:
    """Remove org_id from experiments and trials."""

    # Drop indexes
    op.execute("DROP INDEX IF EXISTS idx_trials_org_provider_status")
    op.execute("DROP INDEX IF EXISTS idx_trials_org_id")
    op.execute("DROP INDEX IF EXISTS idx_experiments_org_name")
    op.execute("DROP INDEX IF EXISTS idx_experiments_org_id")

    # Restore old unique constraint on experiments.name
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS experiments_name_key ON experiments (name)"
    )

    # Drop columns
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS org_id")
    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS org_id")
