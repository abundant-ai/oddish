"""add experiments.owner_user_id

Revision ID: experiments_owner_001
Revises: documents_001
Create Date: 2026-06-10 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "experiments_owner_001"
down_revision: Union[str, Sequence[str], None] = "documents_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE experiments ADD COLUMN IF NOT EXISTS owner_user_id VARCHAR(64)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_experiments_org_owner_user_live "
        "ON experiments (org_id, owner_user_id) "
        "WHERE deleted_at IS NULL"
    )
    # Best-effort backfill from the oldest live task on each experiment.
    op.execute(
        """
        UPDATE experiments e
        SET owner_user_id = sub.owner_user_id
        FROM (
            SELECT DISTINCT ON (te.experiment_id)
                   te.experiment_id,
                   t.created_by_user_id AS owner_user_id
            FROM task_experiments te
            JOIN tasks t ON t.id = te.task_id
            WHERE te.deleted_at IS NULL
              AND t.deleted_at IS NULL
              AND t.created_by_user_id IS NOT NULL
            ORDER BY te.experiment_id, t.created_at ASC, t.id ASC
        ) sub
        WHERE e.id = sub.experiment_id
          AND e.owner_user_id IS NULL
          AND e.deleted_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_experiments_org_owner_user_live")
    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS owner_user_id")
