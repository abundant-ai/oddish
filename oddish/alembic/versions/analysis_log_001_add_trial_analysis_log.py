"""add trials.analysis_log

The analyzer's live event log for the trial's current/most recent
analysis run. The QA worker updates it every few seconds so the UI can
show what the analyzer is doing instead of a bare "Analyzing" state.
One short line per event, so it stays small.

Revision ID: analysis_log_001
Revises: qa_assignments_001
Create Date: 2026-07-30 08:10:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "analysis_log_001"
down_revision: Union[str, Sequence[str], None] = "qa_assignments_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # IF NOT EXISTS: 000_initial_schema creates this column on fresh DBs
    # via create_all() against the live models.
    op.execute("ALTER TABLE trials ADD COLUMN IF NOT EXISTS analysis_log TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS analysis_log")
