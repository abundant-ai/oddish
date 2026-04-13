# Oddish Repository Guide

This file is the technical guide for the entire monorepo. End-user CLI docs live in `oddish/README.md`.

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
oddish/                         # Core Python package (CLI, API, workers, DB)
├── src/oddish/
│   ├── api/                    # FastAPI app and request handlers
│   ├── cli/                    # oddish run/status/cancel/pull/delete
│   ├── db/                     # models, connection helpers, storage
│   ├── workers/                # Harbor execution plus shared queue runtime
│   ├── backfill_queue_keys.py
│   ├── config.py
│   ├── experiment.py
│   ├── queue.py
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
FastAPI server — oddish core (python -m oddish.api)
           or backend cloud layer (Modal / Railway)
        |
        v
Postgres (trials table = the queue)
        |
        v
Workers (auto-started by API, or standalone via python -m oddish.workers.queue.worker)
        |
        v
Harbor task execution → logs/results/artifacts (S3 / Modal volumes)
```

High-level flow:

1. Upload a task bundle.
2. Submit a sweep of agent/model trials for that task.
3. Workers execute trials and optionally run analysis and verdict stages.
4. Use the CLI or dashboard to watch progress and pull logs and artifacts back locally.

## Package Boundaries

`oddish` owns the execution core and shared queue/runtime primitives:

- core models and migrations, including `queue_slots`
- shared queue-slot leasing and one-job worker execution helpers
- stale-heartbeat cleanup and pipeline stage reconciliation

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

### Entry Points

- CLI: `oddish` → `oddish.cli:app`
- API server: `python -m oddish.api`
- Standalone worker: `python -m oddish.workers.queue.worker`
- DB helper CLI: `python -m oddish.db`
- Queue key backfill: `python -m oddish.backfill_queue_keys`

### Local Development

You need a running Postgres instance. Start one however you prefer (e.g.
`docker run -d --name oddish-db -e POSTGRES_USER=oddish -e POSTGRES_PASSWORD=oddish -e POSTGRES_DB=oddish -p 5432:5432 postgres:16-alpine`),
then:

```bash
cd oddish
cp env.example .env
uv sync
uv run python -m oddish.db setup
uv run python -m oddish.api
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

`python -m oddish.api` auto-starts workers by default. If you want separate
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
uv run python -m oddish.api --host 0.0.0.0 --port 9000
uv run python -m oddish.api --n-concurrent '{"openai/gpt-5.2": 8, "anthropic/claude-sonnet-4-5": 8}'
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
| DELETE | `/tasks/{task_id}` | Delete a task, its trials, and associated S3 artifacts when enabled |
| POST | `/tasks/{task_id}/analysis/retry` | Queue or rerun task-wide analysis jobs |
| POST | `/tasks/{task_id}/verdict/retry` | Queue or rerun a task verdict |
| DELETE | `/experiments/{experiment_id}` | Delete an experiment, its tasks/trials, and associated S3 artifacts when enabled |
| PATCH | `/experiments/{experiment_id}` | Update experiment metadata |
| GET | `/tasks/{task_id}/trials/{index}` | Fetch a trial by 0-based index |
| POST | `/trials/{trial_id}/analysis/retry` | Queue or rerun analysis for one trial |
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

# Optional sandbox credentials
DAYTONA_API_KEY=...
MODAL_TOKEN_ID=...
MODAL_TOKEN_SECRET=...
```

Storage defaults:

- uploaded task bundles: `tasks/<task_id>/.oddish-task.tar.gz` in S3-compatible storage
- Harbor job outputs: `/tmp/harbor-jobs`
- Modal workers also check `/mnt/oddish-tasks` before falling back to the S3 download path

### Using as a Library

```python
from oddish.config import settings
from oddish.db import TaskModel, TrialModel, get_session, init_db
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

Dispatcher + single-job pattern:

1. `poll_queue()` runs on a 120s Modal schedule, clears stale queue state, and launches up to `MAX_WORKERS_PER_POLL` single-job workers based on queue depth and concurrency limits.
2. `process_single_job(queue_key)` acquires a queue-slot lease, processes one `trial`/`analysis`/`verdict`, emits updates, and exits.

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

Modal runtime knobs (read by `modal_app.py`):

```bash
ODDISH_ENABLE_MODAL_WORKERS=...
ODDISH_MODAL_API_MIN_CONTAINERS=...
ODDISH_MODAL_API_MAX_CONTAINERS=...
ODDISH_MODAL_WORKER_TIMEOUT_SECONDS=...
ODDISH_MODAL_MAX_WORKERS_PER_POLL=...
ODDISH_MODEL_CONCURRENCY_DEFAULT=...
MODAL_APP_NAME=...
MODAL_VOLUME_NAME=...
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
| `auth/verification.py` | API key + Clerk JWT verification |
| `worker/functions.py` | Modal dispatcher and worker spawning |
| `worker/runtime.py` | Modal runtime patching and storage setup |

---

## `frontend/` — Next.js Dashboard

### App Surface

- `/` — public landing page; signed-in users are redirected to `/dashboard`
- `/dashboard` — main dashboard and experiment entrypoint
- `/tasks` — authenticated task browser with search, pagination, version summaries
- `/experiments/[experiment]` — experiment detail, task and trial inspection, logs, results, files, version history, share controls, cancel
- `/settings` — organization and API key management
- `/admin` — worker queues, queue slots, and orphaned state monitoring
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

### Flow A: Frontend + local core API

```bash
# Terminal 1 — start Postgres, then the core API
docker run -d --name oddish-db -e POSTGRES_USER=oddish -e POSTGRES_PASSWORD=oddish -e POSTGRES_DB=oddish -p 5432:5432 postgres:16-alpine
cd oddish && uv run python -m oddish.db setup && uv run python -m oddish.api

# Terminal 2
cd frontend && pnpm dev:local
```

### Flow B: Frontend + Modal backend

```bash
# Terminal 1
cd backend && uv run modal serve deploy.py

# Terminal 2
cd frontend && pnpm dev:modal
```

---

## Troubleshooting

### API does not start

```bash
uv run python -m oddish.db setup
curl http://localhost:8000/health
```

### Tasks stay queued

- Make sure the API is healthy.
- `oddish.api` auto-starts workers; or run `python -m oddish.workers.queue.worker` separately.
- Check queue concurrency settings if a model-specific queue is saturated.
- Stale-heartbeat cleanup runs periodically and will fail trials whose workers crashed; stuck analyses and verdicts are automatically re-queued.

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
