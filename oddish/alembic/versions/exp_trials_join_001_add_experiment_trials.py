"""add experiment_trials join table and experiments.is_collection"""

from alembic import op
import sqlalchemy as sa

revision = "exp_trials_join_001"
down_revision = "merge_mca01_wjtoken_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS experiment_trials (
            experiment_id VARCHAR(64) NOT NULL
                REFERENCES experiments(id) ON DELETE CASCADE,
            trial_id VARCHAR(160) NOT NULL
                REFERENCES trials(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ,
            PRIMARY KEY (experiment_id, trial_id)
        )
        """
    )

    from sqlalchemy import inspect

    insp = inspect(op.get_bind())
    if "is_collection" not in {c["name"] for c in insp.get_columns("experiments")}:
        op.add_column(
            "experiments",
            sa.Column(
                "is_collection",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )

    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "idx_experiment_trials_trial_id ON experiment_trials (trial_id)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_experiment_trials_trial_id")
    op.drop_column("experiments", "is_collection")
    op.execute("DROP TABLE IF EXISTS experiment_trials")
