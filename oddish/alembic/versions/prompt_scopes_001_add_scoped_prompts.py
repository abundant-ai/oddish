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


def _columns(bind: sa.engine.Connection, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _indexes(bind: sa.engine.Connection, table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind, "prompts")

    if "scope_type" not in columns:
        op.add_column("prompts", sa.Column("scope_type", sa.String(32), nullable=True))
    if "scope_id" not in columns:
        op.add_column("prompts", sa.Column("scope_id", sa.String(160), nullable=True))
    if "org_id" not in columns:
        op.add_column("prompts", sa.Column("org_id", sa.String(64), nullable=True))

    indexes = _indexes(bind, "prompts")
    if "idx_prompts_unique_kind" in indexes:
        op.drop_index("idx_prompts_unique_kind", table_name="prompts")
    if "idx_prompts_unique_kind_scope" not in indexes:
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
    if "ix_prompts_org_id" not in indexes:
        op.create_index("ix_prompts_org_id", "prompts", ["org_id"])


def downgrade() -> None:
    # Refuse to silently collapse multiple scoped rows into one global kind.
    bind = op.get_bind()
    indexes = _indexes(bind, "prompts")
    if "ix_prompts_org_id" in indexes:
        op.drop_index("ix_prompts_org_id", table_name="prompts")
    if "idx_prompts_unique_kind_scope" in indexes:
        op.drop_index("idx_prompts_unique_kind_scope", table_name="prompts")
    op.execute("DELETE FROM prompts WHERE scope_type IS NOT NULL")
    if "idx_prompts_unique_kind" not in indexes:
        op.create_index(
            "idx_prompts_unique_kind",
            "prompts",
            ["kind"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        )
    columns = _columns(bind, "prompts")
    if "org_id" in columns:
        op.drop_column("prompts", "org_id")
    if "scope_id" in columns:
        op.drop_column("prompts", "scope_id")
    if "scope_type" in columns:
        op.drop_column("prompts", "scope_type")
