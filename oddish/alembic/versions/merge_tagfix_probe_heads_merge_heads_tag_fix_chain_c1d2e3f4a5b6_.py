"""merge heads: tag-fix chain (c1d2e3f4a5b6) + probe_presets_001

Revision ID: merge_tagfix_probe_heads
Revises: c1d2e3f4a5b6, probe_presets_001
Create Date: 2026-06-09 01:57:32.276677

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'merge_tagfix_probe_heads'
down_revision: Union[str, Sequence[str], None] = ('c1d2e3f4a5b6', 'probe_presets_001')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
