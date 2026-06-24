# Claude Code Guide — Oddish

The canonical engineering guide for this repo is **`AGENTS.md`** at the repo root. Read it first; it covers the three packages (`oddish/` CLI+server, `backend/` hosted cloud layer, `frontend/` Next.js dashboard), required toolchains, and maintenance rules. End-user CLI docs are in `DOCS.md`.

## Git workflow — NEVER commit or push to `main`

**Never directly commit or push to `main`.** Always check out a new branch first
(`git checkout -b <type>/<short-desc>`), commit there, push that branch, and open
a PR for review. This applies to every change, no matter how small — even a
one-line fix. If you find yourself on `main` with staged or unstaged changes,
create a branch and move the work onto it before committing.

## Gotcha — `list_tasks_core` `load_only` and MissingGreenlet

`list_tasks_core` (`oddish/src/oddish/core/endpoints.py`) powers every `/tasks`
route, including the experiment page. Its **compact path** (`compact_trials=True`)
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

- **Run backend locally:** `cd backend && uvicorn api.app:create_app --factory --reload`. See `backend/README.md` for required env vars.
- **Run frontend locally:** `cd frontend && pnpm dev`. See `frontend/README.md`.
- **Tests:** `pytest` from `oddish/` or `backend/`. Frontend has no test suite wired up yet.
- **Local job fixtures:** `jobs/` at the repo root contains a sample experiment tree mirroring the production S3 layout — useful when developing features that read trial artifacts.
- **Design docs and plans:** `docs/superpowers/specs/` and `docs/superpowers/plans/`.
