"""add_token_timing_trajectory_columns

Revision ID: i4j5k6l7m8n9
Revises: h3i4j5k6l7m8
Create Date: 2026-02-17 12:00:00.000000

Adds columns to trials for Harbor AgentContext (token usage & cost),
per-phase timing breakdown, and ATIF trajectory detection.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "i4j5k6l7m8n9"
down_revision: Union[str, Sequence[str], None] = "h3i4j5k6l7m8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS input_tokens INTEGER")
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS cache_tokens INTEGER")
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS output_tokens INTEGER")
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION")
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS phase_timing JSONB")
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS has_trajectory BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS has_trajectory")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS phase_timing")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS cost_usd")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS output_tokens")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS cache_tokens")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS input_tokens")
