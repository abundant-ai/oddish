"""add_queue_slots_table

Revision ID: k7l8m9n0p1q2
Revises: j6k7l8m9n0p1
Create Date: 2026-02-25 10:15:00.000000
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "k7l8m9n0p1q2"
down_revision: Union[str, Sequence[str], None] = "j6k7l8m9n0p1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS queue_slots (
            queue_key TEXT NOT NULL,
            slot INTEGER NOT NULL,
            locked_by TEXT,
            locked_until TIMESTAMPTZ,
            PRIMARY KEY (queue_key, slot)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_queue_slots_queue_key_locked_until
        ON queue_slots (queue_key, locked_until)
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_queue_slots_queue_key_locked_until")
    op.execute("DROP TABLE IF EXISTS queue_slots")
