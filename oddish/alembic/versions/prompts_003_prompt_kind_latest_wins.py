"""prompts: key->kind (enum vocabulary), drop active_version (latest wins)

Transforms DBs that ran the original prompts_001 shape. Fresh DBs get the
new shape from create_all()/amended prompts_001, so every step is guarded.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "prompts_003"
down_revision: Union[str, Sequence[str], None] = "prompts_trajectory_merge_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("prompts")}
    if "key" in cols:
        op.alter_column("prompts", "key", new_column_name="kind")
        op.execute(
            "UPDATE prompts SET kind = 'QA_PRE_TRIAL' WHERE kind = 'pre_trial_qa'"
        )
        op.execute(
            "UPDATE prompts SET kind = 'QA_POST_TRIAL' WHERE kind = 'post_trial_qa'"
        )
        op.execute(
            "UPDATE prompts SET kind = 'TRAJECTORY_SUMMARY' "
            "WHERE kind = 'trajectory_summary'"
        )
    if "active_version" in cols:
        op.drop_column("prompts", "active_version")
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("prompts")}
    if "idx_prompts_unique_key" in indexes:
        op.execute(
            "ALTER INDEX idx_prompts_unique_key RENAME TO idx_prompts_unique_kind"
        )

    # DBs that recorded the original prompts_002 (pre-trial columns on
    # ``tasks``) never ran its reworked task_versions form; repeat that
    # reconciliation here, guarded, so both histories converge.
    def _has_col(table: str, col: str) -> bool:
        return col in {c["name"] for c in sa.inspect(bind).get_columns(table)}

    if not _has_col("task_versions", "pre_trial"):
        op.add_column(
            "task_versions",
            sa.Column("pre_trial", sa.dialects.postgresql.JSONB, nullable=True),
        )
        op.add_column(
            "task_versions",
            sa.Column(
                "pre_trial_status",
                sa.Enum(name="jobstatus", create_type=False),
                nullable=True,
            ),
        )
        op.add_column(
            "task_versions", sa.Column("pre_trial_error", sa.Text, nullable=True)
        )
        op.add_column(
            "task_versions",
            sa.Column(
                "pre_trial_started_at", sa.DateTime(timezone=True), nullable=True
            ),
        )
        op.add_column(
            "task_versions",
            sa.Column(
                "pre_trial_finished_at", sa.DateTime(timezone=True), nullable=True
            ),
        )
    legacy_pre_trial_columns = [
        col
        for col in (
            "pre_trial",
            "pre_trial_status",
            "pre_trial_error",
            "pre_trial_started_at",
            "pre_trial_finished_at",
        )
        if _has_col("tasks", col)
    ]
    if legacy_pre_trial_columns:
        assignments = ", ".join(
            f"{col} = COALESCE(task_versions.{col}, tasks.{col})"
            for col in legacy_pre_trial_columns
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
    for col in (
        "pre_trial_finished_at",
        "pre_trial_started_at",
        "pre_trial_error",
        "pre_trial_status",
        "pre_trial",
    ):
        if _has_col("tasks", col):
            op.drop_column("tasks", col)


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {i["name"] for i in sa.inspect(bind).get_indexes("prompts")}
    if "idx_prompts_unique_kind" in indexes:
        op.execute(
            "ALTER INDEX idx_prompts_unique_kind RENAME TO idx_prompts_unique_key"
        )
    cols = {c["name"] for c in sa.inspect(bind).get_columns("prompts")}
    if "active_version" not in cols:
        op.add_column("prompts", sa.Column("active_version", sa.Integer, nullable=True))
        op.execute(
            "UPDATE prompts SET active_version = "
            "(SELECT max(version) FROM prompt_versions WHERE prompt_versions.prompt_id = prompts.id)"
        )
    if "kind" in cols:
        op.execute(
            "UPDATE prompts SET kind = 'pre_trial_qa' WHERE kind = 'QA_PRE_TRIAL'"
        )
        op.execute(
            "UPDATE prompts SET kind = 'post_trial_qa' WHERE kind = 'QA_POST_TRIAL'"
        )
        op.execute(
            "UPDATE prompts SET kind = 'trajectory_summary' "
            "WHERE kind = 'TRAJECTORY_SUMMARY'"
        )
        op.alter_column("prompts", "kind", new_column_name="key")
