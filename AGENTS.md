# Oddish Repository Guide

This file is the technical guide for the entire monorepo. End-user CLI docs live in `DOCS.md`.

The repo has three main packages:

- `oddish/` — the core Python CLI, FastAPI server, queueing layer, and worker runtime
- `backend/` — the hosted cloud layer built on top of `oddish`; adds multi-tenant auth, Modal deployment, and product-specific endpoints
- `frontend/` — the Next.js App Router dashboard and public pages

Python `3.12+` is required for `oddish` and `backend`. Node.js `20+` and `pnpm` are required for `frontend`.

## Maintenance Notes

- Keep `oddish/README.md` focused on end-user CLI workflows.
- Put `oddish` implementation details, architecture notes, and local development guidance here.
- If you change the CLI surface in `oddish/src/oddish/cli/`, update `oddish/README.md`.
- If you change API contracts, queue behavior, or storage layout, update this file.
- If you change `backend/` auth, deployment, or worker orchestration, update this file.
- If you change `frontend/` routing, API proxy structure, or auth behavior, update this file.

## Repository Layout

```text
oddish/                         # Core Python package (CLI, server, workers, DB)
├── src/oddish/
│   ├── cli/                    # oddish run/status/cancel/pull/delete
│   ├── core/                   # shared business logic (reused by backend/)
│   ├── server/                 # standalone FastAPI app (python -m oddish.server)
│   ├── db/                     # models, connection helpers, storage
│   ├── workers/                # Unified worker_jobs runtime: dispatcher,
│   │                           #   single-job runner, handlers, cleanup
│   ├── backfill_queue_keys.py
│   ├── config.py
│   ├── experiment.py
│   ├── queue.py                # task/trial enqueue + worker_jobs enqueue helpers
│   └── schemas.py
├── alembic/                    # Core DB migrations
├── env.example
└── pyproject.toml

backend/                        # Hosted cloud layer (Modal deployment)
├── api/
│   ├── app.py                  # FastAPI app factory and lifespan wiring
│   ├── schemas.py              # Pydantic models for org/auth/share responses
│   └── routers/                # tasks, trials, dashboard, orgs, api_keys, admin, webhooks
├── auth/                       # API key + Clerk JWT verification, provisioning, types
├── worker/                     # Modal dispatcher and single-job worker orchestration
├── deploy.py                   # Modal app entrypoint
├── modal_app.py                # Modal image, volumes, shared runtime
├── endpoints.py                # Modal ASGI app function with concurrency/volume wiring
├── serve.py                    # Railway/uvicorn entrypoint for non-Modal deployment
├── cloud_policy.py             # Hosted-only environment policy
├── models.py                   # Cloud auth models (orgs/users/api keys)
├── alembic/                    # Cloud migrations (auth + cloud table extensions)
└── pyproject.toml

frontend/                       # Next.js App Router dashboard
├── src/
│   ├── app/
│   │   ├── page.tsx            # Public landing page / signed-in redirect
│   │   ├── (app)/              # Authenticated app shell (dashboard, tasks, experiments, settings, admin)
│   │   ├── share/[token]/      # Public experiment page
│   │   ├── datasets/           # Public dataset pages
│   │   ├── api/                # Backend proxy route handlers
│   │   └── providers.tsx       # Shared SWR config
│   ├── components/             # Dashboard, detail panels, charts, nav, UI primitives
│   ├── lib/                    # API helpers, backend config, shared types, utilities
│   └── middleware.ts           # Clerk route protection
└── package.json
```

## System Architecture

```text
Browser / oddish CLI
        |
        v
Next.js route handlers (frontend/src/app/api/*)
        |
        v
FastAPI server — oddish standalone (python -m oddish.server)
           or backend cloud layer (Modal / Railway)
        |
        v
Postgres
  - worker_jobs       # unified queue (TRIAL / ANALYSIS / VERDICT / …)
  - trials / tasks    # domain state + live UI columns
  - queue_slots       # per-queue-key concurrency leases
        |
        v
Workers (auto-started by API, or standalone via python -m oddish.workers.queue.worker)
        |
        v
Harbor task execution → logs/results/artifacts (S3)
```

