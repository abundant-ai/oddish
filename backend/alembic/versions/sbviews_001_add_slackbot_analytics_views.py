"""Curated read-only analytics views for the slackbot RO role.

Raw SQL against this schema lies: soft-delete is ORM-enforced, the admin
cost dashboard and the quota gate use different spend predicates, and
"reserved" quota is a live SUM over in-flight trials rather than a ledger.
These views bake those semantics in once, in reviewable SQL, so an agent
(or Metabase) can query them without re-deriving app predicates.

Views only -- the slackbot_ro role, its grants, and its GUCs are a one-time
admin step (slackbot/role_setup.sql): roles are cluster-level, previews
restore prod schema with --no-privileges, and there is no migration
precedent for CREATE ROLE/GRANT in this repo.

Views are invisible to create_all() and to the preview guard's
_assert_model_schema (it walks SQLAlchemy metadata only), so nothing in
backend/ may depend on them at runtime; only the slackbot reads them.

Dollar amounts baked from config defaults at migration time:
default_daily_quota_usd=200.00, unpriced_trial_cost_usd=1.00,
pending_trial_reservation_usd=1.00.

Revision ID: sbviews_001
Revises: org_quotas_monthly_001
Create Date: 2026-07-08
"""

from alembic import op

revision = "sbviews_001"
down_revision = "org_quotas_monthly_001"
branch_labels = None
depends_on = None

_TIMEOUT_CLAUSE = (
    "(t.error_message LIKE '%AgentTimeoutError%' "
    "OR t.error_message LIKE '%Agent execution timed out%')"
)

V_TRIALS = f"""
CREATE OR REPLACE VIEW v_trials AS
SELECT
    t.id,
    t.task_id,
    t.experiment_id,
    t.org_id,
    t.billed_user_id,
    t.billed_user_id IS NOT NULL AS is_billed,
    t.is_probe,
    t.superseded_by_trial_id IS NOT NULL AS is_superseded,
    t.origin,
    t.agent,
    t.provider,
    t.model,
    t.queue_key,
    lower(t.status::text) AS status,
    CASE
        WHEN lower(t.status::text) = 'skipped' THEN 'skipped'
        WHEN t.error_message IS NOT NULL
             AND NOT ({_TIMEOUT_CLAUSE} AND t.reward IS NOT NULL)
            THEN 'harness'
        WHEN lower(t.status::text) = 'failed' THEN
            CASE WHEN {_TIMEOUT_CLAUSE} AND t.reward IS NOT NULL
                THEN CASE WHEN t.reward = 1 THEN 'pass'
                          WHEN t.reward = 0 THEN 'fail'
                          ELSE 'partial' END
                ELSE 'harness' END
        WHEN lower(t.status::text) = 'success' THEN
            CASE WHEN t.reward IS NOT NULL
                THEN CASE WHEN t.reward = 1 THEN 'pass'
                          WHEN t.reward = 0 THEN 'fail'
                          ELSE 'partial' END
                ELSE 'scoreless' END
        ELSE 'other'
    END AS outcome,
    t.reward,
    LEFT(t.error_message, 400) AS error_message,
    t.cost_usd,
    t.input_tokens,
    t.cache_tokens,
    t.output_tokens,
    (
        t.billed_user_id IS NOT NULL
        OR (
            t.origin = 'oddish'
            AND NOT t.is_probe
            AND (t.idempotency_key IS NULL
                 OR t.idempotency_key NOT LIKE 'combine:%')
        )
    ) AS is_real_spend,
    t.created_at,
    t.started_at,
    t.finished_at
FROM trials t
WHERE t.deleted_at IS NULL
"""

V_TRIALS_COMMENT = """
COMMENT ON VIEW v_trials IS $c$One row per live (not soft-deleted) trial.
outcome buckets match the dashboard matrix: pass/partial/fail (scored),
harness (infra/agent error), scoreless, skipped, other (not finished).
is_real_spend is the admin cost-dashboard basis: billed, or native
non-probe non-combine-copy. Most metrics should add NOT is_superseded.
Examples:
SELECT outcome, count(*) FROM v_trials
  WHERE finished_at > now() - interval '1 day' AND NOT is_superseded
  GROUP BY 1;
SELECT model, sum(cost_usd) FROM v_trials
  WHERE is_real_spend AND created_at > now() - interval '7 days'
  GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT 10;$c$
"""

