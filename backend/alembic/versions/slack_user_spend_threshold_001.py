"""add weekly user spend escalation threshold

Revision ID: slack_user_spend_threshold_001
Revises: user_alert_task_finished_001
Create Date: 2026-07-27
"""

from typing import Sequence, Union

from alembic import op

revision: str = "slack_user_spend_threshold_001"
down_revision: Union[str, Sequence[str], None] = "user_alert_task_finished_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE slack_alert_settings
            ADD COLUMN IF NOT EXISTS user_weekly_escalation_delta_usd
                NUMERIC(12, 2) NOT NULL DEFAULT 5000
                CONSTRAINT ck_slack_alert_settings_user_weekly_delta
                CHECK (user_weekly_escalation_delta_usd > 0)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE slack_alert_settings
            DROP COLUMN IF EXISTS user_weekly_escalation_delta_usd
        """
    )
