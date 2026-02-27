# Oddish Core Library

This README is focused on implementation details for the `oddish` package.

## Deep Technical Documentation

This covers architecture, configuration, and operational details.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  CLI (oddish run/status) or API Client                      │
│  - Uses env vars for API URL + auth                         │
│  - Submits tasks via HTTP                                   │
│  - Watches tasks or experiments via CLI status              │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  FastAPI Server (python -m oddish.api)                      │
│  - POST /tasks/upload, /tasks/sweep                         │
│  - GET /tasks, /tasks/{id}, /trials/{id}/logs               │
│  - Auto-starts workers by default                           │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  Postgres                                                   │
│  - experiments table (grouping + sharing metadata)          │
│  - tasks table (task metadata + verdict)                    │
│  - trials table (runs + analysis)                           │
│  - pgqueuer tables (job queue)                              │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  PGQueuer Workers                                           │
│  - Poll queue via SELECT FOR UPDATE SKIP LOCKED             │
│  - Queue-key concurrency limits                             │
│  - Execute trials, analyses, and verdict jobs               │
└─────────────────────────────────────────────────────────────┘
```

## Local Development

### Quick start (recommended)

```bash
cp env.example .env
docker compose up -d db
uv sync
uv run python -m oddish.db setup
uv run python -m oddish.api
```

This starts Postgres, runs the API with workers, and is a good baseline for local development.

### CLI configuration

The CLI can talk to either:

- **Local API**: `http://localhost:8000` (self-hosted)
- **Hosted API** (optional): `https://abundant-ai--api.modal.run`

For local use, point the CLI at your server:

```bash
export ODDISH_API_URL="http://localhost:8000"
```

The local API does not enforce auth by default. For the hosted API, set an API key:

```bash
export ODDISH_API_URL="https://abundant-ai--api.modal.run"
export ODDISH_API_KEY="ok_..."
```

### CLI config precedence

The CLI resolves API settings in this order:

1. `ODDISH_API_URL` / `ODDISH_API_KEY` / `ODDISH_DASHBOARD_URL`
2. `ODDISH_DEFAULT_API_URL` / `ODDISH_DEFAULT_DASHBOARD_URL`
3. Built-in defaults:
   - API: `https://abundant-ai--api.modal.run`
   - Dashboard: `https://www.oddish.app`

### Database setup commands

```bash
uv run python -m oddish.db setup            # Full setup (Alembic + PGQueuer)
uv run python -m oddish.db init             # Run Alembic migrations only
uv run python -m oddish.db install-pgqueuer # Install PGQueuer tables only
uv run python -m oddish.db reset            # Drop and recreate all tables
uv run python -m oddish.db purge            # Delete all data (preserves schema)
```

### API flags

```bash
# Set queue-key concurrency
uv run python -m oddish.api --n-concurrent '{"openai/gpt-5.2": 8, "anthropic/claude-sonnet-4-5": 8}'

# Custom host/port
uv run python -m oddish.api --host 0.0.0.0 --port 9000
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/docs` | Swagger UI |
| GET | `/tasks` | List tasks |
| GET | `/tasks/{task_id}` | Task details with trials |
| DELETE | `/tasks/{task_id}` | Delete a task and its trials |
| DELETE | `/experiments/{experiment_id}` | Delete an experiment and its tasks |
| PATCH | `/experiments/{experiment_id}` | Update experiment name |
| POST | `/tasks/upload` | Upload task tarball |
| POST | `/tasks/sweep` | Create evaluation sweep |
| GET | `/tasks/{task_id}/trials/{index}` | Fetch trial by index |
| GET | `/trials/{trial_id}/logs` | Fetch trial logs |
| GET | `/trials/{trial_id}/result` | Fetch `result.json` |

## Docker Compose

The `docker-compose.yml` orchestrates local development:

```bash
# Database only (for local Python dev)
docker compose up -d db

# Full stack (containerized)
docker compose up -d db api worker

# One-time database initialization
docker compose run --rm db-init
```

Services:

| Service | Description |
|---------|-------------|
| `db` | Postgres 16 |
| `api` | FastAPI server (`python -m oddish.api`) |
| `worker` | Standalone worker (`python -m oddish.workers.queue.worker`) |
| `db-init` | One-time setup: runs Alembic migrations + PGQueuer install |

## Configuration

Oddish loads environment variables from `.env` by default (via Pydantic Settings with the `ODDISH_` prefix).

### Database URL

Both formats supported:

```bash
DATABASE_URL=postgresql+asyncpg://oddish:oddish@localhost:5432/oddish
ODDISH_DATABASE_URL=postgresql+asyncpg://...  # Alternative
```

`DATABASE_URL` takes precedence over `ODDISH_DATABASE_URL`.

