"""merge tag and mine-filter heads

Revision ID: merge_tag_mine_filter_heads
Revises: merge_tag_documents_heads, mine_filter_idx_001
Create Date: 2026-06-11 04:37:56.940807

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'merge_tag_mine_filter_heads'
down_revision: Union[str, Sequence[str], None] = ('merge_tag_documents_heads', 'mine_filter_idx_001')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
