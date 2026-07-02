# Oddish Repository Guide

This file is the technical guide for the entire monorepo. End-user CLI docs live in `DOCS.md`.

The repo has three main packages:

- `oddish/` — the core Python CLI, FastAPI server, queueing layer, and worker runtime
- `backend/` — the hosted cloud layer built on top of `oddish`; adds multi-tenant auth, Modal deployment, and product-specific endpoints
- `frontend/` — the Next.js App Router dashboard and public pages

Python `3.13` is required for `oddish` and `backend`. Node.js `20+` and `pnpm` are required for `frontend`.

## Maintenance Notes

- Keep `DOCS.md` focused on end-user CLI workflows; keep `oddish/README.md` as a short package quick start.
- Put `oddish` implementation details, architecture notes, and local development guidance here.
- If you change the CLI surface in `oddish/src/oddish/cli/`, update `DOCS.md` and the command list in `oddish/README.md`.
- If you change API contracts, queue behavior, or storage layout, update this file.
- If you change `backend/` auth, deployment, or worker orchestration, update this file.
- If you change `frontend/` routing, API proxy structure, or auth behavior, update this file.
- Preserve the package boundary: `oddish/` must remain self-hostable for the
  CLI and standalone server; hosted product concerns (auth, org membership,
  Modal app wiring, managed worker spawning, GitHub/webhook integrations, and
  cloud-only policy) belong in `backend/`.

## Repository Layout

