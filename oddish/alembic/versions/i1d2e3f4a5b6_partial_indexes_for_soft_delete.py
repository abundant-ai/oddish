"""add_partial_indexes_for_soft_delete_hot_paths

Revision ID: i1d2e3f4a5b6
Revises: h9c0d1e2f3a4
Create Date: 2026-05-14 16:35:00.000000

Adds partial indexes that match the soft-delete predicate so that the
session-level ``WHERE deleted_at IS NULL`` filter installed in
:mod:`oddish.db.soft_delete` actually gets to ride an index instead of
filtering after a wider scan. Targets the dashboard recent-experiments
aggregation, the experiment-scoped task/trial listings, queue stats, and
the ``fetch_visible_worker_jobs`` recent-terminal branch.

All indexes are created ``CONCURRENTLY`` and ``IF NOT EXISTS`` so the
migration is safe to apply against a busy database. ``CONCURRENTLY``
requires running outside a transaction, which Alembic supports via the
``with op.get_context().autocommit_block():`` pattern below.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "i1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "h9c0d1e2f3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction.
    with op.get_context().autocommit_block():
        # Dashboard recent-tasks list and experiment task list ordering.
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_tasks_org_created_at_live
            ON tasks (org_id, created_at DESC)
            WHERE deleted_at IS NULL
            """
        )

        # Dashboard trial aggregation + experiment-scoped trial loads.
        # Combines the two filter predicates we always pair on these
        # paths (``deleted_at IS NULL`` from the listener,
        # ``superseded_by_trial_id IS NULL`` from the rerun-history
        # collapse).
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_trials_live_org_experiment_created
            ON trials (org_id, experiment_id, created_at DESC)
            WHERE deleted_at IS NULL AND superseded_by_trial_id IS NULL
            """
        )

        # Queue stats grouping (``oddish.queue.get_queue_stats``).
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_trials_live_org_status
            ON trials (org_id, status)
            WHERE deleted_at IS NULL
            """
        )

        # Recent-terminal branch of fetch_visible_worker_jobs.
        # ``finished_at IS NOT NULL`` is the predicate the OR branch
        # filters on; ordering by ``finished_at DESC`` gives the index
        # an immediately-useful sort.
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_worker_jobs_subject_finished_recent
            ON worker_jobs (subject_table, subject_id, finished_at DESC)
            WHERE finished_at IS NOT NULL
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS idx_worker_jobs_subject_finished_recent"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_trials_live_org_status")
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS idx_trials_live_org_experiment_created"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_tasks_org_created_at_live")
