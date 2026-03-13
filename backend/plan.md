# Unified Job Dispatch Refactor Plan

## Goal

Refactor queue execution so **all job types** can run at high concurrency without
tying long-lived Modal workers to long-lived Postgres clients.

Job types in scope:

- `trial`
- `analysis`
- `verdict`

Today the hot path is:

1. `poll_queue()` decides to spawn workers.
2. Each `process_single_job()` worker acquires a queue slot.
3. Each worker opens a dedicated `pgqueuer` asyncpg connection and keeps it for
   the lifetime of the job.
4. Long-running handlers continue touching Postgres for heartbeats and ORM work.

That means bursty provider fan-out turns directly into bursty Supabase client
fan-out.

The refactor should make Postgres hot only during:

- enqueue
- dispatch reservation
- worker ack
- sparse heartbeat / reconciliation
- final success / failure writeback

## Scope Decision

Migrate **all job types** to the same reservation-backed scheduler in the first
cut.

Reasoning:

- it is simpler to operate one dispatch architecture than two
- it avoids split-brain cleanup logic between `pgqueuer` jobs and reserved jobs
- it keeps queue metrics, observability, and reconciliation uniform
- it lets `backend/worker/queue_manager.py` and the trial-only special cases be
  removed entirely instead of lingering as partial legacy code

This is a bigger migration than a trial-only cut, but the end state is cleaner.

## Core Design

Use a **leased reservation + worker ack** model for every job type.

Do **not** let the dispatcher perform a final hard claim up front.

Instead:

1. Dispatcher reserves a queued job for a short startup lease.
2. Dispatcher also reserves provider capacity for that job.
3. Dispatcher spawns a worker with the reservation token.
4. Worker starts and atomically acks the reservation.
5. Only after ack does the job become truly running.
6. If worker startup is slow or fails, the reservation expires and can be
   reclaimed safely.

This preserves correctness under:

- slow Modal cold starts
- overlapping poll cycles
- duplicate spawns
- worker crash before startup
- worker crash during execution

## Architectural Simplification

To keep all job types on one path, move scheduler/runtime state into a dedicated
table instead of scattering reservation fields across `TrialModel` and `TaskModel`.

Introduce a new table, tentatively:

- `dispatch_jobs`

This table becomes the scheduler source of truth for:

- queueing
- reservation
- running worker metadata
- retries
- heartbeat / cleanup

Domain models remain the product-facing source of truth for:

- trial results and status
- analysis results and status
- verdict results and status

In other words:

- `dispatch_jobs` owns execution mechanics
- `TrialModel` / `TaskModel` own business state

## Invariants

The implementation should maintain these invariants at all times:

1. At most one live worker can own a given reserved job.
2. At most one live worker can consume a queue slot for a given job.
3. A worker may only start if its reservation token still matches.
4. Expired reservations become dispatchable again without manual cleanup.
5. Stale running jobs release capacity without relying on a live `pgqueuer`
   connection.
6. Trial, analysis, and verdict execution must not require a long-lived queue DB
   connection.
7. Domain status fields remain derivable from the canonical dispatch job state.

## Proposed Job Model

Add a new SQLAlchemy model, tentatively `DispatchJobModel`, in
`oddish/src/oddish/db/models.py`.

Suggested columns:

- `id: str | uuid`
- `job_type: Literal["trial", "analysis", "verdict"]`
- `queue_key: str`
- `org_id: str | None`
- `task_id: str | None`
- `trial_id: str | None`
- `status`
- `priority`
- `attempts`
- `max_attempts`
- `dispatch_token`
- `dispatch_requested_at`
- `dispatch_expires_at`
- `worker_id`
- `queue_slot`
- `claimed_at`
- `heartbeat_at`
- `started_at`
- `finished_at`
- `next_retry_at`
- `last_error`
- `payload: jsonb | null`

Suggested status set:

- `queued`
- `reserved`
- `running`
- `retrying`
- `success`
- `failed`
- `canceled`

