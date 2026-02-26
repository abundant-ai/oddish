"""add_github_username

Revision ID: g3h4i5j6k7l8
Revises: f2e3d4c5b6a7
Create Date: 2026-01-27 10:15:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "g3h4i5j6k7l8"
down_revision: Union[str, Sequence[str], None] = "f2e3d4c5b6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS github_username VARCHAR(255)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_github_username "
        "ON users (github_username)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_users_github_username")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS github_username")
