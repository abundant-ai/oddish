"""task-experiment many-to-many

Replaces the single ``tasks.experiment_id`` column with a ``task_experiments``
join table so a task can belong to many experiments simultaneously. Trials
stay 1:1 with an experiment, and we take this opportunity to tighten
``trials.experiment_id`` to ``NOT NULL``.

Changes:
- Create ``task_experiments(task_id, experiment_id)`` with composite PK.
- Backfill from existing data:
  - ``(task_id, task.experiment_id)`` for every task that had one.
  - ``DISTINCT (task_id, trial.experiment_id)`` for every trial that had one.
- Backfill legacy NULL ``trials.experiment_id`` from the parent task's
  ``experiment_id``.
- Make ``trials.experiment_id`` ``NOT NULL``.
- Drop ``tasks.experiment_id`` and its indexes.

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-04-20 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "e8f9a0b1c2d3"
down_revision: Union[str, Sequence[str], None] = "d7e8f9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================================================================
    # 1. Create the task_experiments join table
    # =========================================================================
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS task_experiments (
            task_id VARCHAR(64) NOT NULL
                REFERENCES tasks(id) ON DELETE CASCADE,
            experiment_id VARCHAR(64) NOT NULL
                REFERENCES experiments(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (task_id, experiment_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_experiments_experiment_id "
        "ON task_experiments (experiment_id)"
    )

    # =========================================================================
    # 2. Backfill join rows from the legacy single-FK model
    # =========================================================================
    # 2a. Every task's "home" experiment.
    op.execute(
        """
        INSERT INTO task_experiments (task_id, experiment_id)
        SELECT id, experiment_id
        FROM tasks
        WHERE experiment_id IS NOT NULL
        ON CONFLICT (task_id, experiment_id) DO NOTHING
        """
    )

    # 2b. Every experiment any of a task's trials ran under.
    op.execute(
        """
        INSERT INTO task_experiments (task_id, experiment_id)
        SELECT DISTINCT task_id, experiment_id
        FROM trials
        WHERE experiment_id IS NOT NULL
        ON CONFLICT (task_id, experiment_id) DO NOTHING
        """
    )

    # =========================================================================
    # 3. Backfill trials.experiment_id from the parent task, then make it
    #    NOT NULL.
    # =========================================================================
    op.execute(
        """
        UPDATE trials tr
        SET experiment_id = t.experiment_id
        FROM tasks t
        WHERE tr.task_id = t.id
          AND tr.experiment_id IS NULL
          AND t.experiment_id IS NOT NULL
        """
    )

    # If any NULLs remain at this point they are orphans (task has no
    # experiment_id either). Drop such trials outright; they could never
    # be reached from any experiment in the new model.
    op.execute("DELETE FROM trials WHERE experiment_id IS NULL")

    op.execute("ALTER TABLE trials ALTER COLUMN experiment_id SET NOT NULL")

    # Swap the FK from ON DELETE SET NULL → ON DELETE CASCADE so it stays
    # consistent with the new NOT NULL column. ``delete_experiment_core``
    # still scrubs trials explicitly before dropping the experiment row;
    # CASCADE is just a safety net.
    op.execute("ALTER TABLE trials DROP CONSTRAINT IF EXISTS fk_trials_experiment_id")
    op.execute(
        """
        ALTER TABLE trials
        ADD CONSTRAINT fk_trials_experiment_id
        FOREIGN KEY (experiment_id) REFERENCES experiments(id)
        ON DELETE CASCADE
        """
    )

    # =========================================================================
    # 4. Drop legacy tasks.experiment_id (and indexes that reference it)
    # =========================================================================
    op.execute("DROP INDEX IF EXISTS idx_tasks_org_experiment_created_at")
    op.execute("DROP INDEX IF EXISTS idx_tasks_experiment_id")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS experiment_id")


def downgrade() -> None:
    # Re-add tasks.experiment_id as nullable, backfill from the first join
    # row per task, then recreate the indexes.
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS experiment_id VARCHAR(64)")
    op.execute(
        """
        UPDATE tasks t
        SET experiment_id = (
            SELECT experiment_id
            FROM task_experiments te
            WHERE te.task_id = t.id
            ORDER BY te.created_at ASC
            LIMIT 1
        )
        WHERE t.experiment_id IS NULL
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_experiment_id " "ON tasks (experiment_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_org_experiment_created_at "
        "ON tasks (org_id, experiment_id, created_at)"
    )

    # Trials stay populated; just relax the NOT NULL and flip the FK back
    # to ON DELETE SET NULL.
    op.execute("ALTER TABLE trials ALTER COLUMN experiment_id DROP NOT NULL")
    op.execute("ALTER TABLE trials DROP CONSTRAINT IF EXISTS fk_trials_experiment_id")
    op.execute(
        """
        ALTER TABLE trials
        ADD CONSTRAINT fk_trials_experiment_id
        FOREIGN KEY (experiment_id) REFERENCES experiments(id)
        ON DELETE SET NULL
        """
    )

    # Drop the join table last (the indexes go with it).
    op.execute("DROP TABLE IF EXISTS task_experiments")
