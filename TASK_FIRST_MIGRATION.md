# Task-First Migration Plan

Reference: target model in `oddish/src/oddish/task_first_schema.py`.

## Goal

Flip the data model so **tasks (and their immutable versions) are the
first-class unit**, trials are pure evidence pinned to
`(task_version, agent)`, and experiments become saved selections + read-side
views over that evidence pool. Job replaces "experiment as runner" as the
ad-hoc execution batch.

## Cross-cutting decisions

These are baked into the plan and worth disagreeing with now if they're wrong.

1. **Agent equivalence key** = `sha256(harness | model | provider)`.
   Bundle identity is a separate axis, carried by `TaskVersion.content_hash`
   (the hash of the task bundle zip). Two trials are fungible evidence iff
   their `(task_version_id, agent_equivalence_key)` match. Harbor passthrough
   overrides at submission time are not part of the equivalence key — the
   bundle is the source of truth for what the agent saw.
2. **Cells are frozen at save; the experiment is editable.** Each cell
   stores a concrete `task_version_id` and never silently shifts. But the
   set of cells in an experiment is mutable — you can add cells, remove
   cells, bump `target_n_trials`, and grow the experiment over time. No
   living queries; no auto-resolution against new task versions.
3. **`WorkerJobKind` stays as today** (`TRIAL`, `ANALYSIS`, `VERDICT`,
   `TASK_EXPAND`). `TASK_EXPAND` is a derived-cache job that extracts the
   bundle tarball into a per-file s3 tree for FE rendering. `VERDICT` vs
   `ANALYSIS` consolidation is non-load-bearing; defer to a later cleanup.
4. **Task versions: dropping `updated_at` is semantic theatre.** Real
   write-once enforcement is the DB-level CHECK trigger / role revoke in
   P6. The `updated_at` drop in P0 is cosmetic, included to make the
   intent obvious in the schema.
5. **`JobKind` is reporting metadata, not branching logic.** Same
   execution path for `validation` / `experiment_backfill` / `ad_hoc`;
   the label only affects how jobs surface in the UI and CLI listings.
   Validation runs are not a special code path.

## Phase 0 — Foundation

Cheap, isolated, unblocks everything.

- [ ] Drop `task_versions.updated_at` (alembic migration).
- [ ] Decide and execute fate of `VERDICT` / `TASK_EXPAND`:
  - `VERDICT` → fold into `ANALYSIS` with a `scope` discriminator (trial vs task_version).
  - `TASK_EXPAND` → keep as internal kind for now, do not surface in new APIs.
- [ ] Add helper: `compute_agent_equivalence_key(harness, model, provider)`.

**Risk:** low. **Ships:** silently.

## Phase 1 — `Job` table + agent equivalence (no behaviour change)

Backend-only data shape work. Dual-writes; nothing reads new fields yet.

- [ ] Alembic: create `jobs` table — `id`, `kind` (`validation` | `experiment_backfill` | `ad_hoc`), `status`, `launched_by_user_id`, `launched_at`, `finished_at`, `triggered_by_experiment_id` (nullable, cosmetic), `org_id`.
- [ ] Alembic: add `trials.job_id` (nullable), `trials.agent_equivalence_key` (indexed).
- [ ] Alembic: composite index `(task_version_id, agent_equivalence_key)` on trials.
- [ ] Backfill script: one `Job` per existing experiment, `kind=ad_hoc`, `triggered_by_experiment_id = experiment.id`. Populate `trials.job_id`. Populate `trials.agent_equivalence_key`.
- [ ] `WorkerJobModel` gains a back-pointer to `job_id` so we can find "what's running for this Job".
- [ ] Trial-creation paths (`submit_trials_to_task`, `append_trials_to_task`, `initialize_trial_import`) start writing both `experiment_id` (legacy) and `job_id` (new).
- [ ] New API: `GET /jobs`, `GET /jobs/{id}`, `GET /jobs?status=running`. Read-only.

**Risk:** medium (backfill correctness). **Ships:** API gains `/jobs`, FE/CLI not yet changed.

## Phase 2 — Tasks become the front door

Pure UI / read-side. No schema break.

- [ ] FE: `/tasks` becomes the default landing page (replace `/dashboard` redirect).
- [ ] FE: new task index columns: validation status, latest version, agent coverage matrix sparkline, last activity.
- [ ] FE: task detail page restructure:
  - Version switcher across the top.
  - Per-version: bundle metadata, content_hash, message, validation outcome.
  - Per-version evidence matrix (rows: agents present, cols: aggregate stats — n_trials, mean reward, last run).
- [ ] API: `GET /tasks/{id}/versions/{version}/evidence` — returns the matrix.
- [ ] Backend: `ValidationOutcome` derivation. Either a `validation_outcomes` table (denormalized, refreshed on trial completion) or a JSON column on `task_versions` populated by an analysis job. Pick one, document on the table.
- [ ] CLI: `oddish tasks ls`, `oddish tasks show <id>` (versions + evidence).
- [ ] FE: `/jobs` page (read-only, lists in-flight + recent jobs grouped by kind, clickable through to per-cell progress).

**Risk:** low–medium. **Ships:** users land on tasks, see evidence per version.

## Phase 3 — Experiments become cell matrices

Backend swap of selection model + experiment detail view rewrite.

