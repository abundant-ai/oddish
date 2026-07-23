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


def downgrade() -> None:
    # Refuse to silently collapse multiple scoped rows into one global kind.
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
