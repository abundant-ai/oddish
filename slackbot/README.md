# Carl: Oddish's Slack agent

Carl is the existing Abundant Slack app used by the production `oddish` Modal
deployment for Oddish link unfurls and notification DMs. The same app can answer
questions about Oddish when a permitted teammate mentions it in a channel:

`@carl why did trial abc123 fail?`

The shared Slack Events request URL remains `/webhooks/slack/events` on the
production API. That endpoint routes `link_shared` events to the existing unfurl
handler and `app_mention` events to Carl's agent. Do not create another Slack app
or replace the request URL with a second Modal endpoint.

## What Carl can answer

Carl can report organization and per-user spend, queue health, task and experiment
status, and why a trial failed. For questions the REST tools do not cover, it can
run a read-only query against Oddish Postgres—for example, to break down QA cost
from `analysis_costs` or aggregate spend by day, model, or trial type. Quota
questions are not supported because the quota endpoint requires a user session.

## Configuration

The production `oddish-prod` Modal secret already contains Carl's signing secret,
bot token, workspace ID, and Anthropic credential. Add these values before
subscribing Carl to mentions:

| Key | Purpose |
|---|---|
| `ODDISH_CARL_ALLOWED_USERS` | **Required.** Comma-separated Slack user IDs allowed to ask Carl questions. An empty value fails closed. |
| `ODDISH_CARL_ALLOWED_CHANNELS` | Optional comma-separated channel IDs. When set, answers are confined to these channels. |
| `ODDISH_API_KEY` | Admin `FULL` API key used by Carl's GET-only REST tools. |
| `ODDISH_DATABASE_URL_RO` | DSN for a dedicated non-superuser Postgres role granted `SELECT` only on the analytics tables below. There is no fallback to the backend's read/write DSN. |
| `ODDISH_SQL_TABLES` | Optional table allow-list override. Defaults to `analysis_costs,trials,tasks,experiments,organizations`. |
| `ODDISH_CARL_MAX_BUDGET_USD` | Optional per-question model cost ceiling. Defaults to `1.00`. |
| `ODDISH_ENABLE_CARL_AGENT` | Enables mention routing to the always-registered Modal answer function. Defaults on only for the production `oddish` app. |

The API URL defaults to the production Oddish endpoint. `ODDISH_API_URL` may
override it for another deployment.

Carl's Slack app must retain its existing `link_shared` subscription and
`links:read`, `links:write`, `chat:write`, `im:write`, `users:read`, and
`users:read.email` scopes. Add the `app_mentions:read` scope and `app_mention`
event subscription, then reinstall the existing app. Keep the Events request URL
unchanged.

## Read-only SQL

The database role is the durable access boundary. Carl is an internal
platform-operator tool, so its SQL analytics may span organizations; every user
on `ODDISH_CARL_ALLOWED_USERS` must be a trusted platform operator. Provision a
role that owns nothing, cannot bypass row security, and can only select the
non-sensitive columns Carl needs:

```sql
CREATE ROLE oddish_carl_ro LOGIN PASSWORD '…'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
GRANT CONNECT ON DATABASE oddish TO oddish_carl_ro;
GRANT USAGE ON SCHEMA public TO oddish_carl_ro;
GRANT SELECT ON analysis_costs TO oddish_carl_ro;
GRANT SELECT (
  id, name, task_id, experiment_id, org_id, billed_user_id, agent, provider,
  queue_key, model, environment, harbor_sha, is_probe, status, origin, attempts,
  max_attempts, harbor_stage, created_at, started_at, finished_at, reward,
  input_tokens, cache_tokens, cache_write_tokens, output_tokens, total_steps,
  trajectory_duration_seconds, total_tool_calls, cost_usd, has_trajectory,
  analysis_status, analysis_started_at, analysis_finished_at,
  superseded_by_trial_id, deleted_at
) ON trials TO oddish_carl_ro;
GRANT SELECT (
  id, name, org_id, created_by_user_id, "user", priority, status, link,
  run_analysis, run_probe, started_at, finished_at, verdict_status,
  created_at, updated_at, deleted_at
) ON tasks TO oddish_carl_ro;
GRANT SELECT (
  id, name, org_id, last_activity_at, owner_user_id, owner, link, is_public,
  is_collection, created_at, updated_at, deleted_at
) ON experiments TO oddish_carl_ro;
GRANT SELECT (id, name, slug, plan, is_active, created_at, updated_at)
  ON organizations TO oddish_carl_ro;
```

Keep those grants and `ODDISH_SQL_TABLES` aligned. Never grant access to users,
authentication, chat, documents, API keys, BYOK, or other secret-bearing tables.

The code adds three more layers: PostgreSQL `READ ONLY` transactions; an
AST-parsed allow-list that rejects writes, stacked statements, wildcard column
selection, sensitive columns, dangerous functions, and non-analytics
relations; and a 15-second timeout, 200-row cap, and per-cell output truncation.

## Limitations

- Mentions work in channels only; DMs are still reserved for deterministic
  notifications.
- REST task, trial, and cost data is scoped to the organization attached to
  Carl's API key. Queue details are global only when that key belongs to the
  operator organization.
- If an answer worker is killed after posting `Thinking…`, mention Carl again to
  create a new Slack event and retry.
- Before enabling mentions, verify that a prompt embedded in trial logs cannot
  invoke any tool outside Carl's explicit read-only MCP allow-list.