High-level flow:

1. Upload a task bundle directly to S3 via a presigned PUT URL.
2. Submit a sweep of agent/model trials for that task; each trial, analysis,
   and verdict is enqueued as a row in `worker_jobs` in the same transaction
   as its domain row. Set `max_trial_attempts` on a sweep submission or sweep
   config to override the total attempt budget for newly-created trials.
3. Workers claim one `worker_jobs` row at a time, dispatch to the registered
   handler (`TRIAL` / `ANALYSIS` / `VERDICT`), write heartbeats, and exit.
4. Use the CLI or dashboard to watch progress and pull logs/artifacts
   back locally.

## Package Boundaries

`oddish` owns the execution core and shared queue/runtime primitives:

- core models and migrations, including `worker_jobs` and `queue_slots`
- unified claim/dispatch SQL, one `run_single_worker_job` runner, and a
 handler registry (`TrialJobHandler`, `AnalysisJobHandler`, `VerdictJobHandler`)
- shared queue-slot leasing, per-queue-key concurrency limits, and
 per-user fairness on `TRIAL` claims
- stale-heartbeat reaping, RETRYING → QUEUED mirror-back, and pipeline
 stage reconciliation in one cleanup sweep
- soft-delete semantics on domain rows via the `deleted_at` column and
 a session-level filter (`oddish.db.soft_delete`); every ORM read on a
 registered model gets `WHERE deleted_at IS NULL` automatically

`backend` wraps `oddish` with the hosted-only layer:

- Clerk/API key auth and org-scoped APIs
- Modal worker spawning and runtime patching
- cloud environment policy and GitHub notification hooks
- public sharing routes and other product-specific endpoints

`frontend` provides the user-facing layer:

- authenticated dashboard, task browser, experiment views
- Clerk-based auth and org management
- Next.js route handlers that proxy requests to the backend

---

## `oddish/` — Core Package

### Install Extras

The base `pip install oddish` is CLI-only (light deps). Use extras for server and worker use cases:

```bash
pip install oddish            # CLI only — typer, httpx, pydantic, harbor
pip install oddish[server]    # + FastAPI, SQLAlchemy, asyncpg, alembic, aioboto3
pip install oddish[worker]    # + server + LLM provider SDKs
pip install oddish[all]       # everything including dev tools
```

### Entry Points

- CLI: `oddish` → `oddish.cli:app`
- API server: `python -m oddish.server` (requires `oddish[server]`)
- Standalone worker: `python -m oddish.workers.queue.worker` (requires `oddish[worker]`)
- DB helper CLI: `python -m oddish.db` (requires `oddish[server]`)
- Queue key backfill: `python -m oddish.backfill_queue_keys`

### Soft Delete

Every model that mixes in `TimestampedMixin` has a `deleted_at` column,
but only the classes registered through
`oddish.db.soft_delete.register_soft_delete_models` participate in the
session-level auto-filter:

| Package | Soft-deletable models |
|---------|------------------------|
| `oddish.db.models` | `ExperimentModel`, `TaskModel`, `TrialModel` |
| `backend.models` | `OrganizationModel`, `UserModel`, `APIKeyModel` |

Behavior:

- ORM `SELECT` / `UPDATE` / `DELETE` issued through a session pick up
  `WHERE deleted_at IS NULL` automatically, including eager-loaded
  relationships (`selectinload`, `joinedload`) and aliased subqueries.
- The DELETE endpoints (`delete_task_core`, `delete_experiment_core`,
  `delete_trial_core`) tombstone rows via `UPDATE ... SET deleted_at = NOW()`
  and cancel any matching `worker_jobs` rows. They return an empty
  `s3_prefixes` list so caller best-effort S3 cleanup is a no-op --
  S3 data is preserved for restore.
