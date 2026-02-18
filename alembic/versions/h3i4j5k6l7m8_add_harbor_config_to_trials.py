"""add_harbor_config_to_trials

Revision ID: h3i4j5k6l7m8
Revises: g2h3i4j5k6l7
Create Date: 2026-02-17 00:00:00.000000

Adds a JSONB column for Harbor passthrough config (agent env/kwargs,
verifier settings, environment resource overrides).
"""

from typing import Sequence, Union

from alembic import op


revision: str = "h3i4j5k6l7m8"
down_revision: Union[str, Sequence[str], None] = "g2h3i4j5k6l7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS harbor_config JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS harbor_config")
