# AGENTS.md

Deep technical documentation for the Oddish core library. This covers architecture, configuration, and operational details kept out of the README.

## What is Oddish?

Oddish is a Postgres-backed scheduler for running Harbor agent evaluation tasks. It provides:

- **FastAPI server** (`python -m oddish.api`) - Task submission, monitoring, logs, and results
- **PGQueuer workers** - Provider-aware queues for trials, analysis, and verdict jobs
- **CLI** (`oddish run`, `oddish status`) - Submits tasks and monitors experiments
- **Database models** - Experiments, tasks, trials, and queue state in Postgres

This is the **open-source core** designed for self-hosting or embedding in your own services.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  CLI (oddish run/status) or API Client                      │
│  - Uses env vars for API URL + auth                         │
│  - Submits tasks via HTTP (Authorization: Bearer <token>)   │
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
│  - Provider-aware concurrency limits                        │
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

### CLI auth / configuration

The CLI can talk to either:

- **Hosted API** (default): `https://abundant-ai--api.modal.run` (no local infra)
- **Local API**: `http://localhost:8000` (and optionally starts infra for you)

Configure the CLI via env vars:

```bash
export ODDISH_API_URL="https://abundant-ai--api.modal.run"
export ODDISH_API_KEY="ok_..."
export ODDISH_DASHBOARD_URL="https://www.oddish.app"
```

For hosted deployments, `ODDISH_API_KEY` must be a real API token. The local API
does not enforce auth by default, but the CLI still requires `ODDISH_API_KEY`
unless you set a custom client.

### Manual setup

```bash
# Start Postgres
docker compose up -d db

# Install and run
uv sync
uv run python -m oddish.db setup
uv run python -m oddish.api
```

### API flags

```bash
# Set provider concurrency
uv run python -m oddish.api --n-concurrent '{"claude": 8, "openai": 8, "gemini": 8, "default": 8}'

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
```

Services:

| Service | Description |
|---------|-------------|
| `db` | Postgres 16 |
| `api` | FastAPI server (`python -m oddish.api`) |
| `worker` | Standalone worker (`python -m oddish.workers.queue.worker`) |

## Configuration

Oddish loads environment variables from `.env` by default.

### CLI config precedence

The CLI resolves API settings in this order:

1. `ODDISH_API_URL` / `ODDISH_API_KEY` / `ODDISH_DASHBOARD_URL`
2. `ODDISH_DEFAULT_API_URL` / `ODDISH_DEFAULT_DASHBOARD_URL`
3. Built-in defaults:
   - API: `https://abundant-ai--api.modal.run`
   - Dashboard: `https://www.oddish.app`

### Database URL

Both formats supported:

```bash
DATABASE_URL=postgresql+asyncpg://oddish:oddish@localhost:5432/oddish
ODDISH_DATABASE_URL=postgresql+asyncpg://...  # Alternative
```

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

Oddish runs Harbor tasks in a sandboxed environment. Defaults are:

- Local API: `docker`
- Hhosted API: `modal` (forced)

Override per task with `oddish run --env {docker|daytona|e2b|modal|runloop|gke}`.

### Provider Routing

Oddish maps agent names to providers in `src/oddish/config.py`:

| Agent Pattern | Provider |
|---------------|----------|
| `claude-code` | `claude` |
| `gemini-cli` | `gemini` |
| `codex` | `openai` |
| (fallback, `nop`, `oracle`) | `default` |


### Concurrency Control

Provider concurrency is fixed at API startup (not per job).

Order of precedence:

1. **Manual API startup:** `python -m oddish.api --n-concurrent '{"claude": 8}'`
2. **Default:** `claude: 8, gemini: 8, openai: 8, default: 8`

For self-hosted/local setups, set concurrency on API startup:
```bash
uv run python -m oddish.api --n-concurrent '{"claude": 8, "openai": 8}'
```

Note: for the **local API**, changing concurrency requires restarting the API process.
Use `oddish run ... --fresh` after updating the API startup flags.

### LLM API Keys

Only set keys for providers you use:

