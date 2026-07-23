"""Reconcile prompt and pre-trial schema after historical migration rewrites.

Some databases recorded the original ``prompts_002``/``prompts_003`` files
before their contents were amended.  Replaying the convergence work under a
new revision makes those databases match the current ORM without requiring an
unsafe stamp or edits to already-applied migration history.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "prod_schema_repair_001"
down_revision: Union[str, Sequence[str], None] = "analyzer_block_job_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind: sa.engine.Connection, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    task_version_columns = _columns(bind, "task_versions")

    if "pre_trial" not in task_version_columns:
        op.add_column(
            "task_versions",
            sa.Column("pre_trial", sa.dialects.postgresql.JSONB, nullable=True),
        )
    if "pre_trial_status" not in task_version_columns:
        op.add_column(
            "task_versions",
            sa.Column(
                "pre_trial_status",
                sa.Enum(name="jobstatus", create_type=False),
                nullable=True,
            ),
        )
    if "pre_trial_error" not in task_version_columns:
        op.add_column(
            "task_versions", sa.Column("pre_trial_error", sa.Text, nullable=True)
        )
    if "pre_trial_started_at" not in task_version_columns:
        op.add_column(
            "task_versions",
            sa.Column(
                "pre_trial_started_at", sa.DateTime(timezone=True), nullable=True
            ),
        )
    if "pre_trial_finished_at" not in task_version_columns:
        op.add_column(
            "task_versions",
            sa.Column(
                "pre_trial_finished_at", sa.DateTime(timezone=True), nullable=True
            ),
        )

    # Preserve data from the original migration shape before removing it.
    task_columns = _columns(bind, "tasks")
    legacy_columns = [
        column
        for column in (
            "pre_trial",
            "pre_trial_status",
            "pre_trial_error",
            "pre_trial_started_at",
            "pre_trial_finished_at",
        )
        if column in task_columns
    ]
    if legacy_columns:
        assignments = ", ".join(
            f"{column} = COALESCE(task_versions.{column}, tasks.{column})"
            for column in legacy_columns
        )
        op.execute(
            sa.text(
                f"""
                UPDATE task_versions
                SET {assignments}
                FROM tasks
                WHERE tasks.current_version_id = task_versions.id
                """
            )
        )
        for column in reversed(legacy_columns):
            op.drop_column("tasks", column)


def downgrade() -> None:
    # This is a convergence repair for multiple historical database shapes.
    # Removing columns would destroy valid pre-trial data and cannot reliably
    # reconstruct the schema that existed before the repair.
    pass
