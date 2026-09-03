"""Persist provider reroute provenance and teardown gating.

Revision ID: thunder_fallback_001
Revises: thunder_lane_001, deliveries_002
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "thunder_fallback_001"
down_revision: Union[str, Sequence[str], None] = (
    "thunder_lane_001",
    "deliveries_002",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind: sa.engine.Connection, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
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
