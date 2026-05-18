"""soft_delete_task_experiments

Revision ID: k2l3m4n5o6p7
Revises: 637c45e5ac80
Create Date: 2026-05-16 12:00:00.000000

Adds a tombstone column to the task/experiment membership table so deleting
an experiment or scoped task membership does not physically remove the
association history.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "k2l3m4n5o6p7"
down_revision: Union[str, Sequence[str], None] = "637c45e5ac80"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE task_experiments "
        "ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ"
    )

    with op.get_context().autocommit_block():
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_task_experiments_live_experiment_task
            ON task_experiments (experiment_id, task_id)
            WHERE deleted_at IS NULL
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                idx_task_experiments_live_task_experiment
            ON task_experiments (task_id, experiment_id)
            WHERE deleted_at IS NULL
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "idx_task_experiments_live_task_experiment"
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "idx_task_experiments_live_experiment_task"
        )

    op.execute("ALTER TABLE task_experiments DROP COLUMN IF EXISTS deleted_at")
