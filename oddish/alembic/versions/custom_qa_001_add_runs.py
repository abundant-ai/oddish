"""add custom QA runs (qa_runs lineage table)"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "custom_qa_001"
down_revision = "prompts_003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "qa_runs" in sa.inspect(op.get_bind()).get_table_names():
        return
    jobstatus = sa.dialects.postgresql.ENUM(name="jobstatus", create_type=False)
    op.create_table(
        "qa_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("prompt_version_id", sa.String(64), nullable=False),
        sa.Column("analyzer_block_id", sa.String(64), nullable=True),
        sa.Column("triggered_by_user_id", sa.String(64), nullable=True),
        sa.Column("scope_type", sa.String(32), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("reasoning_effort", sa.String(32), nullable=True),
        sa.Column("llm_client_type", sa.String(32), nullable=False),
        sa.Column("status", jobstatus, nullable=False),
        sa.Column("output", JSONB, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("run_config", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["prompt_version_id"], ["prompt_versions.id"]),
    )
    op.create_index("ix_qa_runs_org_id", "qa_runs", ["org_id"])
    op.create_index("ix_qa_runs_prompt_version_id", "qa_runs", ["prompt_version_id"])
    op.create_index("ix_qa_runs_analyzer_block_id", "qa_runs", ["analyzer_block_id"])


def downgrade() -> None:
    op.drop_table("qa_runs")
