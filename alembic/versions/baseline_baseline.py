"""baseline

Revision ID: baseline
Revises:
Create Date: 2026-01-13 15:44:27.378195

"""

from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = "baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
