"""add task finished toggle to user_alert_preferences

Revision ID: user_alert_task_finished_001
Revises: user_alert_finish_toggles_001
Create Date: 2026-07-21
"""

from typing import Sequence, Union

from alembic import op

revision: str = "user_alert_task_finished_001"
down_revision: Union[str, Sequence[str], None] = "user_alert_finish_toggles_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_alert_preferences
            ADD COLUMN IF NOT EXISTS task_finished_enabled
                BOOLEAN NOT NULL DEFAULT true
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_alert_preferences
            DROP COLUMN IF EXISTS task_finished_enabled
        """
    )
