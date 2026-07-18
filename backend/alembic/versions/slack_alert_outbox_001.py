"""turn slack_expense_alerts into a payload-carrying outbox

Adds the columns that let delivery run purely off the table: the rendered
message text, the DM recipient, and the mention emails. Pre-outbox pending
rows have no payload to deliver; the notifier settles them on sight, so no
data backfill is needed here.

Revision ID: slack_alert_outbox_001
Revises: slack_expense_alerts_001
Create Date: 2026-07-17
"""

from typing import Sequence, Union

from alembic import op

revision: str = "slack_alert_outbox_001"
down_revision: Union[str, Sequence[str], None] = "slack_expense_alerts_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE slack_expense_alerts
            ADD COLUMN payload TEXT,
            ADD COLUMN recipient_email TEXT,
            ADD COLUMN mention_emails JSONB
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE slack_expense_alerts
            DROP COLUMN payload,
            DROP COLUMN recipient_email,
            DROP COLUMN mention_emails
        """
    )
