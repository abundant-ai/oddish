"""Add the attempt-scoped remote sandbox lifecycle ledger.

Revision ID: sandbox_runs_001
Revises: execution_lane_001
"""

from typing import Sequence, Union

from alembic import op


revision: str = "sandbox_runs_001"
down_revision: Union[str, Sequence[str], None] = "execution_lane_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sandbox_runs (
            id VARCHAR(64) PRIMARY KEY,
            worker_job_id VARCHAR(64) NOT NULL
                REFERENCES worker_jobs(id) ON DELETE RESTRICT,
            worker_job_attempt INTEGER NOT NULL,
            trial_id VARCHAR(160) NOT NULL,
            provider VARCHAR(32) NOT NULL,
            state VARCHAR(32) NOT NULL,
            deployment TEXT NOT NULL,
            aws_account_id VARCHAR(12) NOT NULL,
            region VARCHAR(32) NOT NULL,
            launch_token VARCHAR(64) NOT NULL,
            external_id TEXT,
            provisioned_at TIMESTAMPTZ,
            termination_requested_at TIMESTAMPTZ,
            terminated_at TIMESTAMPTZ,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at TIMESTAMPTZ,
            CONSTRAINT uq_sandbox_runs_worker_attempt
                UNIQUE (worker_job_id, worker_job_attempt),
            CONSTRAINT uq_sandbox_runs_launch_token UNIQUE (launch_token),
            CONSTRAINT ck_sandbox_runs_state CHECK (
                state IN ('PROVISIONING', 'RUNNING', 'TERMINATING',
                          'TERMINATED', 'FAILED')
            ),
            CONSTRAINT ck_sandbox_runs_attempt_positive
                CHECK (worker_job_attempt > 0)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "uq_sandbox_runs_provider_external_id "
        "ON sandbox_runs (provider, external_id) WHERE external_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sandbox_runs_state_updated "
        "ON sandbox_runs (state, updated_at)"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION request_sandbox_run_termination()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF OLD.status::text = 'RUNNING'
             AND NEW.status::text <> 'RUNNING' THEN
            UPDATE sandbox_runs
            SET state = 'TERMINATING',
                termination_requested_at = COALESCE(
                    termination_requested_at, NOW()
                ),
                updated_at = NOW()
            WHERE worker_job_id = NEW.id
              AND worker_job_attempt = OLD.attempts
              AND state NOT IN ('TERMINATING', 'TERMINATED');
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_worker_jobs_request_sandbox_termination "
        "ON worker_jobs"
    )
    op.execute(
        """
        CREATE TRIGGER trg_worker_jobs_request_sandbox_termination
        AFTER UPDATE OF status ON worker_jobs
        FOR EACH ROW
        EXECUTE FUNCTION request_sandbox_run_termination()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_worker_jobs_request_sandbox_termination "
        "ON worker_jobs"
    )
    op.execute("DROP FUNCTION IF EXISTS request_sandbox_run_termination()")
    op.execute("DROP TABLE IF EXISTS sandbox_runs")
