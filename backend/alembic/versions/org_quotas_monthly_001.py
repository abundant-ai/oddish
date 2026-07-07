"""add org_quotas table (org-level aggregate MONTHLY cap overrides)

Revision ID: org_quotas_monthly_001
Revises: taskapikey_be_001
Create Date: 2026-07-06 00:00:00.000000

Override-only table: a row overrides the read-time
``default_org_monthly_quota_usd`` (``None`` = no org cap by default) for an org;
a MISSING row means the org is capped at that default. No data seeding. Columns
mirror ``OrgQuotaModel`` (backend/models.py), including the ``TimestampedMixin``
``created_at``/``updated_at``/``deleted_at``. The unique index is PARTIAL
(``WHERE deleted_at IS NULL``) so only one LIVE row exists per org. Deliberately
a separate table from ``quotas``: a NULL ``user_id`` in ``quotas`` would not
collide under its ``(org_id, user_id)`` unique index (NULLs distinct in PG).

Robust to a dev/preview DB where an EARLIER DAILY version of this table already
exists (the unmerged ``add_org_quotas_001`` shipped ``period_kind = 'daily'``):
create-if-not-exists, then idempotently flip the default, drop/re-add the CHECK
as monthly, rewrite any stale ``'daily'`` rows, and ensure the partial unique
index -- so the migration converges on the monthly shape regardless of prior
state.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "org_quotas_monthly_001"
down_revision: Union[str, Sequence[str], None] = "taskapikey_be_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS org_quotas (
            id            VARCHAR(64) PRIMARY KEY,
            org_id        VARCHAR(64) NOT NULL
                          REFERENCES organizations(id) ON DELETE CASCADE,
            limit_usd     NUMERIC(12, 4) NOT NULL,
            period_kind   VARCHAR(16) NOT NULL DEFAULT 'monthly',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at    TIMESTAMPTZ,
            CONSTRAINT ck_org_quotas_period_kind CHECK (period_kind IN ('monthly'))
        )
        """
    )
    # Converge a pre-existing DAILY table (from the unmerged branch) onto the
    # monthly shape. Each step is idempotent, so a fresh CREATE above is a no-op
    # here and an already-daily table is rewritten in place.
    op.execute(
        "ALTER TABLE org_quotas ALTER COLUMN period_kind SET DEFAULT 'monthly'"
    )
    op.execute(
        "ALTER TABLE org_quotas DROP CONSTRAINT IF EXISTS ck_org_quotas_period_kind"
    )
    op.execute("UPDATE org_quotas SET period_kind = 'monthly' WHERE period_kind = 'daily'")
    op.execute(
        "ALTER TABLE org_quotas ADD CONSTRAINT ck_org_quotas_period_kind "
        "CHECK (period_kind IN ('monthly'))"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_org_quotas_org "
        "ON org_quotas (org_id) WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS org_quotas")
