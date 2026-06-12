"""add user settings

Revision ID: o1p2q3r4s5t6
Revises: n0p1q2r3s4t5
Create Date: 2026-06-12 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "o1p2q3r4s5t6"
down_revision: Union[str, Sequence[str], None] = "n0p1q2r3s4t5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS settings JSONB "
        "NOT NULL DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS settings")
