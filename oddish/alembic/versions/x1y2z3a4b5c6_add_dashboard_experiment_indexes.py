"""add_dashboard_experiment_indexes

Revision ID: x1y2z3a4b5c6
Revises: w9x0y1z2a3b4
Create Date: 2026-04-13 17:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "x1y2z3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "w9x0y1z2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "idx_tasks_org_experiment_created_at",
        "tasks",
        ["org_id", "experiment_id", "created_at"],
        unique=False, if_not_exists=True)
    op.create_index(
        "idx_trials_org_experiment_created_at",
        "trials",
        ["org_id", "experiment_id", "created_at"],
        unique=False, if_not_exists=True)


def downgrade() -> None:
    op.drop_index("idx_trials_org_experiment_created_at", table_name="trials")
    op.drop_index("idx_tasks_org_experiment_created_at", table_name="tasks")
