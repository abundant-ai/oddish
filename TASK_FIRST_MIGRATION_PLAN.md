# Task-First Migration Plan

This plan moves Oddish from experiment-owned runs to task-version-owned
evidence. The target shape is documented in
`oddish/src/oddish/task_first_schema.py`.

## Target Model

`Task` is the stable top-level object. `TaskVersion` is an immutable task
bundle snapshot. `Trial` is terminal evidence pinned to
`(task_version_id, agent_equivalence_key)`. `Experiment` is an editable saved
matrix of desired cells. `Job` is a user-visible execution batch. `WorkerJob`
remains the low-level queue/runtime row.

The central invariant is:

```text
fungible evidence = same task_version_id + same agent_equivalence_key
agent_equivalence_key = sha256(harness | model | provider)
```

Harbor passthrough overrides are not part of the equivalence key. Bundle
identity is represented by `TaskVersion.content_hash`.

## Phase 0 - Foundation

Low-risk changes that make later phases explicit without changing behavior.

- Add `oddish.core.agent_identity.compute_agent_equivalence_key`.
- Add the target schema reference module.
- Keep `WorkerJobKind` as-is for now: `TRIAL`, `ANALYSIS`, `VERDICT`,
  `TASK_EXPAND`. `TASK_EXPAND` is internal derived-cache work.
- Decide but do not yet execute the later `VERDICT`/`ANALYSIS` consolidation.
- Drop `task_versions.updated_at` only if the ORM and migration chain can do it
  without breaking legacy migrations. Write-once enforcement happens later.

Ships silently.

## Phase 1 - Jobs and Evidence Keys

Add the new batch/evidence shape while preserving all current readers.

- Create `jobs` table with `id`, `kind`, `status`, `launched_by_user_id`,
  `launched_at`, `finished_at`, `triggered_by_experiment_id`, and `org_id`.
- Create `job_cells` table with `job_id`, `task_version_id`,
  `agent_equivalence_key`, denormalized `harness`, `model`, `provider`, and
  `n_trials`.
- Add nullable `trials.job_id`, `trials.worker_job_id`, and
  `trials.agent_equivalence_key`.
- Add `worker_jobs.job_id`.
- Add indexes on `trials(agent_equivalence_key)` and
  `trials(task_version_id, agent_equivalence_key)`.
- Backfill one `Job(kind='ad_hoc')` per existing experiment, create job cells
  from distinct `(task_version_id, agent)` groups, populate `trials.job_id`,
  `trials.worker_job_id` where inferable, and populate
  `trials.agent_equivalence_key`.
- Update `create_task`, `append_trials_to_task`, and trial import paths to
  dual-write `job_id` and `agent_equivalence_key` while still writing
  `experiment_id`.
- Add read-only `GET /jobs`, `GET /jobs/{id}`, and status filtering.

Behavior remains unchanged. The goal is observability and safe backfill.

## Phase 2 - Tasks Become the Front Door

Switch read surfaces toward task versions and evidence without breaking
experiments.

- Add `GET /tasks/{id}/versions/{version}/evidence`, grouped by
  `agent_equivalence_key`.
- Pick validation storage: either a `validation_outcomes` table refreshed on
  completion or a `task_versions.validation_outcome` JSON column. Prefer the
  table if validation must be queryable/filterable.
- Make `/tasks` the default authenticated landing page.
- Show version metadata, content hash, validation outcome, agent coverage, trial
  counts, mean reward, and last activity.
- Add `oddish tasks ls` and `oddish tasks show <id>`.
- Add a read-only `/jobs` page.

This phase changes read-side emphasis only.

## Phase 3 - Experiments Become Cell Matrices

Introduce saved experiment selections and resolve them against the evidence
pool.

- Create `experiment_cells` with `experiment_id`, `task_version_id`,
  `agent_equivalence_key`, denormalized `harness`, `model`, `provider`, and
  `target_n_trials`.
- Backfill cells from `task_experiments` plus existing trials. For each
  experiment, create one cell per distinct
  `(task_version_id, agent_equivalence_key)` and set `target_n_trials` to the
  historical trial count.
- Add `GET /experiments/{id}/cells`, returning resolved cells and matching
  trials by `(task_version_id, agent_equivalence_key)`.
- Rewrite experiment detail as a matrix: rows are task versions, columns are
  agents, cells show `have / target`, gap, mean reward, and last run.
- Keep `task_experiments` and `trials.experiment_id` populated for legacy
  readers.

This is the main read-path swap.

## Phase 4 - Experiment Authoring and Backfill

Make experiments editable saved selections and use jobs for execution.

- Add `POST /experiments` accepting `{name, cells: [...]}`.
- Add `POST /experiments/{id}/backfill`, which computes current gaps, creates a
  `Job(kind='experiment_backfill')`, creates `job_cells`, and enqueues matching
  worker jobs.
- Add cell mutation endpoints: add, delete, and patch `target_n_trials`.
- Ensure cell edits never mutate `task_version_id`; replacing a version means
  delete plus add.
- Add the experiment builder UI: select task versions, select agents, set
  targets, preview existing evidence and computed gaps.
- Add CLI commands for experiment creation, backfill, and cell edits.

The old sweep CLI can continue to work by creating a Job and, where needed, a
compatibility Experiment.

## Phase 5 - Trials Stop Belonging to Experiments

Remove experiment ownership from new trial writes after all readers use cells.

- Make `trials.experiment_id` nullable.
- Stop writing `trials.experiment_id` for new trials.
- Stop creating synthetic experiments for validation and ad-hoc submissions.
- Make `oddish run` without `--experiment` create `Job(kind='ad_hoc')` only.
- Audit and migrate every reader filtering by `experiment_id`: frontend,
  dashboard, public sharing, GitHub notifications, CLI status/delete/pull.
- Bake one release with nullable compatibility.
- Drop `trials.experiment_id`.
- Drop `task_experiments`.
- Decide whether task versions can be publicly shared, and if so add
  `task_versions.is_public` and `task_versions.public_token`.

This is the highest-risk phase because it removes the old ownership model.

## Phase 6 - Runtime Cleanup

Remove duplicated scheduling state from domain rows.

- Drop trial scheduling columns that are now authoritative on `worker_jobs`:
  status, attempts, current worker, claim, heartbeat, retry, queue key, and stale
  reap fields.
- Update remaining readers to join through `worker_jobs`.
- Rename or alias `/tasks/sweep` to a job creation endpoint for one release.
- Fold `VERDICT` into `ANALYSIS` only if the separate kind no longer buys
  operational clarity.
- Add DB-level write protection for `task_versions`.
- Remove or reduce `task_first_schema.py` once the real schema is the source of
  truth.

## Deferred

- Living-query experiment selections.
- Cross-org evidence pooling.
- Replacing the polymorphic `worker_jobs` queue.
- Deep migration of historical imported artifacts beyond evidence-key backfill.
- New sharing primitives beyond optional task-version public tokens.
