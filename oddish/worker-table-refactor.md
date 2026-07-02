# `worker_jobs` refactor

Historical note for the unified `worker_jobs` cutover. Do not use this file as
an operational runbook.

## Current state

`worker_jobs` is the scheduling source of truth for queued compute work. Domain
tables (`trials`, `tasks`) still keep denormalized status, heartbeat, and verdict
columns for UI/API reads.

Active worker kinds are handled by registered `JobHandler` adapters:

- `TRIAL` runs Harbor trials.
- `QA` runs task-level trajectory classification and task verdict synthesis.
- `TASK_EXPAND` and `TAG_PROJECT` cover newer product jobs.
- `ANALYSIS` and `VERDICT` remain legacy enum values only so old in-flight rows
  can drain across deploys.

## Current source of truth

- `oddish/src/oddish/workers/queue/worker_job_single_job.py` — claim SQL and
  single-job runner.
- `oddish/src/oddish/workers/queue/worker_job_dispatcher.py` — queue-key
  discovery, counts, and spawn planning.
- `oddish/src/oddish/workers/queue/cleanup.py` — stale-heartbeat cleanup,
  stage safety nets, and orphaned-slot release.
- `oddish/src/oddish/workers/jobs/handlers.py` — built-in handler
  registration.
- `oddish/src/oddish/workers/queue/qa_handler.py` — task-level QA job.
- `backend/worker/functions.py` — Modal dispatcher, reconciler, and
  `process_single_job`.
- `backend/api/routers/admin.py` and `oddish/src/oddish/core/admin.py` —
  worker-job admin diagnostics.

## Invariants to preserve

- Claim and cleanup paths should stay kind-generic; adding a job kind should
  mean adding a handler and enqueue site, not a second scheduler.
- Workers must not hold a DB connection for the duration of a Harbor run.
- `queue_slots.locked_by` is tied to `worker_jobs.current_worker_id`; orphaned
  slots must be released per slot, not per queue key.
- Model aliases must canonicalize to one queue key per provider/model to avoid
  split capacity buckets.
- Public/share views must never expose probe trials or ID-only public task/trial
  routes.

See `AGENTS.md` for the maintained worker architecture and runtime pitfalls.
