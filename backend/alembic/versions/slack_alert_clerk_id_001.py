"""carry the recipient's Clerk user id on slack_expense_alerts

Lets delivery prefer the recipient's Clerk-linked Slack account over the
email-based Slack lookup. Nullable and left unbackfilled: existing pending DM
rows simply fall back to the email lookup, exactly as before.

Revision ID: slack_alert_clerk_id_001
Revises: slack_alert_outbox_001
Create Date: 2026-07-17
"""

from typing import Sequence, Union

from alembic import op

revision: str = "slack_alert_clerk_id_001"
down_revision: Union[str, Sequence[str], None] = "slack_alert_outbox_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE slack_expense_alerts
            ADD COLUMN recipient_clerk_user_id TEXT
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE slack_expense_alerts
            DROP COLUMN recipient_clerk_user_id
        """
    )
