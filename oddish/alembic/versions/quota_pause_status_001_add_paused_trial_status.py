"""Add PAUSED to the trial status enum.

Revision ID: quota_pause_status_001
Revises: feedback_001
"""

from typing import Sequence, Union

from alembic import op


revision: str = "quota_pause_status_001"
down_revision: Union[str, Sequence[str], None] = "feedback_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'PAUSED'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value without replacing the enum type.
    pass
