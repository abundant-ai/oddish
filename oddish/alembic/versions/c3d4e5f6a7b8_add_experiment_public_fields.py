"""add_experiment_public_fields

Revision ID: c3d4e5f6a7b8
Revises: a1b2c3d4e5f6
Create Date: 2026-01-25 12:00:00.000000

Adds public sharing fields to experiments:
- is_public: boolean flag indicating public visibility
- public_token: shareable token for public access
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "ALTER TABLE experiments ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE experiments ADD COLUMN IF NOT EXISTS public_token VARCHAR(128)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_experiments_public_token "
        "ON experiments (public_token)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_experiments_public_token")
    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS public_token")
    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS is_public")
