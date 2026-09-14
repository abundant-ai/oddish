"""Require explicit operator approval for hosted organizations."""

from alembic import op

revision = "org_execution_001"
down_revision = "boundanalysis160"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS execution_enabled BOOLEAN NOT NULL DEFAULT false"
    )
    # Reviewed existing workspaces, identified in Clerk on 2026-09-08. Names
    # are not authority: several unrelated organizations are named Abundant.
    op.execute("""
        UPDATE organizations SET execution_enabled = true
        WHERE is_active AND deleted_at IS NULL AND clerk_org_id IN (
            'org_39ufkEqie8rLlVhoK4YMm4IMx0L', -- Abundant
            'org_3H67wVrUZObfjW9JnxGq5pUZQvN', -- Abundant CyberMasters
            'org_3IVmVHXFyfF4ltX8bfQMQTHrH1y'  -- Oddish-onsite
        )
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE organizations DROP COLUMN execution_enabled")
