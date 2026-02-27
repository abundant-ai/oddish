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
| `api/routers/tasks.py` | Task CRUD, uploads, sweep creation, file access |
| `api/routers/trials.py` | Trial listing, retry, logs, result, trajectory |
| `api/routers/dashboard.py` | Dashboard aggregate endpoint |
| `api/routers/public.py` | Public token-based read routes (no auth) |
| `api/routers/admin.py` | Queue-slot and pgqueuer inspection endpoints |
| `api/routers/clerk_webhooks.py` | Clerk org/user synchronization |
| `api/routers/github_webhooks.py` | GitHub status/refresh integrations |
| `auth/verification.py` | API key + Clerk JWT verification and auth caches |
| `auth/provisioning.py` | Clerk user/org provisioning helpers |
| `models.py` | Cloud auth models (orgs/users/api keys) |
| `worker.py` | Dispatcher and single-job worker orchestration |
| `alembic/` | Cloud migrations (auth + cloud table extensions) |

## Configuration

```bash
cp .env.example .env
```

Use `backend/.env.example` as the canonical list of backend environment variables.
For local cloud-app development, the minimum required values are:

- `DATABASE_URL`
- `CLERK_DOMAIN`
- `CLERK_SECRET_KEY`

Recommended for dashboard auth + Clerk sync:

- `CLERK_WEBHOOK_SECRET`

Everything else is optional and documented inline in `.env.example` (S3, provider
keys, sandbox keys, GitHub integration, CORS, concurrency tuning).

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
| GET | `/dashboard` | Health + queues + recent task summary |
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
| GET | `/public/experiments/{token}` | Public experiment metadata |
| GET | `/public/experiments` | List all public experiments |
| GET | `/public/experiments/{token}/tasks` | Public tasks for experiment |
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
alembic upgrade head

# Cloud (run in backend/)
alembic upgrade head
```

## Development Workflows

```bash
# Backend only (Modal local serve)
cd backend
modal serve deploy.py
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
modal serve deploy.py

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