V_EXPERIMENT_SUMMARY = """
CREATE OR REPLACE VIEW v_experiment_summary AS
SELECT
    e.id AS experiment_id,
    e.name,
    e.org_id,
    e.owner_user_id,
    e.is_collection,
    e.created_at,
    COUNT(t.id) AS total_trials,
    COUNT(*) FILTER (WHERE t.status IN ('pending', 'queued', 'running', 'retrying')) AS active_trials,
    COUNT(*) FILTER (WHERE t.status = 'success') AS completed_trials,
    COUNT(*) FILTER (WHERE t.status = 'failed') AS failed_trials,
    COUNT(*) FILTER (WHERE t.status = 'skipped') AS skipped_trials,
    COUNT(*) FILTER (WHERE t.outcome = 'pass') AS pass_count,
    COUNT(*) FILTER (WHERE t.outcome = 'partial') AS partial_count,
    COUNT(*) FILTER (WHERE t.outcome = 'fail') AS fail_count,
    COUNT(*) FILTER (WHERE t.outcome = 'harness') AS harness_count,
    ROUND((COUNT(*) FILTER (WHERE t.outcome = 'pass')) * 100.0
          / NULLIF(COUNT(t.id), 0), 1) AS pass_rate,
    AVG(t.reward) AS avg_reward,
    SUM(t.cost_usd) FILTER (WHERE t.is_real_spend) AS cost_usd,
    MIN(t.created_at) AS first_trial_at,
    MAX(t.finished_at) AS last_finished_at
FROM experiments e
LEFT JOIN v_trials t ON t.experiment_id = e.id AND NOT t.is_superseded
WHERE e.deleted_at IS NULL
GROUP BY e.id
"""

V_EXPERIMENT_SUMMARY_COMMENT = """
COMMENT ON VIEW v_experiment_summary IS $c$One row per live experiment,
aggregated over its own non-superseded trials (probes included, matching
the dashboard; trials gathered into collections are NOT counted here).
cost_usd uses the admin real-spend basis (billed probes count, unbilled
probes/imports/combine-copies do not). pass_rate = pass_count as % of all
counted trials. An experiment is "finished" when total_trials > 0 AND
active_trials = 0. Examples:
SELECT name, pass_rate, cost_usd, total_trials FROM v_experiment_summary
  ORDER BY created_at DESC LIMIT 20;
SELECT name, active_trials, failed_trials FROM v_experiment_summary
  WHERE active_trials > 0;$c$
"""

V_DAILY_SPEND = """
CREATE OR REPLACE VIEW v_daily_spend AS
SELECT
    (t.created_at AT TIME ZONE 'utc')::date AS day,
    t.org_id,
    t.billed_user_id,
    COALESCE(
        t.billed_user_id,
        'ghid:' || NULLIF(btrim(tk.tags ->> 'github_id'), ''),
        'ghuser:' || lower(NULLIF(btrim(tk.tags ->> 'github_username'), '')),
        tk.created_by_user_id,
        '__unattributed__'
    ) AS spender,
    COALESCE(
        u.name,
        u.email,
        '@' || NULLIF(btrim(tk.tags ->> 'github_username'), ''),
        tk.created_by_user_id,
        'Unattributed'
    ) AS spender_label,
    t.model,
    t.provider,
    COUNT(*) AS trial_count,
    SUM(COALESCE(t.cost_usd, 0)) AS cost_usd,
    SUM(COALESCE(t.input_tokens, 0)) AS input_tokens,
    SUM(COALESCE(t.cache_tokens, 0)) AS cache_tokens,
    SUM(COALESCE(t.output_tokens, 0)) AS output_tokens
FROM trials t
LEFT JOIN tasks tk ON tk.id = t.task_id
LEFT JOIN users u ON u.id = t.billed_user_id AND u.deleted_at IS NULL
WHERE t.deleted_at IS NULL
  AND (
    t.billed_user_id IS NOT NULL
    OR (
        t.origin = 'oddish'
        AND NOT t.is_probe
        AND (t.idempotency_key IS NULL
             OR t.idempotency_key NOT LIKE 'combine:%')
    )
  )
GROUP BY 1, 2, 3, 4, 5, 6, 7
"""

