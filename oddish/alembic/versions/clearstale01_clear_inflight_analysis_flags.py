"""Clear in-flight per-trial analysis flags left by the removed pipeline.

Revision ID: clearstale01
Revises: retirejobs01
Create Date: 2026-08-18

Nothing sets analysis_status to PENDING/QUEUED/RUNNING anymore -- the QA
trial's importer writes SUCCESS or FAILED directly. Rows stuck on an
in-flight value came from the removed per-trial pipeline and would read as
"analysis running" in the UI forever. Clear them back to "never analyzed"
so the next QA run classifies them.

Idempotent: the WHERE clause only matches stuck rows, and a re-run matches
nothing. On a fresh database the table is empty and this is a no-op.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "clearstale01"
down_revision: Union[str, Sequence[str], None] = "retirejobs01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '8s'")
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
    # The cleared in-flight states described jobs that no longer exist;
    # there is nothing meaningful to restore.
    pass
