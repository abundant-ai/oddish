# Collect-by-task: task-based collections via `oddish collect`

**Date:** 2026-07-01
**Status:** Approved (design)

## Problem

`oddish combine` builds a "collection" experiment by copying the finished trials of
two or more **source experiments**. It is experiment-granular: it cannot assemble a
collection from a curated set of trials, and it has no notion of "the latest trials
for a task's current version." Producing a per-task rollup today means manually
resolving, for each task, which experiment(s) produced its current-version trials and
passing those experiment IDs — and combine then drags in whatever *else* those
experiments hold.

We want to point at **tasks** and automatically get a new experiment containing the
latest trials for each task's latest version, published read-only for sharing.

## Goals

- Add a **task-based selection mode**: given task ids/names, build a collection from
  each task's current-version trials.
- **Rename** the CLI command `combine` → `collect` (keep a deprecated `combine` alias).
- **Publish by default** from the CLI (public read-only link), with `--no-publish` opt-out.
- Reuse the existing trial-copy machinery unchanged. Server remains the single source
  of truth for "current version."

## Non-goals (YAGNI)

- Arbitrary trial-id curation (`trial_ids` input). The task-mode covers the stated
  need; a general `trial_ids` primitive can be added later if a real use-case appears.
- Filtering to passing-only / most-recent-only / configurable selection. Decision:
  include **all** current-version, terminal, non-superseded trials per task.
- Renaming the server endpoint or removing `combine` outright.

## Selection semantics

For each requested task, include trials matching the **exact filter** that powers
`ls`'s `latest_trials` (`core/endpoints/tasks_query.py:1990`):

- `(TrialModel.task_id, TrialModel.task_version_id)` equals the task's
  `(task_id, current_version_id)` pair,
- `TrialModel.superseded_by_trial_id IS NULL`,
- `TrialModel.status IN {SUCCESS, FAILED}` (terminal only — mirrors combine),
- `TrialModel.is_probe IS NOT TRUE`,
- org-scoped when `org_id` is set.

The final `source_trials` set is the **union** of experiment-sourced trials (today's
behavior) and task-sourced trials, **deduped by trial id**, ordered by
`(task_id, created_at)` so per-task index allocation stays contiguous.

## Server changes

### `schemas.py` — `ExperimentCombineRequest` (line 506)

- Add `task_ids: list[str] = Field(default_factory=list)`.
- Add `publish: bool = Field(default=False)`. **API default stays False** so existing
  `combine` callers are byte-for-byte unaffected; the CLI is what defaults publish on.
- `_validate_sources` (`:540`) becomes **mode-aware**:
  - `task_ids` empty → keep today's rule: ≥2 distinct `source_experiment_ids`.
  - `task_ids` non-empty → require ≥1 total source (task or experiment).
  - Dedupe both `source_experiment_ids` and `task_ids`.
- `ExperimentCombineResponse` (`:1000`) gains:
  - `public_url: str | None` (set when `publish=True`),
  - `trials_from_tasks: int` and `tasks_skipped_empty: int` for transparency.

### `deletion.py` — `combine_experiments_core` (line 669)

- New params: `task_ids: Collection[str] = ()`, `publish: bool = False`.
- Resolve each task by id or name (org-scoped) → its `current_version_id`. A task not
  found → `HTTPException(404, "Task {id} not found")`.
- Select each task's current-version trials via the filter above.
- Build `source_trials` = union(experiment-sourced, task-sourced), deduped by id.
- Steps 5–6 (mint new trial ids/names, copy result fields, copy or reference
  artifacts) are **unchanged**.
- After `session.flush()`, if `publish`: call the **same publish routine the publish
  endpoint uses**, in the same transaction (atomic create+publish), and capture
  `public_url` into the response.
- Guard relaxation: allow a single source **only** in task-mode; experiment-only mode
  keeps the existing `len(resolved) < 2` → 400.

## CLI changes

### `cli/combine.py` → `cli/collect.py`

- Rename the callback `combine` → `collect`; register `app.command()(collect)` in
  `cli/__init__.py`.
- Add a thin **`combine` alias** command registered with `hidden=True` that prints a
  one-line deprecation warning (`"combine is deprecated; use collect"`) and delegates
  to `collect`.
- Arguments:
  - positional `source_experiment_ids` becomes **optional** (default `[]`),
  - add repeatable `--task` / `-t`,
  - both modes composable in a single call.
- Options: `--publish/--no-publish` defaulting **True**; keep `--name/-n`,
  `--copy-artifacts/--no-copy-artifacts`, `--json`, `--api-url/-u`.
- Client-side guard mirroring the server: require ≥1 task or ≥1 experiment, else a
  friendly error and exit 1.
- Payload adds `task_ids` and `publish`.
- Output: on publish, print the **public URL prominently** preceded by an explicit
  `"This is a public, read-only link"` line (outward-facing echo, since publish is now
  the default). `--json` returns the raw response including `public_url`.

## Error handling / edge cases

- **Task not found** → 404 naming the task (server), surfaced by CLI.
- **Task with zero current-version terminal trials** → skipped, counted in
  `tasks_skipped_empty`; only a hard error (400 `"resulting trial set is empty"`) when
  the *entire* resolved set is empty.
- **Overlap** (a trial reachable via both an experiment source and a task source) →
  deduped by id, copied once.
- **Single source** → legal only in task-mode; experiment-only keeps ≥2.
- **Back-compat** → `combine` with only `source_experiment_ids` and `publish=False`
  behaves exactly as today.

## Testing

- **Unit (schema):** validator matrix — only-tasks / only-exps / mixed / none /
  single-exp / single-task; dedupe of both lists.
- **Core (`combine_experiments_core`):**
  - task selection includes only current-version, non-superseded, terminal, non-probe
    trials — assert old-version, superseded, and pending trials are excluded;
  - union + dedupe across experiment- and task-sourced trials;
  - empty-task is skipped and counted; all-empty raises 400;
  - `publish=True` returns a working `public_url` and the experiment is published in
    the same transaction.
- **CLI:** `collect --task` builds the correct payload; `combine` alias prints the
  deprecation warning and still works; publish defaults on and `--no-publish` turns it
  off; `--json` shape includes `public_url`.
- **Back-compat:** existing combine tests pass unchanged (experiment-only mode).

## Rollout / compatibility

- Additive request fields + mode-aware validation → no breaking API change.
- CLI command rename is covered by the hidden `combine` alias; scripts keep working
  with a deprecation notice.
- Endpoint name `POST /experiments/combine` is unchanged.
