"""add_trial_cache_write_tokens

Adds cache_write_tokens to the trials table so Oddish can model the cost
of prompt-cache creation (billed at 1.25x input by Anthropic).

Revision ID: cachewrite_001
Revises: hv2claimidx01
Create Date: 2026-06-30 22:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "cachewrite_001"
down_revision: Union[str, Sequence[str], None] = "hv2claimidx01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS cache_write_tokens")
