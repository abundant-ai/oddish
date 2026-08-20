"""drop analyzers and analyzer_experiments tables

The reports (analyzer) feature has been removed: no code reads or writes
these tables anymore (the ``analyzer_blocks`` table is unrelated and stays —
summaries and capabilities still use it). ``analyzers_001`` and the later
additive ``analyzers_00x`` migrations are kept so the revision history stays
linear and any environment can upgrade through them.

Each DDL step runs in its own autocommit transaction and is idempotent, so a
failed run is safe to re-trigger. ``analyzer_experiments`` goes first because
it holds the foreign keys into ``analyzers``; dropping a table also drops its
indexes.

Revision ID: dropanalyzers01
Revises: agentcap01
Create Date: 2026-08-18 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "dropanalyzers01"
down_revision: Union[str, Sequence[str], None] = "agentcap01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _autocommit(sql: str) -> None:
    with op.get_context().autocommit_block():
        op.execute(sql)


def upgrade() -> None:
    _autocommit("SET lock_timeout = '8s'")
    _autocommit("DROP TABLE IF EXISTS analyzer_experiments")
    _autocommit("DROP TABLE IF EXISTS analyzers")


def downgrade() -> None:
    # Recreates the tables at their final shape (analyzers_001 plus the later
    # additive columns), without data. FKs are added NOT VALID + VALIDATE, and
    # indexes non-concurrently — a downgrade is not a hot-path operation.
    _autocommit("SET lock_timeout = '8s'")
    _autocommit(
        """
        CREATE TABLE IF NOT EXISTS analyzers (
            id VARCHAR(64) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            org_id VARCHAR(64),
            owner_user_id VARCHAR(64),
            owner TEXT,
            status jobstatus NOT NULL DEFAULT 'PENDING',
            error TEXT,
            bad_failure_content TEXT,
            good_failure_content TEXT,
            universal_capabilities_content TEXT,
            headroom_analysis TEXT,
            by_model JSONB,
            reduce_prompt TEXT,
            num_trials INTEGER,
            num_bad_failures INTEGER,
            num_good_failures INTEGER,
            breakdown JSONB,
            findings JSONB,
            models_by_task JSONB,
            save_trial_analyses BOOLEAN NOT NULL DEFAULT false,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ
        )
        """
    )
    _autocommit(
        """
        CREATE TABLE IF NOT EXISTS analyzer_experiments (
            analyzer_id VARCHAR(64) NOT NULL,
            experiment_id VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ,
            PRIMARY KEY (analyzer_id, experiment_id)
        )
        """
    )
    for name, column, ref_table in (
        ("analyzer_experiments_analyzer_id_fkey", "analyzer_id", "analyzers"),
        ("analyzer_experiments_experiment_id_fkey", "experiment_id", "experiments"),
    ):
        _autocommit(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = '{name}'
                ) THEN
                    ALTER TABLE analyzer_experiments
                        ADD CONSTRAINT {name}
                        FOREIGN KEY ({column}) REFERENCES {ref_table}(id)
                        ON DELETE CASCADE
                        NOT VALID;
                END IF;
            END
            $$
            """
        )
        _autocommit(f"ALTER TABLE analyzer_experiments VALIDATE CONSTRAINT {name}")
    _autocommit(
        "CREATE INDEX IF NOT EXISTS idx_analyzer_experiments_experiment_id "
        "ON analyzer_experiments (experiment_id)"
    )
    _autocommit(
        "CREATE INDEX IF NOT EXISTS idx_analyzers_org_created_live "
        "ON analyzers (org_id, created_at) WHERE deleted_at IS NULL"
    )
    _autocommit(
        "CREATE INDEX IF NOT EXISTS idx_analyzers_org_owner_user_live "
        "ON analyzers (org_id, owner_user_id) WHERE deleted_at IS NULL"
    )
