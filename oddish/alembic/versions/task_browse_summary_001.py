"""Add task-version task-browser summaries.

Revision ID: task_browse_summary_001
Revises: verdict_state_001
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "task_browse_summary_001"
down_revision: Union[str, Sequence[str], None] = "verdict_state_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SCOPE = """
    deleted_at IS NULL
    AND superseded_by_trial_id IS NULL
    AND is_probe IS NOT TRUE
    AND (idempotency_key IS NULL OR idempotency_key NOT LIKE 'combine:%')
"""


def _table_exists(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _index_exists(table: str, name: str) -> bool:
    return name in {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)
    }


def upgrade() -> None:
    if not _table_exists("task_version_browse_summaries"):
        op.create_table(
            "task_version_browse_summaries",
            sa.Column("task_version_id", sa.String(160), primary_key=True),
            sa.Column("task_id", sa.String(128), nullable=False),
            sa.Column("last_run_at", sa.DateTime(timezone=True)),
            sa.Column("total_trials", sa.Integer(), nullable=False),
            sa.Column("completed_trials", sa.Integer(), nullable=False),
            sa.Column("failed_trials", sa.Integer(), nullable=False),
            sa.Column("reward_success", sa.Integer(), nullable=False),
            sa.Column("reward_sum", sa.Float(), nullable=False),
            sa.Column("reward_total", sa.Integer(), nullable=False),
            sa.Column("pass_count", sa.Integer(), nullable=False),
            sa.Column("partial_count", sa.Integer(), nullable=False),
            sa.Column("fail_count", sa.Integer(), nullable=False),
            sa.Column("harness_count", sa.Integer(), nullable=False),
            sa.Column("skipped_count", sa.Integer(), nullable=False),
            sa.Column("pending_count", sa.Integer(), nullable=False),
            sa.Column("cost_breakdown", postgresql.JSONB(), nullable=False),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("NOW()"),
            ),
            sa.ForeignKeyConstraint(
                ["task_version_id"], ["task_versions.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        )
    op.alter_column(
        "task_version_browse_summaries",
        "updated_at",
        server_default=sa.text("NOW()"),
    )
    if not _index_exists(
        "task_version_browse_summaries", "idx_task_browse_summary_task_id"
    ):
        op.create_index(
            "idx_task_browse_summary_task_id",
            "task_version_browse_summaries",
            ["task_id"],
        )
    if not _index_exists("trials", "idx_trials_task_browse_preview"):
        op.create_index(
            "idx_trials_task_browse_preview",
            "trials",
            [
                "task_id",
                "task_version_id",
                sa.text("created_at DESC"),
                sa.text("id DESC"),
            ],
            postgresql_where=sa.text(_SCOPE),
        )

    op.execute(
        """
        INSERT INTO task_version_browse_summaries (
            task_version_id, task_id, total_trials, completed_trials,
            failed_trials, reward_success, reward_sum, reward_total,
            pass_count, partial_count, fail_count, harness_count,
            skipped_count, pending_count, cost_breakdown, updated_at
        )
        SELECT id, task_id, 0, 0, 0, 0, 0.0, 0, 0, 0, 0, 0, 0, 0,
               '[]'::jsonb, NOW()
        FROM task_versions
        ON CONFLICT (task_version_id) DO NOTHING
        """
    )
    op.execute(
        f"""
        WITH scoped AS (
            SELECT task_version_id,
                   GREATEST(
                       COALESCE(finished_at, created_at),
                       COALESCE(started_at, created_at), created_at
                   ) AS activity_at,
                   status::text AS status,
                   reward,
                   CASE
                       WHEN status::text = 'SKIPPED' THEN 'skipped'
                       WHEN error_message IS NOT NULL
                            AND NOT (
                                (error_message LIKE '%AgentTimeoutError%'
                                 OR error_message LIKE '%Agent execution timed out%')
                                AND reward IS NOT NULL
                            ) THEN 'harness'
                       WHEN status::text = 'FAILED' THEN
                           CASE WHEN (error_message LIKE '%AgentTimeoutError%'
                                      OR error_message LIKE '%Agent execution timed out%')
                                     AND reward IS NOT NULL
                                THEN CASE WHEN reward = 1 THEN 'pass'
                                          WHEN reward = 0 THEN 'fail'
                                          ELSE 'partial' END
                                ELSE 'harness' END
                       WHEN status::text = 'SUCCESS' THEN
                           CASE WHEN reward IS NULL THEN 'scoreless'
                                WHEN reward = 1 THEN 'pass'
                                WHEN reward = 0 THEN 'fail'
                                ELSE 'partial' END
                       ELSE 'other'
                   END AS bucket
            FROM trials
            WHERE task_version_id IS NOT NULL AND {_SCOPE}
        ), aggregates AS (
            SELECT task_version_id, MAX(activity_at) AS last_run_at,
                   COUNT(*) AS total_trials,
                   COUNT(*) FILTER (WHERE status = 'SUCCESS') AS completed_trials,
                   COUNT(*) FILTER (WHERE status = 'FAILED') AS failed_trials,
                   COUNT(*) FILTER (WHERE reward = 1) AS reward_success,
                   COALESCE(SUM(reward), 0.0) AS reward_sum,
                   COUNT(reward) AS reward_total,
                   COUNT(*) FILTER (WHERE bucket = 'pass') AS pass_count,
                   COUNT(*) FILTER (WHERE bucket = 'partial') AS partial_count,
                   COUNT(*) FILTER (WHERE bucket = 'fail') AS fail_count,
                   COUNT(*) FILTER (WHERE bucket = 'harness') AS harness_count,
                   COUNT(*) FILTER (WHERE bucket = 'skipped') AS skipped_count,
                   COUNT(*) FILTER (WHERE bucket = 'other') AS pending_count
            FROM scoped GROUP BY task_version_id
        )
        UPDATE task_version_browse_summaries summary
        SET last_run_at = aggregates.last_run_at,
            total_trials = aggregates.total_trials,
            completed_trials = aggregates.completed_trials,
            failed_trials = aggregates.failed_trials,
            reward_success = aggregates.reward_success,
            reward_sum = aggregates.reward_sum,
            reward_total = aggregates.reward_total,
            pass_count = aggregates.pass_count,
            partial_count = aggregates.partial_count,
            fail_count = aggregates.fail_count,
            harness_count = aggregates.harness_count,
            skipped_count = aggregates.skipped_count,
            pending_count = aggregates.pending_count
        FROM aggregates
        WHERE summary.task_version_id = aggregates.task_version_id
        """
    )
    op.execute(
        f"""
        WITH raw AS (
            SELECT task_version_id, agent, model,
                   billed_user_id IS NOT NULL AS billed,
                   cost_usd,
                   GREATEST(COALESCE(input_tokens, 0), 0) AS input_tokens,
                   GREATEST(COALESCE(output_tokens, 0), 0) AS output_tokens,
                   GREATEST(COALESCE(cache_tokens, 0), 0) AS cache_tokens,
                   GREATEST(COALESCE(cache_write_tokens, 0), 0) AS cache_write
            FROM trials
            WHERE task_version_id IS NOT NULL AND {_SCOPE}
        ), grouped AS (
            SELECT task_version_id, agent, model, billed,
                   COALESCE(SUM(cost_usd) FILTER (WHERE cost_usd IS NOT NULL), 0.0) AS native_cost,
                   COUNT(*) FILTER (WHERE cost_usd IS NOT NULL) AS native_trials,
                   COALESCE(SUM(GREATEST(input_tokens - cache_tokens - cache_write, 0)
                                    + cache_tokens + cache_write)
                       FILTER (WHERE cost_usd IS NULL AND (input_tokens > 0 OR output_tokens > 0 OR cache_write > 0)), 0) AS est_input,
                   COALESCE(SUM(output_tokens)
                       FILTER (WHERE cost_usd IS NULL AND (input_tokens > 0 OR output_tokens > 0 OR cache_write > 0)), 0) AS est_output,
                   COALESCE(SUM(cache_tokens)
                       FILTER (WHERE cost_usd IS NULL AND (input_tokens > 0 OR output_tokens > 0 OR cache_write > 0)), 0) AS est_cache,
                   COALESCE(SUM(cache_write)
                       FILTER (WHERE cost_usd IS NULL AND (input_tokens > 0 OR output_tokens > 0 OR cache_write > 0)), 0) AS est_cache_write,
                   COUNT(*) FILTER (WHERE cost_usd IS NULL AND (input_tokens > 0 OR output_tokens > 0 OR cache_write > 0)) AS est_trials
            FROM raw GROUP BY task_version_id, agent, model, billed
        ), packed AS (
            SELECT task_version_id,
                   jsonb_agg(jsonb_build_object(
                       'agent', agent, 'model', model, 'billed', billed,
                       'native_cost', native_cost, 'native_trials', native_trials,
                       'est_input', est_input, 'est_output', est_output,
                       'est_cache', est_cache, 'est_cache_write', est_cache_write,
                       'est_trials', est_trials
                   ) ORDER BY agent, model NULLS FIRST, billed) AS cost_breakdown
            FROM grouped GROUP BY task_version_id
        )
        UPDATE task_version_browse_summaries summary
        SET cost_breakdown = packed.cost_breakdown
        FROM packed
        WHERE summary.task_version_id = packed.task_version_id
        """
    )


def downgrade() -> None:
    op.drop_index("idx_trials_task_browse_preview", table_name="trials")
    op.drop_index(
        "idx_task_browse_summary_task_id",
        table_name="task_version_browse_summaries",
    )
    op.drop_table("task_version_browse_summaries")
