"""add jobs and agent equivalence

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-05-01 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'batch_job_kind') THEN
                CREATE TYPE batch_job_kind AS ENUM (
                    'validation',
                    'experiment_backfill',
                    'ad_hoc'
                );
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'batch_job_status') THEN
                CREATE TYPE batch_job_status AS ENUM (
                    'queued',
                    'running',
                    'success',
                    'failed',
                    'cancelled'
                );
            END IF;
        END
        $$;
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id                         VARCHAR(64) PRIMARY KEY,
            kind                       batch_job_kind NOT NULL,
            status                     batch_job_status NOT NULL DEFAULT 'queued',
            launched_by_user_id        VARCHAR(64),
            launched_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at                TIMESTAMPTZ,
            triggered_by_experiment_id VARCHAR(64)
                REFERENCES experiments(id) ON DELETE SET NULL,
            org_id                     VARCHAR(64),
            created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at                 TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS job_cells (
            id                    VARCHAR(64) PRIMARY KEY,
            job_id                VARCHAR(64) NOT NULL
                REFERENCES jobs(id) ON DELETE CASCADE,
            task_version_id       VARCHAR(128) NOT NULL
                REFERENCES task_versions(id) ON DELETE CASCADE,
            agent_equivalence_key VARCHAR(64) NOT NULL,
            harness               VARCHAR(64) NOT NULL,
            model                 VARCHAR(128) NOT NULL,
            provider              VARCHAR(32) NOT NULL,
            n_trials              INTEGER NOT NULL,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at            TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS experiment_cells (
            id                    VARCHAR(64) PRIMARY KEY,
            experiment_id         VARCHAR(64) NOT NULL
                REFERENCES experiments(id) ON DELETE CASCADE,
            task_version_id       VARCHAR(128) NOT NULL
                REFERENCES task_versions(id) ON DELETE CASCADE,
            agent_equivalence_key VARCHAR(64) NOT NULL,
            harness               VARCHAR(64) NOT NULL,
            model                 VARCHAR(128) NOT NULL,
            provider              VARCHAR(32) NOT NULL,
            target_n_trials       INTEGER NOT NULL,
            created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            deleted_at            TIMESTAMPTZ
        )
        """
    )

    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS job_id VARCHAR(64) "
        "REFERENCES jobs(id) ON DELETE SET NULL"
    )
    op.execute("ALTER TABLE trials ALTER COLUMN experiment_id DROP NOT NULL")
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS worker_job_id VARCHAR(64) "
        "REFERENCES worker_jobs(id) ON DELETE SET NULL"
    )
    op.execute(
        "ALTER TABLE trials ADD COLUMN IF NOT EXISTS agent_equivalence_key VARCHAR(64)"
    )
    op.execute(
        "ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS job_id VARCHAR(64) "
        "REFERENCES jobs(id) ON DELETE SET NULL"
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_org_status_launched "
        "ON jobs (org_id, status, launched_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_triggered_by_experiment "
        "ON jobs (triggered_by_experiment_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_job_cells_unique_cell "
        "ON job_cells (job_id, task_version_id, agent_equivalence_key)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_cells_task_version "
        "ON job_cells (task_version_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_cells_agent_equivalence "
        "ON job_cells (agent_equivalence_key)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_experiment_cells_unique_cell "
        "ON experiment_cells (experiment_id, task_version_id, agent_equivalence_key)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_experiment_cells_task_version "
        "ON experiment_cells (task_version_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_experiment_cells_agent_equivalence "
        "ON experiment_cells (agent_equivalence_key)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_trials_job_id ON trials (job_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trials_worker_job_id "
        "ON trials (worker_job_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trials_agent_equivalence_key "
        "ON trials (agent_equivalence_key)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trials_task_version_agent_equivalence "
        "ON trials (task_version_id, agent_equivalence_key)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_worker_jobs_job_id "
        "ON worker_jobs (job_id) WHERE job_id IS NOT NULL"
    )

    # Backfill is intentionally idempotent. pgcrypto is available on the
    # hosted Postgres targets and gives us the same sha256 hex output as
    # oddish.core.agent_identity.compute_agent_equivalence_key.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        """
        UPDATE trials
        SET agent_equivalence_key = encode(
            digest(agent || '|' || COALESCE(model, '') || '|' || provider, 'sha256'),
            'hex'
        )
        WHERE agent_equivalence_key IS NULL
        """
    )
    op.execute(
        """
        INSERT INTO jobs (
            id,
            kind,
            status,
            launched_at,
            finished_at,
            triggered_by_experiment_id,
            org_id,
            created_at,
            updated_at
        )
        SELECT
            'job_' || left(md5(e.id), 24) AS id,
            'ad_hoc'::batch_job_kind AS kind,
            CASE
                WHEN stats.active_count > 0 THEN 'running'::batch_job_status
                WHEN stats.failed_count > 0 THEN 'failed'::batch_job_status
                ELSE 'success'::batch_job_status
            END AS status,
            e.created_at AS launched_at,
            CASE
                WHEN stats.active_count > 0 THEN NULL
                ELSE COALESCE(stats.max_finished_at, e.updated_at, e.created_at)
            END AS finished_at,
            e.id AS triggered_by_experiment_id,
            e.org_id,
            e.created_at,
            NOW()
        FROM experiments e
        LEFT JOIN LATERAL (
            SELECT
                COUNT(*) FILTER (
                    WHERE t.status::text IN ('pending', 'queued', 'running', 'retrying')
                ) AS active_count,
                COUNT(*) FILTER (
                    WHERE t.status::text = 'failed'
                ) AS failed_count,
                MAX(t.finished_at) AS max_finished_at
            FROM trials t
            WHERE t.experiment_id = e.id
        ) stats ON TRUE
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE trials t
        SET job_id = 'job_' || left(md5(t.experiment_id), 24)
        WHERE t.job_id IS NULL
          AND t.experiment_id IS NOT NULL
          AND EXISTS (
              SELECT 1
              FROM jobs j
              WHERE j.id = 'job_' || left(md5(t.experiment_id), 24)
          )
        """
    )
    op.execute(
        """
        UPDATE trials t
        SET worker_job_id = w.id
        FROM (
            SELECT DISTINCT ON (subject_id)
                   id,
                   subject_id
            FROM worker_jobs
            WHERE kind::text = 'TRIAL'
              AND subject_table = 'trials'
              AND subject_id IS NOT NULL
            ORDER BY subject_id, created_at DESC, id DESC
        ) w
        WHERE t.worker_job_id IS NULL
          AND w.subject_id = t.id
        """
    )
    op.execute(
        """
        UPDATE worker_jobs w
        SET job_id = t.job_id
        FROM trials t
        WHERE w.job_id IS NULL
          AND w.subject_table = 'trials'
          AND w.subject_id = t.id
          AND t.job_id IS NOT NULL
        """
    )
    op.execute(
        """
        INSERT INTO job_cells (
            id,
            job_id,
            task_version_id,
            agent_equivalence_key,
            harness,
            model,
            provider,
            n_trials,
            created_at,
            updated_at
        )
        SELECT
            'jcell_' || left(md5(
                t.job_id || '|' || t.task_version_id || '|' || t.agent_equivalence_key
            ), 24) AS id,
            t.job_id,
            t.task_version_id,
            t.agent_equivalence_key,
            t.agent AS harness,
            COALESCE(t.model, '') AS model,
            t.provider,
            COUNT(*)::integer AS n_trials,
            MIN(t.created_at) AS created_at,
            NOW() AS updated_at
        FROM trials t
        WHERE t.job_id IS NOT NULL
          AND t.task_version_id IS NOT NULL
          AND t.agent_equivalence_key IS NOT NULL
        GROUP BY
            t.job_id,
            t.task_version_id,
            t.agent_equivalence_key,
            t.agent,
            COALESCE(t.model, ''),
            t.provider
        ON CONFLICT (job_id, task_version_id, agent_equivalence_key) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO experiment_cells (
            id,
            experiment_id,
            task_version_id,
            agent_equivalence_key,
            harness,
            model,
            provider,
            target_n_trials,
            created_at,
            updated_at
        )
        SELECT
            'ecell_' || left(md5(
                t.experiment_id || '|' || t.task_version_id || '|' || t.agent_equivalence_key
            ), 24) AS id,
            t.experiment_id,
            t.task_version_id,
            t.agent_equivalence_key,
            t.agent AS harness,
            COALESCE(t.model, '') AS model,
            t.provider,
            COUNT(*)::integer AS target_n_trials,
            MIN(t.created_at) AS created_at,
            NOW() AS updated_at
        FROM trials t
        WHERE t.experiment_id IS NOT NULL
          AND t.task_version_id IS NOT NULL
          AND t.agent_equivalence_key IS NOT NULL
        GROUP BY
            t.experiment_id,
            t.task_version_id,
            t.agent_equivalence_key,
            t.agent,
            COALESCE(t.model, ''),
            t.provider
        ON CONFLICT (experiment_id, task_version_id, agent_equivalence_key)
        DO UPDATE SET target_n_trials = experiment_cells.target_n_trials + EXCLUDED.target_n_trials
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_worker_jobs_job_id")
    op.execute("DROP INDEX IF EXISTS idx_trials_task_version_agent_equivalence")
    op.execute("DROP INDEX IF EXISTS idx_trials_agent_equivalence_key")
    op.execute("DROP INDEX IF EXISTS idx_trials_worker_job_id")
    op.execute("DROP INDEX IF EXISTS idx_trials_job_id")
    op.execute("DROP INDEX IF EXISTS idx_job_cells_agent_equivalence")
    op.execute("DROP INDEX IF EXISTS idx_job_cells_task_version")
    op.execute("DROP INDEX IF EXISTS idx_job_cells_unique_cell")
    op.execute("DROP INDEX IF EXISTS idx_experiment_cells_agent_equivalence")
    op.execute("DROP INDEX IF EXISTS idx_experiment_cells_task_version")
    op.execute("DROP INDEX IF EXISTS idx_experiment_cells_unique_cell")
    op.execute("DROP INDEX IF EXISTS idx_jobs_triggered_by_experiment")
    op.execute("DROP INDEX IF EXISTS idx_jobs_org_status_launched")
    op.execute("ALTER TABLE worker_jobs DROP COLUMN IF EXISTS job_id")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS agent_equivalence_key")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS worker_job_id")
    op.execute("ALTER TABLE trials DROP COLUMN IF EXISTS job_id")
    op.execute(
        """
        UPDATE trials
        SET experiment_id = (
            SELECT id FROM experiments ORDER BY created_at ASC LIMIT 1
        )
        WHERE experiment_id IS NULL
          AND EXISTS (SELECT 1 FROM experiments)
        """
    )
    op.execute("ALTER TABLE trials ALTER COLUMN experiment_id SET NOT NULL")
    op.execute("DROP TABLE IF EXISTS job_cells")
    op.execute("DROP TABLE IF EXISTS experiment_cells")
    op.execute("DROP TABLE IF EXISTS jobs")
    op.execute("DROP TYPE IF EXISTS batch_job_status")
    op.execute("DROP TYPE IF EXISTS batch_job_kind")
