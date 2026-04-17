"""add_trial_heartbeat_diagnostics

Adds columns that let us diagnose stale-heartbeat reaps without losing the
worker's last successful heartbeat timestamp:

- trials.stale_reaped_at: set by cleanup when it marks a trial FAILED for
  stale heartbeat. Previously cleanup overwrote heartbeat_at, which made
  post-mortem "when did the worker actually stop?" impossible.
- trials.heartbeat_failure_count / last_heartbeat_error / last_heartbeat_error_at:
  populated by the worker's heartbeat loop when a DB write raises, so we
  can distinguish "worker process died" from "DB/pooler was unreachable".

Revision ID: z3a4b5c6d7e8
Revises: y2z3a4b5c6d7
Create Date: 2026-04-17 22:15:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "z3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "y2z3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS stale_reaped_at TIMESTAMPTZ")
    op.execute(
        "ALTER TABLE trials "
        "ADD COLUMN IF NOT EXISTS heartbeat_failure_count INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS last_heartbeat_error TEXT"
    )
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS last_heartbeat_error_at TIMESTAMPTZ"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS last_heartbeat_error_at")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS last_heartbeat_error")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS heartbeat_failure_count")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS stale_reaped_at")
