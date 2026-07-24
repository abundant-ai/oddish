"""add ANALYZER_BLOCK to worker_job_kind enum

Revision ID: analyzer_block_job_001
Revises: custom_qa_merge_001
Create Date: 2026-07-23 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "analyzer_block_job_001"
down_revision: Union[str, Sequence[str], None] = "custom_qa_merge_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE worker_job_kind ADD VALUE IF NOT EXISTS 'ANALYZER_BLOCK'"
        )


def downgrade() -> None:
    # PostgreSQL cannot safely remove an enum label in supported versions.
    pass
