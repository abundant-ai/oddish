"""add_experiments_table

Revision ID: 9c1d2e3f4a5b
Revises: 8b9c0d1e2f3a
Create Date: 2026-01-23 12:00:00.000000

Adds experiments table with org_id for multi-tenancy support.
Also adds org_id to trials table for efficient org-scoped queries.
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "9c1d2e3f4a5b"
down_revision: Union[str, Sequence[str], None] = "8b9c0d1e2f3a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # =========================================================================
    # 1. Create experiments table with org_id (or add org_id if table exists)
    # =========================================================================
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS experiments (
            id VARCHAR(64) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            org_id VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )
        """
    )

    # Add org_id if table exists but column doesn't
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

    # Unique constraint on (org_id, name) instead of just name
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_experiments_org_name "
        "ON experiments (org_id, name)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_experiments_org_id " "ON experiments (org_id)"
    )

    # =========================================================================
    # 2. Add experiment_id to tasks
    # =========================================================================
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS experiment_id VARCHAR(64)")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_experiment_id_fkey")
    op.execute(
        "ALTER TABLE tasks "
        "ADD CONSTRAINT tasks_experiment_id_fkey "
        "FOREIGN KEY (experiment_id) REFERENCES experiments(id) ON DELETE RESTRICT"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_experiment_id ON tasks (experiment_id)"
    )
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS experiment_name")

    # =========================================================================
    # 3. Add org_id to trials (for efficient org-scoped queue stats)
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


def downgrade() -> None:
    """Downgrade schema."""
    # Drop trials indexes and column
    op.execute("DROP INDEX IF EXISTS idx_trials_org_provider_status")
    op.execute("DROP INDEX IF EXISTS idx_trials_org_id")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS org_id")

    # Drop tasks FK and column
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_experiment_id_fkey")
    op.execute("DROP INDEX IF EXISTS idx_tasks_experiment_id")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS experiment_id")
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS experiment_name VARCHAR(255)"
    )

    # Drop experiments table
    op.execute("DROP INDEX IF EXISTS idx_experiments_org_id")
    op.execute("DROP INDEX IF EXISTS idx_experiments_org_name")
    op.execute("DROP TABLE IF EXISTS experiments")
