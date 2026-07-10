"""add_trajectory_graph_column

Revision ID: trajgraph_001
Revises: trial_events_001
Create Date: 2026-07-09 12:00:00.000000

Adds a JSONB column on trials to store the condensed agent step-graph
(general phases + terminal outcome node), populated on explicit request to
POST /trials/{id}/trajectory/graph.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "trajgraph_001"
down_revision: Union[str, Sequence[str], None] = "trial_events_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS trajectory_graph JSONB"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS trajectory_graph")
