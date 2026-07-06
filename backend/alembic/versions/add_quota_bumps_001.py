"""add quota_bumps table (temporary additive per-user limit boosts)

Revision ID: add_quota_bumps_001
Revises: apk_role_backend_001
Create Date: 2026-07-06 00:00:00.000000

Additive, time-boxed boosts on top of a member's base 24h limit (default or
``quotas`` override). Each row grants ``+amount_usd`` until ``expires_at``;
expiry is resolved at READ time (``expires_at > NOW()``) so there is no
scheduler or revert job. Grants stack (SUM of live rows); revocation stamps
``revoked_at`` and the audit row survives. Columns mirror ``QuotaBumpModel``
(backend/models.py), including the ``TimestampedMixin``
``created_at``/``updated_at``/``deleted_at``. DDL only, no data seeding.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "add_quota_bumps_001"
down_revision: Union[str, Sequence[str], None] = "apk_role_backend_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS quota_bumps (
            id                 VARCHAR(64) PRIMARY KEY,
            org_id             VARCHAR(64) NOT NULL
                               REFERENCES organizations(id) ON DELETE CASCADE,
            user_id            VARCHAR(64) NOT NULL
                               REFERENCES users(id) ON DELETE CASCADE,
            amount_usd         NUMERIC(12, 4) NOT NULL
                               CONSTRAINT ck_quota_bumps_amount_positive CHECK (amount_usd > 0),
            expires_at         TIMESTAMPTZ NOT NULL,
            reason             TEXT,
            granted_by_user_id VARCHAR(64) REFERENCES users(id) ON DELETE SET NULL,
            revoked_at         TIMESTAMPTZ,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at         TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_quota_bumps_org_user_expires
            ON quota_bumps (org_id, user_id, expires_at)
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_quota_bumps_user_id ON quota_bumps (user_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quota_bumps")
