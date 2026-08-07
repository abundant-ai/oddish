"""clear in-flight analysis flags left by the removed pipeline

Revision ID: reset_stale_analysis_001
Revises: drop_legacy_jobs_001
Create Date: 2026-08-07 00:00:00.000000

Nothing sets analysis_status to PENDING/QUEUED/RUNNING anymore -- the QA
trial import writes SUCCESS or FAILED directly. Rows stuck on an
in-flight value came from the removed per-trial pipeline and read as
"analysis running" in the UI forever. Clear them back to "never
analyzed" so the next QA run classifies them.

Idempotent: the WHERE clause only matches stuck rows.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "reset_stale_analysis_001"
down_revision: Union[str, Sequence[str], None] = "drop_legacy_jobs_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE trials
        SET    analysis_status = NULL,
               analysis_error = NULL,
               analysis_started_at = NULL,
               analysis_finished_at = NULL
        WHERE  analysis_status::text IN ('PENDING', 'QUEUED', 'RUNNING')
        """
    )


def downgrade() -> None:
    pass
