"""add_tasks_org_created_at_index

Revision ID: 8b9c0d1e2f3a
Revises: b2c3d4e5f6a7
Create Date: 2026-01-22 16:10:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "8b9c0d1e2f3a"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_org_created_at "
        "ON tasks (org_id, created_at DESC)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_tasks_org_created_at")
