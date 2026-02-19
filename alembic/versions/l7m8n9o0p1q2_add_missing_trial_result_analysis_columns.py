"""add_missing_trial_result_analysis_columns

Revision ID: l7m8n9o0p1q2
Revises: k6l7m8n9o0p1
Create Date: 2026-02-19 00:20:00.000000

Backfills legacy schemas missing trial result/analysis columns.
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "l7m8n9o0p1q2"
down_revision: Union[str, Sequence[str], None] = "k6l7m8n9o0p1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS result JSONB")
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS analysis JSONB")
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS analysis_status jobstatus")
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS analysis_error TEXT")
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS analysis_started_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS analysis_finished_at TIMESTAMPTZ"
    )


def downgrade() -> None:
    """Downgrade schema.

    Non-reversible safely for mixed legacy databases.
    """
    pass
