"""add_run_analysis_to_tasks

Revision ID: a1b2c3d4e5f6
Revises: 8b9c0d1e2f3a
Create Date: 2026-01-24 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "9c1d2e3f4a5b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add run_analysis column to tasks table (default false)
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS run_analysis BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS run_analysis")
