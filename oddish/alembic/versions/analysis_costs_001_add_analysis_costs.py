"""add analysis_costs ledger table

Revision ID: analysis_costs_001
Revises: analyzers_006
Create Date: 2026-07-16 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "analysis_costs_001"
down_revision: Union[str, Sequence[str], None] = "analyzers_007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 000_initial_schema bootstraps a fresh DB via create_all, so on the
    # from-scratch path this table already exists. Skip to stay idempotent.
    if sa.inspect(op.get_bind()).has_table("analysis_costs"):
        return
    op.create_table(
        "analysis_costs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("job_kind", sa.String(64), nullable=False),
        sa.Column("trial_id", sa.String(160), nullable=True),
        sa.Column("experiment_id", sa.String(64), nullable=True),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("billed_user_id", sa.String(64), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("cost_source", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_analysis_costs_job_kind", "analysis_costs", ["job_kind"])
    op.create_index("ix_analysis_costs_trial_id", "analysis_costs", ["trial_id"])
    op.create_index(
        "ix_analysis_costs_experiment_id", "analysis_costs", ["experiment_id"]
    )
    op.create_index("ix_analysis_costs_org_id", "analysis_costs", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_analysis_costs_org_id", table_name="analysis_costs")
    op.drop_index("ix_analysis_costs_experiment_id", table_name="analysis_costs")
    op.drop_index("ix_analysis_costs_trial_id", table_name="analysis_costs")
    op.drop_index("ix_analysis_costs_job_kind", table_name="analysis_costs")
    op.drop_table("analysis_costs")
