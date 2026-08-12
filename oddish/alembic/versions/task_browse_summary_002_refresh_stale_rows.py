"""Refresh stale task-browser summaries.

Revision ID: task_browse_summary_002
Revises: modeldisp01

``task_browse_summary_001`` created and backfilled
``task_version_browse_summaries``, but the maintenance code from #1152 was
reverted the same day (#1156) while the applied migration stayed (#1157).
Every summary row is frozen at its 2026-08-11 backfill state, versions
created since have no row at all, and versions whose scoped trials were all
removed since then still carry nonzero counts. This revision ships with the
re-landed maintenance code: it seeds missing rows, resets every row to
empty, then replays the 001 backfill so summaries are exact at cutover.

Data-only and idempotent; running it on a fresh database right after 001 is
a harmless no-op pass over empty data. Deploys sequence migrations before
code, so a trial write landing between this backfill and code cutover
leaves its version stale only until the next settlement refreshes it.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "task_browse_summary_002"
down_revision: Union[str, Sequence[str], None] = "modeldisp01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in lockstep with task_browse_summary_001 and
# oddish.core.task_browse_metrics.browse_trial_scope.
_SCOPE = """
    deleted_at IS NULL
    AND superseded_by_trial_id IS NULL
    AND is_probe IS NOT TRUE
    AND (idempotency_key IS NULL OR idempotency_key NOT LIKE 'combine:%')
"""


def upgrade() -> None:
    # Seed rows for versions created while maintenance was reverted, and
    # reset every existing row so versions with no remaining in-scope trials
    # (which the aggregate UPDATEs below never touch) drop their stale counts.
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
        ON CONFLICT (task_version_id) DO UPDATE
        SET last_run_at = NULL,
            total_trials = 0,
            completed_trials = 0,
            failed_trials = 0,
            reward_success = 0,
            reward_sum = 0.0,
            reward_total = 0,
            pass_count = 0,
            partial_count = 0,
            fail_count = 0,
            harness_count = 0,
            skipped_count = 0,
            pending_count = 0,
            cost_breakdown = '[]'::jsonb,
            updated_at = NOW()
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
    # Data-only refresh; the maintained rows remain valid without it.
    pass
