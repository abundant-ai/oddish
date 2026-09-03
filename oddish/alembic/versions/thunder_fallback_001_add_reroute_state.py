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


def upgrade() -> None:
    op.add_column(
        "worker_jobs",
        sa.Column("reroute_from_environment", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "worker_jobs",
        sa.Column("reroute_reason", sa.Text(), nullable=True),
    )
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
    op.drop_column("worker_jobs", "reroute_pending_teardown")
    op.drop_column("worker_jobs", "reroute_reason")
    op.drop_column("worker_jobs", "reroute_from_environment")
