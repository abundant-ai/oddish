"""add analysis and verdict modal function ids

Revision ID: u7v8w9x0y1z2
Revises: t6u7v8w9x0y1
Create Date: 2026-04-03 15:45:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "u7v8w9x0y1z2"
down_revision: Union[str, Sequence[str], None] = "t6u7v8w9x0y1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE trials
        ADD COLUMN IF NOT EXISTS analysis_modal_function_call_id VARCHAR(128)
        """
    )
    op.execute(
        """
        ALTER TABLE tasks
        ADD COLUMN IF NOT EXISTS verdict_modal_function_call_id VARCHAR(128)
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS verdict_modal_function_call_id")
    op.execute(
        "ALTER TABLE trials DROP COLUMN IF EXISTS analysis_modal_function_call_id"
    )
