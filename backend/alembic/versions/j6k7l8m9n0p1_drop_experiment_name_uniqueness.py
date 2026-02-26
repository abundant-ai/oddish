"""drop_experiment_name_uniqueness

Revision ID: j6k7l8m9n0p1
Revises: i5j6k7l8m9n0
Create Date: 2026-01-30 13:00:00.000000

Drop unique constraint for experiments (org_id, name).
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "j6k7l8m9n0p1"
down_revision: Union[str, Sequence[str], None] = "i5j6k7l8m9n0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_experiments_org_name")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_experiments_org_name "
        "ON experiments (org_id, name)"
    )