- The `task_experiments` join table also carries `deleted_at` so experiment
  membership is preserved for audit/restore. Because it is a SQLAlchemy
  `Table`, not a registered model, live membership queries and relationship
  joins must explicitly include `task_experiments.deleted_at IS NULL`.
- Raw `text()` SQL doesn't run through the ORM listener; the dispatcher
  claim path (`worker_job_single_job.py`), cleanup sweep, and admin
  diagnostics each add `deleted_at IS NULL` inline.
- The `(org_id, name)` uniqueness on `tasks` is a **partial** unique
  index (`WHERE deleted_at IS NULL`) so a deleted task's name slot is
  reusable.
- To read or rewrite tombstoned rows (admin tooling, future restore
  flows) opt out per statement:
  `session.execute(stmt.execution_options(include_deleted=True))`.

### Worker Runtime (`oddish.workers.queue`)

| File | Purpose |
|------|---------|
| `worker_job_dispatcher.py` | `discover_active_worker_job_queue_keys`, `get_worker_job_org_queue_counts`, `build_spawn_plan` (org-first fair-share, with within-org round-robin across queue_keys) |
| `worker_job_single_job.py` | `_CLAIM_WORKER_JOB_SQL`, `run_single_worker_job`, `heartbeat_worker_job` |
| `trial_handler.py` / `analysis_handler.py` / `verdict_handler.py` | Per-kind execution bodies |
| `cleanup.py` | Zombie reaper, stale-heartbeat sweep, stage safety nets, orphaned-slot release |
| `slots.py` | `queue_slots` lease acquire/release |
| `queue_manager.py` | Per-queue-key concurrency bookkeeping |
| `worker.py` | Standalone poll loop (`python -m oddish.workers.queue.worker`) |

Handler registration lives in `oddish.workers.jobs` (`registry.py`,
`handlers.py`). Both the standalone worker and the backend call
`ensure_builtin_handlers_registered()` at startup.

### Local Development

You need a running Postgres instance. Start one however you prefer (e.g.
`docker run -d --name oddish-db -e POSTGRES_USER=oddish -e POSTGRES_PASSWORD=oddish -e POSTGRES_DB=oddish -p 5432:5432 postgres:16-alpine`),
then:

```bash
cd oddish
cp env.example .env
uv sync --extra server
uv run python -m oddish.db setup
uv run python -m oddish.server
```

That gives you:

- the API on `http://localhost:8000`
- background workers started by the API process

Point the CLI at your local server:

```bash
export ODDISH_API_URL="http://localhost:8000"
```

For the hosted Oddish API instead, keep the default API URL and set:

```bash
export ODDISH_API_KEY="ok_..."
```

### Standalone Workers

`python -m oddish.server` auto-starts workers by default. If you want separate
worker processes for scaling or debugging:

```bash
uv run python -m oddish.workers.queue.worker
```

### Database Commands

```bash
uv run python -m oddish.db init    # run Alembic migrations
uv run python -m oddish.db setup   # alias for init
uv run python -m oddish.db reset   # drop and recreate all tables
uv run python -m oddish.db purge   # delete data, preserve migration state
```

### API Server Flags

```bash
uv run python -m oddish.server --host 0.0.0.0 --port 9000
uv run python -m oddish.server --n-concurrent '{"openai/gpt-5.2": 8, "anthropic/claude-sonnet-4-5": 8}'
```

