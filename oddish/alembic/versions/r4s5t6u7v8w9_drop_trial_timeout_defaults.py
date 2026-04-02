"""drop_trial_timeout_defaults

Revision ID: r4s5t6u7v8w9
Revises: q2r3s4t5u6v7
Create Date: 2026-04-01 20:15:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "r4s5t6u7v8w9"
down_revision: Union[str, Sequence[str], None] = "r3s4t5u6v7w8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE trials ALTER COLUMN timeout_minutes DROP DEFAULT")
    op.execute("ALTER TABLE trials ALTER COLUMN timeout_minutes DROP NOT NULL")


def downgrade() -> None:
    op.execute("UPDATE trials SET timeout_minutes = 60 WHERE timeout_minutes IS NULL")
    op.execute("ALTER TABLE trials ALTER COLUMN timeout_minutes SET DEFAULT 60")
    op.execute("ALTER TABLE trials ALTER COLUMN timeout_minutes SET NOT NULL")
