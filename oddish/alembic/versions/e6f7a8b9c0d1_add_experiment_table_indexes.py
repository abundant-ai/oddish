"""add experiment table indexes

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-04-24 17:20:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "idx_task_experiments_experiment_task",
        "task_experiments",
        ["experiment_id", "task_id"],
        unique=False,
    )
    op.create_index(
        "idx_trials_experiment_task_version",
        "trials",
        ["experiment_id", "task_id", "task_version_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_trials_experiment_task_version", table_name="trials")
    op.drop_index("idx_task_experiments_experiment_task", table_name="task_experiments")