```bash
ANTHROPIC_API_KEY=sk-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
DAYTONA_API_KEY=...
```

## Execution Pipeline

Tasks move through a multi-stage pipeline when `run_analysis` is enabled:

1. **Trials** run for each agent/model pair (status: pending → queued → running → success/failed).
2. **Analyses** run per trial after completion to classify outcomes.
3. **Verdict** runs once per task to summarize analyses.

Each stage is a PGQueuer job routed through provider entrypoints. Task status
progresses from `pending` → `running` → `analyzing` → `verdict_pending` → `completed`
(or `failed` on terminal error).

## Experiments

Every task belongs to an experiment. If no experiment is provided, Oddish
generates a short, human-friendly name (`oddish.experiment.generate_experiment_name`).
The CLI can watch an experiment with `oddish status --experiment <id>`.

## Workers

### Auto-start behavior

By default, `python -m oddish.api` spawns workers in background threads.

To run workers separately (for scaling):

```bash
# Disable auto-start
export ODDISH_AUTO_START_WORKERS=false
uv run python -m oddish.api

# Run worker separately
uv run python -m oddish.workers.queue.worker
```

### How PGQueuer works

Workers claim jobs atomically via Postgres:

```sql
SELECT * FROM pgqueuer
WHERE status = 'queued' AND entrypoint = 'claude'
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
WHERE entrypoint = 'claude' AND status = 'processing';
```

If count >= limit (e.g., 8), worker waits. Concurrency is database state, not worker count.

### Job routing

Queue entrypoints are created per provider (claude, gemini, openai, default).
Each entrypoint handles jobs with `job_type` of `trial`, `analysis`, or `verdict`.

## CLI Reference

```bash
# Configure API URL (local)
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

### GitHub actions Integration

The CLI supports JSON output for easy integration with CI pipelines:

```bash
# Submit tasks and get JSON with task IDs and dashboard links
oddish run ./tasks/* -a codex --n-trials 1 --json

# Output:
# {
#   "experiment": "random-words-123",
#   "experiment_url": "https://www.oddish.app/experiments/random-words-123",
#   "public_experiment_url": null,
#   "total_trials": 3,
#   "tasks": [
#     {"id": "task-abc123", "trials_count": 1, "url": "https://www.oddish.app/tasks/task-abc123"},
#     ...
#   ]
# }
```

Environment variables for CI:
- `ODDISH_API_KEY`: API token (required)

## Repository Structure

```
oddish/
├── src/oddish/              # Main package
│   ├── api.py               # FastAPI server
│   ├── cli.py               # CLI entrypoint
│   ├── queue.py             # Task creation, queue orchestration
│   ├── config.py            # Settings, provider mapping
│   ├── schemas.py           # Pydantic models
│   ├── infra.py             # Docker/infrastructure management
│   ├── db/
│   │   ├── models.py        # SQLAlchemy models (Task, Trial)
│   │   ├── connection.py    # Database connection management
│   │   └── storage.py       # S3/local storage client
│   └── workers/
│       ├── queue/worker.py      # PGQueuer-based worker
│       └── harbor_runner.py     # Harbor task executor
│
├── alembic/                 # Database migrations
├── examples/                # Sample Harbor tasks
├── docker-compose.yml       # Local dev orchestration
└── pyproject.toml           # Package config
```

## Using as a Library

Oddish can be imported as a library in your own services:

```python
# Database models and sessions
from oddish.db import TaskModel, TrialModel, get_session, init_db

# Queue operations
from oddish.queue import create_task, get_task_with_trials

# Worker logic
from oddish.workers.queue import create_queue_manager

# Configuration
from oddish.config import settings

# Schemas
from oddish.schemas import TaskSubmission, TrialSpec
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

2. Check provider concurrency limits (set at API startup) and worker logs

3. Check for errors in API logs

### Harbor execution failures

1. Verify Docker is running
2. Check LLM API key is set for the provider
3. Check trial error message:
   ```bash
   curl http://localhost:8000/tasks/<task_id> | jq '.trials[].error_message'
   ```
