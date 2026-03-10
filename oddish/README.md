# Oddish Core Library

This README documents the `oddish/` Python package in this repository.

`oddish` is the self-hostable core behind Oddish: a Python CLI, FastAPI server,
Postgres-backed queueing layer, and worker runtime for Harbor tasks.

## What Lives Here

The `oddish` package includes:

- the `oddish` CLI (`run`, `status`, `pull`, `clean`)
- the FastAPI app (`python -m oddish.api`)
- database models and Alembic migrations
- PGQueuer-backed trial, analysis, and verdict workers
- local task storage plus optional S3-compatible artifact storage

Python `3.12+` is required.

## Architecture

```text
oddish CLI / API client
        |
        v
FastAPI server (`python -m oddish.api`)
        |
        v
Postgres + PGQueuer tables
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
3. Workers execute trials and optionally run analysis + verdict stages.
4. Use the CLI or API to watch progress and pull logs/artifacts back locally.

## Local Development

### Quick start

```bash
cd oddish
cp env.example .env
docker compose up -d db
uv sync
uv run python -m oddish.db setup
uv run python -m oddish.api
```

That gives you:

- Postgres on `localhost:5432`
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

## CLI Reference

The installed console script is:

```bash
oddish --help
```

Available commands:

- `oddish run`: upload tasks and submit trials
- `oddish status`: inspect system, task, or experiment state
- `oddish pull`: download logs and artifacts locally
- `oddish clean`: delete task/experiment data or reset local infra

### Common examples

```bash
# Run a local task against a local API
export ODDISH_API_URL="http://localhost:8000"
oddish run ./my-task -a claude-code -m anthropic/claude-sonnet-4-5

# Run a dataset sweep from the Harbor registry
oddish run -d swebench@1.0 -a codex -m openai/gpt-5.2 --n-trials 3

# Run from a YAML/JSON sweep config
oddish run -d terminalbench@2.0 -c sweep.yaml

# Watch a task or experiment
oddish status <task_id> --watch
oddish status --experiment <experiment_id> --watch

# Pull remote outputs back to disk
oddish pull <trial_id>
oddish pull <task_id> --watch --interval 5
oddish pull <experiment_id> --include-task-files

# Delete a task or experiment
oddish clean <task_id>
oddish clean --experiment <experiment_id>
```

### Execution environments

Supported `oddish run --env` values come from Harbor:

- `docker`
- `daytona`
- `e2b`
- `modal`
- `runloop`
- `gke`

When `--env` is omitted:

- local API URLs default to `docker`
- Oddish Cloud (`*.modal.run`) defaults to `modal`
- other remote APIs default to `docker`

### Sweep config files

`oddish run -c sweep.yaml` accepts YAML or JSON. A minimal example:

```yaml
agents:
  - name: claude-code
    model_name: anthropic/claude-sonnet-4-5
    n_trials: 3
  - name: codex
    model_name: openai/gpt-5.2
    n_trials: 3

dataset: swebench@1.0
n_tasks: 10
priority: low
```

Per-agent overrides like environment variables, kwargs, and timeouts are passed
through Harbor agent config fields in the sweep config or API payload.

## Database Commands

Use the DB helper CLI through `python -m oddish.db`:

```bash
uv run python -m oddish.db init
uv run python -m oddish.db setup
uv run python -m oddish.db install-pgqueuer
uv run python -m oddish.db uninstall-pgqueuer
uv run python -m oddish.db reset
uv run python -m oddish.db purge
```

What they do:

- `init`: run Alembic migrations
- `setup`: run Alembic migrations and install PGQueuer tables
- `install-pgqueuer`: install queue tables only
- `uninstall-pgqueuer`: remove queue tables
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
| DELETE | `/tasks/{task_id}` | Delete a task and its trials |
| DELETE | `/experiments/{experiment_id}` | Delete an experiment and its tasks |
| PATCH | `/experiments/{experiment_id}` | Update experiment metadata |
| GET | `/tasks/{task_id}/trials/{index}` | Fetch a trial by 0-based index |
| GET | `/trials/{trial_id}/logs` | Fetch logs for a trial |
| GET | `/trials/{trial_id}/result` | Fetch `result.json` for a trial |

Local `localhost` usage does not require API auth by default. Remote APIs
typically require `ODDISH_API_KEY`.

## Docker Compose

`docker-compose.yml` is primarily for local development:

```bash
# Database only, while running Python directly on the host
docker compose up -d db

# Containerized API with its built-in background workers
docker compose up -d db api

# Add a dedicated worker service as well
docker compose up -d db api worker

# One-time DB initialization in a container
docker compose run --rm db-init
```

Services:

- `db`: Postgres 16
- `api`: FastAPI server
- `worker`: standalone queue worker
- `db-init`: one-shot DB setup task

## Configuration

Settings are loaded from `.env`. Most package settings use the `ODDISH_` prefix,
while provider credentials use their usual environment variable names.

### Required for local development

```bash
DATABASE_URL=postgresql+asyncpg://oddish:oddish@localhost:5432/oddish
```

`DATABASE_URL` takes precedence over `ODDISH_DATABASE_URL`.

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

- uploaded task bundles: `/tmp/oddish-tasks`
- Harbor job outputs: `/tmp/harbor-jobs`

## Repository Layout

```text
oddish/
├── src/oddish/
│   ├── api/                  # FastAPI app and request handlers
│   ├── cli/                  # oddish run/status/pull/clean
│   ├── db/                   # models, connection helpers, storage
│   ├── workers/              # Harbor execution and queue workers
│   ├── backfill_queue_keys.py
│   ├── config.py
│   ├── experiment.py
│   ├── infra.py
│   ├── queue.py
│   └── schemas.py
├── alembic/                  # DB migrations
├── docker-compose.yml
├── Dockerfile
├── env.example
├── pyproject.toml
└── README.md
```

## Using as a Library

Some commonly imported surfaces:

```python
from oddish.config import settings
from oddish.db import TaskModel, TrialModel, get_session, init_db
from oddish.queue import create_task
from oddish.schemas import HarborConfig, TaskSubmission, TaskSweepSubmission, TrialSpec
from oddish.workers import create_queue_manager
```

## Troubleshooting

### API does not start

```bash
docker compose ps
uv run python -m oddish.db setup
curl http://localhost:8000/health
```

### Tasks stay queued

- make sure the API is healthy
- remember `oddish.api` auto-starts workers, or run `python -m oddish.workers.queue.worker`
- check queue concurrency settings if a model-specific queue is saturated

### Pulling from a remote API fails

- verify `ODDISH_API_URL`
- verify `ODDISH_API_KEY` for non-local APIs
- try `oddish status` first to confirm auth and connectivity
