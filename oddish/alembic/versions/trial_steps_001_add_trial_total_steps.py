"""add_trial_total_steps

Revision ID: trial_steps_001
Revises: r8a9drop_probe_ratio_001
Create Date: 2026-06-18 23:20:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "trial_steps_001"
down_revision: Union[str, Sequence[str], None] = "r8a9drop_probe_ratio_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS total_steps INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS total_steps")
