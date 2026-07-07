"""add quota_gambles table (double-or-nothing wagers against the 24h quota)

Revision ID: quota_gambles_001
Revises: org_quotas_monthly_001
Create Date: 2026-07-07 00:00:00.000000

Append-only ledger: ``net_usd`` (+wager on a win, -wager on a loss) is summed
over the rolling 24h window and folded into ``get_effective_limit``. Columns
mirror ``QuotaGambleModel`` (backend/models.py), including the
``TimestampedMixin`` ``created_at``/``updated_at``/``deleted_at``. The index
name must stay ``ix_quota_gambles_org_user_created`` -- the preview-DB guard
diffs model vs migrated schema by index name.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "quota_gambles_001"
down_revision: Union[str, Sequence[str], None] = "org_quotas_monthly_001"
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
            wager_usd     NUMERIC(12, 4) NOT NULL,
            net_usd       NUMERIC(12, 4) NOT NULL,
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


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quota_gambles")
