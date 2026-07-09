# Oddish Slack bot

A Slack Events API webhook (Modal-hosted) that answers teammates' questions about
the oddish eval platform. Mention the bot in a channel and a Claude agent answers
using read-only MCP tools that call the oddish backend REST API.

`@Oddish Claude why did trial abc123 fail?`

## What it can answer

Global and per-user spend, quota status (limit/used/utilization), queue health,
task/experiment status, and why a specific trial failed. For anything the fixed
tools don't cover (arbitrary aggregates, filters, joins), the agent can run
read-only SQL against curated analytics views (see *SQL access*). Quota
*changes* stay in the dashboard: the backend rejects API-key auth on every
quota write, so the bot can only link the page.

## Architecture

- `web` function: verifies the Slack signature, acks within 3s, dispatches the
  real work off the request path via `BackgroundTasks`.
- `answer` function: runs the Python `claude-agent-sdk` agent with read-only MCP
  tools (`tools.py`). The tools are GET-only against the oddish backend, and the
  agent is confined to the exact oddish tool names (`tools.allowed_tool_names()`,
  i.e. `mcp__oddish__<tool>`). Every built-in Claude tool is structurally
  disabled (`tools=[]`), `strict_mcp_config=True` refuses any MCP config the CLI
  would otherwise auto-load, and `permission_mode="dontAsk"` denies anything not
  on the allow-list — so only the enumerated oddish tools exist. Each run is also
  capped at `max_budget_usd` (see `SLACK_MAX_BUDGET_USD`). The Claude Code CLI the
  SDK drives is bundled in the `claude-agent-sdk` wheel, so the image needs no
  separate CLI install.

## Secret keys

All live in one Modal secret named `oddish-slackbot`:

| Key | Purpose |
|---|---|
| `SLACK_SIGNING_SECRET` | Authenticate inbound Slack requests (HMAC). |
| `SLACK_BOT_TOKEN` | Authorize outbound Slack writes (`chat.postMessage`, `chat.update`). |
| `SLACK_ALLOWED_USERS` | **Required.** Comma-separated Slack user IDs allowed to drive the bot. If unset or empty, the bot fails closed and refuses to run. |
| `ODDISH_API_URL` | Base URL of the oddish backend. |
| `ODDISH_API_KEY` | Admin (FULL-scope) API key the tools use as a Bearer token. |
| `ANTHROPIC_API_KEY` | Credential the `claude-agent-sdk` uses to call Claude. |
| `SLACK_TEAM_ID` | *Optional.* Slack workspace (team) ID. When set, events from any other workspace are dropped; unset means single-workspace mode with no team check. |
| `SLACK_MAX_BUDGET_USD` | *Optional.* Per-prompt cost ceiling for one agent run (USD). Defaults to `1.00`. |
| `ODDISH_RO_DATABASE_URL` | *Optional.* Plain `postgresql://` DSN for the `slackbot_ro` role (see *SQL access*). Unset disables the SQL tools gracefully. Never reuse `oddish-prod` credentials — that role is full-RW. |
| `SLACK_ALERT_CHANNEL` | *Optional.* Channel ID for scheduled `watch` alerts. Unset disables them. |
| `SLACK_EXPENSIVE_EXPERIMENT_USD` | *Optional.* Experiment-spend alert threshold. Defaults to `2000`. |
| `SLACK_EXPENSIVE_TRIAL_USD` | *Optional.* Trial-spend floor. Defaults to `100`. |
| `SLACK_TRIAL_AVERAGE_MULTIPLIER` | *Optional.* Trial spend must exceed this multiple of the experiment's other priced trials. Defaults to `2`. |
| `ODDISH_DASHBOARD_URL` | *Optional.* Dashboard base for deep links. Defaults to `https://www.oddish.app`. |

## SQL access

