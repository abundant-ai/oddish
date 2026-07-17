"""add idx_trials_live_task_version on trials

Revision ID: 59f44b40b582
Revises: analyzers_007
Create Date: 2026-07-17 19:44:22.343459

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '59f44b40b582'
down_revision: Union[str, Sequence[str], None] = 'analyzers_007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
