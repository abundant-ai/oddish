"""model concurrency advisory (dynamic per-model concurrency controller)

A small key/value table holding the controller's advisory per-queue concurrency
limit. The reconciler recomputes one row per ``queue_key`` each cycle (the
feed-forward provider-limit ceiling trimmed by a queue-feedback AIMD loop); the
dispatcher reads the advisory limit when it is fresh and falls back to the static
``get_model_concurrency`` value otherwise. The static config stays the hard
ceiling and the fallback -- this table is only ever advisory (the senpai guard:
never mutate a limit you must refresh). ``state`` carries the controller's
cross-cycle hysteresis (cooldown latch + sustained-demand counter).

Idempotent (CREATE TABLE IF NOT EXISTS) and self-contained, mirroring
qh01_add_queue_runtime_status.

Revision ID: mca01_model_concurrency_advisory
Revises: r8a9drop_probe_ratio_001
Create Date: 2026-06-22 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "mca01_model_concurrency_advisory"
down_revision: Union[str, Sequence[str], None] = "r8a9drop_probe_ratio_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS model_concurrency_advisory (
            queue_key      TEXT PRIMARY KEY,
            advisory_limit INTEGER NOT NULL,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            state          JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS model_concurrency_advisory")
