"""add_experiment_description

Revision ID: expdesc_001
Revises: qa02_verdict_to_qa
Create Date: 2026-06-15 00:00:00.000000

Adds a user-authored markdown description to experiments:
- description: nullable TEXT shown in the experiment header
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "expdesc_001"
down_revision: Union[str, Sequence[str], None] = "qa02_verdict_to_qa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE experiments ADD COLUMN IF NOT EXISTS description TEXT")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS description")
