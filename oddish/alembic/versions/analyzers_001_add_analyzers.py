"""add analyzers and analyzer_experiments tables

Each DDL step runs in its own autocommit transaction and is idempotent, so a
failed run is safe to re-trigger and no single txn holds a lock on one hot
table while requesting another (see exp_trials_join_001 for the rationale).
The ``analyzers.status`` column reuses the existing native ``jobstatus`` enum
type, so no type is created here.
"""

from alembic import op

revision = "analyzers_001"
down_revision = "trajgraph_001"
branch_labels = None
depends_on = None


def _autocommit(sql: str) -> None:
    with op.get_context().autocommit_block():
        op.execute(sql)


def _add_fk_not_valid(name: str, column: str, ref_table: str) -> None:
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


def upgrade() -> None:
    _autocommit("SET lock_timeout = '8s'")

    # 1. analyzers table (no FKs; reuses existing jobstatus enum type).
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
            num_trials INTEGER,
            num_bad_failures INTEGER,
            num_good_failures INTEGER,
            breakdown JSONB,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ
        )
        """
    )

    # 2. analyzer_experiments join WITHOUT foreign keys — locks nothing hot.
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

    # 3. FKs one referenced table at a time (NOT VALID), then VALIDATE.
    _add_fk_not_valid(
        name="analyzer_experiments_analyzer_id_fkey",
        column="analyzer_id",
        ref_table="analyzers",
    )
    _add_fk_not_valid(
        name="analyzer_experiments_experiment_id_fkey",
        column="experiment_id",
        ref_table="experiments",
    )
    _autocommit(
        "ALTER TABLE analyzer_experiments "
        "VALIDATE CONSTRAINT analyzer_experiments_analyzer_id_fkey"
    )
    _autocommit(
        "ALTER TABLE analyzer_experiments "
        "VALIDATE CONSTRAINT analyzer_experiments_experiment_id_fkey"
    )

    # 4. Indexes.
    _autocommit(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "idx_analyzer_experiments_experiment_id ON analyzer_experiments (experiment_id)"
    )
    _autocommit(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "idx_analyzers_org_created_live ON analyzers (org_id, created_at) "
        "WHERE deleted_at IS NULL"
    )
    _autocommit(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "idx_analyzers_org_owner_user_live ON analyzers (org_id, owner_user_id) "
        "WHERE deleted_at IS NULL"
    )
    _autocommit(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_analyzers_org_id ON analyzers (org_id)"
    )


def downgrade() -> None:
    _autocommit("SET lock_timeout = '8s'")
    _autocommit("DROP INDEX CONCURRENTLY IF EXISTS ix_analyzers_org_id")
    _autocommit("DROP INDEX CONCURRENTLY IF EXISTS idx_analyzers_org_owner_user_live")
    _autocommit("DROP INDEX CONCURRENTLY IF EXISTS idx_analyzers_org_created_live")
    _autocommit(
        "DROP INDEX CONCURRENTLY IF EXISTS idx_analyzer_experiments_experiment_id"
    )
    _autocommit("DROP TABLE IF EXISTS analyzer_experiments")
    _autocommit("DROP TABLE IF EXISTS analyzers")
