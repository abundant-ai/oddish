"""add_provider_slots_table

Revision ID: i5j6k7l8m9n0
Revises: h4i5j6k7l8m9
Create Date: 2026-01-30 12:15:00.000000

Creates provider_slots table for worker concurrency leases.
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "i5j6k7l8m9n0"
down_revision: Union[str, Sequence[str], None] = "h4i5j6k7l8m9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_slots (
            provider TEXT NOT NULL,
            slot INTEGER NOT NULL,
            locked_by TEXT,
            locked_until TIMESTAMPTZ,
            PRIMARY KEY (provider, slot)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_provider_slots_provider_locked_until
        ON provider_slots (provider, locked_until)
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_provider_slots_provider_locked_until")
    op.execute("DROP TABLE IF EXISTS provider_slots")
