"""Add experiment-scoped curated QA reports.

Revision ID: qa_reports_001
Revises: quota_pause_status_001
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "qa_reports_001"
down_revision: Union[str, Sequence[str], None] = "quota_pause_status_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A fresh local database is first built from current metadata. In that
    # case these tables already exist when Alembic reaches this revision.
    tables = {
        "qa_reports",
        "qa_report_tasks",
        "qa_report_items",
        "qa_report_publications",
    }
    present = {name for name in tables if sa.inspect(op.get_bind()).has_table(name)}
    if present == tables:
        return
    if present:
        missing = ", ".join(sorted(tables - present))
        raise RuntimeError(f"Incomplete QA report schema; missing: {missing}")

    op.create_table(
        "qa_reports",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=False),
        sa.Column(
            "experiment_id",
            sa.String(64),
            sa.ForeignKey("experiments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.String(64), nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("conclusion", sa.Text(), nullable=True),
        sa.Column("customer_note", sa.Text(), nullable=True),
        sa.Column("internal_note", sa.Text(), nullable=True),
        sa.Column("draft_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("public_token", sa.String(128), nullable=True),
        sa.Column("published_snapshot_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(is_public AND public_token IS NOT NULL "
            "AND published_snapshot_id IS NOT NULL) "
            "OR (NOT is_public AND public_token IS NULL)",
            name="ck_qa_reports_public_state",
        ),
    )
    op.create_index(
        "uq_qa_reports_experiment_live",
        "qa_reports",
        ["experiment_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_qa_reports_public_token",
        "qa_reports",
        ["public_token"],
        unique=True,
        postgresql_where=sa.text("public_token IS NOT NULL"),
    )
    op.create_index(
        "idx_qa_reports_org_experiment",
        "qa_reports",
        ["org_id", "experiment_id"],
    )

    op.create_table(
        "qa_report_tasks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "report_id",
            sa.String(64),
            sa.ForeignKey("qa_reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.String(128),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_version_id",
            sa.String(160),
            sa.ForeignKey("task_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("internal_note", sa.Text(), nullable=True),
        sa.Column("is_visible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("report_id", "task_id", name="uq_qa_report_tasks_task"),
        sa.UniqueConstraint("id", "report_id", name="uq_qa_report_tasks_id_report"),
    )
    op.create_index(
        "idx_qa_report_tasks_report_order",
        "qa_report_tasks",
        ["report_id", "sort_order"],
    )

    op.create_table(
        "qa_report_items",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "report_id",
            sa.String(64),
            sa.ForeignKey("qa_reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "report_task_id",
            sa.String(64),
            nullable=False,
        ),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(512), nullable=False),
        sa.Column("source_label", sa.String(255), nullable=False),
        sa.Column("source_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_title", sa.Text(), nullable=False),
        sa.Column("source_summary", sa.Text(), nullable=True),
        sa.Column("source_recommendation", sa.Text(), nullable=True),
        sa.Column("source_evidence", sa.Text(), nullable=True),
        sa.Column(
            "is_visible", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "include_evidence", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("recommendation", sa.Text(), nullable=True),
        sa.Column("customer_note", sa.Text(), nullable=True),
        sa.Column("internal_note", sa.Text(), nullable=True),
        sa.Column("tier", sa.String(32), nullable=True),
        sa.Column("dimension", sa.String(64), nullable=True),
        sa.Column("file", sa.Text(), nullable=True),
        sa.Column("line_start", sa.Integer(), nullable=True),
        sa.Column("line_end", sa.Integer(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("outcome", sa.String(64), nullable=True),
        sa.Column("confidence", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "report_id", "source_ref", name="uq_qa_report_items_source"
        ),
        sa.ForeignKeyConstraint(
            ["report_task_id", "report_id"],
            ["qa_report_tasks.id", "qa_report_tasks.report_id"],
            name="fk_qa_report_items_task_report",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "source_type IN ('pre_trial', 'verdict', 'trial_analysis')",
            name="ck_qa_report_items_source_type",
        ),
    )
    op.create_index(
        "idx_qa_report_items_task_order",
        "qa_report_items",
        ["report_task_id", "sort_order"],
    )

    op.create_table(
        "qa_report_publications",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "report_id",
            sa.String(64),
            sa.ForeignKey("qa_reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "scope_task_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("published_by_user_id", sa.String(64), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_qa_report_publications_report",
        "qa_report_publications",
        ["report_id", "published_at"],
    )


def downgrade() -> None:
    op.drop_table("qa_report_publications")
    op.drop_table("qa_report_items")
    op.drop_table("qa_report_tasks")
    op.drop_table("qa_reports")
