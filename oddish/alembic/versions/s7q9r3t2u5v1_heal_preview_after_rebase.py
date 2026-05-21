"""heal preview DBs after the reports-migration rebase

Revision ID: s7q9r3t2u5v1
Revises: r5p9q8s2t1u4
Create Date: 2026-05-21 02:00:00.000000

Pre-rebase, ``r5p9q8s2t1u4`` was parented at ``637c45e5ac80``. When this
PR's preview Supabase branch was first migrated, alembic stamped
``r5p9q8s2t1u4`` as head and never applied ``j2e3f4a5b6c7``
(``experiments.last_activity_at``) or ``k2l3m4n5o6p7``
(``task_experiments.deleted_at``). After the rebase, those two migrations
are now in the chain *before* ``r5p9q8s2t1u4``, so alembic on the stale
preview DB sees its recorded head and skips the missing intermediates --
leaving the schema permanently behind.

This fix-up runs *after* ``r5p9q8s2t1u4`` and idempotently materializes
whatever ``j2e3f4a5b6c7`` / ``k2l3m4n5o6p7`` would have added. On prod
(where those migrations already applied normally) every step here is a
no-op via ``IF NOT EXISTS`` guards.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "s7q9r3t2u5v1"
down_revision: Union[str, Sequence[str], None] = "r5p9q8s2t1u4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # j2e3f4a5b6c7: experiments.last_activity_at + supporting index.
    op.execute(
        "ALTER TABLE experiments "
        "ADD COLUMN IF NOT EXISTS last_activity_at TIMESTAMPTZ"
    )
    # Backfill only when the column was just created (i.e. the value is
    # NULL for live rows). Safe to re-run; the WHERE filter makes it a
    # no-op once populated.
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
          AND e.last_activity_at IS NULL
        """
    )

    # k2l3m4n5o6p7: task_experiments.deleted_at.
    op.execute(
        "ALTER TABLE task_experiments "
        "ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ"
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
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_task_experiments_live_experiment_task
            ON task_experiments (experiment_id, task_id)
            WHERE deleted_at IS NULL
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_task_experiments_live_task_experiment
            ON task_experiments (task_id, experiment_id)
            WHERE deleted_at IS NULL
            """
        )


def downgrade() -> None:
    # This migration only patches in objects that the chained migrations
    # would create anyway, so its downgrade is a no-op -- the real
    # teardown lives on the j2e3f4a5b6c7 / k2l3m4n5o6p7 downgrades.
    pass
