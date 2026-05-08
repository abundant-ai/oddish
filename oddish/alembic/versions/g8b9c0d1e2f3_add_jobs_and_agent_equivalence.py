"""add jobs table and trials.agent_equivalence_key

Phase 1 of the task-first migration. Additive only -- no existing column
or read path changes. Sets up the data shape needed for the cell-matrix
experiment view that lands in P3.

- ``jobs`` table: user-visible batch concept. One row per "I just
  launched some work". Aggregate status is computed at read time from
  child ``worker_jobs``; no denormalized status column.
- ``trials.job_id`` (nullable): the batch a trial was produced by.
  Backfilled in a follow-up migration to one synthetic Job per existing
  experiment.
- ``trials.agent_equivalence_key`` (nullable): canonical
  sha256(harness | model | provider). Backfilled in a follow-up.
- ``worker_jobs.user_job_id`` (nullable): so we can find what's
  currently running for a Job without scanning all worker_jobs.
- New indexes: composite ``(task_version_id, agent_equivalence_key)``
  on trials (load-bearing for the cell-matrix read path), partial
  ``(job_id)`` on trials, partial ``(user_job_id, status)`` on
  worker_jobs.

Revision ID: g8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-05-01 00:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "g8b9c0d1e2f3"
down_revision: Union[str, Sequence[str], None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- jobs table -----------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id VARCHAR(64) PRIMARY KEY,
            kind VARCHAR(64) NOT NULL,
            name VARCHAR(255),
            triggered_by_experiment_id VARCHAR(64)
                REFERENCES experiments(id) ON DELETE SET NULL,
            launched_by_user_id VARCHAR(64),
            launched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            org_id VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ,
            CONSTRAINT jobs_kind_check CHECK (
                kind IN ('validation', 'experiment_backfill', 'ad_hoc')
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_org_launched_at
            ON jobs (org_id, launched_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_triggered_by_experiment
            ON jobs (triggered_by_experiment_id)
            WHERE triggered_by_experiment_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_org_id
            ON jobs (org_id)
        """
    )

    # --- trials new columns --------------------------------------------
    op.execute(
        """
        ALTER TABLE trials
            ADD COLUMN IF NOT EXISTS job_id VARCHAR(64)
                REFERENCES jobs(id) ON DELETE SET NULL
        """
    )
    op.execute(
        """
        ALTER TABLE trials
            ADD COLUMN IF NOT EXISTS agent_equivalence_key VARCHAR(64)
        """
    )

    # --- worker_jobs new column ----------------------------------------
    op.execute(
        """
        ALTER TABLE worker_jobs
            ADD COLUMN IF NOT EXISTS user_job_id VARCHAR(64)
                REFERENCES jobs(id) ON DELETE SET NULL
        """
    )

    # --- new indexes ---------------------------------------------------
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trials_task_version_agent
            ON trials (task_version_id, agent_equivalence_key)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trials_job_id
            ON trials (job_id) WHERE job_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_worker_jobs_user_job
            ON worker_jobs (user_job_id, status)
            WHERE user_job_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_worker_jobs_user_job")
    op.execute("DROP INDEX IF EXISTS idx_trials_job_id")
    op.execute("DROP INDEX IF EXISTS idx_trials_task_version_agent")
    op.execute("ALTER TABLE worker_jobs DROP COLUMN IF EXISTS user_job_id")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS agent_equivalence_key")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS job_id")
    op.execute("DROP INDEX IF EXISTS idx_jobs_org_id")
    op.execute("DROP INDEX IF EXISTS idx_jobs_triggered_by_experiment")
    op.execute("DROP INDEX IF EXISTS idx_jobs_org_launched_at")
    op.execute("DROP TABLE IF EXISTS jobs")
