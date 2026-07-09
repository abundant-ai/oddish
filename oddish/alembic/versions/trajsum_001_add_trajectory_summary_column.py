"""add_trajectory_summary_column

Revision ID: trajsum_001
Revises: org_quota_idx_001
Create Date: 2026-07-08 12:00:00.000000

Adds a JSONB column on trials to store the LLM-generated trajectory
summary, populated lazily on first request to
GET /trials/{id}/trajectory/summary.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "trajsum_001"
down_revision: Union[str, Sequence[str], None] = "org_quota_idx_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS trajectory_summary JSONB"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS trajectory_summary")
