"""backfill worker_jobs from existing trials and tasks

Seeds ``worker_jobs`` rows for every non-terminal domain row at the
time of deploy so the unified dispatcher can pick them up immediately
after cutover. Without this step, in-flight trials submitted before
the unified refactor would be stranded (no ``worker_jobs`` row = no
claim path).

Status mapping:
  trials.status         -> worker_jobs (kind=TRIAL).status
  - PENDING/QUEUED       -> QUEUED
  - RUNNING              -> QUEUED (we don't know which Modal worker
                                    still has it; safer to re-enqueue
                                    and let the first claimer win)
  - RETRYING             -> QUEUED

  trials.analysis_status -> worker_jobs (kind=ANALYSIS).status
  - PENDING/QUEUED       -> QUEUED
  - RUNNING              -> QUEUED

  tasks.verdict_status   -> worker_jobs (kind=VERDICT).status
  - PENDING/QUEUED       -> QUEUED
  - RUNNING              -> QUEUED

Terminal rows (SUCCESS / FAILED) get no worker_jobs row -- they're
already done, no scheduling state to recover.

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-04-20 11:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "b5c6d7e8f9a0"
down_revision: Union[str, Sequence[str], None] = "a4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Defensive: allow re-running the migration without duplicating
    # rows. A trial / analysis / verdict is "already backfilled" when
    # there's a worker_jobs row for it in any scheduling state that
    # could still progress (QUEUED / RETRYING / RUNNING).
    op.execute(
        """
        INSERT INTO worker_jobs (
            id,
            kind,
            status,
            queue_key,
            priority,
            subject_table,
            subject_id,
            payload,
            attempts,
            max_attempts,
            available_after,
            org_id,
            created_at,
            updated_at
        )
        SELECT
            substr(md5('trial-backfill-' || tr.id), 1, 12),
            'TRIAL'::worker_job_kind,
            'QUEUED'::worker_job_status,
            tr.queue_key,
            0,
            'trials',
            tr.id,
            jsonb_build_object('trial_id', tr.id),
            0,
            tr.max_attempts,
            NOW(),
            tr.org_id,
            NOW(),
            NOW()
        FROM trials tr
        WHERE tr.status::text IN ('PENDING', 'QUEUED', 'RUNNING', 'RETRYING')
          AND NOT EXISTS (
              SELECT 1 FROM worker_jobs wj
              WHERE wj.kind::text = 'TRIAL'
                AND wj.subject_table = 'trials'
                AND wj.subject_id = tr.id
                AND wj.status::text IN ('QUEUED', 'RETRYING', 'RUNNING')
          )
        """
    )

    op.execute(
        """
        INSERT INTO worker_jobs (
            id,
            kind,
            status,
            queue_key,
            priority,
            subject_table,
            subject_id,
            payload,
            attempts,
            max_attempts,
            available_after,
            org_id,
            created_at,
            updated_at
        )
        SELECT
            substr(md5('analysis-backfill-' || tr.id), 1, 12),
            'ANALYSIS'::worker_job_kind,
            'QUEUED'::worker_job_status,
            'analysis',
            0,
            'trials',
            tr.id,
            jsonb_build_object('trial_id', tr.id),
            0,
            6,
            NOW(),
            tr.org_id,
            NOW(),
            NOW()
        FROM trials tr
        WHERE tr.analysis_status::text IN ('PENDING', 'QUEUED', 'RUNNING')
          AND NOT EXISTS (
              SELECT 1 FROM worker_jobs wj
              WHERE wj.kind::text = 'ANALYSIS'
                AND wj.subject_table = 'trials'
                AND wj.subject_id = tr.id
                AND wj.status::text IN ('QUEUED', 'RETRYING', 'RUNNING')
          )
        """
    )

    op.execute(
        """
        INSERT INTO worker_jobs (
            id,
            kind,
            status,
            queue_key,
            priority,
            subject_table,
            subject_id,
            payload,
            attempts,
            max_attempts,
            available_after,
            org_id,
            created_at,
            updated_at
        )
        SELECT
            substr(md5('verdict-backfill-' || t.id), 1, 12),
            'VERDICT'::worker_job_kind,
            'QUEUED'::worker_job_status,
            'verdict',
            0,
            'tasks',
            t.id,
            jsonb_build_object('task_id', t.id),
            0,
            6,
            NOW(),
            t.org_id,
            NOW(),
            NOW()
        FROM tasks t
        WHERE t.verdict_status::text IN ('PENDING', 'QUEUED', 'RUNNING')
          AND NOT EXISTS (
              SELECT 1 FROM worker_jobs wj
              WHERE wj.kind::text = 'VERDICT'
                AND wj.subject_table = 'tasks'
                AND wj.subject_id = t.id
                AND wj.status::text IN ('QUEUED', 'RETRYING', 'RUNNING')
          )
        """
    )


def downgrade() -> None:
    # Strip only the rows this migration introduced. Anything enqueued
    # post-deploy has a real generated id; backfilled ids follow the
    # md5-prefixed naming convention above.
    op.execute(
        """
        DELETE FROM worker_jobs
        WHERE id IN (
            SELECT substr(md5('trial-backfill-' || id), 1, 12) FROM trials
        )
           OR id IN (
            SELECT substr(md5('analysis-backfill-' || id), 1, 12) FROM trials
        )
           OR id IN (
            SELECT substr(md5('verdict-backfill-' || id), 1, 12) FROM tasks
        )
        """
    )
