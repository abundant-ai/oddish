"""rollback task-first data model (revert of #55)

Pairs with the code revert of #55. Drops every schema artefact added by
the task-first chain (f7..o6) so the DB matches the reverted code:

- experiment_tasks, experiment_agents (o6)
- experiments.target_n_trials (n5)
- experiment_cells (i0)
- jobs table + trials.job_id + trials.agent_equivalence_key +
  worker_jobs.user_job_id and the indexes from g8/h9
- task_versions immutability trigger and its function (l3)
- restore task_versions.updated_at (f7)

m4's restore of trials.experiment_id and task_experiments is left in
place -- those are pre-#55 schema. Trials inserted with NULL
experiment_id under the new code are deleted before re-tightening
NOT NULL.

Idempotent: every DDL statement uses IF EXISTS / IF NOT EXISTS so this
is safe to apply on a DB that's at any point in the f7..o6 chain
(including a pre-#55 DB that walked through the whole chain in one
upgrade pass).
"""

from typing import Sequence, Union

from alembic import op


revision: str = "p7e8f9a0b1c2"
down_revision: Union[str, Sequence[str], None] = "o6d7e8f9a0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS task_versions_immutable_guard ON task_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS task_versions_block_mutation()")

    op.execute("DROP TABLE IF EXISTS experiment_agents")
    op.execute("DROP TABLE IF EXISTS experiment_tasks")

    op.execute("ALTER TABLE experiments DROP COLUMN IF EXISTS target_n_trials")

    op.execute("DROP INDEX IF EXISTS idx_experiment_cells_task_version")
    op.execute("DROP INDEX IF EXISTS idx_experiment_cells_experiment")
    op.execute("DROP INDEX IF EXISTS idx_experiment_cells_unique")
    op.execute("DROP TABLE IF EXISTS experiment_cells")

    op.execute("DROP INDEX IF EXISTS idx_worker_jobs_user_job")
    op.execute("DROP INDEX IF EXISTS idx_trials_job_id")
    op.execute("DROP INDEX IF EXISTS idx_trials_task_version_agent")
    op.execute("ALTER TABLE worker_jobs DROP COLUMN IF EXISTS user_job_id")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS agent_equivalence_key")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS job_id")
    op.execute("DROP INDEX IF EXISTS idx_jobs_org_id")
    op.execute("DROP INDEX IF EXISTS idx_jobs_triggered_by_experiment")
    op.execute("DROP INDEX IF EXISTS idx_jobs_org_launched_at")
    op.execute("DROP TABLE IF EXISTS jobs")

    op.execute("DELETE FROM trials WHERE experiment_id IS NULL")
    op.execute("ALTER TABLE trials ALTER COLUMN experiment_id SET NOT NULL")

    op.execute(
        "ALTER TABLE task_versions "
        "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ "
        "NOT NULL DEFAULT now()"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Rollback of #55 is one-way. To recover the task-first model, "
        "re-land the code (and let f7..o6 re-apply on a fresh chain)."
    )
