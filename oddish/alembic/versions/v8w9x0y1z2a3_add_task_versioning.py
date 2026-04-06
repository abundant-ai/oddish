"""add task versioning

Revision ID: v8w9x0y1z2a3
Revises: u7v8w9x0y1z2
Create Date: 2026-04-03 18:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "v8w9x0y1z2a3"
down_revision: Union[str, Sequence[str], None] = "u7v8w9x0y1z2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- task_versions table ---
    op.create_table(
        "task_versions",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(64),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("task_path", sa.Text, nullable=False),
        sa.Column("task_s3_key", sa.Text, nullable=True),
        sa.Column("content_hash", sa.String(128), nullable=True),
        sa.Column("message", sa.Text, nullable=True),
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
    )

    op.create_index(
        "idx_task_versions_task_id_version",
        "task_versions",
        ["task_id", "version"],
        unique=True,
    )

    # --- tasks: add current_version_id (FK added separately to avoid
    #     circular dependency issues during table creation) ---
    op.add_column(
        "tasks",
        sa.Column("current_version_id", sa.String(128), nullable=True),
    )
    op.create_foreign_key(
        "fk_tasks_current_version_id",
        "tasks",
        "task_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- trials: pin each trial to a task version ---
    op.add_column(
        "trials",
        sa.Column(
            "task_version_id",
            sa.String(128),
            sa.ForeignKey("task_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_trials_task_version_id",
        "trials",
        ["task_version_id"],
    )

    # --- enforce unique (org_id, name) on tasks ---
    # COALESCE handles NULL org_id (OSS) so the constraint works for both
    # hosted (org_id set) and OSS (org_id NULL) deployments.
    op.create_index(
        "idx_tasks_unique_org_name",
        "tasks",
        [sa.text("COALESCE(org_id, '')"), "name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("idx_tasks_unique_org_name", table_name="tasks")
    op.drop_index("idx_trials_task_version_id", table_name="trials")
    op.drop_column("trials", "task_version_id")
    op.drop_constraint("fk_tasks_current_version_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "current_version_id")
    op.drop_index("idx_task_versions_task_id_version", table_name="task_versions")
    op.drop_table("task_versions")
