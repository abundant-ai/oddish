"""add verifier_costs ledger for CUA/LLM verifier spend

Revision ID: verifier_costs_001
Revises: merge_thunder_staging_001
Create Date: 2026-09-15 00:00:00.000000

Sibling of ``analysis_costs`` / ``modal_costs``. One row per
``(trial_id, attempt, component)`` for CUA loop and judge spend. Never folded
into ``trials.cost_usd`` (solver-only) or ``analysis_spend`` (QA tiles).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "verifier_costs_001"
down_revision: Union[str, Sequence[str], None] = "merge_thunder_staging_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 000_initial_schema bootstraps a fresh DB via create_all, so on the
    # from-scratch path this table already exists. Skip to stay idempotent.
    if sa.inspect(op.get_bind()).has_table("verifier_costs"):
        return
    op.create_table(
        "verifier_costs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("trial_id", sa.String(160), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("component", sa.String(32), nullable=False),
        sa.Column("experiment_id", sa.String(64), nullable=True),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("billed_user_id", sa.String(64), nullable=True),
        sa.Column("task_id", sa.String(128), nullable=True),
        sa.Column("task_version_id", sa.String(64), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("route", sa.String(16), nullable=False),
        sa.Column("llm_key_hash", sa.String(64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("cost_source", sa.String(16), nullable=False),
        sa.Column("unpriced_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "component IN ('cua_loop', 'cua_judge')",
            name="ck_verifier_costs_component",
        ),
        sa.CheckConstraint(
            "route IN ('anthropic', 'bedrock', 'other')",
            name="ck_verifier_costs_route",
        ),
        sa.CheckConstraint(
            "cost_source IN ('native', 'estimated', 'backfill')",
            name="ck_verifier_costs_cost_source",
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_verifier_costs_attempt_nonneg"),
    )
    op.create_index(
        "uq_verifier_costs_trial_attempt_component",
        "verifier_costs",
        ["trial_id", "attempt", "component"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_verifier_costs_trial_id", "verifier_costs", ["trial_id"])
    op.create_index(
        "ix_verifier_costs_experiment_id", "verifier_costs", ["experiment_id"]
    )
    op.create_index("ix_verifier_costs_org_id", "verifier_costs", ["org_id"])
    op.create_index("ix_verifier_costs_task_id", "verifier_costs", ["task_id"])
    op.create_index(
        "ix_verifier_costs_task_version_id", "verifier_costs", ["task_version_id"]
    )
    op.create_index("ix_verifier_costs_component", "verifier_costs", ["component"])
    op.create_index("ix_verifier_costs_route", "verifier_costs", ["route"])


def downgrade() -> None:
    op.drop_index("ix_verifier_costs_route", table_name="verifier_costs")
    op.drop_index("ix_verifier_costs_component", table_name="verifier_costs")
    op.drop_index("ix_verifier_costs_task_version_id", table_name="verifier_costs")
    op.drop_index("ix_verifier_costs_task_id", table_name="verifier_costs")
    op.drop_index("ix_verifier_costs_org_id", table_name="verifier_costs")
    op.drop_index("ix_verifier_costs_experiment_id", table_name="verifier_costs")
    op.drop_index("ix_verifier_costs_trial_id", table_name="verifier_costs")
    op.drop_index(
        "uq_verifier_costs_trial_attempt_component", table_name="verifier_costs"
    )
    op.drop_table("verifier_costs")
