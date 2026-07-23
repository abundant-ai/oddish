"""link analyzer blocks and analysis costs to task-level QA subjects

Revision ID: analyzer_task_cost_001
Revises: costexclkeys01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "analyzer_task_cost_001"
down_revision: Union[str, Sequence[str], None] = "costexclkeys01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    if "task_id" not in _columns("analyzer_blocks"):
        op.add_column(
            "analyzer_blocks", sa.Column("task_id", sa.String(128), nullable=True)
        )
    if "ix_analyzer_blocks_task_id" not in _indexes("analyzer_blocks"):
        op.create_index(
            "ix_analyzer_blocks_task_id", "analyzer_blocks", ["task_id"], unique=False
        )

    cost_columns = _columns("analysis_costs")
    if "task_id" not in cost_columns:
        op.add_column(
            "analysis_costs", sa.Column("task_id", sa.String(128), nullable=True)
        )
    if "analyzer_id" not in cost_columns:
        op.add_column(
            "analysis_costs", sa.Column("analyzer_id", sa.String(64), nullable=True)
        )
    cost_indexes = _indexes("analysis_costs")
    if "ix_analysis_costs_task_id" not in cost_indexes:
        op.create_index(
            "ix_analysis_costs_task_id", "analysis_costs", ["task_id"], unique=False
        )
    if "ix_analysis_costs_analyzer_id" not in cost_indexes:
        op.create_index(
            "ix_analysis_costs_analyzer_id",
            "analysis_costs",
            ["analyzer_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_analysis_costs_analyzer_id", table_name="analysis_costs")
    op.drop_index("ix_analysis_costs_task_id", table_name="analysis_costs")
    op.drop_column("analysis_costs", "analyzer_id")
    op.drop_column("analysis_costs", "task_id")

    op.drop_index("ix_analyzer_blocks_task_id", table_name="analyzer_blocks")
    op.drop_column("analyzer_blocks", "task_id")