V_DAILY_SPEND_COMMENT = """
COMMENT ON VIEW v_daily_spend IS $c$Real spend (admin cost-dashboard
basis) per UTC day x org x spender x model, bucketed by trial created_at.
spender falls back like /admin/costs: billed user id, else ghid:<github
id>, else ghuser:<handle>, else submitter user id, else __unattributed__.
Unlike /admin/costs, trials whose task was soft-deleted stay counted
(their identity may degrade to __unattributed__). Examples:
SELECT day, sum(cost_usd) FROM v_daily_spend
  WHERE day > current_date - 14 GROUP BY 1 ORDER BY 1;
SELECT spender_label, sum(cost_usd), sum(trial_count) FROM v_daily_spend
  WHERE day = current_date GROUP BY 1 ORDER BY 2 DESC LIMIT 10;$c$
"""

V_QUOTA_STATUS = """
CREATE OR REPLACE VIEW v_quota_status AS
SELECT
    u.id AS user_id,
    u.org_id,
    u.name,
    u.email,
    u.github_username,
    COALESCE(q.limit_usd, 200.00) AS limit_usd,
    COALESCE(s.used_usd, 0) AS used_24h_usd,
    COALESCE(r.reserved_usd, 0) AS reserved_usd,
    ROUND(((COALESCE(s.used_usd, 0) + COALESCE(r.reserved_usd, 0))
           / NULLIF(COALESCE(q.limit_usd, 200.00), 0))::numeric, 4) AS utilization,
    (COALESCE(s.used_usd, 0) + COALESCE(r.reserved_usd, 0))
        >= COALESCE(q.limit_usd, 200.00) AS over_quota
FROM users u
LEFT JOIN quotas q
    ON q.org_id = u.org_id AND q.user_id = u.id AND q.deleted_at IS NULL
LEFT JOIN (
    SELECT org_id, billed_user_id,
           SUM(COALESCE(cost_usd,
               CASE WHEN started_at IS NOT NULL THEN 1.00 ELSE 0 END)) AS used_usd
    FROM trials
    WHERE finished_at > NOW() - INTERVAL '24 hours'
    GROUP BY 1, 2
) s ON s.org_id = u.org_id AND s.billed_user_id = u.id
LEFT JOIN (
    SELECT org_id, billed_user_id,
           SUM(GREATEST(COALESCE(cost_usd, 0), 1.00)) AS reserved_usd
    FROM trials
    WHERE finished_at IS NULL
      AND deleted_at IS NULL
      AND superseded_by_trial_id IS NULL
      AND lower(status::text) IN ('pending', 'queued', 'running', 'retrying')
    GROUP BY 1, 2
) r ON r.org_id = u.org_id AND r.billed_user_id = u.id
WHERE u.deleted_at IS NULL AND u.is_active
"""

V_QUOTA_STATUS_COMMENT = """
COMMENT ON VIEW v_quota_status IS $c$Rolling-24h quota exposure per active
user, on the ENFORCEMENT basis (differs from the cost dashboard): settled
= trials finished in the last 24h including soft-deleted ones, unpriced
started trials floored at $1; reserved = in-flight trials floored at
$1 each; limit falls back to the $200 default when no override row.
over_quota mirrors the admission gate: used + reserved >= limit.
Examples:
SELECT name, limit_usd, used_24h_usd, reserved_usd, utilization
  FROM v_quota_status ORDER BY utilization DESC NULLS LAST LIMIT 15;
SELECT count(*) FROM v_quota_status WHERE over_quota;$c$
"""

V_ORG_QUOTA_STATUS = """
CREATE OR REPLACE VIEW v_org_quota_status AS
SELECT
    o.id AS org_id,
    o.name AS org_name,
    oq.limit_usd AS monthly_limit_usd,
    COALESCE(s.used_usd, 0) AS used_month_usd,
    COALESCE(r.reserved_usd, 0) AS reserved_usd
FROM organizations o
LEFT JOIN org_quotas oq ON oq.org_id = o.id AND oq.deleted_at IS NULL
LEFT JOIN (
    SELECT org_id,
           SUM(COALESCE(cost_usd,
               CASE WHEN started_at IS NOT NULL THEN 1.00 ELSE 0 END)) AS used_usd
    FROM trials
    WHERE finished_at >= date_trunc('month', NOW() AT TIME ZONE 'utc') AT TIME ZONE 'utc'
    GROUP BY 1
) s ON s.org_id = o.id
LEFT JOIN (
    SELECT org_id, SUM(GREATEST(COALESCE(cost_usd, 0), 1.00)) AS reserved_usd
    FROM trials
    WHERE finished_at IS NULL
      AND deleted_at IS NULL
      AND superseded_by_trial_id IS NULL
      AND lower(status::text) IN ('pending', 'queued', 'running', 'retrying')
    GROUP BY 1
) r ON r.org_id = o.id
WHERE o.deleted_at IS NULL
"""

