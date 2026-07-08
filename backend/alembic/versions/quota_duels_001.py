"""add quota_duels (1v1 wrestle wagers between org members)

Revision ID: quota_duels_001
Revises: quota_gambles_001
Create Date: 2026-07-08 00:00:00.000000

A duel is an intent until accepted: no escrow while open, so expiry needs no
refund path. Accepting rolls the winner server-side and settles both stakes
as two quota_gambles rows (+wager / -wager) in the same transaction. Columns
mirror ``QuotaDuelModel`` (backend/models.py); index names must match the
model exactly for the preview-DB schema guard.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "quota_duels_001"
down_revision: Union[str, Sequence[str], None] = "quota_gambles_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS quota_duels (
            id                  VARCHAR(64) PRIMARY KEY,
            org_id              VARCHAR(64) NOT NULL
                                REFERENCES organizations(id) ON DELETE CASCADE,
            challenger_user_id  VARCHAR(64) NOT NULL
                                REFERENCES users(id) ON DELETE CASCADE,
            opponent_user_id    VARCHAR(64) NOT NULL
                                REFERENCES users(id) ON DELETE CASCADE,
            wager_usd           NUMERIC(12, 4) NOT NULL,
            status              VARCHAR(16) NOT NULL DEFAULT 'open',
            winner_user_id      VARCHAR(64),
            fight               JSONB,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at          TIMESTAMPTZ,
            CONSTRAINT ck_quota_duels_status
                CHECK (status IN ('open', 'settled', 'declined', 'cancelled')),
            CONSTRAINT ck_quota_duels_wager_positive CHECK (wager_usd > 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_quota_duels_org_challenger "
        "ON quota_duels (org_id, challenger_user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_quota_duels_org_opponent "
        "ON quota_duels (org_id, opponent_user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quota_duels")
