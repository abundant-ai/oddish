"""restore trials.experiment_id and task_experiments if missing

Corrective migration. The earlier
``k2f3a4b5c6d7_drop_trial_experiment_id_AFTER_BAKE`` revision was
authored as a "shelf" migration meant to be applied manually after a
bake period -- but alembic does not honour filename intent, and the
revision sat on the chain head. As a result, every preview deploy ran
``alembic upgrade head`` and dropped ``trials.experiment_id`` and the
``task_experiments`` association, breaking every read path that joins
either (the dashboard endpoint among them).

This migration:

1. Recreates ``trials.experiment_id`` (nullable, FK to experiments) if
   missing, plus the indexes that rode along with it.
2. Recreates the ``task_experiments`` association table if missing,
   with its indexes.

Both operations are idempotent (``IF NOT EXISTS``) so applying this on
a healthy DB (where k2 never ran) is a no-op. The k2 file has been
removed from the chain in the same commit; new deploys will never run
the destructive drop. This migration patches the existing preview DBs
that already saw k2.

A future, properly-coordinated drop will live in its own migration
gated on operator confirmation -- not on the chain.

Revision ID: m4b5c6d7e8f9
Revises: l3a4b5c6d7e8
Create Date: 2026-05-01 02:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "m4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "l3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. trials.experiment_id (nullable FK)
    op.execute(
        """
        ALTER TABLE trials
            ADD COLUMN IF NOT EXISTS experiment_id VARCHAR(64)
                REFERENCES experiments(id) ON DELETE CASCADE
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trials_experiment_id
            ON trials (experiment_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trials_org_experiment_created_at
            ON trials (org_id, experiment_id, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trials_experiment_task_version
            ON trials (experiment_id, task_id, task_version_id)
        """
    )

    # 2. task_experiments association
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS task_experiments (
            task_id VARCHAR(64) NOT NULL
                REFERENCES tasks(id) ON DELETE CASCADE,
            experiment_id VARCHAR(64) NOT NULL
                REFERENCES experiments(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (task_id, experiment_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_task_experiments_experiment_id
            ON task_experiments (experiment_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_task_experiments_experiment_task
            ON task_experiments (experiment_id, task_id)
        """
    )


def downgrade() -> None:
    # No-op: the target state of this migration is "experiment_id and
    # task_experiments exist", which matches every healthy DB. There is
    # nothing safe to undo.
    pass
