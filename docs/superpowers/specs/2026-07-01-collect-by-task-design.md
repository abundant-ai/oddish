# Collect-by-task: task-based read-only collections

**Date:** 2026-07-01
**Status:** Approved design (pivot to collections primitive; command surface = new `oddish collect`)

## Context

The codebase ships a reference-based **collections** primitive: an `is_collection=True`
experiment that *links* existing trials via `experiment_trials`/`task_experiments`
(no row/artifact copy — unlike `combine`, which copies).

- Shared core: `create_trial_collection_core` (`oddish/src/oddish/core/endpoints/collections.py`).
- Deployed route (the real server is **`backend/`**, FastAPI `api.app:create_app` on Modal):
  `backend/api/routers/tasks.py:773` — `POST /experiments/collections`, auth'd via
  `require_auth` → `AuthContext.org_id`, `auth.require_scope(APIKeyScope.TASKS)`.
- Schemas: `TrialCollectionRequest{name, trial_ids}` / `TrialCollectionResponse`
  (`oddish/src/oddish/schemas.py:560`, `:1045`).
- CLI today: `oddish experiment create --name … <trial_ids>` (`cli/experiment.py`).

> Note: `oddish/src/oddish/server/__init__.py` is a **secondary/legacy** server that does
> not register the collections route at all. It is **out of scope** — the deployed API is
> `backend/`. All server work below is in `backend/` + the shared core.

The only gap between this primitive and the goal: it takes `trial_ids` only. We add a
**task-based** mode that queries the DB for the latest trials of each task's latest
version, and a `collect` CLI that publishes by default.

## Goals

- Add **task-based selection** to `create_trial_collection_core`: given task ids/names,
  link each task's current-version trials.
- New **`oddish collect`** command: `oddish collect --task <task…> [<trial_id…>]
  --name … [--no-publish]`.
- **Publish by default** (CLI-orchestrated via the existing `POST /experiments/{id}/publish`).
- Reference-based (no copy). Server stays the source of truth for "current version."
- `combine` and `experiment create` left unchanged.

## Non-goals (YAGNI)

- Touching `combine` or the secondary `oddish/src/oddish/server`.
- Filtering to passing-only / most-recent-only. Include **all** current-version, terminal,
  non-superseded, non-probe trials per task (matches `ls`'s `latest_trials`).
- Auto-refreshing a collection when new trials/versions appear (linked set is fixed at
  creation; re-run to refresh).

## Selection semantics

For each requested task, link its current-version trials. This is the same task/version
scoping `ls`'s `latest_trials` uses, but **terminal-only** (like `combine`) — a stricter
subset than `ls`, which also lists running/pending trials:

- `(TrialModel.task_id, TrialModel.task_version_id)` equals `(task.id, task.current_version_id)`
  (`TaskModel.current_version_id`, `db/models.py:578`),
- `TrialModel.superseded_by_trial_id IS NULL`,
- `TrialModel.status IN {SUCCESS, FAILED}` (terminal only),
- `TrialModel.is_probe IS NOT TRUE`,
- `TrialModel.org_id == org_id`.

Final set = union(explicit `trial_ids` rows, task-sourced rows), deduped by id.
A task with no current version, or zero matching trials, is skipped and counted.

## Server changes

### `schemas.py` — `TrialCollectionRequest` (line 560)

- Add `task_ids: list[str] = Field(default_factory=list)`.
- Change `trial_ids` to default `[]` (was required).
- Model validator: dedupe both lists; require ≥1 across `trial_ids` + `task_ids`
  (else `ValueError`). Keep the non-empty `name` rule.
- `TrialCollectionResponse` (`:1045`) gains `trials_from_tasks: int = 0` and
  `tasks_skipped_empty: int = 0`.

### `collections.py` — `create_trial_collection_core` (line 11)

- New param `task_ids: list[str] | None = None`.
- For each task id/name (org-scoped) resolve `TaskModel`; not found → `HTTPException(404,
  "Task {id} not found")`. Collect `(id, current_version_id)` pairs; skip + count tasks
  with `current_version_id is None`.
- Query current-version terminal non-superseded non-probe trials for those pairs
  (`tuple_(...).in_(pairs)`), org-scoped.
- Merge with the existing `trial_ids` rows, dedupe by id, preserve order (explicit ids
  first, then task-sourced by `(task_id, created_at)`).
- Empty merged set → `HTTPException(400, "resulting trial set is empty")`.
- Existing linking (`experiment_trials` insert + `_link_task_to_experiment`) unchanged.
- Return the new counts.

### `backend/api/routers/tasks.py` — route (line 773)

- Pass `task_ids=payload.task_ids` through to `create_trial_collection_core`.
  (Everything else — auth, scope, commit, cache invalidation — unchanged.)

## CLI changes — new `cli/collect.py`

- `collect(tasks: --task/-t repeatable, trial_ids: optional positional, name: --name/-n,
  publish: --publish/--no-publish default True, json, api_url)`.
- Guard: require ≥1 task or ≥1 trial id, else friendly error + exit 1.
- POST `/experiments/collections` with `{name, task_ids, trial_ids}`.
- If `publish`: POST `/experiments/{id}/publish`; print the **public URL** with an
  explicit `"This is a public, read-only link"` line (outward-facing echo).
- Register `collect` in `cli/__init__.py`.

## Error handling / edge cases

- Task not found → 404 naming the task.
- Task with no current version / zero current-version terminal trials → skipped + counted;
  all-empty → 400.
- Overlap between `trial_ids` and task-sourced trials → deduped, linked once.
- Back-compat: `experiment create <trial_ids>` and the route's `trial_ids`-only callers
  behave exactly as today (task_ids defaults empty).

## Testing

- **Route** (`backend/tests/test_collections_route.py`, httpx + ASGITransport + Postgres):
  `task_ids` creates a collection from current-version trials; 404 unknown task; 400 empty.
- **Core** (`oddish/tests/test_create_trial_collection.py`): task selection links only
  current-version, non-superseded, terminal, non-probe trials (assert old-version /
  superseded / pending / probe excluded); union+dedupe with explicit `trial_ids`;
  empty-task skipped+counted; all-empty raises 400; `is_collection=True`.
- **Schema** (`oddish/tests/`): validator matrix — only-tasks / only-trials / both /
  neither; dedupe.
- **CLI** (`oddish/tests/test_cli_*`): `collect --task` payload; publish default prints
  public URL; `--no-publish` off; `--json` shape.

## Rollout / compatibility

- Additive schema fields + additive core param + one-line route passthrough → no breaking
  change. `combine`, `experiment create`, and the secondary server are untouched.
