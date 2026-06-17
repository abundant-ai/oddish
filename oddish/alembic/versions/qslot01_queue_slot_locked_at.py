"""track per-slot lease start so orphaned leases can be reclaimed per-slot

The orphaned-slot reconciliation could only release a leased ``queue_slots``
row when there were ZERO RUNNING ``worker_jobs`` for the *entire* queue_key. On
a busy queue key (e.g. a hot model) a single live job kept every leaked lease
from a SIGKILLed/preempted worker pinned for the full ~12h lease, so the slot
pool saturated while only a handful of jobs actually ran -- the dispatcher kept
budgeting on the RUNNING count (low) and spawning workers that all failed to
acquire a slot.

Reclamation is moving to a per-slot check keyed on the owning worker's liveness
(``queue_slots.locked_by`` = ``worker_jobs.current_worker_id``). ``locked_at``
records when a lease was taken so the reconciler can apply a short grace window
and avoid racing the brief acquire->claim gap. The partial index supports the
correlated liveness lookup.

Revision ID: qslot01_locked_at
Revises: nopx01_nop_oracle_variants
Create Date: 2026-06-15 14:55:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "qslot01_locked_at"
down_revision: Union[str, Sequence[str], None] = "nopx01_nop_oracle_variants"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE queue_slots ADD COLUMN IF NOT EXISTS locked_at timestamptz")
    # Supports the per-slot liveness lookup in the orphaned-slot reclaim:
    # NOT EXISTS (SELECT 1 FROM worker_jobs WHERE status='RUNNING'
    #             AND current_worker_id = queue_slots.locked_by)
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_worker_jobs_current_worker_id
        ON worker_jobs (current_worker_id)
        WHERE current_worker_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_worker_jobs_current_worker_id")
    op.execute("ALTER TABLE queue_slots DROP COLUMN IF EXISTS locked_at")