### HTTP Endpoints (core)

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/tasks/upload/init` | Prepare a task upload and return a presigned PUT URL when S3 is enabled |
| POST | `/tasks/upload/complete` | Finalize a direct-to-S3 task upload after the client PUT succeeds |
| GET | `/health` | API and DB health check |
| POST | `/tasks/sweep` | Expand a sweep into a task plus trials |
| GET | `/tasks` | List tasks |
| GET | `/tasks/{task_id}` | Fetch a task with trials |
| POST | `/tasks/cancel` | Cancel many tasks in one request |
| DELETE | `/tasks/{task_id}` | Soft-delete a task and its trials (sets `deleted_at`; S3 artifacts are preserved for restore) |
| POST | `/tasks/{task_id}/analysis/retry` | Queue or rerun task-wide analysis jobs |
| POST | `/tasks/{task_id}/verdict/retry` | Queue or rerun a task verdict |
| DELETE | `/experiments/{experiment_id}` | Soft-delete an experiment, its trials, and any now-orphaned tasks |
| PATCH | `/experiments/{experiment_id}` | Update experiment metadata |
| GET | `/tasks/{task_id}/trials/{index}` | Fetch a trial by 0-based index |
| POST | `/trials/{trial_id}/analysis/retry` | Queue or rerun analysis for one trial |
| DELETE | `/trials/{trial_id}` | Soft-delete a single trial, cancel its in-flight jobs, and invalidate the parent task's cached verdict |
| GET | `/trials/{trial_id}/logs` | Fetch logs for a trial |
| GET | `/trials/{trial_id}/result` | Fetch `result.json` for a trial |

### Configuration (oddish)

Settings are loaded from `oddish/.env`. Most package settings use the `ODDISH_` prefix.

```bash
# Required for local development
ODDISH_DATABASE_URL=postgresql+asyncpg://oddish:oddish@localhost:5432/oddish

# Hosted API auth
ODDISH_API_URL=https://abundant-ai--api.modal.run
ODDISH_API_KEY=ok_...

# Queue concurrency
ODDISH_DEFAULT_MODEL_CONCURRENCY=8
ODDISH_NOP_ORACLE_CONCURRENCY=32
ODDISH_MODEL_CONCURRENCY_OVERRIDES='{"openai/gpt-5.2": 8}'

# S3-compatible storage
ODDISH_S3_BUCKET=data
ODDISH_S3_REGION=us-east-1
ODDISH_S3_ACCESS_KEY=...
ODDISH_S3_SECRET_KEY=...
ODDISH_S3_ENDPOINT_URL=https://...

# Provider credentials
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GEMINI_API_KEY=...

# AWS Bedrock — the default route for Claude models on the Modal
# deployment (the image sets CLAUDE_CODE_USE_BEDROCK=1). Provide the
# bearer token here; ANTHROPIC_API_KEY above is used as the fallback
# route for `anthropic/...` model ids.
AWS_BEARER_TOKEN_BEDROCK=...

