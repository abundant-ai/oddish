# Oddish Slack bot

A Slack Events API webhook (Modal-hosted) that answers teammates' questions about
the oddish eval platform. Mention the bot in a channel and a Claude agent answers
using read-only MCP tools: GET-only calls against the oddish backend REST API,
plus a read-only SQL tool for questions the REST endpoints don't cover.

`@Oddish Claude why did trial abc123 fail?`

## What it can answer

Global and per-user spend, queue health, task/experiment status, and why a
specific trial failed. For anything the purpose-built tools don't cover, the
`oddish_sql` tool runs a **read-only** SQL query against the oddish Postgres —
e.g. breaking down QA/analysis cost from the `analysis_costs` ledger, or
aggregating spend by day/model/trial type. **Quota questions are not
supported** — the `/quotas` endpoint is user-auth-only and rejects the bot's API
key, so use the dashboard for quota limits and usage.

## Architecture

- `web` function: verifies the Slack signature, acks within 3s, dispatches the
  real work off the request path via `BackgroundTasks`.
- `answer` function: runs the Python `claude-agent-sdk` agent with read-only MCP
  tools (`tools.py`). The tools are GET-only against the oddish backend REST API
  plus one read-only SQL tool (`oddish_sql`), and the
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
| `ODDISH_DATABASE_URL_RO` | **Required for `oddish_sql`.** DSN for a Postgres role granted only `SELECT`. If unset, falls back to `ODDISH_DATABASE_URL`; either way every query runs in a `READ ONLY` transaction. If neither is set, `oddish_sql` returns a connection error and the other tools keep working. |
| `ODDISH_DATABASE_URL` | Fallback DSN for `oddish_sql` when `ODDISH_DATABASE_URL_RO` is unset. |
| `ANTHROPIC_API_KEY` | Credential the `claude-agent-sdk` uses to call Claude. |
| `SLACK_TEAM_ID` | *Optional.* Slack workspace (team) ID. When set, events from any other workspace are dropped; unset means single-workspace mode with no team check. |
| `SLACK_MAX_BUDGET_USD` | *Optional.* Per-prompt cost ceiling for one agent run (USD). Defaults to `1.00`. |

## Read-only SQL (`oddish_sql`)

For questions the REST tools don't cover, the agent can run one SQL statement
against the oddish Postgres. Read-only is enforced in layers, strongest first:

1. **Postgres `READ ONLY` transaction.** Every query runs inside
   `conn.transaction(readonly=True)`, so the server itself rejects any write
   (INSERT/UPDATE/DELETE/DDL/etc.) with *"cannot execute … in a read-only
   transaction"*. This holds even if the DSN points at a read/write role.
2. **Dedicated RO role (recommended).** Point `ODDISH_DATABASE_URL_RO` at a
   Postgres role granted only `SELECT`. This is defense in depth on top of (1)
   and also blocks superuser-only escape hatches (`pg_read_file`, `COPY … TO
   PROGRAM`); grant it no superuser and no write privileges.
3. **Statement shape check.** The query must be a single statement beginning
   with `SELECT`/`WITH`/`EXPLAIN`/`SHOW`/`TABLE`/`VALUES`; stacked
   (`;`-separated) statements are rejected before hitting the DB.
4. **Blast-radius caps.** A 15s `statement_timeout`, a 10s connect timeout, and
   a 200-row cap on returned rows.

The agent is told to aggregate in SQL and add `LIMIT`, and to introspect
`information_schema.columns` when it doesn't know the schema.

## Deploy steps

1. `cd slackbot && modal deploy app.py`
2. Copy the deployed `web` URL and paste it into `manifest.yaml`
   (`settings.event_subscriptions.request_url`, keeping the `/slack/events` path).
3. Create the Slack app from `manifest.yaml`, install it into the workspace, and
   invite the bot into the channels where it should answer.

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
