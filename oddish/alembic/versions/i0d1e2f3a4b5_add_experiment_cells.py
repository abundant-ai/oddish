"""add experiment_cells table and backfill from task_experiments

Phase 3 of the task-first migration. Cells join experiments to specific
``(task_version_id, agent_equivalence_key)`` pairs with a per-cell trial
target. The experiment becomes a saved selection + a read-side view;
this migration adds the selection table and seeds it from existing
``task_experiments`` + trial data.

Backfill:
- For each ``(experiment, task)`` row in ``task_experiments``, snapshot
  the task's ``current_version_id`` at migration time.
- For each distinct ``agent_equivalence_key`` observed in that
  experiment's trials against that task, create one cell with
  ``target_n_trials = count(trials with that agent for that task in
  that experiment)``.
- Trials whose task_version_id doesn't match the snapshot are still
  counted -- the equivalence pool is by (task_version, agent), not
  (experiment, task_version, agent), but for the *target*-derivation
  we use the historical count under the experiment.

Idempotent on the unique key (experiment_id, task_version_id,
agent_equivalence_key).

Revision ID: i0d1e2f3a4b5
Revises: h9c0d1e2f3a4
Create Date: 2026-05-01 01:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "i0d1e2f3a4b5"
down_revision: Union[str, Sequence[str], None] = "h9c0d1e2f3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- experiment_cells table ----------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS experiment_cells (
            id VARCHAR(64) PRIMARY KEY,
            experiment_id VARCHAR(64) NOT NULL
                REFERENCES experiments(id) ON DELETE CASCADE,
            task_version_id VARCHAR(128) NOT NULL
                REFERENCES task_versions(id) ON DELETE CASCADE,
            agent_equivalence_key VARCHAR(64) NOT NULL,
            target_n_trials INTEGER NOT NULL,
            agent_harness VARCHAR(64) NOT NULL,
            agent_model VARCHAR(128),
            agent_provider VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_experiment_cells_unique
            ON experiment_cells (experiment_id, task_version_id, agent_equivalence_key)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_experiment_cells_experiment
            ON experiment_cells (experiment_id)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_experiment_cells_task_version
            ON experiment_cells (task_version_id)
        """
    )

    # --- backfill -------------------------------------------------------
    # One row per (experiment, task_version_id snapshot, equivalence key)
    # observed in the existing trial pool. ``substr(md5(...), 1, 16)`` is
    # the same id-shape used by the synthetic-job backfill; collisions
    # are guarded by the unique index above.
    op.execute(
        """
        INSERT INTO experiment_cells (
            id, experiment_id, task_version_id, agent_equivalence_key,
            target_n_trials,
            agent_harness, agent_model, agent_provider,
            created_at, updated_at
        )
        SELECT
            substr(md5(
                te.experiment_id || '|' ||
                COALESCE(t.current_version_id, '') || '|' ||
                COALESCE(trials.agent_equivalence_key, '')
            ), 1, 16) AS id,
            te.experiment_id,
            t.current_version_id AS task_version_id,
            trials.agent_equivalence_key,
            COUNT(*) AS target_n_trials,
            (array_agg(trials.agent ORDER BY trials.created_at))[1] AS agent_harness,
            (array_agg(trials.model ORDER BY trials.created_at))[1] AS agent_model,
            (array_agg(trials.provider ORDER BY trials.created_at))[1] AS agent_provider,
            now() AS created_at,
            now() AS updated_at
        FROM task_experiments te
        JOIN tasks t ON t.id = te.task_id
        JOIN trials ON trials.experiment_id = te.experiment_id
                   AND trials.task_id = te.task_id
        WHERE t.current_version_id IS NOT NULL
          AND trials.agent_equivalence_key IS NOT NULL
          AND trials.agent IS NOT NULL
          AND trials.provider IS NOT NULL
        GROUP BY
            te.experiment_id,
            t.current_version_id,
            trials.agent_equivalence_key
        ON CONFLICT (experiment_id, task_version_id, agent_equivalence_key)
        DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_experiment_cells_task_version")
    op.execute("DROP INDEX IF EXISTS idx_experiment_cells_experiment")
    op.execute("DROP INDEX IF EXISTS idx_experiment_cells_unique")
    op.execute("DROP TABLE IF EXISTS experiment_cells")
