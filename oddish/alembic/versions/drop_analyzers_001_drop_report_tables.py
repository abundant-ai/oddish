"""drop the reports (analyzer) tables

Revision ID: drop_analyzers_001
Revises: reset_stale_analysis_001
Create Date: 2026-08-07 00:00:00.000000

The cross-experiment reports feature is removed. Its tables go with it:
this drops the three that remained (``analyzers``, ``analyzer_experiments``,
``analyzer_blocks``); ``analyzer_runs`` was dropped by an earlier migration.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "drop_analyzers_001"
down_revision: Union[str, Sequence[str], None] = "reset_stale_analysis_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS analyzer_experiments")
    op.execute("DROP TABLE IF EXISTS analyzer_blocks")
    op.execute("DROP TABLE IF EXISTS analyzers")


def downgrade() -> None:
    pass
