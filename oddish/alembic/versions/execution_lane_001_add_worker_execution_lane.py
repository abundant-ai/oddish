"""Add credential-scoped worker execution lanes.

Revision ID: execution_lane_001
Revises: verdict_state_001
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "execution_lane_001"
down_revision: Union[str, Sequence[str], None] = "verdict_state_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(bind: sa.engine.Connection, table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, "worker_jobs", "execution_lane"):
        op.add_column(
            "worker_jobs",
            sa.Column(
                "execution_lane",
                sa.String(length=32),
                nullable=True,
                server_default=sa.text("'default'"),
            ),
        )

    op.execute(
        """
        UPDATE worker_jobs AS wj
        SET execution_lane = 'ec2_trial'
        FROM trials AS tr
        WHERE wj.kind::text = 'TRIAL'
          AND wj.subject_table = 'trials'
          AND wj.subject_id = tr.id
          AND lower(COALESCE(tr.environment, '')) = 'ec2'
          AND wj.execution_lane IS DISTINCT FROM 'ec2_trial'
        """
    )
    op.execute(
        "UPDATE worker_jobs SET execution_lane = 'default' "
        "WHERE execution_lane IS NULL"
    )
    op.alter_column(
        "worker_jobs",
        "execution_lane",
        existing_type=sa.String(length=32),
        nullable=False,
        server_default=sa.text("'default'"),
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'ck_worker_jobs_execution_lane'
          ) THEN
            ALTER TABLE worker_jobs
            ADD CONSTRAINT ck_worker_jobs_execution_lane
            CHECK (execution_lane IN ('default', 'ec2_trial'));
          END IF;
        END $$
        """
    )
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_worker_jobs_claim")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_worker_jobs_claim "
            "ON worker_jobs (queue_key, harbor_variant_id, execution_lane, "
            "priority, available_after, created_at) "
            "WHERE status IN ('QUEUED', 'RETRYING')"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_worker_jobs_claim")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_worker_jobs_claim "
            "ON worker_jobs (queue_key, harbor_variant_id, priority, "
            "available_after, created_at) "
            "WHERE status IN ('QUEUED', 'RETRYING')"
        )
    op.execute(
        "ALTER TABLE worker_jobs DROP CONSTRAINT IF EXISTS "
        "ck_worker_jobs_execution_lane"
    )
    if _column_exists(op.get_bind(), "worker_jobs", "execution_lane"):
        op.drop_column("worker_jobs", "execution_lane")
