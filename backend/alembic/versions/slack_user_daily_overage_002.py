"""rename user weekly escalation to per-user daily-overage margin

Revision ID: slack_user_daily_overage_002
Revises: slack_user_spend_threshold_001
Create Date: 2026-07-27

The channel escalation moved from "seven-day spend vs the workspace average" to
"last-24h spend vs the user's own trailing seven-day daily average". The knob
kept the same table but changed meaning, so rename the column, reset its default
to the new $1000 margin, and rename the check constraint to match.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "slack_user_daily_overage_002"
down_revision: Union[str, Sequence[str], None] = "slack_user_spend_threshold_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE slack_alert_settings
            DROP CONSTRAINT IF EXISTS ck_slack_alert_settings_user_weekly_delta
        """
    )
    op.execute(
        """
        ALTER TABLE slack_alert_settings
            RENAME COLUMN user_weekly_escalation_delta_usd
                       TO user_daily_overage_delta_usd
        """
    )
    op.execute(
        """
        ALTER TABLE slack_alert_settings
            ALTER COLUMN user_daily_overage_delta_usd SET DEFAULT 1000
        """
    )
    op.execute(
        """
        ALTER TABLE slack_alert_settings
            ADD CONSTRAINT ck_slack_alert_settings_user_daily_overage
                CHECK (user_daily_overage_delta_usd > 0)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE slack_alert_settings
            DROP CONSTRAINT IF EXISTS ck_slack_alert_settings_user_daily_overage
        """
    )
    op.execute(
        """
        ALTER TABLE slack_alert_settings
            ALTER COLUMN user_daily_overage_delta_usd SET DEFAULT 5000
        """
    )
    op.execute(
        """
        ALTER TABLE slack_alert_settings
            RENAME COLUMN user_daily_overage_delta_usd
                       TO user_weekly_escalation_delta_usd
        """
    )
    op.execute(
        """
        ALTER TABLE slack_alert_settings
            ADD CONSTRAINT ck_slack_alert_settings_user_weekly_delta
                CHECK (user_weekly_escalation_delta_usd > 0)
        """
    )
