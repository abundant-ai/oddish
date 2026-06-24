"""repair TAG_PROJECT coalescing unique index

Revision ID: tagproj_idx_repair_001
Revises: hv2claimidx01
Create Date: 2026-06-23 23:15:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "tagproj_idx_repair_001"
down_revision: Union[str, Sequence[str], None] = "hv2claimidx01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Branches that missed the index may already have duplicate in-flight
    # TAG_PROJECT jobs. Keep the oldest job per subject and cancel the rest so
    # the unique index can be created idempotently.
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY kind, subject_table, subject_id
                    ORDER BY created_at ASC, id ASC
                ) AS rn
            FROM worker_jobs
            WHERE kind = 'TAG_PROJECT'
              AND status IN ('QUEUED', 'RETRYING')
              AND subject_id IS NOT NULL
        )
        UPDATE worker_jobs AS w
        SET
            status = 'CANCELLED',
            finished_at = COALESCE(w.finished_at, NOW()),
            updated_at = NOW(),
            error_message = COALESCE(
                w.error_message,
                'Cancelled by TAG_PROJECT coalescing index repair migration'
            )
        FROM ranked AS r
        WHERE w.id = r.id
          AND r.rn > 1
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_worker_jobs_tag_project_active
        ON worker_jobs (kind, subject_table, subject_id)
        WHERE kind = 'TAG_PROJECT'
          AND status IN ('QUEUED', 'RETRYING')
          AND subject_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_worker_jobs_tag_project_active")
