"""merge alert-settings-pane + slack_alert_clerk_id heads

Revision ID: merge_alertpane_main_001
Revises: user_alert_preferences_001, slack_alert_clerk_id_001
Create Date: 2026-07-17 00:00:00.000000

Empty merge migration reconciling the two alembic heads created by merging
origin/main (the Slack outbox + Clerk-linked-account stack ending at
``slack_alert_clerk_id_001``) into the alert-settings-pane stack (the admin
settings + per-user preferences ending at ``user_alert_preferences_001``).
Both forked from ``slack_expense_alerts_001``. No schema change -- it only
rejoins the divergent history into a single head so the migration-head guard
passes.
"""

from typing import Sequence, Union

revision: str = "merge_alertpane_main_001"
down_revision: Union[str, Sequence[str], None] = (
    "user_alert_preferences_001",
    "slack_alert_clerk_id_001",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
