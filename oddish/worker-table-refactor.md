# `worker_jobs` table refactor — progress + remaining steps

Operational status document for the unified `worker_jobs` migration.
Design source of truth: `.cursor/plans/unified_worker_jobs_table.plan.md`.
This file tracks what's actually shipped in-tree, what the deploy
looks like, and what's left.

---

## TL;DR

Every kind of compute work (trial / analysis / verdict) now runs as
a row in one table — `worker_jobs` — claimed by one SQL, dispatched
by one runner, reaped by one cleanup step. The plan's Phases A → E
are all implemented in-tree **as a single cutover** (the plan
anticipated a staged rollout; we compressed it). Domain tables
(`trials`, `tasks`) keep their status/heartbeat columns for
live-UI visibility; `worker_jobs` is authoritative for scheduling.

- In-tree: **done** (code, migrations, tests).
- Deploy: **not rolled out yet** — requires a coordinated
  deploy-order sequence (see [Deployment](#deployment)).
- Known residuals: see [Follow-ups](#follow-ups).

---

## Scope recap

Before:

- Three scheduling state machines on the domain tables —
  `trials.status`, `trials.analysis_status`, `tasks.verdict_status`.
- Three claim SQLs (`_CLAIM_TRIAL_SQL`, `_CLAIM_ANALYSIS_SQL`,
  `_CLAIM_VERDICT_SQL`) in `workers/queue/single_job.py`.
- Dispatcher unioned three queries to discover active queue keys.
- Cleanup had five passes: running-trials, stale-analysis,
  stale-verdict, terminal-trial-runtime-refs, orphaned-slots.
- Cancel path walked three sidecar state machines plus three
  Modal-fc-id columns.
- Adding a new kind meant adding columns, a claim SQL, a cleanup
  step, and a cancel branch — every single time.

After:

- One `worker_jobs` table with `kind`, `status`, `queue_key`,
  `subject_table`, `subject_id`, `attempts`, retry/heartbeat
  metadata, and `payload`.
- One `UPDATE ... FOR UPDATE OF wj SKIP LOCKED` claim SQL that
  handles TRIAL / ANALYSIS / VERDICT (and any future `kind`).
- One dispatcher scan (`discover_active_worker_job_queue_keys`).
- One stale-heartbeat sweep that branches only to mirror terminal
  state back onto the domain rows.
- One cancel `UPDATE` that covers every kind.
- Adding a new kind: write a handler class, register it. Done.

---

## Progress — what's in-tree

### Phase A — schema + model + registry

**Status: landed.** Self-contained, no behavior change by itself.

- `oddish/alembic/versions/a4b5c6d7e8f9_add_worker_jobs_table.py`
  creates the table + two Postgres enums + five indexes:
  - `idx_worker_jobs_claim` (partial, `status IN ('QUEUED', 'RETRYING')`)
  - `idx_worker_jobs_heartbeat` (partial, `status = 'RUNNING'`)
  - `idx_worker_jobs_subject`
  - `idx_worker_jobs_parent` (partial, `parent_job_id IS NOT NULL`)
  - `idx_worker_jobs_org` (partial, `org_id IS NOT NULL`)
- `oddish/src/oddish/db/models.py` adds `WorkerJobKind`,
  `WorkerJobStatus`, and `WorkerJobModel` (all the columns from the
  plan). Exported from `oddish.db`.
- `oddish/src/oddish/workers/jobs/registry.py`: `JobHandler`
  runtime-checkable `Protocol`, `JobOutcome` / `JobSuccess` /
  `JobFailure` frozen dataclasses with `JobOutcome.ok()` / `.fail()`
  ctors + an invariant enforcing exactly one of
  `success` / `failure` is set. `HANDLERS` dict,
  `register(handler)` (idempotent for same object, raises
  `HandlerAlreadyRegisteredError` on cross-handler collisions),
  `get_handler`, `clear_handlers`.
- Tests: `tests/test_worker_jobs_schema.py` (11 cases),
  `tests/test_worker_jobs_registry.py` (14 cases).

### Phase B — unified claim + dispatcher (initially alongside; now sole)

**Status: landed.**

- `oddish/src/oddish/workers/queue/worker_job_single_job.py`:
  - `_CLAIM_WORKER_JOB_SQL` with a LEFT JOIN onto trials/tasks and
    a per-user RUNNING-count subquery, gated by
    `kind::text = 'TRIAL'` so the JOIN is a no-op for other kinds
    (priority + FIFO).
  - `ClaimedWorkerJob` projection; includes `worker_id` /
    `queue_slot` / `modal_function_call_id` populated from the
    caller's own parameters (no second read).
  - `run_single_worker_job`: claim → dispatch to registered handler
    → `_record_outcome` transitions the row to SUCCESS / RETRYING /
    FAILED. Catches handler exceptions (retryable by default);
    propagates `asyncio.CancelledError`. Defensive guard against
    a buggy handler returning an invalid `JobOutcome`.
  - `heartbeat_worker_job(job_id, pending_failure_count=,
    pending_last_error=)` helper with failure-folding
    semantics matching the trial heartbeat.
  - `post_success_hooks: PostSuccessHooks` parameter so the
    backend can wire kind-specific notifications (see GitHub hook
    recovery below).
- `oddish/src/oddish/workers/queue/worker_job_dispatcher.py`:
  - `discover_active_worker_job_queue_keys()` — one query replacing
    the old three-way union.
  - `get_worker_job_queue_counts(queue_keys)` — shape matches
    legacy `{"queued", "picked"}` so downstream spawn code is
    unchanged.
  - `build_spawn_plan()` — round-robin across queue keys bounded
    by per-key concurrency × per-tick budget; inlined from the
    deleted `dispatch_planner`.
- Tests: `tests/test_worker_jobs_runner.py` (19 cases — enqueue
  row shape, validation delegation, claim-SQL invariants, outcome
  recording, cancellation propagation, missing-handler fallback,
  malformed-outcome coercion).

### Phase C — dual-write (compressed into cutover)

**Status: not shipped as a standalone step.** The plan called for
dual-writing trial rows to `worker_jobs` while the legacy dispatcher
still claimed from `trials`. We compressed this into the cutover.

The pieces that would have been Phase C are now the enqueue-site
updates in Phase D below.

### Phase D — cutover (handlers + enqueue sites + cancel + cleanup + backend)

**Status: landed.**

#### Handlers (`oddish/src/oddish/workers/jobs/handlers.py`)

Three thin `JobHandler` adapters:

- `TrialJobHandler` — delegates to `run_trial_job`, then reads
  `trials.status` to decide outcome. Passes `worker_job_id` through
  so the heartbeat loop updates both tables (see [heartbeat
  fix](#critical-bug-fixed-during-review)).
- `AnalysisJobHandler` — delegates to `run_analysis_job`. Flips a
  stale terminal `trials.analysis_status` back to `QUEUED` before
  the call so the underlying function's idempotency guard doesn't
  short-circuit a legitimate retry.
- `VerdictJobHandler` — same pattern against `run_verdict_job` and
  `tasks.verdict_status`.

All three registered via `ensure_builtin_handlers_registered()`.
The deferred-registration pattern (not an import-time side effect)
exists to avoid the circular import knot between `oddish.queue`
(which imports enqueue helpers) and the handlers module (which
imports modules that eventually import `oddish.queue`).

#### Enqueue sites (`oddish/src/oddish/queue.py`)

Every site that previously created a domain-row scheduling state now
also inserts a `worker_jobs` row in the same transaction:

- `create_task` — inserts `TRIAL` rows alongside each `TrialModel`.
- `append_trials_to_task` — same. Also issues a `CANCELLED` update
  for any in-flight VERDICT `worker_job` for the parent task, so a
  worker already running the verdict against the old trial set can't
  clobber the new one (see [found during review](#data-integrity-hole-verdict-on-append)).
- `maybe_start_analysis_stage` — when it transitions to
  `VERDICT_PENDING`, also enqueues a `VERDICT` row.
- `maybe_start_verdict_stage` — enqueues `VERDICT` on transition.
- `trial_handler._store_trial_results` — when it sets
  `analysis_status=QUEUED`, also enqueues `ANALYSIS`.

Retry API paths in `oddish/src/oddish/core/endpoints.py` —
`retry_trial_core`, `rerun_trial_analysis_core`,
`rerun_task_analysis_core`, `rerun_task_verdict_core` — each
enqueues a fresh `worker_jobs` row.

Helpers live as underscore-prefixed functions in `queue.py`
(`_enqueue_trial_worker_job`, `_enqueue_analysis_worker_job`,
`_enqueue_verdict_worker_job`). Lazy-imported at the retry-API
callsites to keep the import graph acyclic.

#### Cancel path (`oddish/src/oddish/queue.py::cancel_tasks_runs`)

One `UPDATE worker_jobs SET status = 'CANCELLED' ... RETURNING
modal_function_call_id, kind, subject_id` covers every kind.
Domain rows (`trials` / `tasks`) are mirrored back to
`FAILED` + `"Cancelled by user"` for live UI. Harvested Modal
function-call ids are returned so the caller can terminate remote
containers.

#### Cleanup (`oddish/src/oddish/workers/queue/cleanup.py`)

Five-step sweep collapsed to:

1. Zombie 'idle in transaction' reaper (unchanged — already
   kind-agnostic).
2. Stale-heartbeat sweep on `worker_jobs`: one `UPDATE ... WHERE
   status='RUNNING' AND heartbeat_at < NOW() - make_interval(...)`
   that transitions to `RETRYING` (attempts remain) or `FAILED`
   (exhausted) via a `CASE`. Per-kind mirror-back onto
   `trials`/`tasks` follows, bounded to just the reaped rows. RETRYING
   mirror-back sets `analysis_status=QUEUED` / `verdict_status=QUEUED`
   so the UI shows "queued for retry" rather than lingering on
   RUNNING.
3. Stage-transition safety net (tasks-ready-for-analysis /
   tasks-ready-for-verdict) — unchanged semantics, still there as a
   backstop in case a handler-commit stage-transition flush fails.
4. VERDICT_PENDING tasks with no queued verdict_status are
   re-enqueued (calls `_enqueue_verdict_worker_job`).
5. Terminal-trial runtime-ref cleanup + orphaned queue-slot release
   (rewritten against `worker_jobs`-style invariants).

Returned counts: `{worker_jobs_retried, worker_jobs_failed,
tasks_progressed_to_analysis, tasks_progressed_to_verdict,
verdict_pending_completed, terminal_trial_runtime_refs_cleared,
orphaned_active_slots_cleared, zombie_txn_reaped}`.

#### Backend (`backend/worker/functions.py`)

- Single `process_single_job(queue_key)` Modal function —
  kind-agnostic. Acquires a `queue_slots` lease, calls
  `run_single_worker_job`, releases. Shares the concurrency lease
  table with everything else, so per-queue-key concurrency limits
  still apply globally.
- `poll_queue()` scans `worker_jobs` only; no dual dispatcher, no
  legacy `dispatch_planner` import.
- `ensure_builtin_handlers_registered()` at module load so every
  spun-up container has TRIAL / ANALYSIS / VERDICT wired up before
  any claim.
- `_POST_SUCCESS_HOOKS = {TRIAL: notify_github_trial, ANALYSIS:
  notify_github_analysis, VERDICT: notify_github_verdict}` passed
  through `run_single_worker_job`, preserving the GitHub
  notification behavior that the legacy `on_*_complete` hooks had.

### Phase E — dead-code deletion

**Status: landed.**

Deleted:

- `oddish/src/oddish/workers/queue/single_job.py` (`ClaimedJob`,
  `_CLAIM_TRIAL_SQL`, `_CLAIM_ANALYSIS_SQL`, `_CLAIM_VERDICT_SQL`,
  `run_single_job`, `_dispatch_claimed_job`).
- `oddish/src/oddish/workers/queue/dispatch_planner.py`
  (`discover_active_queue_keys`, `get_queue_counts`,
  `build_spawn_plan` — last moved into `worker_job_dispatcher.py`).
- `oddish/tests/test_queue_reconciliation.py` — tested modules
  above.

Kept per plan (domain-state columns, not removed):

- `trials.status`, `analysis_status`, `verdict_status` (tasks) —
  Harbor lifecycle hooks write through them during runs for the
  live UI.
- `trials.heartbeat_at`, `trials.current_worker_id`, etc. — kept
  on domain tables as denorms. Heartbeat loop now writes to
  **both** `trials.heartbeat_at` and `worker_jobs.heartbeat_at`.
  Removing the trial-side columns is a separate follow-up.

### Migrations

1. `a4b5c6d7e8f9_add_worker_jobs_table.py` — schema.
2. `b5c6d7e8f9a0_backfill_worker_jobs_from_domain_rows.py` —
   seeds `worker_jobs` rows for every non-terminal `trials.status`,
   `trials.analysis_status`, `tasks.verdict_status` at deploy time.
   Deterministic `md5()` ids + `NOT EXISTS` guard → idempotent.
   `downgrade()` deletes only the backfill-prefixed rows.

### Frontend

- `backend/api/routers/admin.py`: new `GET /admin/worker-jobs`.
- `oddish/core/admin.py::get_worker_jobs_admin_core`: returns
  counts matrix `{kind: {status: int}}`, stale-RUNNING samples,
  recent failures/cancels, per-kind×queue_key p50/p95 durations.
- `frontend/src/app/api/admin/worker-jobs/route.ts` — Clerk-authed
  proxy.
- `frontend/src/lib/types.ts` — `WorkerJobKind`, `WorkerJobStatus`,
  `WorkerJobSample`, `WorkerJobDurationStat`, `WorkerJobsResponse`.
  Kinds/statuses are `"TRIAL" | ... | (string & {})` so new kinds
  from the backend don't break type-check.
- `frontend/src/components/worker-jobs-card.tsx` — the main UI:
  - Kind × status matrix (TRIAL, ANALYSIS, VERDICT, QA_REVIEW,
    unknown) with per-kind icons and descriptions.
  - Stale-RUNNING table with attempts, heartbeat age, failure
    tooltips.
  - Recent failures/cancels with error tooltip.
  - Duration percentiles.
  - 10s refresh, search filter.
- `frontend/src/app/(app)/admin/page.tsx` — new **Worker Jobs**
  tab is the default, with `OrphanedStateCard` moved under it.
- `frontend/src/components/experiment-trials-table.tsx` —
  `VerdictIndicator` replaced with `TaskPipelineStatus` pill
  (analyses X/Y · verdict icon) so trajectory analysis and task
  verdict feel like first-class agent jobs next to each task name.

### Critical bug fixed during review

`trial_handler._heartbeat_trial_execution` only wrote to
`trials.heartbeat_at`. Cleanup reaps based on
`worker_jobs.heartbeat_at`. Any trial running longer than 15
minutes (Harbor trials run up to 12 hours) would have been falsely
reaped.

Fix threaded `worker_job_id` through `run_trial_job` →
`_execute_trial` → `_heartbeat_trial_execution`, and added a
second write via `heartbeat_worker_job(job_id, ...)` each tick.
Two regression tests guard this in
`tests/test_heartbeat_diagnostics.py`.

### Data-integrity hole: verdict on append

`append_trials_to_task` reset `task.verdict_status = None` on append
but didn't touch the in-flight `VERDICT` `worker_job`. A worker
already claimed on it would complete and write stale synthesis over
the new trial set. Fixed with a `CANCELLED` `UPDATE` in the same
transaction.

### Test status

`uv run pytest --ignore=tests/test_queue_metadata.py`: **87
passing**. The one ignored file predates this refactor and fails
because it imports a no-longer-existing `oddish.api` module.

New test files:

- `test_worker_jobs_schema.py` — metadata assertions on the model.
- `test_worker_jobs_registry.py` — registry / Protocol conformance /
  `JobOutcome` invariants.
- `test_worker_jobs_runner.py` — claim-SQL invariants + dispatch
  behavior with stubbed claim/record.
- `test_worker_jobs_handlers.py` — handler glue layer (success,
  retryable failure, retry-reset, missing row).
- `test_heartbeat_diagnostics.py` — extended with two new tests
  that would catch the reintroduction of the heartbeat-only-writes-
  trials bug.

Frontend `tsc --noEmit`: clean on all touched files. The two stale
errors in `.next/types/app/(app)/usage/...` predate this refactor.

---

## Deployment

### Order matters

1. **Apply Alembic migrations in `oddish/`**:
   ```bash
   cd oddish
   uv run alembic upgrade head
   ```
   This runs both `a4b5c6d7e8f9` (schema) and `b5c6d7e8f9a0`
   (backfill). Backfill inserts a row per non-terminal trial,
   analysis, and verdict, so long-queued work migrates over.

2. **Apply any `backend/` migrations** (no new ones from this
   refactor, but keep this step in your runbook):
   ```bash
   cd backend
   uv run alembic upgrade head
   ```

3. **Deploy backend (Modal)**. The new `poll_queue` reads only
   `worker_jobs`; without step 1 it would find no rows to claim and
   all new work would stall. Step 1 first.

4. **Deploy frontend**. The new admin tab hits
   `/admin/worker-jobs`, which returns 404 on old backends — users
   see a friendly alert, not a crash.

### Order rationale

- Schema must exist before backend can read/write.
- Backfill must run before new dispatcher starts, otherwise
  in-flight work is stranded.
- Frontend is last because it's the only piece that degrades
  gracefully — the other two don't.

### Deployment risk

- **Schema deploys always add (enums, table, indexes) — no column
  drops**. Old backend processes still running during the deploy
  window keep working against `trials` / `tasks` as before; they
  just don't know about `worker_jobs`. Once the new backend takes
  over, the in-flight `trials.status='RUNNING'` from the old
  backend will stay there; the new backend doesn't claim from
  trials.status anymore, so those trials need either (a) the
  backfill to have given them a `worker_jobs` row (it does, they
  were RUNNING) or (b) stale-heartbeat cleanup to reap them. Both
  paths work.
- **Backfill uses `NOT EXISTS` + deterministic ids** — safe to
  re-run.

### Pre-flight checklist

- [ ] Verify migrations apply cleanly in staging:
  `uv run alembic upgrade head` in both `oddish/` and `backend/`.
- [ ] Verify admin endpoint responds:
  `curl $BACKEND/admin/worker-jobs -H "Authorization: Bearer ..."`.
- [ ] Verify `poll_queue` picks up a test task end-to-end in
  staging, including analysis + verdict.
- [ ] Verify user-initiated retry works end-to-end (a new
  `worker_jobs` row appears and claims).
- [ ] Verify cancel works end-to-end (the worker_jobs row goes to
  `CANCELLED`, the Modal function call is terminated, domain
  tables mirror).
- [ ] Verify a running trial longer than 15 minutes does NOT get
  reaped (heartbeat-to-worker_jobs bug regression).

### Rollback

Phases A–D are individually revertable per the plan. After this
combined deploy:

1. **Code revert** (`git revert <commit>`): restores the legacy
   `single_job.py` / `dispatch_planner.py`. The legacy dispatcher
   reads `trials.status` / `analysis_status` / `verdict_status`
   which we kept as domain-state columns, so nothing is lost. The
   enqueue sites that now insert `worker_jobs` rows stop inserting
   them, and the legacy claim path works.
2. **Migrations can stay**. The backfill rows become inert; the
   `worker_jobs` table just sits there. If you truly want to drop
   them:
   ```bash
   uv run alembic downgrade b5c6d7e8f9a0  # undoes backfill
   uv run alembic downgrade a4b5c6d7e8f9  # drops the table
   ```
   But you normally don't need to — an unused table is cheaper
   than a re-deploy risk.

---

## Follow-ups

### Short — should land within a week of deploy

1. **Monitor the stale-reap sweep post-deploy.** The new cleanup
   query runs against a smaller table than the legacy three-way
   union, but still hits `heartbeat_at`-indexed rows. Watch
   `metric=worker_jobs_retried` / `metric=worker_jobs_failed`
   trends — a spike means either a pooler issue or a bug in the
   new heartbeat path.
2. **Admin page — make `stale_after_minutes` configurable.**
   The backend endpoint takes the param; the frontend hardcodes
   the default. A small dropdown would let ops investigate at
   other thresholds without round-tripping through curl.
3. **`retry_trial_core` leaves the old "stuck RUNNING" worker_job
   alive.** If a user retries a stuck trial, the new attempt and
   the old stuck-but-still-formally-RUNNING row coexist. Not a
   correctness issue (both converge to `trials.status` last-write-
   wins, and stale-reap kills the old one eventually), but logs are
   noisy. Fix: cancel the old row in the same txn as the new
   enqueue, same as `append_trials_to_task` does for verdicts.

### Medium — nice, not urgent

4. **Boundary race on analysis.** `ANALYSIS_TIMEOUT` is 900s =
   15min, which is exactly `STALE_HEARTBEAT_MINUTES`. A 15m05s
   analysis could in principle be reaped mid-run. Unlike trials
   there's no periodic heartbeat for analyses. Two options:
   (a) emit a periodic worker_jobs heartbeat from inside
   `run_analysis_job`, (b) drop `ANALYSIS_TIMEOUT` to ≤ 12 minutes
   so the handler self-terminates well before the reap threshold.
5. **Remove `trials` claim-metadata columns.** Per the plan's
   Phase E, `trials.claimed_at` / `current_worker_id` /
   `current_queue_slot` / `modal_function_call_id` are scheduling-
   state columns whose source-of-truth is now `worker_jobs`. We
   kept them as denorms for display. If we commit to never
   displaying them from `trials`, we can drop them to cut row
   width. Same for `trials.heartbeat_at`, `heartbeat_failure_count`,
   `last_heartbeat_error`, `last_heartbeat_error_at` — they're
   now maintained on both tables; `worker_jobs` is authoritative.
6. **One-row-per-attempt mode.** The plan favors inserting a new
   `worker_jobs` row on every retry (immutable history). We
   shipped the "update in place" mode for simplicity. Switching is
   mechanical: change `_record_outcome` RETRYING-branch to
   INSERT + UPDATE old; change the claim SQL to filter only
   `QUEUED`. History becomes trivially auditable.
7. **Priority / `available_after` UX.** The schema supports both;
   nothing in the codebase uses priority (v1 defaults to 0). If we
   expose "run this interactive QA job ahead of the batch" as a
   product feature, both are ready.

### Long — new product surface enabled by this refactor

8. **QA-review kind.** Adding `QA_REVIEW` is now a PR that:
   (a) adds an enum value, (b) writes a `QAReviewJobHandler`,
   (c) calls `register(QAReviewJobHandler())`, (d) enqueues rows
   from wherever QA should trigger (probably off `ANALYSIS`
   success). No schema change, no dispatcher change, no cleanup
   step, no cancel branch.
9. **User-facing Jobs page.** The admin card already surfaces
   every kind by status. Promoting the same view into the main
   dashboard (filtered by org) gives users a "what's waiting" view
   across analysis + verdict + any future kind without needing
   per-kind UI.
10. **Dependency resolution in v2.** `parent_job_id` exists on
    the schema; v1 does stage-transitions via application-level
    helpers (`maybe_start_verdict_stage` etc.). A generic
    "advance any job whose parents are all done" sweep could
    replace those helpers.

---

## Source-of-truth invariants (enforced going forward)

Two code-review rules apply from this deploy onwards:

1. **No new read of `trials.status` / `analysis_status` /
   `verdict_status` for scheduling decisions.** Display reads are
   fine. A "should I enqueue the next stage?" check goes through
   `worker_jobs` (via a stage-transition helper), not domain state.
   Grep rule in CI is the simplest enforcement
   (`rg --glob '*.py' 'trials\.status'` outside
   `dashboard*.py` / `core/helpers.py`).
2. **Handlers never cross-reference the queue table beyond their
   own job row.** A trial handler that wants to know "is my
   corresponding analysis done" looks at the domain-table denorm
   (`trials.analysis_status`), or it enqueues a follow-up job. It
   does NOT run `SELECT status FROM worker_jobs WHERE subject_id =
   ...`. That's the pattern that turned the original design into
   pasta.

---

## Key files

| File | Purpose |
|------|---------|
| `.cursor/plans/unified_worker_jobs_table.plan.md` | Design (read this first) |
| `oddish/alembic/versions/a4b5c6d7e8f9_...` | Schema |
| `oddish/alembic/versions/b5c6d7e8f9a0_...` | Backfill |
| `oddish/src/oddish/db/models.py` | `WorkerJobModel`, enums |
| `oddish/src/oddish/workers/jobs/registry.py` | `JobHandler`, `JobOutcome`, `HANDLERS` |
| `oddish/src/oddish/workers/jobs/enqueue.py` | `enqueue_worker_job` |
| `oddish/src/oddish/workers/jobs/handlers.py` | Trial/Analysis/Verdict adapters |
| `oddish/src/oddish/workers/queue/worker_job_single_job.py` | Unified claim SQL + runner |
| `oddish/src/oddish/workers/queue/worker_job_dispatcher.py` | Discover + counts + spawn plan |
| `oddish/src/oddish/workers/queue/cleanup.py` | Unified stale-reap + stage recon |
| `oddish/src/oddish/queue.py` | Enqueue helpers + cancel |
| `oddish/src/oddish/core/admin.py` | `get_worker_jobs_admin_core` |
| `backend/api/routers/admin.py` | `GET /admin/worker-jobs` |
| `backend/worker/functions.py` | Modal `process_single_job` + `poll_queue` |
| `frontend/src/components/worker-jobs-card.tsx` | Admin UI |
| `frontend/src/components/experiment-trials-table.tsx` | `TaskPipelineStatus` pill |
