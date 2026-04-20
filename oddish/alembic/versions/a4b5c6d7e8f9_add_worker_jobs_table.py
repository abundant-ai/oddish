"""add_worker_jobs_table

Phase A of the unified `worker_jobs` table migration.

Introduces the schema only -- no code reads from or writes to this table
yet. The table is the future authoritative queue for every kind of
compute work (trial / analysis / verdict / QA / future). Domain tables
continue to own their own domain-state columns.

See `.cursor/plans/unified_worker_jobs_table.plan.md` for the full
design. This migration is independently revertable.

Revision ID: a4b5c6d7e8f9
Revises: z3a4b5c6d7e8
Create Date: 2026-04-20 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "z3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create worker_jobs enums, table, and indexes."""
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'worker_job_kind') THEN
                CREATE TYPE worker_job_kind AS ENUM (
                    'TRIAL',
                    'ANALYSIS',
                    'VERDICT',
                    'QA_REVIEW'
                );
            END IF;
        END
        $$;
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'worker_job_status') THEN
                CREATE TYPE worker_job_status AS ENUM (
                    'QUEUED',
                    'RUNNING',
                    'RETRYING',
                    'SUCCESS',
                    'FAILED',
                    'CANCELLED',
                    'BLOCKED'
                );
            END IF;
        END
        $$;
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_jobs (
            id                       TEXT PRIMARY KEY,
            kind                     worker_job_kind   NOT NULL,
            status                   worker_job_status NOT NULL DEFAULT 'QUEUED',
            queue_key                TEXT              NOT NULL,
            priority                 SMALLINT          NOT NULL DEFAULT 0,

            subject_table            TEXT,
            subject_id               TEXT,

            parent_job_id            TEXT REFERENCES worker_jobs(id) ON DELETE SET NULL,

            payload                  JSONB NOT NULL DEFAULT '{}'::jsonb,

            attempts                 INTEGER NOT NULL DEFAULT 0,
            max_attempts             INTEGER NOT NULL DEFAULT 6,
            next_retry_at            TIMESTAMPTZ,
            available_after          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

            current_worker_id        TEXT,
            current_queue_slot       INTEGER,
            modal_function_call_id   TEXT,
            claimed_at               TIMESTAMPTZ,
            heartbeat_at             TIMESTAMPTZ,
            stale_reaped_at          TIMESTAMPTZ,
            heartbeat_failure_count  INTEGER NOT NULL DEFAULT 0,
            last_heartbeat_error     TEXT,
            last_heartbeat_error_at  TIMESTAMPTZ,

            error_message            TEXT,
            result_summary           JSONB,

            created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at               TIMESTAMPTZ,
            started_at               TIMESTAMPTZ,
            finished_at              TIMESTAMPTZ,

            org_id                   TEXT
        )
        """
    )

    # Claim path: used by every dispatcher tick.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_worker_jobs_claim
            ON worker_jobs (queue_key, priority DESC, available_after, created_at)
            WHERE status IN ('QUEUED', 'RETRYING')
        """
    )

    # Stale-heartbeat path.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_worker_jobs_heartbeat
            ON worker_jobs (status, heartbeat_at)
            WHERE status = 'RUNNING'
        """
    )

    # Dashboard / admin / status path: "what's the state of all jobs
    # for this trial/task?" -- used by the admin worker_jobs panel and
    # by stage-transition helpers.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_worker_jobs_subject
            ON worker_jobs (subject_table, subject_id)
        """
    )

    # Dependency lookups.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_worker_jobs_parent
            ON worker_jobs (parent_job_id)
            WHERE parent_job_id IS NOT NULL
        """
    )

    # Org-scoped filters.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_worker_jobs_org
            ON worker_jobs (org_id, status)
            WHERE org_id IS NOT NULL
        """
    )


def downgrade() -> None:
    """Drop worker_jobs indexes, table, and enums."""
    op.execute("DROP INDEX IF EXISTS idx_worker_jobs_org")
    op.execute("DROP INDEX IF EXISTS idx_worker_jobs_parent")
    op.execute("DROP INDEX IF EXISTS idx_worker_jobs_subject")
    op.execute("DROP INDEX IF EXISTS idx_worker_jobs_heartbeat")
    op.execute("DROP INDEX IF EXISTS idx_worker_jobs_claim")
    op.execute("DROP TABLE IF EXISTS worker_jobs")
    op.execute("DROP TYPE IF EXISTS worker_job_status")
    op.execute("DROP TYPE IF EXISTS worker_job_kind")