# Optional sandbox credentials
DAYTONA_API_KEY=...
MODAL_TOKEN_ID=...
MODAL_TOKEN_SECRET=...
```

### Claude model routing: AWS Bedrock only

**oddish runs Claude exclusively through AWS Bedrock.** The Modal image
bakes in `CLAUDE_CODE_USE_BEDROCK=1`, and Claude Code authenticates with
`AWS_BEARER_TOKEN_BEDROCK` from the runtime Modal secret. There is no
Anthropic API route — `ANTHROPIC_API_KEY` is not used for trials.

Claude Code invokes Bedrock via the legacy `InvokeModel` API, which only
accepts **cross-region inference profile ids** (a `global.`/`us.`/... prefix)
or ARNs. A bare `anthropic.claude-...` foundation-model id is *not* invokable
on-demand — Bedrock rejects it with "Retry your request with the ID or ARN
of an inference profile". So `harbor_runner` normalizes whatever model id a
trial supplies via `oddish.config.to_bedrock_model_id` before handing it to
Harbor. That normalizer accepts any of these forms:

- already invokable (`global.`/`us.`/... inference profiles,
  `arn:aws:bedrock:...`) — passed through, minus any redundant `bedrock/`
  prefix.
- Anthropic-style (`anthropic/claude-opus-4-7`, bare `claude-opus-4-7`) **or**
  a bare Bedrock foundation-model id (`anthropic.claude-opus-4-7`) — mapped to
  an invokable inference profile id via the explicit
  `_ANTHROPIC_TO_BEDROCK_MODEL_IDS` table in `oddish/config.py`. **A Claude
  model with no table entry raises a `ValueError`** — add an entry there
  before running that model.
- non-Claude models (`openai/...`, `gemini-...`) — passed through untouched.

The table maps to `global.` inference profiles (recommended by AWS, no
pricing premium) except Opus 4.1 / Opus 4, which have no global profile and
use `us.`. If you need regional data residency, change the prefixes there.

You can pass any of those forms anywhere a model is accepted: `oddish run
-m ...`, sweep configs (`model_name:`), or `--n-concurrent` overrides.
Concurrency limits are keyed off the full `provider/model` string.

> Trial *analysis* (the `claude -p` classifier) uses its own `ANALYSIS_MODEL`
> (`oddish/config.py`), which is already a `global.` inference profile id. It
> is not wired through `to_bedrock_model_id`.

Storage defaults:

- S3-compatible storage is **required**. Clients PUT task bundles directly
  to a presigned URL returned by `/tasks/upload/init` and then call
  `/tasks/upload/complete`.
- uploaded task bundles: `tasks/<task_id>/.oddish-task.tar.gz`
- Harbor job outputs: `/tmp/harbor-jobs`
- Modal workers also check `/mnt/oddish-tasks` before falling back to the S3 download path

### Using as a Library

```python
from oddish.config import settings
from oddish.db import (
    TaskModel,
    TrialModel,
    WorkerJobModel,
    WorkerJobKind,
    WorkerJobStatus,
    get_session,
    init_db,
)
from oddish.queue import create_task
from oddish.schemas import HarborConfig, TaskSubmission, TaskSweepSubmission, TrialSpec
from oddish.workers import run_polling_worker
```

---

## `backend/` — Hosted Cloud Layer

### Authentication Model

The backend accepts auth from `Authorization`, `X-Clerk-Authorization`, or `X-Authorization`.

- **API keys** (`ok_...`): stored hashed (SHA-256) in `api_keys`; scopes are `full`, `tasks`, `read`
- **Clerk JWTs**: validated against Clerk JWKS; org context extracted from token claims

Auth flow: read token → if `ok_` prefix validate API key → otherwise validate Clerk JWT and resolve org/user → return `AuthContext`.

If a Clerk JWT arrives without `org_id`, the backend tries to resolve a single existing org membership, or provisions a personal org.

### Worker Architecture

Dispatcher + single-job pattern, backed by the unified `worker_jobs` table:

1. `poll_queue()` runs on a 120s Modal schedule. It calls
   `cleanup_orphaned_queue_state` (zombie-txn reap + stale-heartbeat sweep +
   stage safety nets + orphaned slot release), discovers active queue keys
   via `discover_active_worker_job_queue_keys`, and launches up to
   `MAX_WORKERS_PER_POLL` single-job containers.
2. `process_single_job(queue_key)` acquires a `queue_slots` lease for the
   queue key and calls `run_single_worker_job`, which atomically claims one
   row from `worker_jobs`, dispatches to the registered handler
   (`TRIAL` / `ANALYSIS` / `VERDICT`), writes heartbeats on both
   `worker_jobs.heartbeat_at` and the mirrored domain column, records the
   outcome (`SUCCESS` / `RETRYING` / `FAILED` / `CANCELLED`), runs the
   post-success hook (GitHub notification) when applicable, and exits.

Handler registration happens at container load via
`ensure_builtin_handlers_registered()`. Post-success hooks
(`notify_github_trial`, `notify_github_analysis`, `notify_github_verdict`)
are wired through `_POST_SUCCESS_HOOKS` in `worker/functions.py`.

### Local Development

```bash
# Modal local serve
cd backend
uv sync
uv run modal serve deploy.py
```

### Configuration (backend)

```bash
cp backend/.env.example backend/.env
```

Minimum required:

```bash
ODDISH_DATABASE_URL=...
CLERK_DOMAIN=...
```

Required for Clerk-backed org management:

```bash
CLERK_SECRET_KEY=...
```

Required for Clerk webhook ingestion:

```bash
CLERK_WEBHOOK_SECRET=...
```

Common optional settings:

```bash
CORS_ALLOWED_ORIGINS=...
CLERK_ISSUER=...
CLERK_JWT_AUDIENCE=...
ODDISH_S3_*=...
OPENAI_API_KEY=... ANTHROPIC_API_KEY=... GEMINI_API_KEY=...
GITHUB_TOKEN=...
ODDISH_DASHBOARD_URL=...
```

Hosted API containers keep a conservative warm SQLAlchemy pool by default so
Modal bursts do not overrun shared Postgres poolers. The engine still disables
prepared statement caching so it remains compatible with transaction-mode
poolers such as Supavisor / PgBouncer.

Modal runtime knobs (read by `modal_app.py`):

```bash
ODDISH_ENABLE_MODAL_WORKERS=...
ODDISH_MODAL_API_MIN_CONTAINERS=...
ODDISH_MODAL_API_MAX_CONTAINERS=...
ODDISH_MODAL_POLL_INTERVAL_SECONDS=...
ODDISH_MODAL_WORKER_TIMEOUT_SECONDS=...
ODDISH_MODAL_WORKER_NONPREEMPTIBLE=...
ODDISH_MODAL_MAX_WORKERS_PER_POLL=64
ODDISH_DEFAULT_MODEL_CONCURRENCY=...
ODDISH_MODAL_NOP_ORACLE_CONCURRENCY=...
MODAL_APP_NAME=...
MODAL_SECRET_ENVIRONMENT=...
```

### Database Migrations

Two migration stacks are required:

```bash
# Core tables (run in oddish/)
uv run alembic upgrade head

