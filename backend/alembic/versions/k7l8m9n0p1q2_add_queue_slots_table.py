"""add_queue_slots_table

Revision ID: k7l8m9n0p1q2
Revises: j6k7l8m9n0p1
Create Date: 2026-02-25 10:15:00.000000
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "k7l8m9n0p1q2"
down_revision: Union[str, Sequence[str], None] = "j6k7l8m9n0p1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Backend no longer owns queue_slots; retained for revision continuity."""
    return None


def downgrade() -> None:
    """Backend no longer owns queue_slots; retained for revision continuity."""
    return None
