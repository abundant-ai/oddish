"""add slack_alert_settings table

Revision ID: slack_alert_settings_001
Revises: slack_expense_alerts_001
Create Date: 2026-07-17
"""

from typing import Sequence, Union

from alembic import op

revision: str = "slack_alert_settings_001"
down_revision: Union[str, Sequence[str], None] = "slack_expense_alerts_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS slack_alert_settings (
            id TEXT PRIMARY KEY
                CONSTRAINT ck_slack_alert_settings_singleton CHECK (id = 'global'),
            experiment_milestone_usd NUMERIC(12, 2) NOT NULL
                CONSTRAINT ck_slack_alert_settings_milestone
                CHECK (experiment_milestone_usd > 0),
            experiment_repeat_usd NUMERIC(12, 2) NOT NULL
                CONSTRAINT ck_slack_alert_settings_repeat
                CHECK (experiment_repeat_usd > 0),
            trial_ping_usd NUMERIC(12, 2) NOT NULL
                CONSTRAINT ck_slack_alert_settings_trial_ping
                CHECK (trial_ping_usd > 0),
            trial_escalation_usd NUMERIC(12, 2) NOT NULL
                CONSTRAINT ck_slack_alert_settings_escalation
                CHECK (trial_escalation_usd >= trial_ping_usd),
            always_ping_emails TEXT[] NOT NULL DEFAULT '{}',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_by_user_id VARCHAR(64)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS slack_alert_settings")
