"""add_external_probe_runs

Tracks probe submissions routed to the agent-sandbox-service instead of
creating local TrialModel rows.  Each row holds the probe_run_id returned
by the service so that the backend can call get_probe() for live status,
plus org/user/task denormalization for scoped queries.

Revision ID: a4b5c6d7e8f9
Revises: z3a4b5c6d7e8
Create Date: 2026-04-30 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "z3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "external_probe_runs",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_external_probe_runs_org_id",
        "external_probe_runs",
        ["org_id"],
    )
    op.create_index(
        "idx_external_probe_runs_task_id",
        "external_probe_runs",
        ["task_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_external_probe_runs_task_id", table_name="external_probe_runs")
    op.drop_index("idx_external_probe_runs_org_id", table_name="external_probe_runs")
    op.drop_table("external_probe_runs")