- [ ] Alembic: create `experiment_cells` table — `(experiment_id, task_version_id, agent_equivalence_key, target_n_trials)` plus denormalized agent struct (`harness`, `model`, `provider`).
- [ ] Backfill: for each row in `task_experiments`, pick `task.current_version_id` at migration time; for each distinct agent equivalence key seen in that experiment's trials, create a cell with `target_n_trials = count`.
- [ ] New read path: `GET /experiments/{id}/cells` returns `ResolvedExperimentCell` list — joins `experiment_cells` to `trials` on `(task_version_id, agent_equivalence_key)`.
- [ ] FE: experiment detail flips from "trials table" to **cell matrix**. Rows = task versions, cols = agents. Each cell shows `have / target`, gap, mean reward, last run. Drill-in opens the trial drawer (existing component, refilled from the cell query).
- [ ] FE: per-cell + per-experiment "backfill gaps" CTA (wires up in Phase 4).
- [ ] Keep `task_experiments` populated in parallel for readers that haven't migrated; flag for Phase 5 deletion.

**Risk:** medium-high (this is the load-bearing read swap). **Ships:** experiments behave like the new model from the user's POV.

## Phase 4 — Experiment creation flow

The new builder + backfill action.

- [ ] FE: experiment builder page — pick agents, pick task versions (filter by tag, validation status), set `target_n_trials` (global or per-cell), preview matrix with current evidence + computed gaps.
- [ ] API: `POST /experiments` accepts `{name, cells: [{task_version_id, agent, target_n_trials}]}`.
- [ ] API: `POST /experiments/{id}/backfill` enqueues a `Job` of kind `experiment_backfill` with cells matching current gaps. Returns `{job_id}`.
- [ ] API: `POST /experiments/{id}/cells`, `DELETE /experiments/{id}/cells/{cell_id}`, `PATCH /experiments/{id}/cells/{cell_id}` (target_n_trials only). Editing cells never mutates an existing cell's `task_version_id` — replacing a cell means delete + add.
- [ ] FE: cell-level edit UI on the experiment detail page — add cells (pick task version + agent + target), remove cells, bump targets. Adds and removes show up in the matrix immediately; backfill picks them up next run.
- [ ] CLI: `oddish experiments create -c spec.yaml`, `oddish experiments backfill <id>`, `oddish experiments add-cell <id> --task-version=... --agent=... --n=...`.

**Risk:** medium. **Ships:** users can author experiments in the UI for the first time. Sweep CLI continues to work, internally creates a Job + Experiment.

## Phase 5 — Trial stops belonging to an experiment

The decoupling. Highest blast radius.

- [ ] Make `trials.experiment_id` nullable.
- [ ] Stop writing `trials.experiment_id` on any new trial. All paths route through `Job`.
- [ ] Validation runs and ad-hoc submissions stop creating synthetic experiments. `oddish run` without `--experiment` creates a `Job(kind=ad_hoc)` and no experiment.
- [ ] Audit every reader that filters by `experiment_id` (FE, GitHub notifier, dashboards, public endpoints) and migrate to the cell-join read path.
- [ ] Bake one release.
- [ ] Drop `trials.experiment_id`.
- [ ] Drop `task_experiments`.
- [ ] Public sharing decision: experiments still publishable; additionally allow publishing a task version (its evidence matrix). Add `task_versions.is_public` / `public_token` if we want it. Decide before this phase ships.

**Risk:** high (read paths). **Ships:** the inversion is real. Trials live independently.

## Phase 6 — Cleanup

The rake-up phase.

- [ ] Drop `trials.status`, `trials.attempts`, `trials.current_worker_id`, `trials.claimed_at`, `trials.heartbeat_at`, `trials.next_retry_at`, `trials.queue_key`, `trials.stale_reaped_at`, etc. Authoritative state lives on `worker_jobs`.
- [ ] Update remaining UI/API readers that hit those columns to join through `worker_jobs`.
- [ ] Final pass: rename `/tasks/sweep` → `/jobs` (or alias) since it now creates a Job. Keep alias for one release.
- [ ] If `VERDICT` / `TASK_EXPAND` weren't folded in Phase 0, do it now.
- [ ] Add DB-level write protection on `task_versions` (CHECK trigger or revoke UPDATE from app role).
- [ ] Delete `oddish/src/oddish/task_first_schema.py` or convert to docstring-only reference.

**Risk:** low–medium per item. **Ships:** model is clean.

## Out of scope (deferred)

- Living-query experiment selections.
- Cross-org / cross-experiment evidence pooling.
- Replacing `worker_jobs` polymorphic queue with anything else.
- Migration of historical IMPORTED trials beyond the equivalence-key backfill.
- New auth / sharing primitives beyond extending public_token to task versions.

## Order of operations summary

```
P0  drop updated_at, fold VERDICT/TASK_EXPAND
P1  jobs table + agent_equivalence_key + dual-write
P2  /tasks front door + task detail rewrite + /jobs page
P3  experiment_cells + cell-matrix experiment view
P4  experiment builder + backfill action
P5  drop trials.experiment_id, drop task_experiments
P6  drop dead queue columns, lock immutability, rename surfaces
```

Phases 0–2 are independently shippable with no FE breaking changes.
Phase 3 is the user-visible flip. Phase 5 is the load-bearing schema break.
