"""add quotas table (per-user daily limit overrides)

Revision ID: add_quotas_table_001
Revises: g1h2i3j4k5l6
Create Date: 2026-07-01 00:00:00.000000

Override-only table: a row overrides the read-time ``DEFAULT_DAILY_QUOTA_USD``
for a ``(org_id, user_id)`` membership; a MISSING row means the member is
enforced at that default (default-at-read). No data seeding -- rows exist only
to override, so preview and prod both rely on the read-time default. Columns
mirror ``QuotaModel`` (backend/models.py), including the ``TimestampedMixin``
``created_at``/``updated_at``/``deleted_at``.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "add_quotas_table_001"
down_revision: Union[str, Sequence[str], None] = "g1h2i3j4k5l6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS quotas (
            id            VARCHAR(64) PRIMARY KEY,
            org_id        VARCHAR(64) NOT NULL
                          REFERENCES organizations(id) ON DELETE CASCADE,
            user_id       VARCHAR(64) NOT NULL
                          REFERENCES users(id) ON DELETE CASCADE,
            limit_usd     NUMERIC(12, 4) NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at    TIMESTAMPTZ,
            CONSTRAINT uq_quotas_org_user UNIQUE (org_id, user_id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_quotas_org_id ON quotas (org_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_quotas_user_id ON quotas (user_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quotas")
