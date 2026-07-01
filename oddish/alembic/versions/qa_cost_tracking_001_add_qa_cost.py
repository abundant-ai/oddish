"""add QA cost columns (trials.analysis_cost_usd, tasks.verdict_cost_usd)

Tracks the LLM spend of the QA / analysis pipeline so it can be reported
separately from trial-execution spend on the admin cost dashboard:

- ``trials.analysis_cost_usd`` -- cost of analyzing one trial (trajectory
  classification, or the probe summary for probe trials).
- ``tasks.verdict_cost_usd`` -- cost of the one per-task verdict synthesis call.

Both are nullable with no backfill: rows analyzed before this migration keep a
NULL cost (treated as 0 in the aggregation), and new QA runs populate them.

Revision ID: qa_cost_tracking_001
Revises: merge_mca01_wjtoken_heads
Create Date: 2026-06-30 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "qa_cost_tracking_001"
down_revision: Union[str, Sequence[str], None] = "merge_mca01_wjtoken_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS analysis_cost_usd DOUBLE PRECISION"
    )
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS verdict_cost_usd DOUBLE PRECISION"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS analysis_cost_usd")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS verdict_cost_usd")
