"""move analysis jobs to dedicated queue

Revision ID: 0f1e2d3c4b5a
Revises: 74a0eab3e564
Create Date: 2026-06-05 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0f1e2d3c4b5a"
down_revision: Union[str, Sequence[str], None] = "74a0eab3e564"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


OLD_ANALYSIS_QUEUE_KEY = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
NEW_ANALYSIS_QUEUE_KEY = "analysis"


def upgrade() -> None:
    """Move non-terminal analysis jobs off the shared Haiku model queue."""
    op.execute(
        f"""
        UPDATE worker_jobs
        SET queue_key = '{NEW_ANALYSIS_QUEUE_KEY}'
        WHERE kind::text = 'ANALYSIS'
          AND status::text IN ('QUEUED', 'RETRYING', 'RUNNING')
          AND queue_key = '{OLD_ANALYSIS_QUEUE_KEY}'
        """
    )


def downgrade() -> None:
    """Restore non-terminal analysis jobs to the historical model queue."""
    op.execute(
        f"""
        UPDATE worker_jobs
        SET queue_key = '{OLD_ANALYSIS_QUEUE_KEY}'
        WHERE kind::text = 'ANALYSIS'
          AND status::text IN ('QUEUED', 'RETRYING', 'RUNNING')
          AND queue_key = '{NEW_ANALYSIS_QUEUE_KEY}'
        """
    )
