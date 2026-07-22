"""add modal compute cost span ledger and rate card

Revision ID: modal_costs_001
Revises: concurrency_override_001
Create Date: 2026-07-22 00:00:00.000000

Two tables: ``modal_costs`` (per-container compute span ledger, the compute
sibling of ``analysis_costs``) and ``modal_rates`` (append-only usd-per-second
rate card), plus the verified modal.com/pricing seed rows.

000_initial_schema bootstraps a fresh DB via create_all, so each table may
already exist on the from-scratch path. Each table is guarded independently —
never skip the whole migration because one table exists — and the rate seed
runs unconditionally (idempotent via ON CONFLICT DO NOTHING on the natural
key) so create_all-bootstrapped DBs still get seed rows.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from oddish.db import generate_id

revision: str = "modal_costs_001"
down_revision: Union[str, Sequence[str], None] = "concurrency_override_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Verified against modal.com/pricing on 2026-07-22 (usd per second).
# Backdated effective_at so historical backfill spans are priceable.
# Mirrored exactly by oddish.costs.modal_cost.DEFAULT_RATES — drift between
# the two is unit-tested (tests/test_modal_cost_pricing.py parses this file).
SEED_EFFECTIVE_AT = datetime(2025, 1, 1, tzinfo=timezone.utc)
SEED_NOTE = "modal.com/pricing 2026-07-22"
SEED_RATES: tuple[tuple[str, str, str], ...] = (
    ("modal", "function:cpu_core_sec", "0.0000131"),
    ("modal", "function:mem_gib_sec", "0.00000222"),
    ("modal", "sandbox:cpu_core_sec", "0.00003942"),
    ("modal", "sandbox:mem_gib_sec", "0.00000667"),
    ("modal", "gpu:B300", "0.001972"),
    ("modal", "gpu:B200", "0.001736"),
    ("modal", "gpu:H200", "0.001261"),
    ("modal", "gpu:H100", "0.001097"),
    ("modal", "gpu:RTX_PRO_6000", "0.000842"),
    ("modal", "gpu:A100-80GB", "0.000694"),
    ("modal", "gpu:A100-40GB", "0.000583"),
    ("modal", "gpu:L40S", "0.000542"),
    ("modal", "gpu:A10", "0.000306"),
    ("modal", "gpu:L4", "0.000222"),
    ("modal", "gpu:T4", "0.000164"),
)

_MODAL_COSTS_INDEXES = (
    "ix_modal_costs_open_spans",
    "ix_modal_costs_finished_at",
    "ix_modal_costs_org_id",
    "ix_modal_costs_experiment_id",
    "ix_modal_costs_trial_id",
    "uq_modal_costs_provider_external_id",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("modal_costs"):
        op.create_table(
            "modal_costs",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("trial_id", sa.String(160), nullable=True),
            sa.Column("experiment_id", sa.String(64), nullable=True),
            sa.Column("org_id", sa.String(64), nullable=True),
            sa.Column("billed_user_id", sa.String(64), nullable=True),
            sa.Column("worker_job_id", sa.Text(), nullable=True),
            sa.Column("worker_job_attempt", sa.Integer(), nullable=True),
            sa.Column("attempt", sa.Integer(), nullable=True),
            sa.Column("component_role", sa.String(32), nullable=False),
            sa.Column("span_ordinal", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("external_id", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("basis", sa.String(16), nullable=False),
            sa.Column("spec_source", sa.String(16), nullable=False),
            sa.Column("cpu_request", sa.Float(), nullable=True),
            sa.Column("cpu_limit", sa.Float(), nullable=True),
            sa.Column("mem_request_mb", sa.Integer(), nullable=True),
            sa.Column("mem_limit_mb", sa.Integer(), nullable=True),
            sa.Column("cpu_enforcement_mode", sa.String(16), nullable=True),
            sa.Column("mem_enforcement_mode", sa.String(16), nullable=True),
            sa.Column("gpu_type", sa.String(32), nullable=True),
            sa.Column("gpu_count", sa.Integer(), nullable=True),
            sa.Column("price_multiplier", sa.Numeric(), nullable=True),
            sa.Column("rate_snapshot", JSONB, nullable=True),
            sa.Column("rate_ids", JSONB, nullable=True),
            sa.Column("cost_usd", sa.Numeric(14, 6), nullable=True),
            sa.Column("unpriced_reason", sa.String(64), nullable=True),
            sa.Column("cost_source", sa.String(16), nullable=False),
            sa.Column("estimator_version", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "worker_job_id",
                "worker_job_attempt",
                "component_role",
                "span_ordinal",
                name="uq_modal_costs_job_attempt_role_ordinal",
            ),
            sa.CheckConstraint(
                "finished_at IS NULL OR finished_at >= started_at",
                name="ck_modal_costs_span_order",
            ),
            sa.CheckConstraint(
                "cpu_request IS NULL OR cpu_request >= 0",
                name="ck_modal_costs_cpu_request_nonneg",
            ),
            sa.CheckConstraint(
                "cpu_limit IS NULL OR cpu_limit >= 0",
                name="ck_modal_costs_cpu_limit_nonneg",
            ),
            sa.CheckConstraint(
                "mem_request_mb IS NULL OR mem_request_mb >= 0",
                name="ck_modal_costs_mem_request_nonneg",
            ),
            sa.CheckConstraint(
                "mem_limit_mb IS NULL OR mem_limit_mb >= 0",
                name="ck_modal_costs_mem_limit_nonneg",
            ),
            sa.CheckConstraint(
                "gpu_count IS NULL OR gpu_count >= 0",
                name="ck_modal_costs_gpu_count_nonneg",
            ),
        )
        op.create_index("ix_modal_costs_trial_id", "modal_costs", ["trial_id"])
        op.create_index(
            "ix_modal_costs_experiment_id", "modal_costs", ["experiment_id"]
        )
        op.create_index("ix_modal_costs_org_id", "modal_costs", ["org_id"])
        op.create_index("ix_modal_costs_finished_at", "modal_costs", ["finished_at"])
        op.create_index(
            "ix_modal_costs_open_spans",
            "modal_costs",
            ["worker_job_id"],
            postgresql_where=sa.text("finished_at IS NULL"),
        )
        op.create_index(
            "uq_modal_costs_provider_external_id",
            "modal_costs",
            ["provider", "external_id"],
            unique=True,
            postgresql_where=sa.text("external_id IS NOT NULL"),
        )

    if not inspector.has_table("modal_rates"):
        op.create_table(
            "modal_rates",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("sku", sa.String(64), nullable=False),
            sa.Column("usd_per_sec", sa.Numeric(16, 10), nullable=False),
            sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("note", sa.String(256), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "provider",
                "sku",
                "effective_at",
                name="uq_modal_rates_provider_sku_effective",
            ),
        )

    _seed_rates(bind)


def _seed_rates(bind: sa.engine.Connection) -> None:
    """Insert the verified rate rows; a no-op for rows already present."""
    stmt = sa.text(
        """
        INSERT INTO modal_rates
            (id, provider, sku, usd_per_sec, effective_at, note,
             created_at, updated_at)
        VALUES
            (:id, :provider, :sku, :usd_per_sec, :effective_at, :note,
             NOW(), NOW())
        ON CONFLICT (provider, sku, effective_at) DO NOTHING
        """
    )
    for provider, sku, usd_per_sec in SEED_RATES:
        bind.execute(
            stmt,
            {
                "id": generate_id(),
                "provider": provider,
                "sku": sku,
                "usd_per_sec": Decimal(usd_per_sec),
                "effective_at": SEED_EFFECTIVE_AT,
                "note": SEED_NOTE,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("modal_costs"):
        for index_name in _MODAL_COSTS_INDEXES:
            op.drop_index(index_name, table_name="modal_costs", if_exists=True)
        op.drop_table("modal_costs")
    if inspector.has_table("modal_rates"):
        op.drop_table("modal_rates")
