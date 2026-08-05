"""Add attributed history for model concurrency changes.

Revision ID: concurrency_audit_001
Revises: trajgraph_002
"""

from alembic import op


revision = "concurrency_audit_001"
down_revision = "trajgraph_002"
branch_labels = depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE model_concurrency_audit (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            queue_key TEXT NOT NULL,
            old_override_limit INTEGER,
            new_override_limit INTEGER,
            old_effective_limit INTEGER NOT NULL,
            new_effective_limit INTEGER NOT NULL,
            actor_user_id TEXT NOT NULL,
            actor_api_key_id TEXT,
            changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ck_model_concurrency_audit_old_override
                CHECK (old_override_limit IS NULL OR old_override_limit BETWEEN 0 AND 10000),
            CONSTRAINT ck_model_concurrency_audit_new_override
                CHECK (new_override_limit IS NULL OR new_override_limit BETWEEN 0 AND 10000),
            CONSTRAINT ck_model_concurrency_audit_old_effective
                CHECK (old_effective_limit BETWEEN 0 AND 10000),
            CONSTRAINT ck_model_concurrency_audit_new_effective
                CHECK (new_effective_limit BETWEEN 0 AND 10000)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_model_concurrency_audit_queue_id
        ON model_concurrency_audit (queue_key, id DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE model_concurrency_audit")
