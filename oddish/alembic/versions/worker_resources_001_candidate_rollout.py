"""Add disabled worker resource rollout and durable attempt attribution."""

from alembic import op
import sqlalchemy as sa

revision = "worker_resources_001"
down_revision = "delivery_progress_001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "worker_resource_rollout",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fraction", sa.Float(), nullable=False, server_default="0"),
        sa.Column("max_workers", sa.Integer(), nullable=False, server_default="2"),
        sa.Column(
            "configuration",
            sa.Text(),
            nullable=False,
            server_default="candidate-cpu0.6-mem3072",
        ),
        sa.CheckConstraint("id = 1", name="ck_worker_rollout_singleton"),
        sa.CheckConstraint(
            "fraction >= 0 AND fraction <= 1", name="ck_worker_rollout_fraction"
        ),
        sa.CheckConstraint("max_workers >= 0", name="ck_worker_rollout_max_workers"),
        if_not_exists=True,
    )
    op.execute(
        "INSERT INTO worker_resource_rollout (id) VALUES (1) ON CONFLICT DO NOTHING"
    )
    op.execute(
        "ALTER TABLE queue_slots ADD COLUMN IF NOT EXISTS resource_candidate boolean NOT NULL DEFAULT false"
    )
    op.create_table(
        "worker_resource_attempts",
        sa.Column("worker_job_id", sa.Text(), primary_key=True),
        sa.Column("attempt", sa.Integer(), primary_key=True),
        sa.Column("configuration", sa.Text(), nullable=False),
        sa.Column("modal_function_call_id", sa.Text(), nullable=True),
        sa.Column("cpu_request", sa.Float(), nullable=False),
        sa.Column("cpu_limit", sa.Float(), nullable=True),
        sa.Column("memory_mb", sa.Integer(), nullable=False),
        sa.Column("nonpreemptible", sa.Boolean(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )


def downgrade():
    op.drop_table("worker_resource_attempts")
    op.drop_column("queue_slots", "resource_candidate")
    op.drop_table("worker_resource_rollout")
