"""add experiment_trials join table and experiments.is_collection

Each schema change below runs in its own autocommit transaction so the
migration never holds a lock on one hot table while requesting a lock on
another. The original single-transaction form deadlocked against live trial
traffic: ``CREATE TABLE experiment_trials`` took a ``ShareRowExclusiveLock`` on
``trials`` via its FK while ``ADD COLUMN is_collection`` waited on the
``AccessExclusiveLock`` for ``experiments`` — and concurrent app txns lock
``experiments`` then ``trials`` in the opposite order, closing the cycle.

The FKs are added one referenced table at a time as ``NOT VALID`` (a brief
metadata-only lock) and then ``VALIDATE``d (``ShareUpdateExclusiveLock``, which
neither blocks nor deadlocks with DML). ``lock_timeout`` makes any contended
step fail fast rather than queue ahead of worker DML; every step is idempotent,
so a failed run is safe to re-trigger.
"""

from alembic import op

revision = "exp_trials_join_001"
down_revision = "merge_mca01_wjtoken_heads"
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
                ALTER TABLE experiment_trials
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
    # Session-level; persists across the autocommit steps on this connection.
    _autocommit("SET lock_timeout = '8s'")

    # 1. experiments.is_collection — experiments-only AccessExclusive, own txn.
    _autocommit(
        "ALTER TABLE experiments "
        "ADD COLUMN IF NOT EXISTS is_collection BOOLEAN NOT NULL DEFAULT false"
    )

    # 2. Join table WITHOUT foreign keys — locks nothing hot.
    _autocommit(
        """
        CREATE TABLE IF NOT EXISTS experiment_trials (
            experiment_id VARCHAR(64) NOT NULL,
            trial_id VARCHAR(160) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ,
            PRIMARY KEY (experiment_id, trial_id)
        )
        """
    )

    # 3. FKs one referenced table at a time (NOT VALID), then VALIDATE.
    _add_fk_not_valid(
        name="experiment_trials_experiment_id_fkey",
        column="experiment_id",
        ref_table="experiments",
    )
    _add_fk_not_valid(
        name="experiment_trials_trial_id_fkey",
        column="trial_id",
        ref_table="trials",
    )
    _autocommit(
        "ALTER TABLE experiment_trials "
        "VALIDATE CONSTRAINT experiment_trials_experiment_id_fkey"
    )
    _autocommit(
        "ALTER TABLE experiment_trials "
        "VALIDATE CONSTRAINT experiment_trials_trial_id_fkey"
    )

    # 4. Lookup index on the trial side.
    _autocommit(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
        "idx_experiment_trials_trial_id ON experiment_trials (trial_id)"
    )


def downgrade() -> None:
    _autocommit("SET lock_timeout = '8s'")
    _autocommit("DROP INDEX CONCURRENTLY IF EXISTS idx_experiment_trials_trial_id")
    _autocommit("DROP TABLE IF EXISTS experiment_trials")
    _autocommit("ALTER TABLE experiments DROP COLUMN IF EXISTS is_collection")