### Storage

**Local (default):**

- Tasks: `/tmp/oddish-tasks`
- Harbor artifacts: `/tmp/harbor-jobs`

**S3/R2 (production):**

```bash
ODDISH_S3_ENABLED=true
ODDISH_S3_BUCKET=data
ODDISH_S3_ACCESS_KEY=...
ODDISH_S3_SECRET_KEY=...
ODDISH_S3_ENDPOINT_URL=https://...
```

Task uploads land under `tasks/<task_id>/`. Trial artifacts are uploaded under
`tasks/<task_id>/trials/<trial_id>/` when possible, with a fallback of
`trials/<trial_id>/` for legacy IDs.

### Execution Environments

Oddish runs Harbor tasks in a sandboxed environment.

CLI behavior when `--env` is omitted:

- Local API URL (`localhost`) defaults to `docker`
- Hosted Modal API URL (`*.modal.run`) defaults to `modal`
- Other remote API URLs default to `docker`

You can always override per task with:
`oddish run --env {docker|daytona|e2b|modal|runloop|gke}`.

### Queue-Key Routing

Oddish routes jobs by **queue key** (normalized model string) for PGQueuer entrypoints.
Agent names still map to provider buckets for compatibility/attribution, but queueing
uses `get_queue_key_for_trial(agent, model)` and defaults to the agent fallback only
when no model is provided.

### Concurrency Control

Queue-key concurrency is fixed at API startup (not per job).

Order of precedence:

1. **Manual API startup:** `python -m oddish.api --n-concurrent '{"openai/gpt-5.2": 8}'`
2. **Default:** `ODDISH_DEFAULT_MODEL_CONCURRENCY` (with optional model overrides)

For self-hosted setups, set concurrency on API startup:
```bash
uv run python -m oddish.api --n-concurrent '{"openai/gpt-5.2": 8, "anthropic/claude-sonnet-4-5": 8}'
```

Changing concurrency requires restarting the API process.

### LLM API Keys

Only set keys for providers you use:

```bash
ANTHROPIC_API_KEY=sk-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
```

### Sandbox Provider Keys

Set keys for the sandbox environments you use:

```bash
DAYTONA_API_KEY=...
MODAL_TOKEN_ID=...
MODAL_TOKEN_SECRET=...
```

## Execution Pipeline

Tasks move through a multi-stage pipeline when `run_analysis` is enabled:

1. **Trials** run for each agent/model pair (status: pending → queued → running → success/failed).
2. **Analyses** run per trial after completion to classify outcomes.
3. **Verdict** runs once per task to summarize analyses.

Each stage is a PGQueuer job routed through queue-key entrypoints. Task status
progresses from `pending` → `running` → `analyzing` → `verdict_pending` → `completed`
(or `failed` on terminal error).

## Experiments

Every task belongs to an experiment. If no experiment is provided, Oddish
generates a short, human-friendly name (`oddish.experiment.generate_experiment_name`).
The CLI can watch an experiment with `oddish status --experiment <id>`.

## Workers

### Auto-start behavior

By default, `python -m oddish.api` spawns workers in background threads.

For separate worker processes (for scaling), run:

```bash
uv run python -m oddish.workers.queue.worker
```

In Docker Compose, this maps to the dedicated `worker` service.

### How PGQueuer works

Workers claim jobs atomically via Postgres:

```sql
SELECT * FROM pgqueuer
WHERE status = 'queued' AND entrypoint = 'openai/gpt-5.2'
ORDER BY priority DESC
LIMIT 1
FOR UPDATE SKIP LOCKED;

UPDATE pgqueuer SET status = 'processing' WHERE id = ?;
```

- `FOR UPDATE`: Locks the row
- `SKIP LOCKED`: Other workers skip locked rows
- **Result:** Each job claimed by exactly one worker

### Concurrency enforcement

PGQueuer checks processing count before claiming:

```sql
SELECT COUNT(*) FROM pgqueuer
WHERE entrypoint = 'openai/gpt-5.2' AND status = 'processing';
```

If count >= limit (e.g., 8), worker waits. Concurrency is database state, not worker count.

### Job routing

Queue entrypoints are created per queue key (typically model identifiers).
Each entrypoint handles jobs with `job_type` of `trial`, `analysis`, or `verdict`.

## CLI Reference

```bash
# Point at local API
export ODDISH_API_URL="http://localhost:8000"

# Run a task
oddish run ./my-task -a claude-code -m claude-sonnet-4-5

# Run a sweep
oddish run -d terminalbench@2.0 -c sweep.yaml

# Optional run flags
oddish run ./my-task --run-analysis --env daytona

# Monitor
oddish status
oddish status <task_id> --watch
oddish status --experiment <experiment_id> --watch

# Cleanup
oddish clean <task_id>
oddish clean --experiment <id>
oddish clean --all-experiments
```

