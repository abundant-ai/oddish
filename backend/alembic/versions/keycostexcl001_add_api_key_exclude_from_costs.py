"""add api_keys.exclude_from_costs

Revision ID: keycostexcl001
Revises: slack_expense_alerts_001
Create Date: 2026-07-16 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "keycostexcl001"
down_revision: Union[str, Sequence[str], None] = "slack_expense_alerts_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS "
        "exclude_from_costs BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE api_keys DROP COLUMN IF EXISTS exclude_from_costs")
