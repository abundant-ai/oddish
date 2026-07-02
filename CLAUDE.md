# Claude Code Guide — Oddish

The canonical engineering guide for this repo is **`AGENTS.md`** at the repo root. Read it first; it covers the three packages (`oddish/` CLI+server, `backend/` hosted cloud layer, `frontend/` Next.js dashboard), required toolchains, and maintenance rules. End-user CLI docs are in `DOCS.md`.

## Git workflow

Never directly commit or push to `main`. Check out a feature branch, commit
there, push that branch, and open a PR for review.

## Preserve package boundaries

`oddish/` is the self-hostable core package for the CLI, standalone FastAPI
server, DB models/migrations, queue/runtime primitives, and worker handlers.
`backend/` is the hosted cloud layer for Clerk/API-key auth, org membership,
Modal app/deployment wiring, managed worker spawning, GitHub/webhooks, and
cloud-only policy. Do not import `backend/`, `backend.auth`, `backend.models`,
`cloud_policy`, `idempotency_store`, Clerk, or Modal app/deployment modules from
`oddish/`. Shared logic should live in host-agnostic `oddish` modules and be
wrapped by `backend/`.

## Task names stay unique and indexed

`tasks.name` is the human-readable lookup key within an org. Live task names
must stay unique and indexed (`idx_tasks_unique_org_name`) so re-uploading the
same task name updates the existing task by creating a new `task_versions` row
instead of creating a different task. Renaming a task is allowed, but it must
preserve the live `(org_id, name)` uniqueness invariant and the task's version
history.

## Never expose probes in public/share views

Probes are an **experimental, internal-only** feature. They must never appear in
any public, unauthenticated surface — the `/share/[token]` experiment view, the
`/datasets/[token]` view, or any `/public/*` API response. Both public views are
fed by the same endpoints in `oddish/src/oddish/core/sharing/public.py`, so the
filtering lives at the **data layer** (don't return `is_probe` trials), not just
the UI:

- `get_public_task` (`sharing/helpers.py`) strips `is_probe` trials from the
  loaded task, covering `get_public_task_status`.
- `list_public_experiment_tasks` excludes `is_probe` when filtering each task's
  trials.
- `list_public_task_trials` always passes `probe=False` (never honors a
  caller-supplied probe filter publicly).

When adding a new public/share endpoint or surfacing a new trial/task field
publicly, exclude probes the same way. Filter at the query/data layer — UI guards
alone are not enough, since the trials still ship to the browser.

## Gotcha — `list_tasks_core` `load_only` and MissingGreenlet

`list_tasks_core` (`oddish/src/oddish/core/endpoints/tasks_query.py`) powers
every `/tasks` route, including the experiment page. Its **compact path** (`compact_trials=True`)
restricts the trial/task/experiment selectin loads with `load_only(...)`, which
makes *only* the enumerated columns eager and **defers everything else**. Under
async SQLAlchemy, reading a deferred column in a response builder fires a
lazy-load outside the request greenlet and 500s with
`sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called`.

So: whenever you surface a **new `TrialModel` / `TaskModel` / `ExperimentModel`
column in the FE** (i.e. read it in `build_trial_response`,
`build_compact_trial_response`, or `_build_task_status_response` in
`core/helpers.py`), you **must also add that column to the matching `load_only`
set** in `list_tasks_core`. The full (non-compact) builder has no `load_only`, so
it won't catch the omission — the failure only shows up on the compact experiment
page. Builder unit tests can't catch it either (in-memory models have all attrs
set); the bug lives in the query options, not the builder.

## What this project is

Oddish runs evals on [Harbor](https://github.com/laude-institute/harbor) tasks in the cloud: provider-aware queuing, real-time monitoring, Postgres-backed state, S3 log storage. End users replace `harbor run` with `oddish run`. The hosted layer (`backend/` + `frontend/`) is deployed on Modal and surfaces a dashboard at oddish.app.

## Useful pointers

- **Run backend locally:** `cd backend && uv run modal serve deploy.py`. See `backend/README.md` for required env vars.
- **Run frontend locally:** `cd frontend && pnpm dev`. See `frontend/README.md`.
- **Tests:** `pytest` from `oddish/` or `backend/`. Frontend has no test suite wired up yet.
- **Self-hosting:** see `SELF_HOSTING.md` for Modal, Clerk, migrations, and local HTTPS.