```text
oddish/                         # Core Python package (CLI, server, workers, DB)
├── src/oddish/
│   ├── analyze/                # QA prompts and analysis helpers
│   ├── cli/                    # oddish run/upload/ls/status/cancel/pull/...
│   ├── core/                   # shared endpoint/service logic (reused by backend/)
│   ├── server/                 # standalone FastAPI app (python -m oddish.server)
│   ├── db/                     # models, connection helpers, storage
│   ├── dispatch/               # local/cloud dispatch cycle backends
│   ├── integrations/           # GitHub and external integrations
│   ├── mcp/                    # doc-store MCP server
│   ├── runtime/                # runtime result/log helpers
│   ├── workers/                # worker_jobs runtime, handlers, cleanup
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
│   ├── services/               # hosted services, including cc_chat
│   └── routers/                # tasks, trials, dashboard, documents, tags, skills, admin, webhooks
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
  - worker_jobs       # unified queue (TRIAL / QA / …)
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
2. Submit a sweep of agent/model trials for that task; each trial is
   enqueued as a `worker_jobs` row in the same transaction as its domain
   row. Set `max_trial_attempts` on a sweep submission or sweep config to
   override the total attempt budget for newly-created trials.
3. Workers claim one `worker_jobs` row at a time, dispatch to the registered
   handler (`TRIAL` / `QA`), write heartbeats, and exit.
4. Trajectory analysis is **task-scoped**: when every trial of a
   `run_analysis` task is terminal, a single `QA` job is enqueued. That one
   job classifies every live trial's trajectory (same taxonomy / evidence /
   reasoning, written to `trials.analysis`) and then synthesizes the task
   verdict (`tasks.verdict`). A sweep of `T` tasks × `N` trials therefore
   enqueues `T` QA jobs instead of `T × (N + 1)`. (The pre-refactor per-trial
   `ANALYSIS` and per-task `VERDICT` kinds are kept as legacy enum values only
   so historical / in-flight rows remain valid and drain across a deploy;
   nothing enqueues them anymore. `trials.analysis` holds the per-trial
   classification and `tasks.verdict` the task-level result — both are outputs
   of the one QA job.)
5. Trial completion persists queryable execution metrics on the trial row:
   input/cache/output tokens, total trajectory steps, native runtime cost when
   reported, phase timing, and trajectory availability. Use the CLI or dashboard
   to watch progress and pull logs/artifacts back locally.

## Package Boundaries

`oddish` owns the execution core and shared queue/runtime primitives:

- core models and migrations, including `worker_jobs` and `queue_slots`
- unified claim/dispatch SQL, one `run_single_worker_job` runner, and a
 handler registry (`TrialJobHandler`, `QaJobHandler`, plus the legacy
 `AnalysisJobHandler` kept to drain in-flight rows)
- the task-level QA job (`run_task_qa_job`): classify every live trial via
 the shared `classify_trial_and_store`, then synthesize the task verdict
- shared queue-slot leasing, per-queue-key concurrency limits, and
 per-user fairness on `TRIAL` claims
- stale-heartbeat reaping, RETRYING → QUEUED mirror-back, and pipeline
 stage reconciliation in one cleanup sweep
- soft-delete semantics on domain rows via the `deleted_at` column and
 a session-level filter (`oddish.db.soft_delete`); every ORM read on a
 registered model gets `WHERE deleted_at IS NULL` automatically

`oddish` must not import from `backend/`, `backend.auth`, `backend.models`,
`cloud_policy`, `idempotency_store`, Clerk, or Modal app/deployment modules.
Keep optional provider/runtime SDK imports lazy behind core abstractions so a
CLI/self-host install can run without hosted deployment dependencies. If shared
behavior is needed by both products, put the host-agnostic primitive under
`oddish/src/oddish/core`, `oddish/src/oddish/workers`, or another neutral
`oddish` module, then wrap it from `backend/`.

`backend` wraps `oddish` with the hosted-only layer:

- Clerk/API key auth and org-scoped APIs
- Modal worker spawning and runtime patching
- cloud environment policy and GitHub notification hooks
- public sharing routes and other product-specific endpoints

`frontend` provides the user-facing layer:

- authenticated dashboard, task browser, experiment views
- Clerk-based auth and org management
- Next.js route handlers that proxy requests to the backend

### Task Identity

`tasks.name` is the human-readable lookup key within an org. Live task names
must stay unique and indexed (`idx_tasks_unique_org_name`) so an upload of the
same task name resolves to the existing task and creates a new `task_versions`
row instead of creating a different task. Renaming a task is allowed, but any
rename path must preserve the live `(org_id, name)` uniqueness invariant and
must not split the task's version history.

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
- `unlink_task_from_experiment_core` (same module) is the *scoped* sibling:
  it tombstones only the `task_experiments` join row for one
  `(task_id, experiment_id)` pair plus that experiment's trials for the
  task, and **never** tombstones the task row. It exists so a *shared* task
  can be pulled out of one experiment without disturbing the others (a
  whole-task `delete_task_core` would hit every experiment). It also fires
  the membership-removed tag hook so inherited EXPERIMENT tags drop. The
  experiment-scoped trials are tombstoned alongside the link to keep the
  experiment consistent — its task list is join-driven, but dashboard trial
  counts key off `trials.experiment_id`.
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
| `trial_handler.py` | TRIAL execution body |
| `qa_handler.py` | Task-level QA job: `run_task_qa_job` classifies every live trial then synthesizes the verdict |
| `analysis_handler.py` | `classify_trial_and_store` (shared per-trial classifier) + the transitional `run_analysis_job` wrapper for in-flight legacy ANALYSIS rows |
| `cleanup.py` | Zombie reaper, stale-heartbeat sweep, stage safety nets, **per-slot** orphaned-slot release (see invariants below) |
| `slots.py` | `queue_slots` lease acquire/release (`locked_by` / `locked_until` / `locked_at`) |
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
| POST | `/tasks/{task_id}/qa/retry` | (Re)run the single task-level QA job: reset every live trial's classification + the verdict, then classify all trials and synthesize a fresh verdict |
| POST | `/tasks/{task_id}/qa/cancel` | Cancel a task's in-flight QA job |
| POST | `/experiments/combine` | Create a new experiment that merges the task memberships and finished trials (with artifacts) of two or more source experiments |
| DELETE | `/experiments/{experiment_id}` | Soft-delete an experiment, its trials, and any now-orphaned tasks |
| DELETE | `/experiments/{experiment_id}/tasks/{task_id}` | Soft-delete just the task↔experiment association (the `task_experiments` join row) plus that experiment's trials for the task; the task itself and its data in other experiments are left intact. Use to pull a *shared* task out of one experiment. Hosted backend only |
| PATCH | `/experiments/{experiment_id}` | Update experiment metadata |
| GET | `/tasks/{task_id}/trials/{index}` | Fetch a trial by 0-based index |
| DELETE | `/trials/{trial_id}` | Soft-delete a single trial, cancel its in-flight jobs, and invalidate the parent task's cached verdict |
| POST | `/trials/{trial_id}/retry` | Retry a trial by creating a fresh replacement row. Optional body: `registry_auth` to supply fresh per-run registry credentials for the replacement trial |
| GET | `/trials/{trial_id}/logs` | Fetch logs for a trial |
| GET | `/trials/{trial_id}/result` | Fetch `result.json` for a trial |

Public share links use 256-bit `public_token` values and are access-by-link, not
enumerable. The unauthenticated `/public/experiments` list intentionally returns
no share tokens. Public task/trial/file routes must stay scoped under
`/public/experiments/{public_token}/...` and verify membership in that shared
experiment; do not reintroduce `/public/tasks/{task_id}` or
`/public/trials/{trial_id}` ID-only access. Unpublishing an experiment clears
`public_token`, so republishing mints a fresh link and old URLs stay revoked.

### Configuration and model routing

Settings are loaded from `oddish/.env`; see `oddish/env.example`,
`backend/.env.example`, and `frontend/env.example` for the complete env surface.
Keep these routing rules in sync with `oddish/src/oddish/config.py` and
`oddish/src/oddish/workers/harbor/runner.py`:

- Claude trials run through AWS Bedrock only. `CLAUDE_CODE_USE_BEDROCK=1` is
  baked into the Modal image, and Claude model aliases must normalize to an
  invokable inference profile (`global.` / `us.` / ARN) via
  `to_bedrock_model_id`. `ANTHROPIC_API_KEY` is not a trial route.
- OpenAI-family jobs default to Azure OpenAI. Use
  `ODDISH_OPENAI_PROVIDER=openai` plus `OPENAI_API_KEY` only when intentionally
  routing to public OpenAI.
- z.ai, MiniMax, Moonshot/Kimi, Fireworks, and xAI each have explicit canonical
  provider prefixes and queue keys: `zai/`, `minimax/`, `moonshot/`,
  `fireworks/`, and `xai/`. Add or change provider aliases in `config.py`, then
  update env injection in the Harbor runner and the network allowlist notes.
- Provider secrets are referenced by env var name (`AWS_BEARER_TOKEN_BEDROCK`,
  `ZAI_API_KEY`, `MINIMAX_API_KEY`, `MOONSHOT_API_KEY`, `FIREWORKS_API_KEY`,
  `XAI_API_KEY`) and must not be persisted on trial rows.

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

There are exactly two org roles: `admin` (manage users/settings) and `member`
(run evals, view results). New users default to `member`.

Auth flow: read token → if `ok_` prefix validate API key → otherwise validate Clerk JWT and resolve org/user → return `AuthContext`.

API key creation is user-auth only (API-key auth is rejected so one key cannot
mint another) and requires an `admin` with an `@abundant.ai` email in the main
Abundant org (`can_create_api_keys` / `require_api_key_creator`).

If a Clerk JWT arrives without `org_id`, the backend tries to resolve a single existing org membership, or provisions a personal org.

### Worker Architecture

Dispatcher + reconciler + single-job pattern, backed by the unified
`worker_jobs` table. **Dispatch and reconciliation are deliberately separate
scheduled functions** so a slow or deadlocking reconciliation sweep can never
block worker spawning (previously they shared one function under a tight 60s
timeout, so a sweep that timed out or deadlocked spawned zero workers that
cycle — and a SIGKILL mid-sweep left orphaned `idle in transaction` locks that
deadlocked the next sweep):

1. `poll_queue()` runs on a `POLL_INTERVAL_SECONDS` (180s) Modal schedule under
   `DISPATCHER_TIMEOUT_SECONDS` (120s). It does only two things: discover active
   queue keys via `discover_active_worker_job_queue_keys`, and launch up to
   `MAX_WORKERS_PER_POLL` single-job containers via the org-first fair-share
   `build_spawn_plan`. It runs no cleanup, so dispatch is never blocked by it.
   `MAX_WORKERS_PER_POLL` is the dominant throughput ceiling: long agent trials
   hold a `queue_slots` lease for their full duration, so steady-state running
   workers ≈ `spawns_per_poll × trial_duration / poll_interval`. It must stay
   high enough to fill the per-model concurrency limits (which sum into the
   hundreds); the per-queue-key slot caps and `WORKER_MAX_CONTAINERS` remain the
   real bounds.
2. `reconcile_queue_state()` runs on its own `CLEANUP_INTERVAL_SECONDS` (240s)
   schedule under a generous `CLEANUP_TIMEOUT_SECONDS` (600s) so it is never
   SIGKILLed mid-transaction. It runs (each phase wrapped best-effort so one
   failure doesn't abort the rest): stale `queue_slots` lease cleanup,
   `cleanup_orphaned_queue_state` (zombie-txn reap + stale-heartbeat sweep +
   stage safety nets + **per-slot** orphaned slot release — see "Worker runtime
   invariants" below), and the experiments owner
   backfill (`dashboard_owner_backfill` — converges `owner_user_id` so the
   dashboard Mine filter stays on its indexed fast path). The display-hygiene
   clear of terminal-trial claim metadata
   (`clear_terminal_trial_runtime_refs`) runs after the main reconciliation
   transaction commits, in batched `FOR UPDATE SKIP LOCKED` transactions, so it
   can neither deadlock against live workers nor roll back the sweep.
3. `process_single_job(queue_key)` acquires a `queue_slots` lease for the
   queue key (stamping `locked_by = <worker_id>`, `locked_at = NOW()`,
   `locked_until = NOW() + WORKER_TIMEOUT + 30s`) and calls
   `run_single_worker_job` → `drain_worker_jobs`, which atomically claims one or
   more rows from `worker_jobs` (stamping `current_worker_id = <worker_id>`),
   dispatches to the registered handler (`TRIAL` or the task-level `QA` job;
   `ANALYSIS` only for legacy in-flight rows), writes heartbeats on both
   `worker_jobs.heartbeat_at` and the mirrored domain column, records the
   outcome (`SUCCESS` / `RETRYING` / `FAILED` / `CANCELLED`), runs the
   post-success hook (GitHub notification) when applicable, releases the slot
   in its `finally`, and exits.

Handler registration happens at container load via
`ensure_builtin_handlers_registered()`. Post-success hooks
(`notify_github_trial`, `notify_github_qa`, and the transitional
`notify_github_analysis`) are wired through `_POST_SUCCESS_HOOKS` in
`worker/functions.py`. The task-level `QA` job fires `notify_github_qa`,
which refreshes the whole PR comment (per-trial classifications + task
verdict) in one update.

### Worker Runtime Invariants & Pitfalls

Load-bearing properties, several learned from incidents. Changing them naively
silently breaks throughput or correctness — read before touching
`worker/functions.py`, `slots.py`, `cleanup.py`, or the dispatcher.

1. **Workers hold NO DB connection during the Harbor run.** A trial runs for
   minutes to ~12h but only touches the DB for a few ms (claim, 30s heartbeats,
   outcome), so workers use `NullPool` (`Settings.db_use_null_pool`) + per-op
   `asyncpg` connections. ⚠️ Never introduce a pooled/long-lived connection or
   open session spanning the run: it pins one idle connection per running trial
   and exhausts the Supavisor/PgBouncer cap. (The API keeps a warm `QueuePool`
   only because it's short-lived — that reasoning doesn't transfer to workers.)

2. **`queue_slots` is the real concurrency gate.** Per-queue-key concurrency is
   enforced by leasing a `queue_slots` row (`acquire_queue_slot`, `FOR UPDATE
   SKIP LOCKED`), not by spawn count. The dispatcher budgets on `worker_jobs`
   RUNNING (`limit - running`) while the worker gates on a free slot — if those
   counters drift, the dispatcher over-spawns workers that exit immediately
   (watch for `metric=queue_lock_contention` floods).

3. **Slot leases can outlive their worker — reclaim per-slot.** The lease
   (`locked_until`) is `WORKER_TIMEOUT_SECONDS + 30` (~12h); a SIGKILLed /
   preempted worker never runs its `finally` release. `cleanup_orphaned_queue_state`
   frees a slot whenever its `locked_by` has no `RUNNING` `worker_jobs` row on
   `current_worker_id` (with a `locked_at` grace, `ORPHANED_SLOT_GRACE_MINUTES`,
   for the acquire→claim gap). ⚠️ Never gate this per-queue_key (e.g. "release
   only if zero jobs RUNNING on the key") — that was the original bug: one live
   job pinned every leaked lease for ~12h and starved the queue. The link is
   always `queue_slots.locked_by == worker_jobs.current_worker_id`.

4. **One model ⇒ one queue_key.** Limits key off the full `queue_key`; the same
   model under two keys gets the *sum* of both buckets against one provider quota
   (→ 429s, split dashboards, starvation). Canonicalize at enqueue in
   `oddish.config` (`normalize_trial_model` / `get_queue_key_for_trial` /
   `normalize_queue_key`): nop/oracle + variants collapse to the single
   `nop_oracle` id (`is_nop_oracle_agent`); z.ai / MiniMax / Moonshot / xAI map
   to `<provider>/<id>`. ⚠️ Known gap: Gemini isn't canonicalized — a bare
   `gemini-…` becomes `google/…` while `gemini/…` stays `gemini/…`, splitting one
   model across two buckets.

5. **No provider-level concurrency cap.** Each Bedrock/Gemini model id is its own
   bucket, but they share one AWS/Google account quota — the sum of per-model
   limits can exceed account RPM/TPM with no global throttle (a source of 429s).

6. **Stale-heartbeat reap can double-run a trial.** If heartbeats stall for
   `STALE_HEARTBEAT_MINUTES` (15, e.g. a pooler blip), the reaper flips the live
   trial to `RETRYING` and another worker may run it concurrently — no fencing
   token. The window is a deliberate trade-off (raised from 10 after an incident);
   shrink with care.

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
AZURE_OPENAI_*=... ANTHROPIC_API_KEY=... GEMINI_API_KEY=...
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
ODDISH_MODAL_DISPATCHER_TIMEOUT_SECONDS=...
ODDISH_MODAL_CLEANUP_INTERVAL_SECONDS=...
ODDISH_MODAL_CLEANUP_TIMEOUT_SECONDS=...
ODDISH_MODAL_WORKER_TIMEOUT_SECONDS=...
ODDISH_MODAL_WORKER_NONPREEMPTIBLE=...
ODDISH_MODAL_MAX_WORKERS_PER_POLL=256
ODDISH_MODAL_API_CPU=2.0
ODDISH_MODAL_API_MEMORY_MB=4096
ODDISH_MODAL_WORKER_CPU=1.0
ODDISH_MODAL_WORKER_MEMORY_MB=3072
ODDISH_MODAL_DISPATCHER_CPU=1.0
ODDISH_MODAL_DISPATCHER_MEMORY_MB=1024
ODDISH_MODAL_RECONCILER_CPU=1.0
ODDISH_MODAL_RECONCILER_MEMORY_MB=2048
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
| `worker/functions.py` | Modal dispatcher (`poll_queue`), reconciler (`reconcile_queue_state`), and kind-agnostic single-job runner |
| `worker/runtime.py` | Modal runtime patching and storage setup |
| `worker/github.py` | GitHub notification hooks used as post-success actions |

---

## `frontend/` — Next.js Dashboard

The frontend is a Next.js 16 / React 19 App Router app. Browser code calls
`src/app/api/*` route handlers, which forward to the backend from
`NEXT_PUBLIC_API_URL` and preserve auth. Public routes are `/`, `/share/*`,
`/datasets/*`, and `/api/public/*`; everything else is Clerk-protected.

See `frontend/README.md` for route groups, scripts, env vars, and deployment
commands. See `SELF_HOSTING.md` for full-stack local development and production
deployment.


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

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
| ------ | ---------- |
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.
