"""drop trials.experiment_id and task_experiments  --  APPLY AFTER BAKE

Final cut of the task-first migration. After this is applied:

- Trials are no longer reachable through ``trials.experiment_id`` --
  the relationship goes via ``trials.job_id -> jobs.triggered_by_experiment_id``
  for legacy lookup, or via ``experiment_cells`` joined to trials on
  ``(task_version_id, agent_equivalence_key)`` for the canonical
  evidence-pool query.
- ``task_experiments`` (task_id <-> experiment_id) is removed; the new
  selection model is ``experiment_cells`` (task_version_id keyed).

PRECONDITIONS for applying this migration safely:

1. All writers have been updated to stop populating ``trials.experiment_id``.
   Verify via:
       SELECT count(*) FROM trials
       WHERE experiment_id IS NOT NULL
         AND created_at > NOW() - INTERVAL '1 hour';
   Should be zero on a healthy deployment after the writer changes ship.

2. All readers have been switched off ``trials.experiment_id`` and
   ``task_experiments`` joins. Audit grep:
       grep -rn 'experiment_id' oddish/src/oddish backend/api
       grep -rn 'task_experiments' oddish/src/oddish backend/api
   Remaining hits should be only on the ``ExperimentCellModel`` table
   or on ``JobModel.triggered_by_experiment_id`` (cosmetic backref).

3. At least one full release has shipped between the writer turn-off
   and this migration so an operator can roll back without a
   destructive schema rewind.

Revision ID: k2f3a4b5c6d7
Revises: j1e2f3a4b5c6
Create Date: 2026-05-01 01:45:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "k2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "j1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_trials_experiment_task_version")
    op.execute("DROP INDEX IF EXISTS idx_trials_org_experiment_created_at")
    op.execute(
        "ALTER TABLE trials DROP CONSTRAINT IF EXISTS trials_experiment_id_fkey"
    )
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS experiment_id")
    op.execute("DROP TABLE IF EXISTS task_experiments")


def downgrade() -> None:
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
        ALTER TABLE trials
            ADD COLUMN IF NOT EXISTS experiment_id VARCHAR(64)
                REFERENCES experiments(id) ON DELETE CASCADE
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_trials_org_experiment_created_at
            ON trials (org_id, experiment_id, created_at)
        """
    )