Why use a dedicated table:

- one set of reservation columns for all job types
- one cleanup path for all job types
- one dispatcher query shape for all job types
- one queue metrics source instead of mixing `pgqueuer`, `TrialModel`, and
  `TaskModel`

## Domain Model Mapping

The dispatch table is generic, but domain status updates stay type-specific.

Mapping:

- `trial` job maps to `TrialModel.status`
- `analysis` job maps to `TrialModel.analysis_status`
- `verdict` job maps to `TaskModel.verdict_status`

Recommended rules:

- enqueue creates a `dispatch_jobs` row and sets the matching domain status to
  `QUEUED`
- worker ack sets the matching domain status to `RUNNING`
- final success/failure syncs both `dispatch_jobs.status` and the domain status
- cleanup updates domain status only when the cleanup decision is terminal or retrying

Keep existing runtime correlation fields during rollout if useful:

- `TrialModel.current_worker_id`
- `TrialModel.current_queue_slot`
- `TrialModel.claimed_at`
- `TrialModel.heartbeat_at`
- `TaskModel` may gain parallel verdict runtime metadata if needed

But these become secondary diagnostics. `dispatch_jobs` is the canonical source.

## Data Model Changes

Primary files:

- `oddish/src/oddish/db/models.py`
- Alembic migration tree

Add:

- new `DispatchJobModel`
- indexes for claimable jobs by `status`, `queue_key`, `next_retry_at`
- indexes for stale-job scans via `status`, `heartbeat_at`, `dispatch_expires_at`
- indexes for lookups by `trial_id`, `task_id`, and `job_type`

Migration notes:

- create the table first without deleting `pgqueuer`
- keep backward compatibility during rollout
- do not remove existing runtime fields from domain models until the new path is stable

## Capacity Tracking

Keep `queue_slots` in phase 1, but move ownership from the worker-claim step to
the dispatcher reservation step for **all** job types.

That means:

- dispatcher reserves a queue slot and stamps the `dispatch_jobs` row in the
  same logical flow
- worker inherits the reserved slot instead of acquiring its own
- cleanup releases slots for expired reservations and stale runners

Why keep `queue_slots`:

- it already models per-queue/provider concurrency correctly
- it gives deterministic slot accounting for trials, analyses, and verdicts
- it avoids a larger concurrency rewrite during the first migration

Longer term, `queue_slots` can still be revisited, but it is not required for
the first structural fix.

## Dispatcher Flow

Primary file:

- `backend/worker/functions.py`

Replace the current spawn-only planner with a reservation dispatcher that reads
from `dispatch_jobs`.

New high-level flow for `poll_queue()`:

1. Acquire a global advisory lock so only one dispatcher run performs
   reservations at a time.
2. Cleanup expired reservations and stale running jobs before planning.
3. Compute per-queue capacity from:
   - configured concurrency limit
   - active `running` jobs
   - active `reserved` jobs
4. Reserve up to `MAX_WORKERS_PER_POLL` jobs across queue keys.
5. Commit reservations before spawning workers.
6. Spawn one worker per reservation.
7. If spawn fails synchronously, immediately release the reservation and slot.

Dispatcher reservation query requirements:

- only reserve `queued` or eligible `retrying` jobs
- skip rows with non-expired reservations
- atomically stamp:
  - `dispatch_token`
  - `dispatch_requested_at`
  - `dispatch_expires_at`
  - `worker_id`
  - `queue_slot`
  - `status = reserved`
- use `FOR UPDATE SKIP LOCKED` and/or an advisory lock per queue when needed

Suggested helper module:

- `backend/worker/reservations.py`

Suggested helpers:

- `reserve_jobs_for_dispatch(...)`
- `ack_reserved_job(...)`
- `release_expired_reservations(...)`
- `release_running_job_capacity(...)`
- `clear_job_reservation(...)`
- `finalize_job_success(...)`
- `finalize_job_failure(...)`

## Worker Flow

Primary files:

- `backend/worker/functions.py`
- `oddish/src/oddish/workers/queue/trial_handler.py`
- `oddish/src/oddish/workers/queue/analysis_handler.py`
- `oddish/src/oddish/workers/queue/verdict_handler.py`

Change worker signature from queue-only to reservation-specific input:

```python
process_single_job(
    job_id: str,
    dispatch_token: str,
    worker_id: str,
    queue_slot: int,
)
```

New worker startup flow:

1. Start container and configure runtime paths.
2. Load and atomically ack the reserved job:
   - match `job_id`
   - match `dispatch_token`
   - require `dispatch_expires_at > now()`
   - require status is still `reserved`
3. If ack fails, log and exit quietly.
4. On ack success:
   - set `dispatch_jobs.status = running`
   - set `claimed_at`
   - set `heartbeat_at`
   - clear `dispatch_expires_at`
   - sync the domain status to `RUNNING`
5. Route execution by `job_type`:
   - `trial` -> run trial by `trial_id`
   - `analysis` -> run analysis by `trial_id`
   - `verdict` -> run verdict by `task_id`
6. Update sparse worker heartbeats during execution.
7. Finalize success/failure and clear reservation metadata and queue slot.

Critical change:

- remove `create_single_job_queue_manager()` / `run_single_job_without_listener()`
  from the hot path entirely
- do not open a long-lived dedicated asyncpg queue connection per worker
- handlers should accept domain IDs / payload instead of a `pgqueuer` `Job`
  object where feasible

## Handler Refactor

### Trial handler

Refactor `run_trial_job(...)` so it can be called with a `trial_id` and runtime
metadata instead of a `pgqueuer` job wrapper.

Suggested new entrypoint:

```python
run_trial_job_by_id(
    trial_id: str,
    queue_key: str,
    worker_id: str,
    queue_slot: int,
)
```

### Analysis handler

Refactor `run_analysis_job(...)` to support:

```python
run_analysis_job_by_trial_id(
    trial_id: str,
    queue_key: str,
    worker_id: str,
    queue_slot: int,
)
```

### Verdict handler

Refactor `run_verdict_job(...)` to support:

```python
run_verdict_job_by_task_id(
    task_id: str,
    queue_key: str,
    worker_id: str,
    queue_slot: int,
)
```

Keep the old `pgqueuer` adapters only during rollout if needed, then delete them.

## Enqueue Path

Primary file:

- `oddish/src/oddish/queue.py`

Replace:

- `enqueue_trial()`
- `enqueue_analysis()`
- `enqueue_verdict()`

New behavior:

- create a `dispatch_jobs` row for every queued unit of work
- update the matching domain status immediately
- do not insert a `pgqueuer` row for any job type

Concrete mapping:

- `enqueue_trial()`:
  - insert `dispatch_jobs(job_type="trial", trial_id=..., task_id=..., queue_key=...)`
  - set `TrialModel.status = QUEUED`
- `enqueue_analysis()`:
  - insert `dispatch_jobs(job_type="analysis", trial_id=..., task_id=..., queue_key=...)`
  - set `TrialModel.analysis_status = QUEUED`
- `enqueue_verdict()`:
  - insert `dispatch_jobs(job_type="verdict", task_id=..., queue_key=...)`
  - set `TaskModel.verdict_status = QUEUED`

This becomes the only queueing mechanism.

## Cleanup / Reconciliation

Primary file:

- `backend/worker/cleanup.py`

Replace `pgqueuer`-based orphan detection with `dispatch_jobs`-based reconciliation.

New cleanup buckets:

- `reserved_expired`
- `reserved_missing_slot`
- `running_stale_heartbeat`
- `running_missing_slot`
- `running_missing_worker_metadata`
- `terminal_with_runtime_metadata`
- `domain_queued_without_dispatch_job`
- `domain_running_without_dispatch_job`

Cleanup actions:

