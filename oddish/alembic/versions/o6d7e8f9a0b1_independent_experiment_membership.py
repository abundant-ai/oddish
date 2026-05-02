"""independent experiment membership: experiment_tasks + experiment_agents

Up to now, an experiment was a collection of ``experiment_cells`` --
each cell pinning a (task_version, agent) pair plus a target. Adding
an agent without any tasks (or vice versa) wasn't possible.

This migration introduces two membership tables:

- ``experiment_tasks`` -- task versions in the experiment, independent
  of agents
- ``experiment_agents`` -- agent identities in the experiment,
  independent of tasks

The matrix becomes a cross product of these two lists at resolve
time. ``experiment_cells`` is kept as an optional per-pair override
for ``target_n_trials``; cells without an override fall back to
``experiments.target_n_trials``.

Both tables are backfilled from the union of tasks/agents already
present in ``experiment_cells`` so existing experiments resolve
identically post-migration.

Revision ID: o6d7e8f9a0b1
Revises: n5c6d7e8f9a0
Create Date: 2026-05-02 14:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "o6d7e8f9a0b1"
down_revision: Union[str, Sequence[str], None] = "n5c6d7e8f9a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS experiment_tasks (
            experiment_id   TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
            task_version_id TEXT NOT NULL REFERENCES task_versions(id),
            added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (experiment_id, task_version_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_experiment_tasks_task_version
            ON experiment_tasks(task_version_id)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS experiment_agents (
            experiment_id         TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
            agent_equivalence_key TEXT NOT NULL,
            agent_harness         TEXT NOT NULL,
            agent_model           TEXT,
            agent_provider        TEXT NOT NULL,
            added_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (experiment_id, agent_equivalence_key)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_experiment_agents_eqkey
            ON experiment_agents(agent_equivalence_key)
        """
    )

    # Backfill from existing experiment_cells so resolves stay
    # identical.
    op.execute(
        """
        INSERT INTO experiment_tasks (experiment_id, task_version_id)
        SELECT DISTINCT experiment_id, task_version_id
        FROM experiment_cells
        WHERE deleted_at IS NULL
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO experiment_agents (
            experiment_id, agent_equivalence_key,
            agent_harness, agent_model, agent_provider
        )
        SELECT DISTINCT ON (experiment_id, agent_equivalence_key)
            experiment_id, agent_equivalence_key,
            agent_harness, agent_model, agent_provider
        FROM experiment_cells
        WHERE deleted_at IS NULL
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS experiment_agents")
    op.execute("DROP TABLE IF EXISTS experiment_tasks")
