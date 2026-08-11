"""add per-API-key spend limits and trial attribution

Revision ID: apikeylimit_be_001
Revises: dropchat001
Create Date: 2026-08-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "apikeylimit_be_001"
down_revision: str | Sequence[str] | None = "dropchat001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE IF EXISTS api_keys "
        "ADD COLUMN IF NOT EXISTS limit_usd NUMERIC(12, 4)"
    )
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS api_key_id VARCHAR(64)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_trials_api_key_id ON trials (api_key_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_trials_api_key_id")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS api_key_id")
    op.execute("ALTER TABLE IF EXISTS api_keys DROP COLUMN IF EXISTS limit_usd")
