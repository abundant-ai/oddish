# Oddish Package Guide

This file is the technical guide for the `oddish/` package. End-user CLI docs
live in `README.md`.

`oddish` is the core behind Oddish: a Python CLI, FastAPI server,
Postgres-backed queueing layer, and worker runtime for Harbor tasks.

Python `3.12+` is required.

## Maintenance Notes

- Keep `README.md` focused on end-user CLI workflows.
- Put implementation details, architecture notes, and local development guidance here.
- If you change the CLI surface in `src/oddish/cli/`, update `README.md`.
- If you change API contracts, queue behavior, or storage layout, update this file.

## What Lives Here

The `oddish` package includes:

- the `oddish` CLI (`run`, `status`, `cancel`, `pull`, `delete`)
- the FastAPI app (`python -m oddish.api`)
- database models and Alembic migrations
- Postgres-native trial, analysis, and verdict workers (fair scheduling via
  `FOR UPDATE SKIP LOCKED` directly on the `trials` table)
- shared queue coordination primitives such as queue-slot leasing, single-job
  processing, and stale-heartbeat cleanup
- local task storage plus optional S3-compatible artifact storage

## Architecture

```text
oddish CLI / API client
        |
        v
FastAPI server (`python -m oddish.api`)
        |
        v
Postgres (trials table = the queue)
        |
        v
Workers (`oddish.api` auto-start or standalone worker process)
        |
        v
Harbor task execution + logs/results/artifacts
```

High-level flow:

1. Upload a task bundle.
2. Submit a sweep of agent/model trials for that task.
3. Workers execute trials and optionally run analysis and verdict stages.
4. Use the CLI or API to watch progress and pull logs and artifacts back locally.

## Hosted Boundary

`oddish` owns the execution core plus the shared queue/runtime primitives
reused by Oddish Cloud:

- core models and migrations, including `queue_slots`
- shared queue-slot leasing and one-job worker execution helpers
- stale-heartbeat cleanup and pipeline stage reconciliation

`backend/` keeps the hosted-only layer on top of that core:

- Clerk/API key auth and org-scoped APIs
- Modal worker spawning and runtime patching
- cloud environment policy and GitHub notification hooks
- public sharing routes and other product-specific endpoints

## Entry Points

- CLI: `oddish` -> `oddish.cli:app`
- API server: `python -m oddish.api`
- standalone worker: `python -m oddish.workers.queue.worker`
- DB helper CLI: `python -m oddish.db`
- queue key backfill script: `python -m oddish.backfill_queue_keys`

## Local Development

### Quick start

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

### Standalone workers

`python -m oddish.api` auto-starts workers by default. If you want separate
worker processes for scaling or debugging, run:

```bash
uv run python -m oddish.workers.queue.worker
```

## Task Expectations

`oddish run` accepts either a single Harbor task directory or a dataset
directory. A task directory is expected to contain:

- `task.toml`
- `instruction.md`
- `environment/`
- `tests/`

## Database Commands

Use the DB helper CLI through `python -m oddish.db`:

```bash
uv run python -m oddish.db init
uv run python -m oddish.db setup
uv run python -m oddish.db reset
uv run python -m oddish.db purge
```

What they do:

- `init`: run Alembic migrations
- `setup`: run Alembic migrations (alias for `init`)
- `reset`: drop and recreate all tables
- `purge`: delete data from public-schema tables while preserving migration state

## API Server

Run the API directly with:

```bash
uv run python -m oddish.api
```

Useful flags:

```bash
# Override host and port
uv run python -m oddish.api --host 0.0.0.0 --port 9000

# Override queue concurrency at startup
uv run python -m oddish.api --n-concurrent '{"openai/gpt-5.2": 8, "anthropic/claude-sonnet-4-5": 8}'
```

### HTTP endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/health` | API and DB health check |
| POST | `/tasks/upload` | Upload a task tarball |
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

Remote APIs require `ODDISH_API_KEY`.

## Configuration

Settings are loaded from `.env`. Most package settings use the `ODDISH_` prefix,
while provider credentials use their usual environment variable names.

### Required for local development

```bash
ODDISH_DATABASE_URL=postgresql+asyncpg://oddish:oddish@localhost:5432/oddish
```

### Common optional settings

```bash
# Hosted API auth
ODDISH_API_URL=https://abundant-ai--api.modal.run
ODDISH_API_KEY=ok_...

# CLI URL overrides
ODDISH_DASHBOARD_URL=https://www.oddish.app
ODDISH_DEFAULT_API_URL=https://abundant-ai--api.modal.run
ODDISH_DEFAULT_DASHBOARD_URL=https://www.oddish.app

# Queue concurrency
ODDISH_DEFAULT_MODEL_CONCURRENCY=8
ODDISH_MODEL_CONCURRENCY_OVERRIDES='{"openai/gpt-5.2": 8}'

# S3-compatible storage
ODDISH_S3_ENABLED=true
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

- uploaded task bundles: `/tmp/oddish-tasks` locally, or a single
  `tasks/<task_id>/.oddish-task.tar.gz` archive in S3 when remote storage is enabled
- Harbor job outputs: `/tmp/harbor-jobs`
- Modal workers also check the fixed mount path `/mnt/oddish-tasks` for
  extracted `tasks/` objects before falling back to the normal S3 download path

## Repository Layout

```text
oddish/
├── src/oddish/
│   ├── api/                  # FastAPI app and request handlers
│   ├── cli/                  # oddish run/status/cancel/pull/delete
│   ├── db/                   # models, connection helpers, storage
│   ├── workers/              # Harbor execution plus shared queue runtime
│   ├── backfill_queue_keys.py
│   ├── config.py
│   ├── experiment.py
│   ├── queue.py
│   └── schemas.py
├── alembic/                  # DB migrations
├── env.example
├── pyproject.toml
├── README.md
└── AGENTS.md
```

## Using as a Library

Some commonly imported surfaces:

```python
from oddish.config import settings
from oddish.db import TaskModel, TrialModel, get_session, init_db
from oddish.queue import create_task
from oddish.schemas import HarborConfig, TaskSubmission, TaskSweepSubmission, TrialSpec
from oddish.workers import run_polling_worker
```

## Troubleshooting

### API does not start

```bash
uv run python -m oddish.db setup
curl http://localhost:8000/health
```

### Tasks stay queued

- make sure the API is healthy
- remember `oddish.api` auto-starts workers, or run `python -m oddish.workers.queue.worker`
- check queue concurrency settings if a model-specific queue is saturated
- stale-heartbeat cleanup runs periodically and will fail trials whose workers
  crashed; stuck analyses and verdicts are automatically re-queued

### Pulling from a remote API fails

- verify `ODDISH_API_URL`
- verify `ODDISH_API_KEY`
- try `oddish status` first to confirm auth and connectivity
