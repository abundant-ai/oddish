"""Add source-backed task aliases, metadata assertions, and delivery history.

Reviewed facts from external delivery records live in their own tables,
apart from live delivery checklists and finalized snapshots. Task foreign
keys RESTRICT deletes so a retired (soft-deleted) task keeps its history.

Revision ID: delivery_history_001
Revises: verifier_costs_001
Create Date: 2026-09-17
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "delivery_history_001"
down_revision: Union[str, Sequence[str], None] = "verifier_costs_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    # Fresh databases bootstrap from current models (000_initial), which
    # already include these tables; every step is guarded.
    op.create_table(
        "metadata_import_receipts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column("plan_schema", sa.String(64), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("source_as_of", sa.String(64), nullable=True),
        sa.Column("source_revision", sa.String(128), nullable=True),
        sa.Column("inventory_captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "input_hashes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "summary",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_by_user_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mode IN ('preview', 'apply')",
            name="ck_metadata_import_receipts_mode",
        ),
        sa.CheckConstraint(
            "outcome IN ('previewed', 'applied', 'rejected')",
            name="ck_metadata_import_receipts_outcome",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "idx_metadata_import_receipts_org_created",
        "metadata_import_receipts",
        ["org_id", "created_at"],
        if_not_exists=True,
    )

    op.create_table(
        "task_source_records",
        sa.Column("org_id", sa.String(64), primary_key=True),
        sa.Column("record_id", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("source_key", postgresql.JSONB(), nullable=False),
        sa.Column("names", postgresql.JSONB(), nullable=False),
        sa.Column("explicit_task_ids", postgresql.JSONB(), nullable=False),
        sa.Column("source_urls", postgresql.JSONB(), nullable=False),
        sa.Column("facts", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column(
            "first_import_id",
            sa.String(64),
            sa.ForeignKey("metadata_import_receipts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "last_import_id",
            sa.String(64),
            sa.ForeignKey("metadata_import_receipts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        *_timestamps(),
        if_not_exists=True,
    )
    op.create_index(
        "idx_task_source_records_org_kind",
        "task_source_records",
        ["org_id", "kind"],
        if_not_exists=True,
    )

    op.create_table(
        "task_aliases",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column(
            "task_id",
            sa.String(128),
            sa.ForeignKey("tasks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "source",
            sa.String(32),
            nullable=False,
            server_default="delivery_backfill",
        ),
        sa.Column(
            "evidence_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "import_id",
            sa.String(64),
            sa.ForeignKey("metadata_import_receipts.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retracted_by_user_id", sa.String(64), nullable=True),
        *_timestamps(),
        if_not_exists=True,
    )
    op.create_index(
        "uq_task_aliases_org_name_live",
        "task_aliases",
        ["org_id", "name"],
        unique=True,
        postgresql_where=sa.text("valid_until IS NULL AND retracted_at IS NULL"),
        if_not_exists=True,
    )
    op.create_index(
        "idx_task_aliases_org_task",
        "task_aliases",
        ["org_id", "task_id"],
        if_not_exists=True,
    )

    op.create_table(
        "task_metadata_assertions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column(
            "task_id",
            sa.String(128),
            sa.ForeignKey("tasks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("field", sa.String(64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "source",
            sa.String(32),
            nullable=False,
            server_default="delivery_backfill",
        ),
        sa.Column(
            "evidence_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "import_id",
            sa.String(64),
            sa.ForeignKey("metadata_import_receipts.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("retracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retracted_by_user_id", sa.String(64), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint(
            "org_id",
            "task_id",
            "field",
            "value",
            name="uq_task_metadata_assertions_fact",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "idx_task_metadata_assertions_org_field",
        "task_metadata_assertions",
        ["org_id", "field", "value"],
        if_not_exists=True,
    )

    op.create_table(
        "task_delivery_history",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column(
            "task_id",
            sa.String(128),
            sa.ForeignKey("tasks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_record_id", sa.String(64), nullable=False),
        sa.Column("customer_label", sa.String(255), nullable=False),
        sa.Column(
            "customer_id",
            sa.String(64),
            sa.ForeignKey("customers.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("program", sa.String(255), nullable=True),
        sa.Column("batch", sa.String(255), nullable=False),
        sa.Column("source_date", sa.String(64), nullable=True),
        sa.Column("customer_task_name", sa.String(255), nullable=True),
        sa.Column("membership", sa.String(128), nullable=False),
        sa.Column(
            "shipped_version_id",
            sa.String(160),
            sa.ForeignKey("task_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("shipped_content_hash", sa.String(128), nullable=True),
        sa.Column(
            "customer_acceptance",
            sa.String(32),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "import_id",
            sa.String(64),
            sa.ForeignKey("metadata_import_receipts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["org_id", "source_record_id"],
            ["task_source_records.org_id", "task_source_records.record_id"],
            ondelete="RESTRICT",
            name="fk_task_delivery_history_source_record",
        ),
        sa.UniqueConstraint(
            "org_id", "source_record_id", name="uq_task_delivery_history_source"
        ),
        sa.CheckConstraint(
            "customer_acceptance IN ('unknown', 'accepted', 'rejected', 'returned')",
            name="ck_task_delivery_history_acceptance",
        ),
        if_not_exists=True,
    )
    for name, columns in [
        ("idx_task_delivery_history_org_task", ["org_id", "task_id"]),
        ("idx_task_delivery_history_org_customer", ["org_id", "customer_label"]),
        ("idx_task_delivery_history_org_customer_id", ["org_id", "customer_id"]),
    ]:
        op.create_index(name, "task_delivery_history", columns, if_not_exists=True)


def downgrade() -> None:
    op.drop_table("task_delivery_history")
    op.drop_table("task_metadata_assertions")
    op.drop_table("task_aliases")
    op.drop_table("task_source_records")
    op.drop_table("metadata_import_receipts")
