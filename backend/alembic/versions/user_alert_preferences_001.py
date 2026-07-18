"""add user_alert_preferences table

Revision ID: user_alert_preferences_001
Revises: slack_alert_settings_001
Create Date: 2026-07-17
"""

from typing import Sequence, Union

from alembic import op

revision: str = "user_alert_preferences_001"
down_revision: Union[str, Sequence[str], None] = "slack_alert_settings_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_alert_preferences (
            user_id VARCHAR(64) PRIMARY KEY
                REFERENCES users(id) ON DELETE CASCADE,
            cost_milestone_enabled BOOLEAN NOT NULL DEFAULT true,
            expensive_trial_enabled BOOLEAN NOT NULL DEFAULT true,
            experiment_failed_enabled BOOLEAN NOT NULL DEFAULT true,
            trial_failed_enabled BOOLEAN NOT NULL DEFAULT true,
            qa_failed_enabled BOOLEAN NOT NULL DEFAULT true,
            experiment_milestone_usd NUMERIC(12, 2)
                CONSTRAINT ck_user_alert_prefs_milestone
                CHECK (experiment_milestone_usd IS NULL OR experiment_milestone_usd > 0),
            trial_ping_usd NUMERIC(12, 2)
                CONSTRAINT ck_user_alert_prefs_trial_ping
                CHECK (trial_ping_usd IS NULL OR trial_ping_usd > 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_alert_preferences")
