"""add_cloud_ready_columns

Add cloud-ready columns to tasks table:
- org_id: Organization ID for multi-tenancy (nullable string)
- created_by_user_id: User who created the task (nullable string)

These columns have no FK constraints in OSS. Cloud deployments add FK
constraints via a separate migration after creating the auth tables.

Revision ID: b2c3d4e5f6a7
Revises: 3e83b4d6a123
Create Date: 2026-01-17 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "3e83b4d6a123"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add org_id column (nullable string, no FK in OSS)
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS org_id VARCHAR(64)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_tasks_org_id ON tasks (org_id)")

    # Add created_by_user_id column (nullable string, no FK in OSS)
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS created_by_user_id VARCHAR(64)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_tasks_org_id")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS org_id")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS created_by_user_id")
