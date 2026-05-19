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
  │  - Enqueues trial / analysis / verdict work as worker_jobs rows
  ▼
Postgres
  - worker_jobs   (unified queue; TRIAL / ANALYSIS / VERDICT)
  - trials/tasks  (domain state + live UI columns)
  - queue_slots   (per-queue-key concurrency leases)
  + cloud tables  (orgs / users / api_keys)
  │
  ▼
Worker dispatcher (`worker/functions.py::poll_queue`, every 180s)
  │  - Runs unified cleanup (stale-heartbeat + stage safety nets)
  │  - Discovers active queue keys from worker_jobs
  │  - Spawns single-job Modal containers per queue key
  ▼
Single-job worker (`process_single_job`)
  │  - Acquires a queue_slots lease
  │  - Claims ONE worker_jobs row (any kind)
  │  - Dispatches to the registered handler
  │  - Writes heartbeats, records outcome, exits
  ▼
Modal sandboxes (Harbor execution, logs/artifacts to S3)
```

### Worker architecture

Dispatcher + single-job pattern backed by the unified `worker_jobs` table:

1. `poll_queue()` runs on a 180s Modal schedule. It calls
   `cleanup_orphaned_queue_state` (zombie-txn reap, stale-heartbeat sweep,
   stage safety nets, orphaned-slot release), discovers active queue keys
   via `discover_active_worker_job_queue_keys`, and launches up to
   `MAX_WORKERS_PER_POLL` single-job containers.
2. `process_single_job(queue_key)` acquires a `queue_slots` lease for the
   queue key and calls `run_single_worker_job`, which atomically claims one
   row from `worker_jobs`, dispatches to the registered handler
   (`TRIAL` / `ANALYSIS` / `VERDICT`), writes heartbeats to both
   `worker_jobs.heartbeat_at` and the mirrored domain column, records the
   outcome, runs the post-success hook, releases the lease, and exits.

`_POST_SUCCESS_HOOKS = {TRIAL: notify_github_trial, ANALYSIS: …,
VERDICT: …}` is threaded through so GitHub notifications fire after the
row is `SUCCESS`. Handlers are registered at module load via
`ensure_builtin_handlers_registered()` so every container has
`TRIAL` / `ANALYSIS` / `VERDICT` wired up before any claim. Adding a new
kind (e.g. `QA_REVIEW`) is one handler class plus a `register` call — no
new claim SQL, cleanup step, or dispatcher branch.

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
| `api/routers/trials.py` | Trial listing, retry/reanalysis, logs, result, trajectory, and debug file inspection |
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
- provider keys such as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DAYTONA_API_KEY`
- GitHub notifier settings such as `GITHUB_TOKEN` and `ODDISH_DASHBOARD_URL`

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

Modal runtime knobs are read directly by `modal_app.py`, including:

