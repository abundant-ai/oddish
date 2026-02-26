"""add_clerk_org_id

Revision ID: d1e2f3a4b5c6
Revises: c4d5e6f7a8b9
Create Date: 2026-01-21 01:20:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add clerk_org_id column (idempotent)
    op.execute(
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS clerk_org_id VARCHAR(64)"
    )
    # Unique index for clerk_org_id (idempotent)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_organizations_clerk_org_id "
        "ON organizations (clerk_org_id)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_organizations_clerk_org_id")
    op.execute("ALTER TABLE organizations DROP COLUMN IF EXISTS clerk_org_id")
