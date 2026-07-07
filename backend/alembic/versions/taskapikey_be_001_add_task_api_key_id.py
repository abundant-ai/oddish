"""add tasks.api_key_id

Revision ID: taskapikey_be_001
Revises: u7v8w9x0y1z2
Create Date: 2026-07-06 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "taskapikey_be_001"
down_revision: Union[str, Sequence[str], None] = "u7v8w9x0y1z2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS api_key_id VARCHAR(64)")


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS api_key_id")