- `ODDISH_ENABLE_MODAL_WORKERS`
- `ODDISH_MODAL_API_MIN_CONTAINERS`
- `ODDISH_MODAL_API_BUFFER_CONTAINERS`
- `ODDISH_MODAL_API_MAX_CONTAINERS`
- `ODDISH_MODAL_API_CONCURRENCY_TARGET`
- `ODDISH_MODAL_API_CONCURRENCY_MAX`
- `ODDISH_MODAL_POLL_INTERVAL_SECONDS`
- `ODDISH_MODAL_WORKER_TIMEOUT_SECONDS`
- `ODDISH_MODAL_WORKER_MIN_CONTAINERS`
- `ODDISH_MODAL_WORKER_BUFFER_CONTAINERS`
- `ODDISH_MODAL_WORKER_SCALEDOWN_WINDOW_SECONDS`
- `ODDISH_MODAL_WORKER_MAX_CONTAINERS`
- `ODDISH_MODAL_MAX_WORKERS_PER_POLL` *(optional, default `64`)*
- `ODDISH_DEFAULT_MODEL_CONCURRENCY`
- `MODAL_APP_NAME`
- `MODAL_SECRET_ENVIRONMENT`

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
| DELETE | `/tasks/{task_id}` | Delete task and queued jobs |
| POST | `/tasks/{task_id}/analysis/retry` | Re-queue analysis jobs for completed trials in a task |
| POST | `/tasks/{task_id}/verdict/retry` | Re-queue verdict generation for a task |
| GET | `/tasks/{task_id}/trials` | Trials for task |
| GET | `/tasks/{task_id}/trials/{index}` | Trial by index |
| GET | `/tasks/{task_id}/versions` | List stored task versions |
| GET | `/tasks/{task_id}/versions/{version}` | Get one stored task version |
| DELETE | `/trials/{trial_id}` | Delete a single trial and its associated S3 artifacts (admin only) |
| POST | `/trials/{trial_id}/retry` | Re-queue trial |
| POST | `/trials/{trial_id}/analysis/retry` | Re-queue analysis for a completed trial |
| GET | `/trials/{trial_id}/logs` | Trial logs |
| GET | `/trials/{trial_id}/logs/structured` | Structured trial logs |
| GET | `/trials/{trial_id}/files` | List trial files |
| GET | `/trials/{trial_id}/files/{path}` | Fetch trial file |
| GET | `/trials/{trial_id}/debug-files` | Trial file debug listing |
| GET | `/trials/{trial_id}/result` | Trial result.json |
| GET | `/trials/{trial_id}/trajectory` | Trial trajectory |
| GET | `/tasks/{task_id}/files` | List task files (presigned URLs) |
| GET | `/tasks/{task_id}/files/{path}` | Fetch task file |

### Experiment sharing and management

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/experiments/{experiment_id}/share` | Get publish/share state |
| PATCH | `/experiments/{experiment_id}` | Rename experiment |
| POST | `/experiments/{experiment_id}/publish` | Publish experiment |
| POST | `/experiments/{experiment_id}/unpublish` | Unpublish experiment |
| DELETE | `/experiments/{experiment_id}` | Delete experiment + tasks/trials |

### Organization and auth

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/org` | Current org metadata |
| GET | `/users` | List org users |
| POST | `/users` | Invite user |
| DELETE | `/users/{user_id}` | Deactivate user |
| GET | `/api-keys` | List API keys |
| POST | `/api-keys` | Create API key (owner role required) |
| DELETE | `/api-keys/{key_id}` | Revoke API key |

### Public sharing (no auth required)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/public/experiments/{public_token}` | Public experiment metadata |
| GET | `/public/experiments` | List public experiments for dataset browsing |
| GET | `/public/experiments/{public_token}/tasks` | Public tasks and trials for a shared experiment |
| GET | `/public/tasks/{task_id}` | Public task status (with optional counts-only mode) |
| GET | `/public/tasks/{task_id}/trials` | Public trial list |
| GET | `/public/trials/{trial_id}/logs` | Public trial logs |
| GET | `/public/trials/{trial_id}/logs/structured` | Public structured logs |
| GET | `/public/trials/{trial_id}/trajectory` | Public trajectory |
| GET | `/public/trials/{trial_id}/files` | Public trial file listing |
| GET | `/public/trials/{trial_id}/files/{path}` | Public trial file |
| GET | `/public/trials/{trial_id}/result` | Public result |
| GET | `/public/tasks/{task_id}/files` | Public task file listing (supports version/presign params) |
| GET | `/public/tasks/{task_id}/files/{path}` | Public task file content or presign metadata |

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
curl -H "Authorization: Bearer $ODDISH_API_KEY" "$ODDISH_MODAL_API_URL/tasks" | jq

# dashboard queue overview
curl -H "Authorization: Bearer $ODDISH_API_KEY" "$ODDISH_MODAL_API_URL/dashboard" | jq '.queues'
```