Two extra tools, `oddish_list_views` and `oddish_run_sql`, let the agent answer
questions the fixed tools can't. Raw SQL against the base tables would lie
(soft-delete is ORM-enforced, spend/quota predicates live in app code), so the
tools only see curated views — `v_trials`, `v_experiment_summary`,
`v_daily_spend`, `v_quota_status`, `v_org_quota_status`, `v_queue_state`,
`v_runtime_status` — that bake those semantics in once. Each view carries a
`COMMENT` with column semantics and example queries; `oddish_list_views`
surfaces them so the schema self-documents.

Grants are the security boundary (no SELECT-regex or keyword blocklists):

1. Views ship in backend migration `sbviews_001` (applied with the normal
   migration flow).
2. Run `role_setup.sql` once as an admin **through the session pooler (port
   5432)** to create `slackbot_ro`, grant it the views, and pin role-level
   GUCs (`default_transaction_read_only`, 5s statement timeout, UTC). The GUCs
   must live on the role because Supavisor transaction pooling drops
   client-supplied settings.
3. Add `ODDISH_RO_DATABASE_URL` to the `oddish-slackbot` secret.

Every `oddish_run_sql` call logs the query with the Slack asker/thread for
audit. One statement per call (asyncpg extended protocol), 200 rows returned.

## Scheduled alerts

`watch` runs every 5 minutes (`modal.Period`) when `SLACK_ALERT_CHANNEL` is
set, iterating a `CHECKS` table of deterministic checks with templated
output — no agent, no raw log text, so nothing prompt-injectable runs
unattended. Alert-once dedupe lives in the `oddish-slackbot-watch-state`
Dict. It intentionally covers only experiment and trial spend; Catfish owns
model and total-spend alerts.

Current checks:

- Experiments at or above $2,000. A recent trial completion can trigger this
  while other trials are still running; otherwise the final completion catches
  it. The alert includes the experiment owner and dashboard link.
- Finished trials over $100 whose cost is also over 2× the average of the
  experiment's other priced trials. The first priced trial alerts when it is
  over $100.

Experiment totals use `/admin/costs`; trial alerts use persisted native trial
costs and skip trials without one. Thresholds are configurable with the secret
keys above. Alerts are emitted once per entity and threshold.

## Deploy steps

1. `cd slackbot && modal deploy app.py`
2. Copy the deployed `web` URL and paste it into `manifest.yaml`
   (`settings.event_subscriptions.request_url`, keeping the `/slack/events` path).
3. Create the Slack app from `manifest.yaml`, install it into the workspace, and
   invite the bot into the channels where it should answer.
4. Optional: wire up SQL access (see above) and set `SLACK_ALERT_CHANNEL` for
   scheduled alerts.

## Limitations

- **Channels only, no DMs.** The manifest subscribes to `app_mention` only, so
  direct messages to the bot are silently ignored. To support DMs you would add
  the `message.im` event and the `im:history` scope, and handle that event type
  in `_dispatch`.
- **Single shared identity.** Every backend call is attributed to the one admin
  API key regardless of who asked; the Slack asker is captured in the logs only.
- **Mixed scoping.** The cost and queue-health tools hit admin endpoints that are
  global across orgs, but the trial-log and task-list tools use org-scoped
  endpoints resolved from the API key's org. Trials or tasks in other orgs can
  therefore surface as "not found" or be missing from lists even though the bot
  otherwise speaks platform-wide. Point the key at the org whose runs you ask
  about, or use the dashboard for cross-org trial/task detail.

## Residual risk

If the Modal `answer` worker is terminated before it runs its timeout/error
handlers (e.g. an infra SIGKILL), the thread can stay on the "Thinking…"
placeholder while `seen_events` still holds the claim, so a re-mention (a new
`event_id`) is the recovery path rather than an automatic retry of the same
event.

The end-to-end permission-gate behavior (that `allowed_tools` +
`permission_mode="dontAsk"` denies every non-oddish tool on the shipped
SDK/CLI combo) has not been exercised in a live deploy. Verify on the first
deploy that a prompt-injected instruction cannot make the agent run a
non-oddish tool.
