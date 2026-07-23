"""merge prompt-registry and concurrency-override heads

Revision ID: prompts_merge_001
Revises: concurrency_override_001, prompts_002
Create Date: 2026-07-22 11:24:41.899774

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'prompts_merge_001'
down_revision: Union[str, Sequence[str], None] = ('concurrency_override_001', 'prompts_002')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
