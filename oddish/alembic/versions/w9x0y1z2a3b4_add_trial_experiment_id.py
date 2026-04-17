"""add experiment_id to trials

Revision ID: w9x0y1z2a3b4
Revises: v8w9x0y1z2a3
Create Date: 2026-04-07 01:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "w9x0y1z2a3b4"
down_revision: Union[str, Sequence[str], None] = "v8w9x0y1z2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trials",
        sa.Column("experiment_id", sa.String(64), nullable=True),
    )
    op.create_foreign_key(
        "fk_trials_experiment_id",
        "trials",
        "experiments",
        ["experiment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_trials_experiment_id", "trials", ["experiment_id"])

    # Backfill: copy experiment_id from task -> trial for all existing trials.
    op.execute(
        """
        UPDATE trials t
        SET experiment_id = tk.experiment_id
        FROM tasks tk
        WHERE t.task_id = tk.id
          AND t.experiment_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("idx_trials_experiment_id", table_name="trials")
    op.drop_constraint("fk_trials_experiment_id", "trials", type_="foreignkey")
    op.drop_column("trials", "experiment_id")
