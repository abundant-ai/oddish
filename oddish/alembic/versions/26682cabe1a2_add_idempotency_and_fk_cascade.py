"""add_idempotency_and_fk_cascade

Revision ID: 26682cabe1a2
Revises: 585cf6a154a3
Create Date: 2026-01-13 23:50:44.422085

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "26682cabe1a2"
down_revision: Union[str, Sequence[str], None] = "585cf6a154a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add idempotency_key column to trials table (idempotent for local/dev resets)
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(64)"
    )

    # Create unique index on idempotency_key (idempotent)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_trials_idempotency_key "
        "ON trials (idempotency_key)"
    )

    # Ensure FK uses ON DELETE CASCADE (drop/recreate is idempotent)
    op.execute("ALTER TABLE trials DROP CONSTRAINT IF EXISTS trials_task_id_fkey")
    op.execute(
        "ALTER TABLE trials "
        "ADD CONSTRAINT trials_task_id_fkey "
        "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Recreate original foreign key without CASCADE
    op.execute("ALTER TABLE trials DROP CONSTRAINT IF EXISTS trials_task_id_fkey")
    op.execute(
        "ALTER TABLE trials "
        "ADD CONSTRAINT trials_task_id_fkey "
        "FOREIGN KEY (task_id) REFERENCES tasks(id)"
    )

    # Drop idempotency_key index and column (idempotent)
    op.execute("DROP INDEX IF EXISTS idx_trials_idempotency_key")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS idempotency_key")
