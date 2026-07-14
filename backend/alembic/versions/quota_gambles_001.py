"""add quota_gambles ledger + quota_gamble_sessions (casino wagers vs 24h quota)

Revision ID: quota_gambles_001
Revises: org_quotas_monthly_001
Create Date: 2026-07-07 00:00:00.000000

``quota_gambles`` is the settled ledger: ``net_usd`` (wager * (multiplier - 1))
is summed over the rolling 24h window and folded into ``get_effective_limit``.
``quota_gamble_sessions`` holds in-flight multi-step hands (blackjack); the
ledger row is inserted as a -wager escrow at deal and updated at settle, so an
abandoned hand stays a loss. Columns mirror ``QuotaGambleModel`` /
``QuotaGambleSessionModel`` (backend/models.py), including the
``TimestampedMixin`` timestamps. Index names must stay ``ix_``-prefixed exactly
as in the models -- the preview-DB guard diffs model vs migrated schema by
index name.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "quota_gambles_001"
down_revision: Union[str, Sequence[str], None] = "slack_expense_alerts_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS quota_gambles (
            id            VARCHAR(64) PRIMARY KEY,
            org_id        VARCHAR(64) NOT NULL
                          REFERENCES organizations(id) ON DELETE CASCADE,
            user_id       VARCHAR(64) NOT NULL
                          REFERENCES users(id) ON DELETE CASCADE,
            game          VARCHAR(32) NOT NULL DEFAULT 'coinflip',
            wager_usd     NUMERIC(12, 4) NOT NULL,
            net_usd       NUMERIC(12, 4) NOT NULL,
            multiplier    NUMERIC(12, 4),
            detail        JSONB,
            won           BOOLEAN NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at    TIMESTAMPTZ,
            CONSTRAINT ck_quota_gambles_wager_positive CHECK (wager_usd > 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_quota_gambles_org_user_created "
        "ON quota_gambles (org_id, user_id, created_at)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS quota_gamble_sessions (
            id            VARCHAR(64) PRIMARY KEY,
            org_id        VARCHAR(64) NOT NULL
                          REFERENCES organizations(id) ON DELETE CASCADE,
            user_id       VARCHAR(64) NOT NULL
                          REFERENCES users(id) ON DELETE CASCADE,
            game          VARCHAR(32) NOT NULL,
            wager_usd     NUMERIC(12, 4) NOT NULL,
            status        VARCHAR(16) NOT NULL DEFAULT 'active',
            state         JSONB NOT NULL,
            gamble_id     VARCHAR(64),
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at    TIMESTAMPTZ,
            CONSTRAINT ck_quota_gamble_sessions_status
                CHECK (status IN ('active', 'settled'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_quota_gamble_sessions_org_user "
        "ON quota_gamble_sessions (org_id, user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quota_gamble_sessions")
    op.execute("DROP TABLE IF EXISTS quota_gambles")
