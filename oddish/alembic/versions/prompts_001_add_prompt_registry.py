"""add prompt registry tables

Two tables for DB-backed, versioned analyzer prompts. ``000_initial_schema``
runs ``create_all()`` on fresh DBs, so guard with the inspector.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "prompts_001"
down_revision: Union[str, Sequence[str], None] = "analyzers_008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = sa.inspect(bind).get_table_names()
    if "prompts" not in existing:
        op.create_table(
            "prompts",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("kind", sa.String(128), nullable=False),
            sa.Column("description", sa.Text, nullable=False, server_default=""),
        )
        op.create_index(
            "idx_prompts_unique_kind",
            "prompts",
            ["kind"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        )
    if "prompt_versions" not in existing:
        op.create_table(
            "prompt_versions",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("prompt_id", sa.String(64), nullable=False),
            sa.Column("version", sa.Integer, nullable=False),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_by", sa.String(64), nullable=True),
            sa.ForeignKeyConstraint(
                ["prompt_id"], ["prompts.id"], ondelete="CASCADE"
            ),
            sa.UniqueConstraint(
                "prompt_id", "version", name="uq_prompt_versions_prompt_version"
            ),
        )
        op.create_index(
            "ix_prompt_versions_prompt_id", "prompt_versions", ["prompt_id"]
        )


def downgrade() -> None:
    op.drop_index("ix_prompt_versions_prompt_id", table_name="prompt_versions")
    op.drop_table("prompt_versions")
    op.execute("DROP INDEX IF EXISTS idx_prompts_unique_kind")
    op.execute("DROP INDEX IF EXISTS idx_prompts_unique_key")
    op.drop_table("prompts")
