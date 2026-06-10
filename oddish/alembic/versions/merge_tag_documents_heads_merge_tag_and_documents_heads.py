"""merge tag and documents heads

Revision ID: merge_tag_documents_heads
Revises: aa04ta05sweep, documents_001
Create Date: 2026-06-10 05:39:08.917461

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'merge_tag_documents_heads'
down_revision: Union[str, Sequence[str], None] = ('aa04ta05sweep', 'documents_001')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
