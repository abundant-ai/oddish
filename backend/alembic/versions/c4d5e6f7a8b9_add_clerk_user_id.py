"""add_clerk_user_id

Revision ID: c4d5e6f7a8b9
Revises: a1b2c3d4e5f6
Create Date: 2026-01-20 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add clerk_user_id column (idempotent)
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS clerk_user_id VARCHAR(64)")
    # Unique index for clerk_user_id (idempotent)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_clerk_user_id "
        "ON users (clerk_user_id)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_users_clerk_user_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS clerk_user_id")
