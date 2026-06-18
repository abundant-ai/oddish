"""add_trial_total_steps

Revision ID: a4b5c6d7e8f0
Revises: z3a4b5c6d7e8
Create Date: 2026-06-18 23:20:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "a4b5c6d7e8f0"
down_revision: Union[str, Sequence[str], None] = "z3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS total_steps INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS total_steps")
