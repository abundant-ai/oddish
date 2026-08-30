"""add delivery tables

A delivery is a customer-facing shipping checklist over a set of tasks
(docs/delivery-design.md). Readiness is computed at read time from
existing signals; these tables store only membership, manual ticks,
config, and the finalize snapshot.

Revision ID: deliveries_001
Revises: quota_pause_status_001
Create Date: 2026-08-30 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "deliveries_001"
down_revision: Union[str, Sequence[str], None] = "quota_pause_status_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deliveries",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("created_by_user_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("customer_name", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(32), nullable=False, server_default="active"
        ),
        sa.Column(
            "check_config",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("qa_config", JSONB, nullable=True),
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("public_token", sa.String(128), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_by_user_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'finalized')", name="ck_deliveries_status"
        ),
        sa.CheckConstraint(
            "(is_public AND public_token IS NOT NULL) OR NOT is_public",
            name="ck_deliveries_public_state",
        ),
        sa.CheckConstraint(
            "status <> 'finalized' OR finalized_at IS NOT NULL",
            name="ck_deliveries_finalized_at",
        ),
    )
    op.create_index(
        "uq_deliveries_public_token",
        "deliveries",
        ["public_token"],
        unique=True,
        postgresql_where=sa.text("public_token IS NOT NULL"),
    )
    op.create_index(
        "idx_deliveries_org_created_at", "deliveries", ["org_id", "created_at"]
    )

    op.create_table(
        "delivery_tasks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "delivery_id",
            sa.String(64),
            sa.ForeignKey("deliveries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.String(128),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "pinned_version_id",
            sa.String(160),
            sa.ForeignKey("task_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("customer_note", sa.Text(), nullable=True),
        sa.Column("internal_note", sa.Text(), nullable=True),
        sa.Column(
            "is_visible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("delivery_id", "task_id", name="uq_delivery_tasks_task"),
    )
    op.create_index(
        "idx_delivery_tasks_delivery_order",
        "delivery_tasks",
        ["delivery_id", "sort_order"],
    )

    op.create_table(
        "delivery_manual_checks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "delivery_id",
            sa.String(64),
            sa.ForeignKey("deliveries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "delivery_task_id",
            sa.String(64),
            sa.ForeignKey("delivery_tasks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("check_key", sa.String(64), nullable=False),
        sa.Column(
            "task_version_id",
            sa.String(160),
            sa.ForeignKey("task_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("checked_by_user_id", sa.String(64), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_delivery_manual_checks_task",
        "delivery_manual_checks",
        ["delivery_id", "delivery_task_id", "check_key"],
        unique=True,
        postgresql_where=sa.text("delivery_task_id IS NOT NULL"),
    )
    op.create_index(
        "uq_delivery_manual_checks_delivery",
        "delivery_manual_checks",
        ["delivery_id", "check_key"],
        unique=True,
        postgresql_where=sa.text("delivery_task_id IS NULL"),
    )

    op.create_table(
        "delivery_snapshots",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "delivery_id",
            sa.String(64),
            sa.ForeignKey("deliveries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.Column(
            "scope", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("created_by_user_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_delivery_snapshots_delivery",
        "delivery_snapshots",
        ["delivery_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("delivery_snapshots")
    op.drop_table("delivery_manual_checks")
    op.drop_table("delivery_tasks")
    op.drop_table("deliveries")
