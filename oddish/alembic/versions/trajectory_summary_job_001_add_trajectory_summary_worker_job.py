"""add trajectory summary worker job

First-view summary generation is durable queue work. PostgreSQL enforces that
only one QUEUED, RETRYING, or RUNNING summary job can exist for a trial, so
concurrent page views cannot duplicate the LLM call.

Revision ID: trajectory_summary_job_001
Revises: trial_facets_001
Create Date: 2026-08-06 16:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "trajectory_summary_job_001"
down_revision: Union[str, Sequence[str], None] = "trial_facets_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL forbids using a new enum label until the ALTER TYPE
    # transaction commits, so add it in Alembic's autocommit block before
    # creating the partial index that references it.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE worker_job_kind ADD VALUE IF NOT EXISTS 'TRAJECTORY_SUMMARY'"
        )

    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            uq_worker_jobs_trajectory_summary_active
        ON worker_jobs (kind, subject_table, subject_id)
        WHERE kind = 'TRAJECTORY_SUMMARY'
          AND status IN ('QUEUED', 'RETRYING', 'RUNNING')
          AND subject_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_worker_jobs_trajectory_summary_active")
    # PostgreSQL cannot remove one enum label safely. Keeping the unused label
    # matches every other worker_job_kind downgrade in this repository.