V_ORG_QUOTA_STATUS_COMMENT = """
COMMENT ON VIEW v_org_quota_status IS $c$Calendar-month (UTC) settled
spend plus in-flight reservations per live org, vs the org monthly cap.
monthly_limit_usd NULL = uncapped (the default). Same enforcement basis
as v_quota_status (soft-deleted trials count once settled). Example:
SELECT org_name, used_month_usd, reserved_usd, monthly_limit_usd
  FROM v_org_quota_status ORDER BY used_month_usd DESC;$c$
"""

V_QUEUE_STATE = """
CREATE OR REPLACE VIEW v_queue_state AS
SELECT
    queue_key,
    lower(kind::text) AS kind,
    COUNT(*) FILTER (WHERE status::text IN ('QUEUED', 'RETRYING')
                     AND available_after <= NOW()) AS queued_ready,
    COUNT(*) FILTER (WHERE status::text IN ('QUEUED', 'RETRYING')
                     AND available_after > NOW()) AS queued_scheduled,
    COUNT(*) FILTER (WHERE status::text = 'RUNNING') AS running,
    MIN(created_at) FILTER (WHERE status::text IN ('QUEUED', 'RETRYING')) AS oldest_queued_at,
    COUNT(*) FILTER (WHERE status::text IN ('SUCCESS', 'FAILED', 'CANCELLED')) AS finished_24h,
    COUNT(*) FILTER (WHERE status::text = 'FAILED') AS failed_24h
FROM worker_jobs
WHERE status::text IN ('QUEUED', 'RETRYING', 'RUNNING')
   OR finished_at > NOW() - INTERVAL '24 hours'
GROUP BY 1, 2
"""

V_QUEUE_STATE_COMMENT = """
COMMENT ON VIEW v_queue_state IS $c$worker_jobs queue depth per queue_key
x kind: queued_ready (dispatchable now), queued_scheduled (retry backoff
in the future), running, plus finished/failed counts over the last 24h.
Concurrency limits live in deploy config, not the DB -- use the
queue-health tool for limit/fill/heartbeats. Examples:
SELECT queue_key, queued_ready, running FROM v_queue_state
  WHERE kind = 'trial' AND queued_ready > 0 ORDER BY queued_ready DESC;
SELECT kind, sum(failed_24h) FROM v_queue_state GROUP BY 1;$c$
"""

V_RUNTIME_STATUS = """
CREATE OR REPLACE VIEW v_runtime_status AS
SELECT
    component,
    updated_at,
    GREATEST(EXTRACT(EPOCH FROM (NOW() - updated_at)), 0) AS age_seconds,
    payload
FROM queue_runtime_status
"""

V_RUNTIME_STATUS_COMMENT = """
COMMENT ON VIEW v_runtime_status IS $c$Last heartbeat per scheduled
component (dispatcher beats ~180s, reconciler ~240s) with its status
payload. age_seconds far above the cadence means the component is down.
Example:
SELECT component, age_seconds, payload FROM v_runtime_status;$c$
"""

_STATEMENTS = [
    V_TRIALS,
    V_TRIALS_COMMENT,
    V_EXPERIMENT_SUMMARY,
    V_EXPERIMENT_SUMMARY_COMMENT,
    V_DAILY_SPEND,
    V_DAILY_SPEND_COMMENT,
    V_QUOTA_STATUS,
    V_QUOTA_STATUS_COMMENT,
    V_ORG_QUOTA_STATUS,
    V_ORG_QUOTA_STATUS_COMMENT,
    V_QUEUE_STATE,
    V_QUEUE_STATE_COMMENT,
    V_RUNTIME_STATUS,
    V_RUNTIME_STATUS_COMMENT,
]

_VIEWS = [
    "v_experiment_summary",
    "v_trials",
    "v_daily_spend",
    "v_quota_status",
    "v_org_quota_status",
    "v_queue_state",
    "v_runtime_status",
]


def upgrade() -> None:
    for stmt in _STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for view in _VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {view}")
