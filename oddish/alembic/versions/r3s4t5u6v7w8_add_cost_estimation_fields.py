"""add_cost_estimation_fields

Revision ID: r3s4t5u6v7w8
Revises: q2r3s4t5u6v7
Create Date: 2026-03-27 12:00:00.000000

Adds cost_is_estimated and cost_estimation_method columns to trials so the
UI can distinguish native vs estimated costs and show how the cost was derived.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "r3s4t5u6v7w8"
down_revision: Union[str, Sequence[str], None] = "q2r3s4t5u6v7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS cost_is_estimated BOOLEAN")
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS cost_estimation_method TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS cost_estimation_method")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS cost_is_estimated")
