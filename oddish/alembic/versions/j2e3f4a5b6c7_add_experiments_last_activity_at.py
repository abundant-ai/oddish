"""add_experiments_last_activity_at_column_and_backfill

Revision ID: j2e3f4a5b6c7
Revises: i1d2e3f4a5b6
Create Date: 2026-05-14 16:45:00.000000

Adds a denormalized ``experiments.last_activity_at`` column that powers
the dashboard "recent experiments" sort. Previously the sort key was
derived from ``GREATEST(MAX(tasks.created_at), MAX(trials.created_at))``
per experiment, forcing aggregation across the org's full task and
trial set before the outer ``ORDER BY ... LIMIT`` even though the page
size is small.

The column is backfilled in a single ``UPDATE`` statement using lateral
``MAX`` subqueries against the existing ``task_experiments``/``trials``
indexes. Application write paths (``create_task`` / append-trials /
trial state transitions) maintain it best-effort going forward; a
reconciliation pass in the cleanup sweep repairs any drift.

The supporting partial index lives in the model definition and is
created here as well so the column is queryable immediately after
the migration.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "j2e3f4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "i1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Guarded so this is a no-op on a fresh DB, where 000_initial_schema's
    # ``Base.metadata.create_all`` already created the column from the live
    # model. Matches the IF NOT EXISTS pattern used by the other migrations.
    op.execute(
        "ALTER TABLE experiments "
        "ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMP WITH TIME ZONE"
    )

    # One-shot backfill. Greatest-of(latest task created_at via the
    # task_experiments junction, latest trial created_at). Both
    # subqueries skip soft-deleted rows so freshly-deleted history
    # doesn't drag the sort key forward.
    op.execute(
        """
        UPDATE experiments e
        SET last_activity_at = GREATEST(
            (
                SELECT MAX(t.created_at)
                FROM task_experiments te
                JOIN tasks t ON t.id = te.task_id
                WHERE te.experiment_id = e.id
                  AND t.deleted_at IS NULL
            ),
            (
                SELECT MAX(tr.created_at)
                FROM trials tr
                WHERE tr.experiment_id = e.id
                  AND tr.deleted_at IS NULL
                  AND tr.superseded_by_trial_id IS NULL
            )
        )
        WHERE e.deleted_at IS NULL
        """
    )

    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_experiments_org_last_activity_live
            ON experiments (org_id, last_activity_at DESC NULLS LAST)
            WHERE deleted_at IS NULL
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS idx_experiments_org_last_activity_live"
        )
    op.drop_column("experiments", "last_activity_at")
