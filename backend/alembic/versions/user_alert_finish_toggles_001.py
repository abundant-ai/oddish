"""add experiment/trial finished toggles to user_alert_preferences

Revision ID: user_alert_finish_toggles_001
Revises: merge_alertpane_main_001
Create Date: 2026-07-20
"""

from typing import Sequence, Union

from alembic import op

revision: str = "user_alert_finish_toggles_001"
down_revision: Union[str, Sequence[str], None] = "merge_alertpane_main_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_alert_preferences
            ADD COLUMN IF NOT EXISTS experiment_finished_enabled
                BOOLEAN NOT NULL DEFAULT true,
            ADD COLUMN IF NOT EXISTS trial_finished_enabled
                BOOLEAN NOT NULL DEFAULT true
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_alert_preferences
            DROP COLUMN IF EXISTS experiment_finished_enabled,
            DROP COLUMN IF EXISTS trial_finished_enabled
        """
    )
