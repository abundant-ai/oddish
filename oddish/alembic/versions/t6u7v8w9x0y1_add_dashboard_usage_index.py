"""add_dashboard_usage_index

Revision ID: t6u7v8w9x0y1
Revises: s5t6u7v8w9x0
Create Date: 2026-04-02 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "t6u7v8w9x0y1"
down_revision: Union[str, Sequence[str], None] = "s5t6u7v8w9x0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "idx_trials_dashboard_usage",
        "trials",
        ["org_id", "created_at", "model", "provider"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_trials_dashboard_usage", table_name="trials")
