"""queue runtime status (dispatcher / reconciler heartbeat)

A tiny key/value table holding the last-known status of the out-of-band
queue components that otherwise only log to the Modal console: the
dispatcher (``poll_queue``) and the reconciler (``reconcile_queue_state``).
Each writes one row keyed by ``component`` with a JSON ``payload`` summary
and ``updated_at`` so the admin dashboard can show "last poll spawned N
workers 12s ago" and "last reconcile reaped 3 zombies, no errors".

Revision ID: qh01_runtime_status
Revises: probe_runs_idx_001
Create Date: 2026-06-15 12:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "qh01_runtime_status"
down_revision: Union[str, Sequence[str], None] = "probe_runs_idx_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS queue_runtime_status (
            component  TEXT PRIMARY KEY,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            payload    JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS queue_runtime_status")
