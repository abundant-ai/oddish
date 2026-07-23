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


def upgrade() -> None:
    op.add_column(
        "analyzer_blocks", sa.Column("prompt_id", sa.String(64), nullable=True)
    )
    op.create_index(
        "ix_analyzer_blocks_prompt_id", "analyzer_blocks", ["prompt_id"]
    )
    # Historical rows predate scoping, so every one of them belongs to the
    # global row for its kind. Backfilling makes existing usage counts survive.
    op.execute(
        """
        UPDATE analyzer_blocks AS b
           SET prompt_id = p.id
          FROM prompts AS p
         WHERE p.kind = b.prompt_key
           AND p.scope_type IS NULL
           AND b.prompt_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_analyzer_blocks_prompt_id", table_name="analyzer_blocks")
    op.drop_column("analyzer_blocks", "prompt_id")