### GitHub Actions Integration

The CLI supports JSON output for CI pipelines:

```bash
oddish run ./tasks/* -a codex --n-trials 1 --json

# Output:
# {
#   "experiment": "random-words-123",
#   "experiment_url": "...",
#   "total_trials": 3,
#   "tasks": [
#     {"id": "task-abc123", "trials_count": 1, "url": "..."},
#     ...
#   ]
# }
```

Environment variables for CI:
- `ODDISH_API_URL`: API endpoint (your self-hosted URL, or the hosted API)
- `ODDISH_API_KEY`: API token (required for the hosted API)

## Repository Structure

```
oddish/
├── src/oddish/
│   ├── __init__.py          # Public API (lazy-loaded exports)
│   ├── config.py            # Settings, provider mapping
│   ├── schemas.py           # Pydantic request/response models
│   ├── queue.py             # Task creation, queue orchestration
│   ├── experiment.py        # Experiment name generation
│   ├── infra.py             # Docker/infrastructure helpers
│   ├── api/
│   │   ├── __init__.py      # FastAPI app + endpoint wiring
│   │   ├── endpoints.py     # Core endpoint logic
│   │   ├── helpers.py       # Response builders
│   │   ├── tasks.py         # Task upload handling
│   │   └── trial_io.py      # Trial logs/result reading
│   ├── cli/
│   │   ├── __init__.py      # Typer app entry point
│   │   ├── run.py           # Run command (task submission)
│   │   ├── status.py        # Status command (monitoring)
│   │   ├── clean.py         # Clean command (deletion)
│   │   ├── api.py           # HTTP client helpers
│   │   ├── config.py        # API URL/auth resolution
│   │   └── infra.py         # Local infrastructure helpers
│   ├── db/
│   │   ├── __init__.py      # DB exports
│   │   ├── __main__.py      # CLI: python -m oddish.db
│   │   ├── models.py        # SQLAlchemy models (Experiment, Task, Trial)
│   │   ├── connection.py    # Engine, session factory, pool management
│   │   └── storage.py       # S3/local storage client
│   └── workers/
│       ├── harbor_runner.py # Harbor task executor + artifact upload
│       └── queue/
│           ├── queue_manager.py    # PGQueuer setup + entrypoints
│           ├── worker.py           # Standalone worker entry point
│           ├── trial_handler.py    # Trial execution handler
│           ├── analysis_handler.py # Post-trial analysis handler
│           ├── verdict_handler.py  # Task-level verdict handler
│           ├── db_helpers.py       # Worker DB utilities
│           └── shared.py           # Shared worker utilities
│
├── alembic/                 # Database migrations
├── alembic.ini              # Alembic configuration
├── docker-compose.yml       # Local dev orchestration
├── env.example              # Example .env file
├── pyproject.toml           # Package config and dependencies
└── README.md
```

## Using as a Library

Oddish can be imported as a library in your own services:

```python
# Database models and sessions
from oddish.db import TaskModel, TrialModel, get_session, init_db

# Queue operations
from oddish.queue import create_task, get_task_with_trials, get_queue_stats

# Worker logic
from oddish.workers.queue import create_queue_manager

# Configuration
from oddish.config import settings

# Schemas
from oddish.schemas import TaskSubmission, TrialSpec
```

## Database Migrations

Oddish uses Alembic for schema management. The version table is `alembic_version_oddish` (to avoid conflicts if you run your own Alembic migrations in the same database).

PGQueuer tables are managed separately via `oddish.db install-pgqueuer`.

```bash
# Run all migrations
uv run alembic upgrade head

# Check current version
uv run alembic current

# Full setup (migrations + PGQueuer)
uv run python -m oddish.db setup
```

## Troubleshooting

### Port conflicts

| Service | Port |
|---------|------|
| Postgres | 5432 |
| API | 8000 |

If ports are in use, stop the conflicting process or change the port.

### Database connection errors

```bash
# Verify Postgres is running
docker compose ps

# Test connection
psql $DATABASE_URL -c "SELECT 1"

# Check migrations
uv run alembic current
uv run alembic upgrade head
```

### Tasks stuck in "queued"

1. Check workers are running and API is healthy:
   ```bash
   curl http://localhost:8000/health
   oddish status
   ```

2. Check queue-key concurrency limits (set at API startup) and worker logs

3. Check for errors in API logs

### Harbor execution failures

1. Verify the sandbox environment is available (Docker running, Daytona key set, etc.)
2. Check LLM API key is set for the provider
3. Check trial error message:
   ```bash
   curl http://localhost:8000/tasks/<task_id> | jq '.trials[].error_message'
   ```
