"""add_trial_execution_tracking

Revision ID: p1q2r3s4t5u6
Revises: o0p1q2r3s4t5
Create Date: 2026-03-11 18:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "p1q2r3s4t5u6"
down_revision: Union[str, Sequence[str], None] = "o0p1q2r3s4t5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS current_pgqueuer_job_id INTEGER"
    )
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS current_worker_id VARCHAR(160)"
    )
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS current_queue_slot INTEGER")
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ")
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trials_status_heartbeat_at ON trials (status, heartbeat_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_trials_current_pgqueuer_job_id ON trials (current_pgqueuer_job_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_trials_current_worker_id ON trials (current_worker_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_trials_current_worker_id")
    op.execute("DROP INDEX IF EXISTS ix_trials_current_pgqueuer_job_id")
    op.execute("DROP INDEX IF EXISTS idx_trials_status_heartbeat_at")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS heartbeat_at")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS claimed_at")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS current_queue_slot")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS current_worker_id")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS current_pgqueuer_job_id")