# Cloud tables/extensions (run in backend/)
uv run alembic upgrade head
```

### Key Files

| Path | Purpose |
|------|---------|
| `deploy.py` | Modal app entrypoint |
| `modal_app.py` | Modal image, volumes, shared runtime |
| `endpoints.py` | Modal ASGI app function |
| `serve.py` | Railway/uvicorn entrypoint |
| `cloud_policy.py` | Hosted-only environment policy |
| `api/app.py` | FastAPI app factory |
| `api/routers/tasks.py` | Task upload, browse, sweep, sharing, retries |
| `api/routers/trials.py` | Trial logs, result, trajectory, retries |
| `api/routers/dashboard.py` | Cached aggregate dashboard endpoint |
| `api/routers/admin.py` | Auth wrapper over `oddish.core.admin` (slots, queue status, orphaned state, worker_jobs) |
| `auth/verification.py` | API key + Clerk JWT verification |
| `worker/functions.py` | Modal dispatcher (`poll_queue`) and kind-agnostic single-job runner |
| `worker/runtime.py` | Modal runtime patching and storage setup |
| `worker/github.py` | GitHub notification hooks used as post-success actions |

---

## `frontend/` — Next.js Dashboard

### App Surface

- `/` — public landing page; signed-in users are redirected to `/dashboard`
- `/dashboard` — main dashboard and experiment entrypoint
- `/tasks` — authenticated task browser with search, pagination, version summaries
- `/experiments/[experiment]` — experiment detail, task and trial inspection, logs, results, files, version history, share controls, cancel
- `/settings` — organization and API key management
- `/admin` — two tabs:
  - **Worker Jobs** (default): unified `worker_jobs` kind×status matrix,
    stale-RUNNING samples, recent failures/cancels, duration percentiles,
    plus `OrphanedStateCard`
  - **Concurrency**: `queue_slots` leases and per-queue-key health
- `/share/[token]` — read-only public experiment view
- `/datasets` and `/datasets/[token]` — public dataset listing and detail

### Request Flow

```text
Browser UI
  -> Next.js pages and client components
  -> Next.js route handlers in src/app/api/*
  -> backend API (FastAPI or Modal)
```

The backend URL is configured via `NEXT_PUBLIC_API_URL` in `src/lib/backend-config.ts`.

Dashboard and experiment detail pages seed client-side SWR from their server
render and suppress the immediate mount revalidation when the fallback payload
already matches the default view. The route handlers for
`src/app/api/dashboard/route.ts` and
`src/app/api/experiments/[experiment]/tasks/route.ts` emit `Server-Timing`
headers and forward upstream timing data for latency debugging. The backend
dashboard aggregation now stays on a single DB session per request to avoid
doubling connection pressure during bursts. Experiment-scoped task responses
include `experiment_created_at`, sourced from `ExperimentModel.created_at`, so
the experiment header does not infer creation time from one of its tasks.

### Local Development

```bash
cd frontend
pnpm install
cp env.example .env.local
# set NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY, CLERK_SECRET_KEY, NEXT_PUBLIC_API_URL
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000).

### Scripts

```bash
pnpm dev           # Next.js dev server
pnpm build         # Production build
pnpm start         # Run production server
pnpm lint          # ESLint
pnpm format        # Prettier formatting
pnpm format:check  # Check Prettier formatting
```

### Configuration (frontend)

```bash
# Required
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_...
CLERK_SECRET_KEY=sk_test_...
NEXT_PUBLIC_API_URL=http://localhost:8000

# Recommended for org-aware backend auth
CLERK_JWT_TEMPLATE=oddish

# Optional
NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL=/dashboard
NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL=/dashboard
NEXT_PUBLIC_APP_URL=https://local.oddish.app
```

### Auth

Public routes: `/`, `/sign-in/*`, `/sign-up/*`, `/share/*`, `/datasets/*`, `/api/public/*`. Everything else is protected by Clerk middleware.

Clerk JWT template claims:

```json
{
  "email": "{{user.primary_email_address}}",
  "org_id": "{{org.id}}",
  "org_role": "{{org.role}}"
}
```

### Deployment

```bash
docker build -t oddish-frontend frontend/
docker run --rm -p 3000:3000 --env-file frontend/.env.local oddish-frontend
```

### UI Stack

- Next.js 15 App Router, React 19
- Tailwind CSS, shadcn/ui, Radix primitives
- SWR for client-side data fetching
- Clerk for auth
- Recharts, Shiki, @tanstack/react-virtual

---

## Full-Stack Local Development

### Frontend + (ephemeral) Modal backend

Two workflows, both documented in detail in [`SELF_HOSTING.md`](SELF_HOSTING.md):

1. **Plain HTTP localhost** (Clerk *test* keys):

   ```bash
   # Terminal 1
   cd backend && uv run modal serve deploy.py

   # Terminal 2 — set NEXT_PUBLIC_API_URL to the modal serve URL
   cd frontend && pnpm dev
   ```

2. **Local HTTPS on `local.oddish.app`** (Clerk *production* keys). The helper
   script listens on port 443 and re-execs itself under `sudo`, so the
   root-owned `.next/` from a prior run has to be cleared:

   ```bash
   # Terminal 1
   cd backend && uv run modal serve deploy.py

   # Terminal 2 — ensures Clerk prod keys accept the oddish.app origin
   cd frontend && sudo rm -rf .next && ./run-prod-clerk-local.sh
   ```

   `NEXT_PUBLIC_API_URL` in `.env.local` should point at the `-dev` Modal URL
   from Terminal 1, and `NEXT_PUBLIC_APP_URL` should be
   `https://local.oddish.app`.

Use `modal deploy deploy.py` for production deployments (see `SELF_HOSTING.md`).


---

## Troubleshooting

### API does not start

```bash
uv run python -m oddish.db setup
curl http://localhost:8000/health
```

### Pulling from a remote API fails

- Verify `ODDISH_API_URL` and `ODDISH_API_KEY`.
- Try `oddish status` first to confirm auth and connectivity.

### Frontend "Failed to fetch" or disconnected backend

```bash
curl ${NEXT_PUBLIC_API_URL:-http://localhost:8000}/openapi.json
```

### Clerk auth issues

- Verify Clerk keys in `frontend/.env.local`.
- If org-scoped backend access fails, confirm `CLERK_JWT_TEMPLATE` is set and includes `org_id`.
- If using production Clerk keys locally, use `frontend/run-prod-clerk-local.sh`.
