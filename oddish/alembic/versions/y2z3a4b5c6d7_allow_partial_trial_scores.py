"""allow_partial_trial_scores

Revision ID: y2z3a4b5c6d7
Revises: x1y2z3a4b5c6
Create Date: 2026-04-17 10:15:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "y2z3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "x1y2z3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE trials
        ALTER COLUMN reward TYPE DOUBLE PRECISION
        USING reward::DOUBLE PRECISION
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE trials
        ALTER COLUMN reward TYPE INTEGER
        USING ROUND(reward)::INTEGER
        """
    )
