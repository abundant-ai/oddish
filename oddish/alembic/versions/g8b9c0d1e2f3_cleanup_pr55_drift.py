"""cleanup_pr55_drift

Drops the schema artefacts left over from PR #55 ("task-first data
model") that the rollback in commit 9423b02 removed from the
codebase but never finished cleaning up in the database. After the
rollback the local repo head was ``e6f7a8b9c0d1`` and the rollback
migration ``p7e8f9a0b1c2`` "drops every artefact added by f7..o6 in
reverse FK order, deletes trials inserted with NULL experiment_id
under the new code, and re-tightens trials.experiment_id to NOT
NULL" -- but inspection of the live database shows the drops
landed only partially.

Concretely, the following PR #55 artefacts still exist:

- columns: ``trials.job_id`` (FK -> jobs), ``trials.agent_equivalence_key``,
  ``worker_jobs.user_job_id`` (FK -> jobs)
- supporting indexes: ``idx_trials_job_id``,
  ``idx_trials_task_version_agent``, ``idx_worker_jobs_user_job``
- tables: ``jobs``, ``experiment_agents``, ``experiment_cells``,
  ``experiment_tasks``
- nullability: ``trials.experiment_id`` is still ``NULLABLE`` even
  though the rollback intended ``NOT NULL``

None of these are referenced by any code path in the post-rollback
tree. They are pure debris -- live workers that still write to them
do so because they are running pre-rollback code on warm Modal
containers; the next deploy from this branch stops the writes.

This migration drops everything in FK-safe order and tightens
``trials.experiment_id`` to ``NOT NULL``. All operations use
``IF EXISTS`` / guarded ``DO`` blocks so the migration is safe to
re-run.

Downgrade is intentionally not implemented: re-creating PR #55's
table-shape from this revision is not useful and would conflict with
the assumption baked into the rest of the chain that
``trials.experiment_id`` is ``NOT NULL``.

Revision ID: g8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-05-11 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "g8b9c0d1e2f3"
down_revision: Union[str, Sequence[str], None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Defensively scrub any NULL trials.experiment_id rows so the
    #    SET NOT NULL at the end of this migration can land.
    #    The current production DB has zero such rows, so this is a
    #    no-op in practice; it stays as a guard for environments that
    #    diverged differently.
    # ------------------------------------------------------------------
    op.execute("DELETE FROM trials WHERE experiment_id IS NULL")

    # ------------------------------------------------------------------
    # 2. Drop foreign keys that point INTO the soon-to-be-dropped
    #    ``jobs`` table. Doing this explicitly (instead of relying on
    #    ``DROP TABLE ... CASCADE``) keeps the audit log obvious.
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE trials DROP CONSTRAINT IF EXISTS trials_job_id_fkey")
    op.execute(
        "ALTER TABLE worker_jobs "
        "DROP CONSTRAINT IF EXISTS worker_jobs_user_job_id_fkey"
    )

    # ------------------------------------------------------------------
    # 3. Drop the vestigial columns. ``DROP COLUMN`` cascades to the
    #    column's indexes (``idx_trials_job_id``,
    #    ``idx_trials_task_version_agent``, ``idx_worker_jobs_user_job``),
    #    so no explicit ``DROP INDEX`` is needed.
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS job_id")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS agent_equivalence_key")
    op.execute("ALTER TABLE worker_jobs DROP COLUMN IF EXISTS user_job_id")

    # ------------------------------------------------------------------
    # 4. Drop the vestigial tables. Order matters even with CASCADE:
    #    keep the explicit FK-leaf-first ordering so downstream
    #    archeology of the DDL log makes sense.
    # ------------------------------------------------------------------
    op.execute("DROP TABLE IF EXISTS experiment_cells")
    op.execute("DROP TABLE IF EXISTS experiment_agents")
    op.execute("DROP TABLE IF EXISTS experiment_tasks")
    op.execute("DROP TABLE IF EXISTS jobs")

    # ------------------------------------------------------------------
    # 5. Re-tighten ``trials.experiment_id`` to NOT NULL to match the
    #    SQLAlchemy model (``TrialModel.experiment_id: nullable=False``).
    #    Wrapped in a guard so a partially-applied environment that
    #    already tightened this doesn't fail.
    # ------------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'trials'
                  AND column_name = 'experiment_id'
                  AND is_nullable = 'YES'
            ) THEN
                ALTER TABLE trials ALTER COLUMN experiment_id SET NOT NULL;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    """Not implemented.

    Re-creating PR #55's tables / columns from this revision would
    only resurrect dead state. If a regression genuinely requires
    them again, write a fresh forward migration that re-introduces
    only what's needed.
    """
    raise NotImplementedError(
        "Downgrade of cleanup_pr55_drift is intentionally not provided"
    )
