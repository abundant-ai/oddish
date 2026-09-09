"""Add the Thunder execution lane and fallback state.

Revision ID: thunder_fallback_001
Revises: delivery_qa_work_001
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "thunder_fallback_001"
down_revision: Union[str, Sequence[str], None] = "delivery_qa_work_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind: sa.engine.Connection, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    op.execute(
        "ALTER TABLE worker_jobs DROP CONSTRAINT IF EXISTS "
        "ck_worker_jobs_execution_lane"
    )
    op.execute(
        "ALTER TABLE worker_jobs ADD CONSTRAINT ck_worker_jobs_execution_lane "
        "CHECK (execution_lane IN ('default', 'ec2_trial', 'thunder_trial'))"
    )

    # ``000_initial`` builds from current ORM metadata on a fresh database, so
    # these columns already exist when CI subsequently replays this revision.
    # Deployed databases lack them. Guard each addition so both paths converge,
    # including a partially applied schema repaired by a retry.
    columns = _columns(op.get_bind(), "worker_jobs")
    if "reroute_from_environment" not in columns:
        op.add_column(
            "worker_jobs",
            sa.Column("reroute_from_environment", sa.String(length=32), nullable=True),
        )
    if "reroute_reason" not in columns:
        op.add_column(
            "worker_jobs",
            sa.Column("reroute_reason", sa.Text(), nullable=True),
        )
    if "reroute_pending_teardown" not in columns:
        op.add_column(
            "worker_jobs",
            sa.Column(
                "reroute_pending_teardown",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    columns = _columns(op.get_bind(), "worker_jobs")
    if "reroute_pending_teardown" in columns:
        op.drop_column("worker_jobs", "reroute_pending_teardown")
    if "reroute_reason" in columns:
        op.drop_column("worker_jobs", "reroute_reason")
    if "reroute_from_environment" in columns:
        op.drop_column("worker_jobs", "reroute_from_environment")
    op.execute(
        """
        UPDATE worker_jobs
        SET execution_lane = 'default'
        WHERE execution_lane = 'thunder_trial'
        """
    )
    op.execute(
        "ALTER TABLE worker_jobs DROP CONSTRAINT IF EXISTS "
        "ck_worker_jobs_execution_lane"
    )
    op.execute(
        "ALTER TABLE worker_jobs ADD CONSTRAINT ck_worker_jobs_execution_lane "
        "CHECK (execution_lane IN ('default', 'ec2_trial'))"
    )
