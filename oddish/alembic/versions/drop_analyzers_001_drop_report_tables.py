"""drop the reports (analyzer) tables

Revision ID: drop_analyzers_001
Revises: reset_stale_analysis_001
Create Date: 2026-08-07 00:00:00.000000

The cross-experiment reports feature is removed. Its tables go with it.
``analyzer_runs`` and the block tables were dropped by earlier
migrations; this drops the two that remained.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "drop_analyzers_001"
down_revision: Union[str, Sequence[str], None] = "reset_stale_analysis_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ``analyzer_blocks`` is deliberately NOT dropped. The reports feature it
    # was built for is gone, but the cohort comparison shipped since (backend/
    # api/services/cohort_comparison.py) stores its computed comparison in this
    # table, and reads legacy trajectory-summary rows as a fallback behind the
    # trials.trajectory_summary mirror. Dropping it would delete a live
    # feature's storage; the table is otherwise inert once reports are gone.
    op.execute("DROP TABLE IF EXISTS analyzer_experiments")
    op.execute("DROP TABLE IF EXISTS analyzers")


def downgrade() -> None:
    pass
