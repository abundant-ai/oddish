# Oddish Backend

Serverless API and worker orchestration for Oddish Cloud, deployed on [Modal](https://modal.com), with multi-tenant authentication and authorization.

## Overview

The backend wraps the OSS `oddish` core with:
- Multi-tenant API (`org_id`-scoped queries)
- Dual auth (API keys + Clerk JWTs)
- Modal-hosted API/workers/sandboxes, or Railway/Docker for standalone deployment
- Queue-key concurrency controls
- Public token-based sharing endpoints

## System Architecture

### Data flow

```
User (Dashboard, CLI, SDK)
  │
  ▼
Modal API (FastAPI in `endpoints.py` and `api/routers/*`)
  │  - Auth: API key or Clerk JWT
  │  - Enqueues trial and task QA work as worker_jobs rows
  ▼
Postgres
  - worker_jobs   (unified queue, including TRIAL / QA / TASK_EXPAND)
  - trials/tasks  (domain state + live UI columns)
  - queue_slots   (per-queue-key concurrency leases)
  + cloud tables  (orgs / users / api_keys)
  │
  ▼
Scheduled functions
  ├─ poll_queue
  │    - Discovers active queue keys from worker_jobs
  │    - Spawns single-job Modal containers per queue key
  ├─ reconcile_queue_state
  │    - Runs cleanup, stage safety nets, and owner backfill
  └─ process_single_job
       - Acquires a queue_slots lease
       - Claims ONE worker_jobs row (any kind)
       - Dispatches to the registered handler
       - Writes heartbeats, records outcome, exits
  ▼
Modal sandboxes (Harbor execution, logs/artifacts to S3)
```

### Worker architecture

Dispatcher + single-job pattern backed by the unified `worker_jobs` table:

1. `poll_queue()` runs on a 180s Modal schedule. It discovers active queue
   keys via `discover_active_worker_job_queue_keys` and launches up to
   `MAX_WORKERS_PER_POLL` single-job containers.
2. `reconcile_queue_state()` runs separately. It calls
   `cleanup_orphaned_queue_state` (zombie-txn reap, stale-heartbeat sweep,
   stage safety nets, orphaned-slot release) and runs the experiments owner
   backfill (dashboard Mine fast path).
3. `process_single_job(queue_key)` acquires a `queue_slots` lease for the
   queue key and calls `run_single_worker_job`, which atomically claims one
   row from `worker_jobs`, dispatches to the registered handler
  (`TRIAL` / `QA` / `TASK_EXPAND` / `TAG_PROJECT`), writes heartbeats to both
   `worker_jobs.heartbeat_at` and the mirrored domain column, records the
   outcome, runs the post-success hook, releases the lease, and exits.

Post-success hooks for `TRIAL`, `QA`, and transitional `ANALYSIS` rows are
threaded through so GitHub notifications fire after the row is `SUCCESS`.
Handlers are registered at module load via `ensure_builtin_handlers_registered()`
so every container has
`TRIAL`, `QA`, `TASK_EXPAND`, `TAG_PROJECT`, and transitional `ANALYSIS`
wired up before any claim. Adding a new kind (e.g. `QA_REVIEW`) is one
handler class plus a `register` call — no new claim SQL, cleanup step, or
dispatcher branch.

## Authentication Model

The backend accepts auth from `Authorization`, `X-Clerk-Authorization`, or `X-Authorization`.

### API keys (programmatic access)

```bash
curl -H "Authorization: Bearer ok_abc123..." "$API_URL/tasks"
```

- Key format starts with `ok_`
- Stored hashed (SHA-256) in `api_keys`
- Scope options: `full`, `tasks`, `read`

### Clerk JWTs (dashboard access)

- Validated against Clerk JWKS
- Organization context extracted from token claims
- User and org membership resolved to internal auth context

### Auth flow

1. Read token from accepted header.
2. If token starts with `ok_`, validate API key and scope.
3. Otherwise validate Clerk JWT and resolve org/user.
4. Return auth context (`org_id`, `user_id`, `scope`) to route handlers.

If a Clerk JWT arrives without an `org_id`, the backend will try to resolve a
single existing org membership and, if none exists, provision a personal org for
that user.

## Multi-tenancy

All task/trial/experiment access is org-scoped. Cloud-side schema adds:

- `experiments.org_id`
- `tasks.org_id`, `tasks.created_by_user_id`, `tasks.task_s3_key`
- `trials.org_id`, `trials.trial_s3_key`

The API layer enforces this scope in all list/read/write queries.

## Key Files

| Path | Purpose |
|------|---------|
| `deploy.py` | Modal app entrypoint (imports API + worker functions) |
| `modal_app.py` | Modal image, bucket mounts, and shared runtime setup |
| `endpoints.py` | Modal ASGI app function with concurrency and secrets wiring |
| `serve.py` | Railway/uvicorn entrypoint for non-Modal deployment |
| `Dockerfile` | Container image for Railway or standalone deployment |
| `cloud_policy.py` | Hosted-only environment policy (allowed sandboxes, default cloud env) |
| `api/app.py` | FastAPI app factory + startup/lifespan wiring |
| `api/schemas.py` | Pydantic models for org/auth/share responses |
| `api/routers/tasks.py` | Task upload, browse, versions, sweep creation, sharing, retries, and file access |
| `api/routers/trials.py` | Trial listing, retry, logs, result, trajectory, and debug file inspection |
| `api/routers/dashboard.py` | Cached aggregate dashboard endpoint (queues, usage, tasks, experiments) |
| `api/routers/orgs.py` | Current org lookup and Clerk-backed user management |
| `api/routers/api_keys.py` | Org API key listing, creation, and revocation |
| `api/routers/admin.py` | Queue-slot, queue-status, orphaned-state, and **worker_jobs** inspection endpoints |
| `api/routers/clerk_webhooks.py` | Clerk org/user synchronization |
| `api/routers/github_webhooks.py` | GitHub status/refresh integrations |
| `auth/verification.py` | API key + Clerk JWT verification and auth caches |
| `auth/provisioning.py` | Clerk user/org provisioning helpers |
| `auth/types.py` | `AuthContext` dataclass and `AuthMethod` enum |
| `models.py` | Cloud auth models (orgs/users/api keys) |
| `slack_notifications.py` | Scheduled expensive experiment/trial Slack and owner-email notifications |
| `worker/functions.py` | Modal dispatcher (`poll_queue`) and kind-agnostic `process_single_job` runner |
| `worker/runtime.py` | Modal runtime patching and storage setup |
| `worker/github.py` | Thin wrappers delegating GitHub notifications to `oddish.integrations.github` |
| `alembic/` | Cloud migrations (auth + cloud table extensions) |

## Configuration

```bash
cp .env.example .env
```

Use `backend/.env.example` as the starting point for local backend config.
For the API and worker runtime, the minimum required values are:

- `ODDISH_DATABASE_URL`
- `CLERK_DOMAIN`

Required for Clerk-backed org invites, membership lookups, and GitHub username enrichment:

- `CLERK_SECRET_KEY`

Required if you want Clerk webhook ingestion enabled:

- `CLERK_WEBHOOK_SECRET`

S3-compatible storage is **required**. Task bundles and trial artifacts are
uploaded directly from the client to S3 via presigned PUT URLs, and the
backend streams logs/results/files back through the same bucket. You must
configure the full `ODDISH_S3_*` set:

- `ODDISH_S3_BUCKET`
- `ODDISH_S3_REGION`
- `ODDISH_S3_ACCESS_KEY`
- `ODDISH_S3_SECRET_KEY`
- `ODDISH_S3_ENDPOINT_URL` (for non-AWS S3-compatible providers)

Common optional settings:

- `CORS_ALLOWED_ORIGINS`
- `CLERK_ISSUER`
- `CLERK_JWT_AUDIENCE`
- provider keys such as `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION`, `ODDISH_AZURE_OPENAI_DEPLOYMENTS`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DAYTONA_API_KEY`
- `ODDISH_OPENAI_PROVIDER=openai` plus `OPENAI_API_KEY` only when intentionally routing OpenAI-family jobs to public OpenAI
- GitHub notifier settings such as `GITHUB_TOKEN` and `ODDISH_DASHBOARD_URL`
- `SLACK_EXPENSE_WEBHOOK_URL` for deterministic expensive experiment/trial Slack notifications, plus `RESEND_API_KEY` and `ODDISH_EXPENSE_EMAIL_FROM` to send the same alerts directly to each attributed experiment owner's account email. Unattributed experiments remain Slack-only. Notifications are on by default for the production app; defaults are experiment alerts at each $1,000 milestone and anomaly alerts for trials over $70 that exceed twice the average of other trials in the experiment for the same task and model. Thresholds and the experiment repeat interval use the `ODDISH_SLACK_*` settings in `.env.example`; previews opt in with `ODDISH_ENABLE_SLACK_EXPENSE_NOTIFICATIONS=true` and can attach a preview-only notification secret via `ODDISH_SLACK_EXPENSE_SECRET_NAME` / `ODDISH_SLACK_EXPENSE_SECRET_ENVIRONMENT`.

### Observability (Pydantic Logfire)

Optional. Provision a write token in Logfire and add it to the
`oddish-prod` Modal secret so the API containers and workers both
pick it up:

- `LOGFIRE_TOKEN` — Logfire write token (the only required value).
- `LOGFIRE_ENVIRONMENT` *(optional)* — overrides the auto-detected
  label (`production` / `preview` / `development`). PR previews on
  Modal are auto-tagged `preview` and ride with `oddish.pr=<number>`
  as a span attribute, so you can filter `deployment.environment ==
  "preview"` across all PRs and drill into one with `oddish.pr`.
- `LOGFIRE_SERVICE_NAME` *(optional)* — defaults to `oddish-backend`.
- `ODDISH_LOGFIRE_INSTRUMENT_SQLA` *(optional, default `0`)* — set to
  `1` to also wrap SQLAlchemy executes with span instrumentation. We
  already wrap asyncpg one layer down, and the SQLA wrapper walks
  every statement's expression tree, which is meaningful overhead on
  hot paths.

Modal runtime knobs are read directly by `modal_app.py`, which is the source
of truth for the full list and defaults. They cover worker enablement
(`ODDISH_ENABLE_MODAL_WORKERS`, `ODDISH_ENABLE_SLACK_EXPENSE_NOTIFICATIONS`),
API/worker/dispatcher/reconciler container
scaling and CPU/memory sizing (`ODDISH_MODAL_API_*`, `ODDISH_MODAL_WORKER_*`,
`ODDISH_MODAL_DISPATCHER_*`, `ODDISH_MODAL_RECONCILER_*`), schedule intervals
and timeouts (`ODDISH_MODAL_POLL_INTERVAL_SECONDS`,
`ODDISH_MODAL_CLEANUP_*_SECONDS`, `ODDISH_MODAL_WORKER_TIMEOUT_SECONDS`),
throughput (`ODDISH_MODAL_MAX_WORKERS_PER_POLL`, default `256`;
`ODDISH_MODAL_WORKER_MAX_CONTAINERS`, default `2688`), per-model concurrency
(`ODDISH_DEFAULT_MODEL_CONCURRENCY`, `ODDISH_MODEL_CONCURRENCY_OVERRIDES`,
`ODDISH_MODAL_NOP_ORACLE_CONCURRENCY`), and app naming (`MODAL_APP_NAME`,
`MODAL_SECRET_ENVIRONMENT`).

Local `backend/.env` values are layered on top of the shared Modal secret for local deploys.

### oddish runtime patching

`endpoints.py`, `serve.py`, and `worker/runtime.py` patch oddish settings at startup:

- `endpoints.py` / `serve.py`: set `db_use_null_pool` for per-request DB connections
- `worker/runtime.py`: refresh DB connection pools per container, ensure the per-container Harbor scratch dir exists (defaults to `/tmp/harbor-jobs`), and force Harbor environment to Modal-compatible mode

## API Endpoints

All routes require auth unless marked public.

### Core and task/trial operations

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/dashboard` | Cached aggregate response for queues, pipeline stats, usage, tasks, and experiments |
| POST | `/tasks/upload/init` | Start a direct-to-S3 task upload and return a presigned PUT URL |
| POST | `/tasks/upload/complete` | Finalize a direct-to-S3 task upload after the client PUT succeeds |
| POST | `/trials/import/init` | Register an off-oddish trial and return a presigned artifact URL |
| POST | `/trials/import/complete` | Finalize an imported trial after the client PUT succeeds |
| POST | `/tasks/sweep` | Expand one task into multiple trials; accepts optional `max_trial_attempts` for newly-created trials |
| GET | `/tasks` | List tasks (org-scoped, paginated/filtered) |
| GET | `/tasks/browse` | Browse latest task versions with pagination and search |
| GET | `/tasks/{task_id}` | Task details |
| POST | `/tasks/cancel` | Cancel in-flight trials and queue jobs for one or more tasks (org-scoped); Modal workers terminated when applicable |
| POST | `/tasks/{task_id}/qa/retry` | Re-run task QA: classify trials and synthesize the verdict |
| POST | `/tasks/{task_id}/qa/cancel` | Cancel a task's in-flight QA job |
| GET | `/tasks/{task_id}/trials` | Trials for task |
| GET | `/tasks/{task_id}/trials/{index}` | Trial by index |
| GET | `/tasks/{task_id}/versions` | List stored task versions |
| GET | `/tasks/{task_id}/versions/{version}` | Get one stored task version |
| DELETE | `/trials/{trial_id}` | Soft-delete a trial, cancel jobs, and invalidate the cached verdict (admin only) |
| POST | `/trials/{trial_id}/retry` | Re-queue trial |
| GET | `/trials/{trial_id}/logs` | Trial logs |
| GET | `/trials/{trial_id}/logs/structured` | Structured trial logs |
| GET | `/trials/{trial_id}/files` | List trial files |
| GET | `/trials/{trial_id}/files/{path}` | Fetch trial file |
| GET | `/trials/{trial_id}/debug-files` | Trial file debug listing |
| GET | `/trials/{trial_id}/result` | Trial result.json |
| GET | `/trials/{trial_id}/trajectory` | Trial trajectory |
| GET | `/tasks/{task_id}/files` | List task files (presigned URLs) |
| GET | `/tasks/{task_id}/files/{path}` | Fetch task file |
| DELETE | `/tasks/{task_id}` | Soft-delete a task and all its trials (admin only) |

### Experiment sharing and management

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/experiments/{experiment_id}/share` | Get publish/share state |
| PATCH | `/experiments/{experiment_id}` | Rename experiment |
| POST | `/experiments/{experiment_id}/publish` | Publish experiment |
| POST | `/experiments/{experiment_id}/unpublish` | Unpublish experiment |
| DELETE | `/experiments/{experiment_id}` | Soft-delete experiment + its trials and now-orphaned tasks (admin only) |
| DELETE | `/experiments/{experiment_id}/tasks/{task_id}` | Unlink a shared task from one experiment (tombstones the join row + that experiment's trials; the task survives) |

### Organization and auth

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/org` | Current org metadata |
| GET | `/users` | List org users |
| POST | `/users` | Invite user |
| DELETE | `/users/{user_id}` | Deactivate user |
| GET | `/api-keys` | List API keys |
| POST | `/api-keys` | Create API key (any org admin or member; admins mint full/tasks/read, members mint tasks/read) |
| GET | `/api-keys/permissions` | Whether the current user may create API keys |
| DELETE | `/api-keys/{key_id}` | Revoke API key |

### Public sharing (no auth required)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/public/experiments/{public_token}` | Public experiment metadata |
| GET | `/public/experiments` | List public experiments for dataset browsing |
| GET | `/public/experiments/{public_token}/tasks` | Public tasks and trials for a shared experiment |
| GET | `/public/experiments/{public_token}/tasks/{task_id}` | Public task status within a shared experiment |
| GET | `/public/experiments/{public_token}/tasks/{task_id}/trials` | Public trial list within a shared experiment |
| GET | `/public/experiments/{public_token}/trials/{trial_id}/logs` | Public trial logs |
| GET | `/public/experiments/{public_token}/trials/{trial_id}/logs/structured` | Public structured logs |
| GET | `/public/experiments/{public_token}/trials/{trial_id}/trajectory` | Public trajectory |
| GET | `/public/experiments/{public_token}/trials/{trial_id}/files` | Public trial file listing |
| GET | `/public/experiments/{public_token}/trials/{trial_id}/files/{path}` | Public trial file |
| GET | `/public/experiments/{public_token}/trials/{trial_id}/result` | Public result |
| GET | `/public/experiments/{public_token}/tasks/{task_id}/files` | Public task file listing |
| GET | `/public/experiments/{public_token}/tasks/{task_id}/files/{path}` | Public task file content or presign metadata |

### Admin and integrations

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/admin/slots` | `queue_slots` lease state |
| GET | `/admin/queue-status` | Per-kind queue counts sourced from `trials`/`tasks` |
| GET | `/admin/orphaned-state` | Stale/orphaned queue state diagnostics |
| GET | `/admin/worker-jobs` | Unified `worker_jobs` kind×status matrix, stale-RUNNING samples, recent failures/cancels, and duration percentiles |
| POST | `/admin/tasks/expand-backfill` | Backfill sweep expansion for older tasks missing worker_jobs rows (admin only) |
| POST | `/webhooks/clerk` | Clerk webhook ingestion |
| POST | `/github/tasks/{task_id}/refresh` | Refresh task PR comment |
| POST | `/github/experiments/{experiment_id}/refresh` | Refresh experiment PR comments |
| GET | `/github/status` | GitHub integration status |

## Database and Migrations

Two migration stacks are required on fresh environments:
1. Core tables: `oddish/alembic/`
2. Cloud tables/extensions: `backend/alembic/`

```bash
# Core (run in oddish/)
uv run alembic upgrade head

# Cloud (run in backend/)
uv run alembic upgrade head
```

Apply migrations against the database in `ODDISH_DATABASE_URL` (for example a hosted Postgres instance).

## Development Workflows

```bash
# Install backend deps (includes the local ../oddish path dependency)
cd backend
uv sync
```

```bash
# Backend only (Modal local serve)
cd backend
uv run modal serve deploy.py
```

For full-stack local development, run the Modal backend and point the frontend at it:

```bash
# Terminal 1 — backend
cd backend
uv run modal serve deploy.py

# Terminal 2 — frontend
cd frontend
pnpm dev
```

Set `NEXT_PUBLIC_API_URL` in `frontend/.env.local` to the `modal serve` URL
(printed by Terminal 1, e.g. `https://<workspace>--api-dev.modal.run`). See
`frontend/env.example` for the full frontend env surface, and
[`../SELF_HOSTING.md`](../SELF_HOSTING.md) for the HTTPS / production-Clerk
variant of this loop.

### Smoke tests

```bash
# authenticated list
curl -H "Authorization: Bearer $ODDISH_API_KEY" "$ODDISH_API_URL/tasks" | jq

# dashboard queue overview
curl -H "Authorization: Bearer $ODDISH_API_KEY" "$ODDISH_API_URL/dashboard" | jq '.queues'
```

## User quotas — enforcement rollout (`ODDISH_QUOTA_MODE`)

Per-user dollar budgets use a rolling 24-hour window. Spend counts until 24h
after the trial finished. The operator toggle is `shadow` (default) → `enforce`
via the `ODDISH_QUOTA_MODE` env var; each stage is a config flip, no redeploy of
code (`off` stays available as a full no-op opt-out, and is also the
schema-guard fail-safe below):

1. **`shadow`** (default) — compute the check and emit a structured
   `quota.would_block` event (`metric=quota.would_block reason=… org_id=…
   billed_user_id=… used=… limit=…`) but never raise. Scrape those logs to
   enumerate who *would* be blocked and which submissions have an unresolved
   payer (`billed_user_id` None — an unlinked GitHub author); notify those users
   to link at oddish.app. `billed_user_id` is stamped at trial creation, so the
   usage data accrues before any enforcement.
2. **`enforce`** — over-budget submissions get HTTP **402** with
   `{"detail": {message, used_usd, reserved_usd, limit_usd}}`; an
   unattributable run gets **403**.

There is **no seed/coverage pre-step**: stamping is already live from the
attribution slice, and a member with no `quotas` override row is enforced at
`ODDISH_DEFAULT_DAILY_QUOTA_USD` (default-at-read). When `quota_mode != off`, the
API startup verifies `trials.billed_user_id` and the `quotas` + `quota_bumps` +
`org_quotas`
tables exist and otherwise forces `off` (fail-safe, never a silent SUM
fail-open). Tune `ODDISH_DEFAULT_DAILY_QUOTA_USD` and
`ODDISH_PENDING_TRIAL_RESERVATION_USD` without a code change.

**Temporary quota bumps.** `POST /quotas/{user_id}/bumps` grants `+amount_usd`
for `duration_hours`, with expiry computed on the DB clock (immune to client
clock skew); `DELETE /quotas/{user_id}/bumps` revokes a member's live
bumps by stamping `revoked_at` (audit rows survive). Effective limit is
`base + SUM(live bumps)` where live means `revoked_at IS NULL`, `deleted_at IS
NULL`, and `expires_at > NOW()` (read-time expiry on the DB clock — no scheduler
or revert job). After a bump expires the member's spend still counts in the
rolling window, so they may block at the base limit — that is intended.

### Org-wide monthly cap

Layered on top of the per-user rolling window is an **org-wide aggregate
CALENDAR-MONTH (UTC) cap**: the sum of *all* payers' settled spend in an org
(including unattributed NULL-billed spend) plus its in-flight reservation. It
resets on the 1st (UTC) to match billing periods. It ships **inert** —
`ODDISH_DEFAULT_ORG_MONTHLY_QUOTA_USD` is unset (`None` = no org cap) and no
`org_quotas` override rows exist, so the org check short-circuits until a cap is
configured. Enable per-org via `PUT /quotas/org` (admin, `require_can_manage_quotas`)
or globally via the env default. Under `enforce`, an over-cap submission gets
HTTP **402** (`"Your organization is over its monthly budget …"`); under
`shadow` it emits `metric=quota.would_block reason=org_over_budget`. Admins see
month-to-date org usage on `GET /quotas`; any member can read the org budget
snapshot + adaptive daily goal on `GET /quotas/org`. Advisory-lock order is
org → payer → row locks (ENFORCE-only, and the org lock is taken only when a cap
is actually configured).
