"""Attribute analyzer blocks to a specific prompts row.

Revision ID: analyzer_block_prompt_id_001
Revises: prompt_scopes_001
"""

from alembic import op
import sqlalchemy as sa

revision = "analyzer_block_prompt_id_001"
down_revision = "prompt_scopes_001"
branch_labels = None
depends_on = None


def _columns(bind: sa.engine.Connection, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _indexes(bind: sa.engine.Connection, table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()

    if "prompt_id" not in _columns(bind, "analyzer_blocks"):
        op.add_column(
            "analyzer_blocks", sa.Column("prompt_id", sa.String(64), nullable=True)
        )
    if "ix_analyzer_blocks_prompt_id" not in _indexes(bind, "analyzer_blocks"):
        op.create_index(
            "ix_analyzer_blocks_prompt_id", "analyzer_blocks", ["prompt_id"]
        )
    # Historical rows predate scoping, so every one of them belongs to the
    # global row for its kind. Backfilling makes existing usage counts survive.
    # deleted_at IS NULL matches the live-uniqueness partial index: raw SQL
    # bypasses the ORM soft-delete listener, so a soft-deleted duplicate kind
    # would otherwise make this join non-deterministic.
    op.execute(
        """
        UPDATE analyzer_blocks AS b
           SET prompt_id = p.id
          FROM prompts AS p
         WHERE p.kind = b.prompt_key
           AND p.scope_type IS NULL
           AND p.deleted_at IS NULL
           AND b.prompt_id IS NULL
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "ix_analyzer_blocks_prompt_id" in _indexes(bind, "analyzer_blocks"):
        op.drop_index("ix_analyzer_blocks_prompt_id", table_name="analyzer_blocks")
    if "prompt_id" in _columns(bind, "analyzer_blocks"):
        op.drop_column("analyzer_blocks", "prompt_id")
