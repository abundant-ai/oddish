"""add slack_expense_alerts table

Revision ID: slack_expense_alerts_001
Revises: add_quota_bumps_001
Create Date: 2026-07-09
"""

from typing import Sequence, Union

from alembic import op

revision: str = "slack_expense_alerts_001"
down_revision: Union[str, Sequence[str], None] = "add_quota_bumps_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE slack_expense_alerts (
            alert_key TEXT PRIMARY KEY,
            claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            notified_at TIMESTAMPTZ
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE slack_expense_alerts")
