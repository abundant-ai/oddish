"""drop_unique_clerk_user_id

Revision ID: h4i5j6k7l8m9
Revises: g3h4i5j6k7l8
Create Date: 2026-01-27 11:15:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "h4i5j6k7l8m9"
down_revision: Union[str, Sequence[str], None] = "g3h4i5j6k7l8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_users_clerk_user_id")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_clerk_user_id " "ON users (clerk_user_id)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_users_clerk_user_id")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_clerk_user_id "
        "ON users (clerk_user_id)"
    )
