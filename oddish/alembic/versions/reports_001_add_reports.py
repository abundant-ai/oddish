"""add reports and report_experiments tables

Each DDL step runs in its own autocommit transaction and is idempotent, so a
failed run is safe to re-trigger and no single txn holds a lock on one hot
table while requesting another (see exp_trials_join_001 for the rationale).
The ``reports.status`` column reuses the existing native ``jobstatus`` enum
type, so no type is created here.
"""

from alembic import op

revision = "reports_001"
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
                ALTER TABLE report_experiments
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

    # 1. reports table (no FKs; reuses existing jobstatus enum type).
    _autocommit(
        """
        CREATE TABLE IF NOT EXISTS reports (
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

    # 2. report_experiments join WITHOUT foreign keys — locks nothing hot.
    _autocommit(
        """
        CREATE TABLE IF NOT EXISTS report_experiments (
            report_id VARCHAR(64) NOT NULL,
            experiment_id VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ,
            PRIMARY KEY (report_id, experiment_id)
        )
        """
    )

    # 3. FKs one referenced table at a time (NOT VALID), then VALIDATE.
    _add_fk_not_valid(
        name="report_experiments_report_id_fkey",
        column="report_id",
        ref_table="reports",
    )
    _add_fk_not_valid(
        name="report_experiments_experiment_id_fkey",
        column="experiment_id",
        ref_table="experiments",
    )
    _autocommit(
        "ALTER TABLE report_experiments "
        "VALIDATE CONSTRAINT report_experiments_report_id_fkey"
    )
    _autocommit(
        "ALTER TABLE report_experiments "
        "VALIDATE CONSTRAINT report_experiments_experiment_id_fkey"
    )

    # 4. Indexes.
    _autocommit(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "idx_report_experiments_experiment_id ON report_experiments (experiment_id)"
    )
    _autocommit(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "idx_reports_org_created_live ON reports (org_id, created_at) "
        "WHERE deleted_at IS NULL"
    )
    _autocommit(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "idx_reports_org_owner_user_live ON reports (org_id, owner_user_id) "
        "WHERE deleted_at IS NULL"
    )
    _autocommit(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "ix_reports_org_id ON reports (org_id)"
    )


def downgrade() -> None:
    _autocommit("SET lock_timeout = '8s'")
    _autocommit("DROP INDEX CONCURRENTLY IF EXISTS ix_reports_org_id")
    _autocommit("DROP INDEX CONCURRENTLY IF EXISTS idx_reports_org_owner_user_live")
    _autocommit("DROP INDEX CONCURRENTLY IF EXISTS idx_reports_org_created_live")
    _autocommit(
        "DROP INDEX CONCURRENTLY IF EXISTS idx_report_experiments_experiment_id"
    )
    _autocommit("DROP TABLE IF EXISTS report_experiments")
    _autocommit("DROP TABLE IF EXISTS reports")
