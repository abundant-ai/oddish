"""drop legacy trial claim-path indexes

The trials table is no longer on the scheduling hot path -- all claim
and stale-heartbeat queries hit ``worker_jobs`` instead. The two
indexes this migration drops were built specifically for:

- ``idx_trials_claimable (status, queue_key, next_retry_at)`` -- the
  legacy ``_CLAIM_TRIAL_SQL`` lookup path, deleted in the unified
  refactor.
- ``idx_trials_status_heartbeat_at (status, heartbeat_at)`` -- the
  legacy stale-heartbeat sweep, replaced by
  ``idx_worker_jobs_heartbeat`` on ``worker_jobs``.

``idx_trials_status (status)`` is kept because several display/API
queries still filter by ``trials.status``.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-04-20 23:59:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "c6d7e8f9a0b1"
down_revision: Union[str, Sequence[str], None] = "b5c6d7e8f9a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_trials_claimable")
    op.execute("DROP INDEX IF EXISTS idx_trials_status_heartbeat_at")


def downgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trials_claimable "
        "ON trials (status, queue_key, next_retry_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trials_status_heartbeat_at "
        "ON trials (status, heartbeat_at)"
    )
