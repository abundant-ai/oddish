"""merge worker_jobs provider/external_id and task_link heads

Revision ID: 74a0eab3e564
Revises: 8f530ca04743, a0b1c2d3e4f5
Create Date: 2026-06-03 19:59:51.829483

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '74a0eab3e564'
down_revision: Union[str, Sequence[str], None] = ('8f530ca04743', 'a0b1c2d3e4f5')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