1. If `reserved` and reservation expired:
   - clear reservation fields
   - release queue slot
   - move `dispatch_jobs.status` back to `queued`
   - leave domain status queued
2. If `running` and heartbeat stale:
   - release queue slot
   - move `dispatch_jobs.status` to `retrying` or `failed`
   - sync domain status accordingly
3. If terminal and still has runtime metadata:
   - clear runtime metadata
   - release queue slot
4. If a domain row is marked queued/running with no corresponding active
   dispatch job:
   - repair it from `dispatch_jobs` if possible
   - otherwise fail or reset according to job type and retry policy

Important behavioral change:

- do not fail a job merely because a startup reservation expired
- only fail when the worker had already acked and then violated runtime policy

## Retry Semantics

The reservation model should make retries explicit for all job types.

Recommended policy:

- reservation expires before worker ack:
  return to `queued`
- worker acks but dies before meaningful progress:
  move to `retrying`
- worker exceeds max attempts or deterministic fatal error:
  move to `failed`

Source of retry policy:

- trials can reuse `attempts`, `max_attempts`, and `next_retry_at`
- analyses and verdicts should get equivalent fields in `dispatch_jobs` even if
  the domain table only stores the public-facing status

## Queue Metrics and Dashboard Queries

Primary files likely touched:

- `oddish/src/oddish/queue.py`
- `backend/api/routers/dashboard.py`
- admin queue views

Current queue metrics mix:

- `pgqueuer`
- `TrialModel.status`
- `TrialModel.analysis_status`
- `TaskModel.verdict_status`

After migration, queue metrics should be computed from `dispatch_jobs` only.

Dashboard/admin should report:

- queued
- reserved
- running
- retrying
- failed
- stale

Domain status views should continue to use the domain tables for end-user
presentation, but operational queue health should come from `dispatch_jobs`.

## Observability

Every dispatcher and worker log line should include enough correlation data to
debug duplicate or stale work under concurrency.

Log prefix fields:

- `job_id`
- `job_type`
- `trial_id` if present
- `task_id` if present
- `queue_key`
- `dispatch_token`
- `worker_id`
- `queue_slot`
- `modal.current_input_id()` where available

Add metrics for:

- reservations_created
- reservations_expired
- reservation_ack_success
- reservation_ack_rejected
- spawn_failures
- running_heartbeat_stale
- slots_released_by_cleanup
- jobs_retried_by_type
- jobs_failed_by_type

## Rollout Plan

### Phase 0: Instrument and guardrails

Files:

- `backend/worker/functions.py`
- `backend/worker/cleanup.py`
- dashboard/admin queue files as needed

Tasks:

- keep current singleton dispatcher
- add stronger logging / counters
- confirm orphan cleanup is stable
- add feature flag for new backend

Suggested flag:

- `ODDISH_JOB_BACKEND=pgqueuer|dispatch_jobs`

### Phase 1: Schema and generic job model

Files:

- `oddish/src/oddish/db/models.py`
- active Alembic migration tree

Tasks:

- add `DispatchJobModel`
- add indexes
- add SQLAlchemy helpers / repository functions
- add tests for token matching and expiry behavior

### Phase 2: Reservation helpers and dispatcher

Files:

- new `backend/worker/reservations.py`
- `backend/worker/functions.py`
- `backend/worker/slots.py`

Tasks:

- implement generic reservation planning
- reserve jobs across all job types
- spawn workers with explicit `job_id` payloads
- keep old path behind feature flag until verified

### Phase 3: Worker ack and handler routing

Files:

- `backend/worker/functions.py`
- `oddish/src/oddish/workers/queue/trial_handler.py`
- `oddish/src/oddish/workers/queue/analysis_handler.py`
- `oddish/src/oddish/workers/queue/verdict_handler.py`

Tasks:

- implement generic worker ack
- route execution by `job_type`
- remove long-lived queue-manager dependency from the hot path
- keep adapter shims only if necessary during rollout

### Phase 4: Enqueue cutover for all job types

Files:

