"""add_harbor_stage_to_trials

Revision ID: 7270853bccfe
Revises: 26682cabe1a2
Create Date: 2026-01-14 13:57:03.472615

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "7270853bccfe"
down_revision: Union[str, Sequence[str], None] = "26682cabe1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "ALTER TABLE trials " "ADD COLUMN IF NOT EXISTS harbor_stage VARCHAR(32)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS harbor_stage")
