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

import sqlalchemy as sa
from alembic import op

revision: str = "analysis_log_001"
down_revision: Union[str, Sequence[str], None] = "qa_assignments_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trials", sa.Column("analysis_log", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("trials", "analysis_log")
