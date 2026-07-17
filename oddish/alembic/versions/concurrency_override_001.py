from alembic import op


revision = "concurrency_override_001"
down_revision = "analyzers_007"
branch_labels = depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS model_concurrency_overrides (
            queue_key        TEXT PRIMARY KEY,
            concurrency_limit INTEGER NOT NULL
                CONSTRAINT ck_model_concurrency_overrides_limit
                CHECK (concurrency_limit BETWEEN 0 AND 10000),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS model_concurrency_overrides")
