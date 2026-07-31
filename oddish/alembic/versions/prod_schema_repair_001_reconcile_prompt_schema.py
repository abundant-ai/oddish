"""Reconcile prompt and pre-trial schema after historical migration rewrites.

Some databases recorded the original ``prompts_002``/``prompts_003`` files
before their contents were amended.  Replaying the convergence work under a
new revision makes those databases match the current ORM without requiring an
unsafe stamp or edits to already-applied migration history.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "prod_schema_repair_001"
down_revision: Union[str, Sequence[str], None] = "analyzer_block_job_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind: sa.engine.Connection, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


LEGACY_PRE_TRIAL_COLUMNS = (
    "pre_trial",
    "pre_trial_status",
    "pre_trial_error",
    "pre_trial_started_at",
    "pre_trial_finished_at",
)


def _assert_no_orphaned_pre_trial_data(
    bind: sa.engine.Connection, legacy_columns: list[str]
) -> None:
    """Refuse to drop pre-trial data that the copy below would not carry over.

    The copy joins on ``tasks.current_version_id``, which is nullable, so a task
    without a current version (or pointing at a missing one) would have its
    pre-trial data silently destroyed.  ``downgrade()`` cannot reconstruct it,
    so stop and let an operator decide instead.
    """
    has_data = " OR ".join(f"tasks.{column} IS NOT NULL" for column in legacy_columns)
    orphaned = bind.execute(
        sa.text(
            f"""
            SELECT count(*) FROM tasks
            WHERE ({has_data})
              AND NOT EXISTS (
                  SELECT 1 FROM task_versions
                  WHERE task_versions.id = tasks.current_version_id
              )
            """
        )
    ).scalar_one()
    if orphaned:
        raise RuntimeError(
            f"{orphaned} task(s) carry pre-trial data with no matching "
            "task_versions row (current_version_id is NULL or dangling). "
            "Dropping tasks.pre_trial_* would destroy it and downgrade() "
            "cannot restore it. Re-point or clear those rows, then re-run."
        )


def upgrade() -> None:
    bind = op.get_bind()

    # Lock ordering: production traffic touches `tasks` before `task_versions`,
    # so take the exclusive lock on `tasks` up front while holding nothing else.
    # Acquiring it after the UPDATE below (which locks `task_versions` first)
    # inverts that order and deadlocks against live queries. lock_timeout keeps
    # a blocked migration from queueing all `tasks` traffic behind its request.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE tasks IN ACCESS EXCLUSIVE MODE")

    task_version_columns = _columns(bind, "task_versions")

    if "pre_trial" not in task_version_columns:
        op.add_column(
            "task_versions",
            sa.Column("pre_trial", postgresql.JSONB, nullable=True),
        )
    if "pre_trial_status" not in task_version_columns:
        op.add_column(
            "task_versions",
            sa.Column(
                "pre_trial_status",
                postgresql.ENUM(name="jobstatus", create_type=False),
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
        column for column in LEGACY_PRE_TRIAL_COLUMNS if column in task_columns
    ]
    if legacy_columns:
        _assert_no_orphaned_pre_trial_data(bind, legacy_columns)
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
