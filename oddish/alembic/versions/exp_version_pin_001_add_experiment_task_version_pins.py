"""add experiment_task_version_pins table

Revision ID: exp_version_pin_001
Revises: prod_schema_repair_001
Create Date: 2026-07-23 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "exp_version_pin_001"
down_revision: Union[str, Sequence[str], None] = "prod_schema_repair_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "experiment_task_version_pins",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("org_id", sa.String(64), nullable=True),
        sa.Column("experiment_id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("task_version_id", sa.String(160), nullable=False),
        sa.Column("created_by_user_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        if_not_exists=True,
    )
    op.create_index(
        "ix_experiment_task_version_pins_org_id",
        "experiment_task_version_pins",
        ["org_id"],
        if_not_exists=True,
    )
    op.create_index(
        "idx_experiment_task_version_pins_task_id",
        "experiment_task_version_pins",
        ["task_id"],
        if_not_exists=True,
    )
    # Partial on ``deleted_at IS NULL`` so clearing a pin (a tombstone) leaves
    # the (experiment, task) slot free for a later re-pin.
    op.create_index(
        "uq_experiment_task_version_pins_target",
        "experiment_task_version_pins",
        ["experiment_id", "task_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_experiment_task_version_pins_target",
        table_name="experiment_task_version_pins",
    )
    op.drop_index(
        "idx_experiment_task_version_pins_task_id",
        table_name="experiment_task_version_pins",
    )
    op.drop_index(
        "ix_experiment_task_version_pins_org_id",
        table_name="experiment_task_version_pins",
    )
    op.drop_table("experiment_task_version_pins")
