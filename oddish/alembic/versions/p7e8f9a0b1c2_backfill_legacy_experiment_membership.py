"""backfill experiment_tasks/agents from legacy task_experiments + trials

The PR that introduced ``experiment_tasks`` / ``experiment_agents`` in
``o6d7e8f9a0b1`` only seeded those membership tables from
``experiment_cells``. Cells in turn only got backfilled (in
``i0d1e2f3a4b5``) for ``(experiment, task)`` pairs that already had at
least one trial.

That regression makes existing experiments disappear from the
matrix-resolver view in two cases:

1. An experiment had a task added (a row in ``task_experiments``) but
   no trials had been run for it yet -- the task vanishes from the
   resolved view, since ``experiment_tasks`` has no row.
2. An experiment had trials run with an agent that was later removed
   from any cell, or the cell backfill skipped it -- the agent
   disappears from the resolved view's agent list.

This migration patches both:

- Insert ``experiment_tasks`` rows from ``task_experiments`` joined to
  the task's ``current_version_id`` snapshot. Tasks with no current
  version are skipped (there's no version to pin to).
- Insert ``experiment_agents`` rows from the distinct
  ``(experiment_id, agent_equivalence_key)`` pairs observed in
  ``trials``. Agent identity columns are picked from the most recent
  trial so the harness/model/provider strings match what the user
  actually ran.

Both inserts are ``ON CONFLICT DO NOTHING`` so they're safe to re-run
and don't disturb existing rows.

Revision ID: p7e8f9a0b1c2
Revises: o6d7e8f9a0b1
Create Date: 2026-05-08 09:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "p7e8f9a0b1c2"
down_revision: Union[str, Sequence[str], None] = "o6d7e8f9a0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Tasks: snapshot the task's current_version_id at this point in
    # time so old experiments resolve to *something*. ON CONFLICT keeps
    # any rows already inserted by o6d7e8f9a0b1 untouched.
    op.execute(
        """
        INSERT INTO experiment_tasks (experiment_id, task_version_id)
        SELECT DISTINCT te.experiment_id, t.current_version_id
        FROM task_experiments te
        JOIN tasks t ON t.id = te.task_id
        WHERE t.current_version_id IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )

    # Agents: one row per distinct (experiment, equivalence_key) seen
    # in trials. The DISTINCT ON picks the most recent trial's identity
    # strings so what surfaces in the agent picker matches the latest
    # spelling the user ran.
    op.execute(
        """
        INSERT INTO experiment_agents (
            experiment_id, agent_equivalence_key,
            agent_harness, agent_model, agent_provider
        )
        SELECT DISTINCT ON (experiment_id, agent_equivalence_key)
            experiment_id,
            agent_equivalence_key,
            agent          AS agent_harness,
            model          AS agent_model,
            provider       AS agent_provider
        FROM trials
        WHERE experiment_id IS NOT NULL
          AND agent_equivalence_key IS NOT NULL
          AND agent IS NOT NULL
          AND provider IS NOT NULL
        ORDER BY experiment_id, agent_equivalence_key, created_at DESC
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    # No-op: backfill rows are indistinguishable from rows the
    # application has since written. Removing them would risk dropping
    # legitimate user state, and the upstream o6d7e8f9a0b1 downgrade
    # already drops the whole tables.
    pass
