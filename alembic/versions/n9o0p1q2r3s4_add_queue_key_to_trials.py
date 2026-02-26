"""add_queue_key_to_trials

Revision ID: n9o0p1q2r3s4
Revises: m8n9o0p1q2r3
Create Date: 2026-02-25 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "n9o0p1q2r3s4"
down_revision: Union[str, Sequence[str], None] = "m8n9o0p1q2r3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS queue_key VARCHAR(128)")
    op.execute(
        """
        UPDATE trials
        SET queue_key = COALESCE(NULLIF(model, ''), provider, 'default')
        WHERE queue_key IS NULL
        """
    )
    op.execute("ALTER TABLE trials ALTER COLUMN queue_key SET NOT NULL")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trials_org_queue_key_status "
        "ON trials (org_id, queue_key, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trials_claimable "
        "ON trials (status, queue_key, next_retry_at)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS idx_trials_org_queue_key_status")
    op.execute("DROP INDEX IF EXISTS idx_trials_claimable")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS queue_key")
