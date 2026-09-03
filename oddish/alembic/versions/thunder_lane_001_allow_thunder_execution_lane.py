"""Allow Thunder jobs on their credential and capacity scoped lane.

Revision ID: thunder_lane_001
Revises: quota_pause_status_001
"""

from typing import Sequence, Union

from alembic import op


revision: str = "thunder_lane_001"
down_revision: Union[str, Sequence[str], None] = "quota_pause_status_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE worker_jobs DROP CONSTRAINT IF EXISTS "
        "ck_worker_jobs_execution_lane"
    )
    op.execute(
        "ALTER TABLE worker_jobs ADD CONSTRAINT ck_worker_jobs_execution_lane "
        "CHECK (execution_lane IN ('default', 'ec2_trial', 'thunder_trial'))"
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE worker_jobs
        SET execution_lane = 'default'
        WHERE execution_lane = 'thunder_trial'
        """
    )
    op.execute(
        "ALTER TABLE worker_jobs DROP CONSTRAINT IF EXISTS "
        "ck_worker_jobs_execution_lane"
    )
    op.execute(
        "ALTER TABLE worker_jobs ADD CONSTRAINT ck_worker_jobs_execution_lane "
        "CHECK (execution_lane IN ('default', 'ec2_trial'))"
    )