- `oddish/src/oddish/queue.py`
- any code that directly assumes `pgqueuer` rows exist

Tasks:

- stop creating `pgqueuer` rows for trials
- stop creating `pgqueuer` rows for analyses
- stop creating `pgqueuer` rows for verdicts
- create `dispatch_jobs` rows instead
- sync domain statuses from enqueue path

### Phase 5: Reconciliation hardening

Files:

- `backend/worker/cleanup.py`
- dashboard/admin query code

Tasks:

- remove `picked`-row-driven cleanup logic
- base stale detection on `dispatch_jobs`
- ensure queue slots always reconcile from `dispatch_jobs` and domain state
- ensure domain status repair is correct for all job types

### Phase 6: Queue metrics cutover

Files:

- `oddish/src/oddish/queue.py`
- `backend/api/routers/dashboard.py`
- admin views

Tasks:

- switch operational queue counts to `dispatch_jobs`
- preserve end-user-facing domain status displays
- add reserved-job visibility in dashboard/admin UI

### Phase 7: Remove legacy path

Files:

- `backend/worker/queue_manager.py`
- `backend/worker/functions.py`
- `oddish/src/oddish/queue.py`
- any obsolete `pgqueuer` helper code

Tasks:

- delete the trial/analysis/verdict `pgqueuer` worker path
- stop writing `current_pgqueuer_job_id`
- delete `pgqueuer` cleanup assumptions
- keep only the unified `dispatch_jobs` scheduler

## Testing Plan

### Unit tests

- reservation creation for queued job by type
- duplicate dispatcher polls do not double-reserve
- worker ack succeeds only for valid token
- worker ack fails after lease expiry
- cleanup releases expired reservations
- stale running job releases slot and transitions correctly
- domain status sync is correct for trial / analysis / verdict

### Integration tests

- slow worker startup after reservation
- duplicate worker spawn with same job
- overlapping poll cycles
- worker dies before ack
- worker dies after ack but before completion
- retry path after stale heartbeat
- queue slot reconciliation after crash
- analysis and verdict jobs coexist correctly with trial jobs on same queue key

### Load tests

Simulate:

- one hot provider at 64
- multiple hot providers at 64
- mixed trial + analysis + verdict load
- delayed Modal startups
- API load concurrent with active workers

Measure:

- Supabase client connections
- queue latency
- worker startup latency
- stale reservation rate
- retry correctness
- domain status lag versus dispatch job status

## Trade-Offs

Benefits:

- one scheduler model for all job types
- no split maintenance between `pgqueuer` and reservation-based jobs
- breaks the direct coupling between running work and live queue DB connections
- gives deterministic recovery for slow or failed worker startup
- turns burst failure mode from "connection storm" into "queued reservations"
- makes queue metrics and cleanup uniform

Costs:

- larger first migration
- one new central job table
- more explicit state-machine complexity
- more up-front queue/query refactoring
- one broader schema migration instead of a narrower trial-only cut

Accepted trade-off:

Prefer a single coherent scheduler refactor over a smaller trial-only migration
that leaves the system half on `pgqueuer` and half on reservations.

## Exit Criteria

The refactor is successful when all of the following are true:

1. No job worker holds a long-lived `pgqueuer` connection.
2. Trial, analysis, and verdict enqueue paths no longer depend on `pgqueuer`.
3. Dispatcher reservations recover automatically from slow startup and crash.
4. Queue slots are released deterministically after expiry or stale heartbeat.
5. Queue metrics come from one source of truth.
6. Supabase client connections during large mixed runs grow sublinearly relative
   to logical job concurrency.
7. Trial, analysis, and verdict final states remain correct under duplicate
   spawn and crash scenarios.

## Implementation Notes

- Migrate all job types together to avoid dual scheduler complexity.
- Prefer feature-flagged cutover over big-bang replacement.
- Use `dispatch_jobs` as the scheduler source of truth.
- Treat `pgqueuer` as legacy and remove it completely once the new path lands.
