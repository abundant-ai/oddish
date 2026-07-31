"""Partition scoped prompt uniqueness by organization.

Revision ID: prompt_scope_org_001
Revises: analyzer_block_prompt_id_001
"""

from alembic import op
import sqlalchemy as sa


revision = "prompt_scope_org_001"
down_revision = "analyzer_block_prompt_id_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("idx_prompts_unique_kind_scope", table_name="prompts")
    op.create_index(
        "idx_prompts_unique_kind_scope",
        "prompts",
        [
            "kind",
            sa.text("COALESCE(org_id, '')"),
            sa.text("COALESCE(scope_type, '')"),
            sa.text("COALESCE(scope_id, '')"),
        ],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_prompts_unique_kind_scope", table_name="prompts")
    op.create_index(
        "idx_prompts_unique_kind_scope",
        "prompts",
        [
            "kind",
            sa.text("COALESCE(scope_type, '')"),
            sa.text("COALESCE(scope_id, '')"),
        ],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
