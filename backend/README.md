# Oddish Backend

Serverless API and worker orchestration for Oddish Cloud, deployed on [Modal](https://modal.com), with multi-tenant authentication and authorization.

## Overview

The backend wraps the OSS `oddish` core with:
- Multi-tenant API (`org_id`-scoped queries)
- Dual auth (API keys + Clerk JWTs)
- Modal-hosted API/workers/sandboxes
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
  │  - Writes task/trial state in Postgres
  ▼
Postgres (oddish + cloud tables)
  │
  ▼
Worker dispatcher (`worker.py`, every 30s)
  │  - Spawns single-job workers by queue key
  ▼
Single-job workers (process one job, then exit)
  │
  ▼
Modal sandboxes (Harbor execution, logs/artifacts to S3 or volume)
```

### Worker architecture

Dispatcher + single-job pattern:
1. `poll_queue()` runs every 30s, checks queue depth per key, launches up to `MAX_WORKERS_PER_POLL`.
2. `process_single_job(queue_key)` acquires queue-slot lease, processes one `trial`/`analysis`/`verdict`, emits updates, exits.

This keeps concurrency deterministic and avoids long-lived worker drift.

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
| `modal_app.py` | Modal image, volumes, and shared runtime setup |
| `endpoints.py` | ASGI entrypoint and oddish settings patching for Modal |
| `api/app.py` | FastAPI app factory + startup/lifespan wiring |
| `api/schemas.py` | Pydantic models for org/auth/share responses |
| `api/routers/tasks.py` | Task CRUD, uploads, sweep creation, sharing, and file access |
| `api/routers/trials.py` | Trial listing, retry, logs, result, trajectory, and debug file inspection |
| `api/routers/dashboard.py` | Cached aggregate dashboard endpoint (health, queues, usage, tasks, experiments) |
| `api/routers/orgs.py` | Current org lookup and Clerk-backed user management |
| `api/routers/api_keys.py` | Org API key listing, creation, and revocation |
| `api/routers/public.py` | Public token-based read routes (no auth) |
| `api/routers/admin.py` | Queue-slot and pgqueuer inspection endpoints |
| `api/routers/clerk_webhooks.py` | Clerk org/user synchronization |
| `api/routers/github_webhooks.py` | GitHub status/refresh integrations |
| `auth/verification.py` | API key + Clerk JWT verification and auth caches |
| `auth/provisioning.py` | Clerk user/org provisioning helpers |
| `models.py` | Cloud auth models (orgs/users/api keys) |
| `worker.py` | Dispatcher and single-job worker orchestration |
| `integrations/github/` | PR comment formatting, metadata parsing, and notification client |
| `alembic/` | Cloud migrations (auth + cloud table extensions) |

## Configuration

```bash
cp .env.example .env
```

Use `backend/.env.example` as the starting point for local backend config.
For the API and worker runtime, the minimum required values are:

- `DATABASE_URL`
- `CLERK_DOMAIN`

Required for Clerk-backed org invites, membership lookups, and GitHub username enrichment:

- `CLERK_SECRET_KEY`

Required if you want Clerk webhook ingestion enabled:

- `CLERK_WEBHOOK_SECRET`

Common optional settings:

- `CORS_ALLOWED_ORIGINS`
- `CLERK_ISSUER`
- `CLERK_JWT_AUDIENCE`
- `ODDISH_S3_*`
- provider keys such as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DAYTONA_API_KEY`
- GitHub notifier settings such as `GITHUB_TOKEN` and `ODDISH_DASHBOARD_URL`

Modal runtime knobs are read directly by `modal_app.py`, including:

- `ODDISH_ENABLE_MODAL_WORKERS`
- `ODDISH_MODAL_API_MIN_CONTAINERS`
- `ODDISH_MODAL_API_MAX_CONTAINERS`
- `MODAL_APP_NAME`
- `MODAL_VOLUME_NAME`

### oddish runtime patching

`endpoints.py` and `worker.py` patch oddish settings for Modal execution:

- disable auto-started local workers
- point storage paths to mounted Modal volumes
- force Harbor environment to Modal-compatible mode

## API Endpoints

All routes require auth unless marked public.

### Core and task/trial operations

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Liveness check (no auth) |
| GET | `/dashboard` | Cached aggregate response for health, queues, pipeline stats, usage, tasks, and experiments |
| POST | `/tasks/upload` | Upload task archive |
| POST | `/tasks/sweep` | Expand one task into multiple trials |
| GET | `/tasks` | List tasks (org-scoped, paginated/filtered) |
| GET | `/tasks/{task_id}` | Task details |
| DELETE | `/tasks/{task_id}` | Delete task and queued jobs |
| GET | `/tasks/{task_id}/trials` | Trials for task |
| GET | `/tasks/{task_id}/trials/{index}` | Trial by index |
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
| GET | `/public/experiments` | List all public experiments |
| GET | `/public/experiments/{public_token}/tasks` | Public tasks for experiment |
| GET | `/public/tasks/{task_id}` | Public task status |
| GET | `/public/tasks/{task_id}/trials` | Public trial list |
| GET | `/public/trials/{trial_id}/logs` | Public trial logs |
| GET | `/public/trials/{trial_id}/logs/structured` | Public structured logs |
| GET | `/public/trials/{trial_id}/trajectory` | Public trajectory |
| GET | `/public/trials/{trial_id}/files` | Public trial file listing |
| GET | `/public/trials/{trial_id}/files/{path}` | Public trial file |
| GET | `/public/trials/{trial_id}/result` | Public result |
| GET | `/public/tasks/{task_id}/files` | Public task files |
| GET | `/public/tasks/{task_id}/files/{path}` | Public file download |

### Admin and integrations

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/admin/slots` | Queue slot lease state |
| GET | `/admin/pgqueuer` | pgqueuer queue inspection |
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

For full-stack local development, use one of these flows:

```bash
# Flow A: Frontend + local core API
# Terminal 1
cd oddish
docker compose up -d db
uv run python -m oddish.api

# Terminal 2
cd frontend
pnpm dev:local
```

```bash
# Flow B: Frontend + Modal backend
# Terminal 1
cd backend
uv run modal serve deploy.py

# Terminal 2
cd frontend
pnpm dev:modal
```

### Smoke tests

```bash
# health
curl "$ODDISH_MODAL_API_URL/health" | jq

# authenticated list
curl -H "Authorization: Bearer $ODDISH_API_KEY" "$ODDISH_MODAL_API_URL/tasks" | jq

# dashboard queue overview
curl -H "Authorization: Bearer $ODDISH_API_KEY" "$ODDISH_MODAL_API_URL/dashboard" | jq '.queues'
```
