"""Shared request reservations for QA model routing."""

from alembic import op

revision = "qa_model_router_001"
down_revision = "qa_dispatch_001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE IF NOT EXISTS model_request_pools (
        id text PRIMARY KEY, cooldown_until timestamptz,
        observed_at timestamptz, observed_load double precision NOT NULL DEFAULT 0
    )""")
    op.execute("""CREATE TABLE IF NOT EXISTS model_request_leases (
        id text PRIMARY KEY, pool_id text NOT NULL, worker_job_id text NOT NULL,
        created_at timestamptz NOT NULL, expires_at timestamptz NOT NULL,
        active boolean NOT NULL, input_tokens bigint NOT NULL, output_tokens bigint NOT NULL
    )""")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_model_request_pool_expiry ON model_request_leases (pool_id, expires_at)"
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_model_request_worker_created ON model_request_leases (worker_job_id, created_at)"
    )


def downgrade():
    op.drop_table("model_request_leases")
    op.drop_table("model_request_pools")
