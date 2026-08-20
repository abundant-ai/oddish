"""Add provider-wide sandbox capacity leases.

Revision ID: sandbox_capacity_001
Revises: sandbox_runs_001
"""

from typing import Sequence, Union

from alembic import op


revision: str = "sandbox_capacity_001"
down_revision: Union[str, Sequence[str], None] = "sandbox_runs_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS sandbox_capacity_leases (
            provider TEXT NOT NULL,
            slot INTEGER NOT NULL,
            locked_by TEXT,
            worker_job_id VARCHAR(64)
                REFERENCES worker_jobs(id) ON DELETE SET NULL,
            locked_at TIMESTAMPTZ,
            locked_until TIMESTAMPTZ,
            PRIMARY KEY (provider, slot),
            CONSTRAINT ck_sandbox_capacity_slot_nonnegative CHECK (slot >= 0),
            CONSTRAINT ck_sandbox_capacity_lock_shape CHECK (
                (locked_by IS NULL AND worker_job_id IS NULL
                 AND locked_at IS NULL AND locked_until IS NULL)
                OR
                (locked_by IS NOT NULL AND locked_at IS NOT NULL
                 AND locked_until IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sandbox_capacity_provider_locked_until "
        "ON sandbox_capacity_leases (provider, locked_until)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_sandbox_capacity_worker_job "
        "ON sandbox_capacity_leases (worker_job_id) "
        "WHERE worker_job_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sandbox_capacity_leases")
