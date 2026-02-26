# Self-Hosting Oddish

Run Oddish locally with your own infrastructure.

## Prerequisites

- **Docker** (for local Postgres database)
- **Python 3.12+**
- **uv**

## Quick Start

### 1. Configure environment

Create a local `.env` file:

```bash
cp env.example .env
```

### 2. Start the database

```bash
docker compose up -d db
```

### 3. Install dependencies

```bash
uv sync
```

### 4. Run database migrations

```bash
uv run python -m oddish.db setup
```

### 5. Start the API server

```bash
uv run python -m oddish.api
```

The API will be available at `http://localhost:8000`.

### 6. Configure the CLI

Point the CLI at your local API (no auth needed):

```bash
export ODDISH_API_URL="http://localhost:8000"
```

> **Note:** The local API does not enforce auth, and the CLI does not require
> `ODDISH_API_KEY` for localhost **when `ODDISH_API_URL` points to localhost**.
> If you skip this, the CLI defaults to the hosted API and will require an
> `ODDISH_API_KEY`.

### 7. Run evaluations

```bash
oddish run -d terminal-bench@2.0 -a codex -m gpt-5.2-codex --n-trials 3
```

## Environment Variables

For self-hosting, create a `.env` file (or `cp env.example .env`):

```bash
# Database (required)
DATABASE_URL=postgresql+asyncpg://oddish:oddish@localhost:5432/oddish

# LLM providers
ANTHROPIC_API_KEY=sk-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...

# Cloud sandbox providers (optional)
DAYTONA_API_KEY=...
```

> **Note:** Trial artifacts are stored locally by default. You can ignore S3 settings for self-hosting.

## Infrastructure Management

### Stop services (keep data)

```bash
oddish clean --stop-only
```

### Stop services and delete data

```bash
oddish clean
```

### Manual Docker management

```bash
# View logs
docker compose logs -f db

# Stop database
docker compose down

# Reset database (delete all data)
docker compose down -v
docker compose up -d db
uv run python -m oddish.db setup
```

## Queue-Key Concurrency

Control how many trials run per queue key (typically a normalized model id):

```bash
# Pass concurrency on API startup
uv run python -m oddish.api --n-concurrent '{"openai/gpt-5.2": 8, "anthropic/claude-sonnet-4-5": 4}'
```

The API server persists between runs. Use `--fresh` to restart after updating concurrency.

## Architecture

Self-hosted Oddish runs as:

```
┌──────────────────────────────────────────┐
│ CLI (oddish run, status)                 │
└──────────────────────────────────────────┘
            │
            ▼
┌──────────────────────────────────────────┐
│ API Server (FastAPI, port 8000)          │
│ - Task/trial management                  │
│ - Queue operations                       │
│ - Worker coordination                    │
└──────────────────────────────────────────┘
            │
            ▼
┌──────────────────────────────────────────┐
│ Postgres Database                        │
│ - Tasks, trials, queue state             │
│ - PGQueuer for job scheduling            │
└──────────────────────────────────────────┘
            │
            ▼
┌──────────────────────────────────────────┐
│ Workers (spawned by API)                 │
│ - Execute trials in sandboxed runtimes   │
│ - Queue-key concurrency limits           │
└──────────────────────────────────────────┘
```

## Scaling Workers

For production deployments, you may want to run workers independently of the API:

```bash
# Terminal 1: API only (disable auto-started workers)
export ODDISH_AUTO_START_WORKERS=false
uv run python -m oddish.api

# Terminal 2-N: Workers (start as many as you want)
uv run python -m oddish.workers.queue.worker
```

See [AGENTS.md](AGENTS.md) for more details on architecture and scaling.
