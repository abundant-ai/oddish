"""add user attribution_cache

Revision ID: m9n0p1q2r3s4
Revises: l8m9n0p1q2r3
Create Date: 2026-06-10 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "m9n0p1q2r3s4"
down_revision: Union[str, Sequence[str], None] = "l8m9n0p1q2r3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS attribution_cache JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS attribution_cache")
