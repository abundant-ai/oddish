"""Add version-owned task QA run provenance.

Revision ID: task_qa_runs_001
Revises: agentcap01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "task_qa_runs_001"
down_revision: str | Sequence[str] | None = "agentcap01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables(bind: sa.engine.Connection) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind: sa.engine.Connection, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _indexes(bind: sa.engine.Connection, table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def _fk_on(bind: sa.engine.Connection, table: str, column: str) -> str | None:
    for fk in sa.inspect(bind).get_foreign_keys(table):
        if fk.get("constrained_columns") == [column]:
            return fk.get("name") or ""
    return None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # New code refuses to guess a source version.  Active jobs cross the deploy
    # only when the existing payload already pins one; operators must drain or
    # cancel older unscoped work before applying the migration.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM worker_jobs
            WHERE kind::text IN ('QA', 'VERDICT')
              AND COALESCE(payload->>'mode', '') <> 'pre_trial'
              AND status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
              AND NULLIF(payload->>'task_version_id', '') IS NULL
          ) THEN
            RAISE EXCEPTION USING MESSAGE =
              'active full-QA worker job lacks task_version_id; drain/cancel it or re-upload the legacy task before deploying task_qa_runs';
          END IF;
        END $$
        """
    )

    bind = op.get_bind()
    # ``000_initial_schema`` builds fresh databases from the live model graph,
    # so every operation below must tolerate that schema already being present.
    if "task_qa_runs" not in _tables(bind):
        op.create_table(
            "task_qa_runs",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("task_id", sa.String(length=128), nullable=False),
            sa.Column("task_version_id", sa.String(length=160), nullable=False),
            sa.Column("worker_job_id", sa.String(length=64), nullable=False),
            sa.Column("disposition", sa.String(length=16), nullable=True),
            sa.Column(
                "input_trial_ids",
                postgresql.ARRAY(sa.Text()),
                server_default=sa.text("'{}'::text[]"),
                nullable=False,
            ),
            sa.Column(
                "input_analysis_fingerprints",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column(
                "verdict", postgresql.JSONB(astext_type=sa.Text()), nullable=True
            ),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("pre_trial_block_id", sa.String(length=64), nullable=True),
            sa.Column("verdict_block_id", sa.String(length=64), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("NOW()"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("NOW()"),
                nullable=False,
            ),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "disposition IS NULL OR disposition IN "
                "('published', 'failed', 'cancelled', 'superseded')",
                name="ck_task_qa_runs_disposition",
            ),
            sa.ForeignKeyConstraint(
                ["task_id"], ["tasks.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["task_version_id"], ["task_versions.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["worker_job_id"], ["worker_jobs.id"], ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["pre_trial_block_id"], ["analyzer_blocks.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["verdict_block_id"], ["analyzer_blocks.id"], ondelete="SET NULL"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "worker_job_id", name="uq_task_qa_runs_worker_job_id"
            ),
        )
    if "idx_task_qa_runs_version_created" not in _indexes(bind, "task_qa_runs"):
        op.execute(
            "CREATE INDEX idx_task_qa_runs_version_created "
            "ON task_qa_runs (task_version_id, created_at DESC)"
        )

    op.execute(
        """
        INSERT INTO task_qa_runs (
          id, task_id, task_version_id, worker_job_id,
          input_trial_ids, input_analysis_fingerprints,
          started_at, created_at, updated_at
        )
        SELECT
          'qa_' || w.id,
          COALESCE(NULLIF(w.payload->>'task_id', ''), w.subject_id),
          w.payload->>'task_version_id',
          w.id,
          '{}'::text[],
          '{}'::jsonb,
          w.started_at,
          w.created_at,
          w.updated_at
        FROM worker_jobs w
        WHERE w.kind::text IN ('QA', 'VERDICT')
          AND COALESCE(w.payload->>'mode', '') <> 'pre_trial'
          AND w.status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
        ON CONFLICT (worker_job_id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE worker_jobs w
        SET payload = jsonb_set(w.payload, '{qa_run_id}', to_jsonb(r.id), true)
        FROM task_qa_runs r
        WHERE r.worker_job_id = w.id
          AND COALESCE(w.payload->>'qa_run_id', '') = ''
        """
    )

    task_columns = _columns(bind, "tasks")
    if "published_qa_run_id" not in task_columns:
        op.add_column(
            "tasks",
            sa.Column("published_qa_run_id", sa.String(length=64), nullable=True),
        )
    if "verdict_version_id" not in task_columns:
        op.add_column(
            "tasks",
            sa.Column("verdict_version_id", sa.String(length=160), nullable=True),
        )
    if _fk_on(bind, "tasks", "published_qa_run_id") is None:
        op.create_foreign_key(
            "fk_tasks_published_qa_run_id_task_qa_runs",
            "tasks",
            "task_qa_runs",
            ["published_qa_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if _fk_on(bind, "tasks", "verdict_version_id") is None:
        op.create_foreign_key(
            "fk_tasks_verdict_version_id_task_versions",
            "tasks",
            "task_versions",
            ["verdict_version_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    published_run_fk = _fk_on(bind, "tasks", "published_qa_run_id")
    if published_run_fk:
        op.drop_constraint(published_run_fk, "tasks", type_="foreignkey")
    verdict_version_fk = _fk_on(bind, "tasks", "verdict_version_id")
    if verdict_version_fk:
        op.drop_constraint(verdict_version_fk, "tasks", type_="foreignkey")
    task_columns = _columns(bind, "tasks")
    if "verdict_version_id" in task_columns:
        op.drop_column("tasks", "verdict_version_id")
    if "published_qa_run_id" in task_columns:
        op.drop_column("tasks", "published_qa_run_id")
    if "task_qa_runs" in _tables(bind):
        if "idx_task_qa_runs_version_created" in _indexes(bind, "task_qa_runs"):
            op.drop_index("idx_task_qa_runs_version_created", table_name="task_qa_runs")
        op.drop_table("task_qa_runs")
