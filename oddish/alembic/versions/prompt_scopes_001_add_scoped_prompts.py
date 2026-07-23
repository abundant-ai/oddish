"""Add tenant and domain scopes to versioned prompts.

Revision ID: prompt_scopes_001
Revises: prod_schema_repair_001
"""

from alembic import op
import sqlalchemy as sa


revision = "prompt_scopes_001"
down_revision = "prod_schema_repair_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("prompts", sa.Column("scope_type", sa.String(32), nullable=True))
    op.add_column("prompts", sa.Column("scope_id", sa.String(160), nullable=True))
    op.add_column("prompts", sa.Column("org_id", sa.String(64), nullable=True))
    op.drop_index("idx_prompts_unique_kind", table_name="prompts")
    op.create_index(
        "idx_prompts_unique_kind_scope",
        "prompts",
        ["kind", sa.text("COALESCE(scope_type, '')"), sa.text("COALESCE(scope_id, '')")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_prompts_org_id", "prompts", ["org_id"])
    op.create_table(
        "qa_assignments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("prompt_id", sa.String(64), sa.ForeignKey("prompts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=True),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("scope_type", sa.String(32), nullable=False),
        sa.Column("scope_id", sa.String(160), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("reasoning_effort", sa.String(32), nullable=True),
        sa.Column("llm_client_type", sa.String(32), nullable=False),
        sa.Column("allow_oddish_cli", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_by_user_id", sa.String(64), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_qa_assignments_org_id", "qa_assignments", ["org_id"])
    op.create_index("ix_qa_assignments_org_scope", "qa_assignments", ["org_id", "scope_type", "scope_id"])
    op.add_column("analyzer_runs", sa.Column("qa_assignment_id", sa.String(64), nullable=True))
    op.add_column("analyzer_runs", sa.Column("stage_event_key", sa.String(255), nullable=True))
    op.create_foreign_key(
        "fk_analyzer_runs_qa_assignment_id", "analyzer_runs", "qa_assignments",
        ["qa_assignment_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_analyzer_runs_qa_assignment_id", "analyzer_runs", ["qa_assignment_id"])
    op.create_index(
        "uq_analyzer_runs_assignment_event", "analyzer_runs",
        ["qa_assignment_id", "stage_event_key"], unique=True,
        postgresql_where=sa.text("qa_assignment_id IS NOT NULL")
    )


def downgrade() -> None:
    # Refuse to silently collapse multiple scoped rows into one global kind.
    op.drop_index("uq_analyzer_runs_assignment_event", table_name="analyzer_runs")
    op.drop_index("ix_analyzer_runs_qa_assignment_id", table_name="analyzer_runs")
    op.drop_constraint("fk_analyzer_runs_qa_assignment_id", "analyzer_runs", type_="foreignkey")
    op.drop_column("analyzer_runs", "stage_event_key")
    op.drop_column("analyzer_runs", "qa_assignment_id")
    op.drop_index("ix_qa_assignments_org_scope", table_name="qa_assignments")
    op.drop_index("ix_qa_assignments_org_id", table_name="qa_assignments")
    op.drop_table("qa_assignments")
    op.drop_index("ix_prompts_org_id", table_name="prompts")
    op.drop_index("idx_prompts_unique_kind_scope", table_name="prompts")
    op.execute("DELETE FROM prompts WHERE scope_type IS NOT NULL")
    op.create_index(
        "idx_prompts_unique_kind",
        "prompts",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_column("prompts", "org_id")
    op.drop_column("prompts", "scope_id")
    op.drop_column("prompts", "scope_type")
