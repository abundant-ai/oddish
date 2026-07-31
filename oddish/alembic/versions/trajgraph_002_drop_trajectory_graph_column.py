"""drop_trajectory_graph_column

Revision ID: trajgraph_002
Revises: qa_assignments_001
Create Date: 2026-07-31 12:00:00.000000

The Agent Graph feature (PR #645) is removed; nothing reads or writes
``trials.trajectory_graph`` anymore. Drop the column. ``trajgraph_001``
stays in the chain (later revisions descend from it) — this revision
undoes its effect rather than rewriting history.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "trajgraph_002"
down_revision: Union[str, Sequence[str], None] = "qa_assignments_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS trajectory_graph")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS trajectory_graph JSONB"
    )
