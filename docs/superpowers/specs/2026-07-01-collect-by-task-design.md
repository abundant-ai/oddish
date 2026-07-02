# Collect-by-task: task-based read-only collections

**Date:** 2026-07-01
**Status:** Revised design (pivoted to the collections primitive) — command surface pending confirmation

## Context / why this was revised

The first draft extended `combine` (which **copies** trials + duplicates S3 artifacts).
While speccing the plan we found the codebase already ships a dedicated,
**reference-based** collections primitive (PR #552, relanded #556) that is a much
better fit for "a read-only collection":

- `create_trial_collection_core` (`core/endpoints/collections.py`) creates an
  `is_collection=True` experiment and **links** existing trials into it via
  `experiment_trials` / `task_experiments` — **no row copy, no artifact duplication**.
- `TrialCollectionRequest{name, trial_ids}` / `TrialCollectionResponse` (`schemas.py:560`,
  `:1045`).
- CLI `oddish experiment create --name … <trial_ids>` (`cli/experiment.py`), registered
  in `cli/__init__.py`.

Three gaps stand between that primitive and the goal:

1. **The HTTP route `POST /experiments/collections` is not registered** in
   `server/__init__.py` (the CLI POSTs to it, but nothing maps to
   `create_trial_collection_core`) — a shipped bug: `oddish experiment create` 404s.
2. It accepts **`trial_ids` only** — no task-based selection.
3. It does **not publish** — collections are created private.

## Problem

We want to point at **tasks** and automatically get a read-only collection experiment
containing the latest trials for each task's current version, published for sharing —
without copying trials (a collection is a *view*, not a snapshot).

## Goals

- **Register** the missing `POST /experiments/collections` route (fix the shipped gap).
- Add **task-based selection**: given task ids/names, link each task's current-version
  trials into the collection.
- **Publish by default** from the CLI (public read-only link), with `--no-publish`.
- Stay **reference-based** — reuse `create_trial_collection_core`; no trial/artifact copy.
- Server remains the single source of truth for "current version."

## Non-goals (YAGNI)

- Touching `combine` (it keeps its copy-based merge role, unchanged).
- Filtering to passing-only / most-recent-only. Decision: include **all** current-version,
  terminal, non-superseded, non-probe trials per task (matches `ls`'s `latest_trials`).
- Auto-refreshing a collection when new trials/versions appear (the linked set is fixed
  at creation; re-run to refresh).

## Open decision (needs user confirmation)

**Command surface.** Earlier the user asked to "rename `combine`→`collect` and add a
`--task` flag" — decided *before* the collections primitive was found. Options now:

- **(recommended) New `oddish collect` command** on the collections primitive:
  `oddish collect --task <task…> [<trial_id…>] --name … [--no-publish]`. `collect` denotes
  reference-based collections; `combine` keeps its copy-merge meaning. Clean split.
- **Extend `oddish experiment create`** with `--task` (keep `trial_ids` positional too).
  No new top-level command.

The spec below is written against **`oddish collect`** but the server/core changes are
identical either way; only the CLI wiring differs.

## Selection semantics

For each requested task, link trials matching the filter behind `ls`'s `latest_trials`
(`core/endpoints/tasks_query.py:1990`):

- `(TrialModel.task_id, TrialModel.task_version_id)` equals the task's
  `(task_id, current_version_id)`,
- `TrialModel.superseded_by_trial_id IS NULL`,
- `TrialModel.status IN {SUCCESS, FAILED}` (terminal only),
- `TrialModel.is_probe IS NOT TRUE`,
- org-scoped when `org_id` is set.

Final trial set = union(explicit `trial_ids`, task-sourced trials), deduped by id.

## Server changes

### `server/__init__.py` — register the route (bug fix)

Add, mirroring the `combine` route (`:572`):

```python
@api.post("/experiments/collections", response_model=TrialCollectionResponse)
async def create_trial_collection(
    payload: TrialCollectionRequest,
) -> TrialCollectionResponse:
    async with get_session() as session:
        return await create_trial_collection_core(
            session,
            name=payload.name,
            trial_ids=payload.trial_ids,
            task_ids=payload.task_ids,
            org_id=current_org_id(),
        )
```

(Import `create_trial_collection_core` + `TrialCollectionRequest`/`Response`; use the
same org-id resolution the other routes use.)

### `schemas.py` — `TrialCollectionRequest` (line 560)

- Add `task_ids: list[str] = Field(default_factory=list)`.
- Make `trial_ids` default to `[]` (was required).
- Validator: require ≥1 of `trial_ids` / `task_ids`; keep the non-empty `name` rule.
- `TrialCollectionResponse` (`:1045`) gains `trials_from_tasks: int` and
  `tasks_skipped_empty: int`.

### `collections.py` — `create_trial_collection_core` (line 11)

- New param `task_ids: list[str] = []`.
- Resolve each task by id or name (org-scoped) → `current_version_id`; task not found →
  `HTTPException(404, "Task {id} not found")`.
- Select each task's current-version trials via the filter above; a task yielding zero →
  skip, increment `tasks_skipped_empty`.
- Merge with explicit `trial_ids` (existing path), dedupe by id, preserve order.
- If the merged set is empty → `HTTPException(400, "resulting trial set is empty")`.
- The existing linking logic (`experiment_trials` insert + `_link_task_to_experiment`) is
  unchanged. Populate the new response counts.

### Publishing

Reuse the shipped publish endpoint `POST /experiments/{id}/publish`
(`core/sharing/helpers.py`). Publish-by-default is **CLI-orchestrated**: create the
collection, then call publish, then print the public URL. Keeps the server change to the
route + task selection; no publish logic duplicated server-side.

## CLI changes (`oddish collect`, new file `cli/collect.py`)

- Command `collect(tasks: --task/-t repeatable, trial_ids: optional positional,
  name: --name/-n, publish: --publish/--no-publish default True, json, api_url)`.
- Guard: require ≥1 task or ≥1 trial id, else friendly error + exit 1.
- POST `/experiments/collections` with `{name, task_ids, trial_ids}`.
- If `publish`: POST `/experiments/{id}/publish`; print the **public URL** with an
  explicit `"This is a public, read-only link"` line (outward-facing echo).
- Register in `cli/__init__.py`. Reuse the `experiment create` summary formatter.

## Error handling / edge cases

- Missing route → **fixed** by registration (was a 404 for all collection creation).
- Task not found → 404 naming the task.
- Task with zero current-version terminal trials → skipped + counted; all-empty → 400.
- Overlap between `trial_ids` and task-sourced trials → deduped, linked once.
- Back-compat: `experiment create <trial_ids>` (no `task_ids`, private) behaves as today,
  now that the route exists.

## Testing

- **Route:** `POST /experiments/collections` returns 200 (regression for the missing
  route); 404 on unknown trial/task; 400 on empty set.
- **Schema:** validator matrix — only-tasks / only-trials / both / neither; dedupe.
- **Core:** task selection links only current-version, non-superseded, terminal, non-probe
  trials (assert old-version / superseded / pending excluded); union+dedupe with explicit
  trial_ids; empty-task skipped+counted; all-empty raises 400; `is_collection=True`.
- **CLI:** `collect --task` payload; publish default on prints public URL; `--no-publish`
  off; `--json` shape.

## Rollout / compatibility

- Additive schema fields + a new route that fixes a shipped 404 → no breaking change.
- `combine` untouched. `experiment create` gains task support (or a new `collect` command,
  pending the command-surface decision above).
