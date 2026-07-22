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
| `ODDISH_DATABASE_URL_RO` | **Required for `oddish_sql`.** DSN for a dedicated, non-superuser Postgres role granted `SELECT` only on the analytics tables (see Read-only SQL below). There is **no fallback** to `ODDISH_DATABASE_URL`: if this is unset, `oddish_sql` refuses to run (fails closed) and the other tools keep working. Do **not** put the backend's read/write DSN in the slackbot secret. |
| `ANTHROPIC_API_KEY` | Credential the `claude-agent-sdk` uses to call Claude. |
| `ODDISH_SQL_TABLES` | *Optional.* Comma-separated override of the tables `oddish_sql` may read. Defaults to `analysis_costs,trials,tasks,experiments,model_pricing,orgs`. `information_schema` is always readable. |
| `SLACK_ALLOWED_CHANNELS` | *Optional.* Comma-separated Slack channel IDs. When set, the bot answers only in those channels (so DB-backed answers can be confined to a private/trusted channel); unset means any channel it's invited to, gated by `SLACK_ALLOWED_USERS`. |
| `SLACK_TEAM_ID` | *Optional.* Slack workspace (team) ID. When set, events from any other workspace are dropped; unset means single-workspace mode with no team check. |
| `SLACK_MAX_BUDGET_USD` | *Optional.* Per-prompt cost ceiling for one agent run (USD). Defaults to `1.00`. |

## Read-only SQL (`oddish_sql`)

For questions the REST tools don't cover, the agent can run one SQL statement
against the oddish Postgres. This is the highest-risk surface — the bot also
reads untrusted content (trial logs, task names) and posts to a channel, so a
prompt injection could try to make it query and leak data. It's contained in
layers, and the first two are the ones that actually hold:

1. **Least-privilege DB role (the real access boundary).** Point
   `ODDISH_DATABASE_URL_RO` at a **dedicated, non-superuser** role
   (`NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`) that owns
   nothing and is granted `SELECT` **only** on the analytics tables — never on
   `users`/auth, chat, `documents`, `api_keys`/BYOK, or other secret-bearing
   tables. Then even a query that gets past everything else hits `permission
   denied`, not a data dump. There is no fallback to the backend DSN, so a
   missing/mistyped secret disables the tool instead of handing it a full role.
2. **Code-level allow-list (AST-parsed).** Each query is parsed with `pglast`
   (the real Postgres grammar, not a regex) and rejected unless it is a single
   `SELECT`/`EXPLAIN`/`SHOW` that touches only allow-listed relations
   (`ODDISH_SQL_TABLES`, default `analysis_costs,trials,tasks,experiments,`
   `model_pricing,orgs`, plus `information_schema`) and calls no dangerous
   function (`pg_read_file`, `lo_export`, `dblink`, `set_config`, `pg_sleep`,
   …). Data-modifying CTEs, `EXPLAIN ANALYZE <write>`, `SELECT INTO`, locking
   clauses, and stacked statements are all rejected before touching the DB. This
   is what a prompt injection can't talk its way past.
3. **Postgres `READ ONLY` transaction.** Every query also runs inside
   `conn.transaction(readonly=True)`, so the server rejects any write with
   *"cannot execute … in a read-only transaction"* — a backstop even if the role
   were over-granted.
4. **Blast-radius caps.** A 15s `statement_timeout`, a 10s connect timeout, a
   200-row cap, and per-cell truncation so a wide field can't break the Slack
   output.

The agent is also told (system prompt) that tool output is untrusted data, to
aggregate in SQL, and to introspect `information_schema.columns` for schema. The
prompt is defense-in-depth only — layers 1 and 2 are the controls.

### Provisioning the RO role (example)

```sql
CREATE ROLE oddish_slackbot_ro LOGIN PASSWORD '…'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
GRANT CONNECT ON DATABASE oddish TO oddish_slackbot_ro;
GRANT USAGE ON SCHEMA public TO oddish_slackbot_ro;
GRANT SELECT ON analysis_costs, trials, tasks, experiments, model_pricing, orgs
  TO oddish_slackbot_ro;   -- and nothing else
```

Keep the code allow-list (`ODDISH_SQL_TABLES`) and the role's grants in sync.

## Deploy steps

1. Provision the read-only DB role (see "Provisioning the RO role" above) and
   put its DSN in the `oddish-slackbot` Modal secret as `ODDISH_DATABASE_URL_RO`.
2. `cd slackbot && modal deploy app.py`
3. Copy the deployed `web` URL and paste it into `manifest.yaml`
   (`settings.event_subscriptions.request_url`, keeping the `/slack/events` path).
4. Create the Slack app from `manifest.yaml`, install it into the workspace, and
   invite the bot into the channels where it should answer.

## Limitations

- **Channels only, no DMs.** The manifest subscribes to `app_mention` only, so
  direct messages to the bot are silently ignored. To support DMs you would add
  the `message.im` event and the `im:history` scope, and handle that event type
  in `_dispatch`.
- **`oddish_sql` reachability is only as tight as the RO role + allow-list.**
  The lethal-trifecta risk (untrusted input + SQL tool + channel output) is
  contained by the layers in Read-only SQL above, but two of them are
  operator-configured: the RO role's grants and `ODDISH_SQL_TABLES` must both
  stay scoped to non-sensitive analytics tables, and `SLACK_ALLOWED_USERS` (and
  optionally `SLACK_ALLOWED_CHANNELS`) must stay tight. The code allow-list and
  READ ONLY transaction hold regardless, but widening the role's grants without
  widening the code allow-list (or vice-versa) is the mistake to avoid.
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
