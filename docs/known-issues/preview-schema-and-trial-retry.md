# Known issues — notes only (nothing fixed in this PR)

Found while e2e-testing #422 (per-run registry credentials) against a private
GHCR image on a PR-preview backend. Recorded for follow-up; no code changes here.

## 1. Preview DBs are missing migration-only objects → new-task creation 500s

**Symptom.** Creating a task on a preview backend 500s:
`asyncpg.InvalidColumnReferenceError: there is no unique or exclusion constraint
matching the ON CONFLICT specification` (insert into `worker_jobs` for a
`TAG_PROJECT` job). The queue reconciler/dispatcher also log
`relation "queue_runtime_status" does not exist` and
`relation "tag_projection_sweep_state" does not exist`.

**Root cause.** `.github/scripts/preview/bootstrap_preview_db.py` builds the
preview schema with `Base.metadata.create_all(...)` then `alembic stamp head` —
it never *runs* migrations. Any object defined only in a migration's raw DDL
(absent from the SQLAlchemy model graph) is therefore missing from every preview
DB, fresh or reused. Confirmed missing:

- `uq_worker_jobs_tag_project_active` — `oddish/alembic/versions/aa00ta01core_add_tag_tables.py`
- `queue_runtime_status` — `oddish/alembic/versions/qh01_add_queue_runtime_status.py`
- `tag_projection_sweep_state` — `oddish/alembic/versions/aa04ta05sweep_add_tag_sweep_state.py`

**Supposed fix (not applied).** On rebuild, drop to an empty `public` schema and
`alembic upgrade head` (actually run migrations) instead of `create_all` +
`stamp`; or mirror the migration-only DDL into the model graph so `create_all`
reproduces it.

## 2. Verifier-disabled / reward-less trials retry until exhaustion

**Symptom.** A trial whose env setup + run succeed but yields no reward (e.g.
`--disable-verification`, or a `nop` agent with no verifier) is marked `RETRYING`
with the contentless message `Trial <id> marked RETRYING`, looping to
`max_attempts` and then `FAILED`.

**Root cause.** No reward → `reward=None` in
`oddish/src/oddish/workers/harbor/outcome.py:216`; `_store_trial_results` skips
the `error_message` write and falls into the `attempts < max_attempts → RETRYING`
branch (`oddish/src/oddish/workers/queue/trial_handler.py:622-623`); the handler
surfaces only the generic fallback
(`oddish/src/oddish/workers/jobs/handlers.py:91-93`).

**Supposed fix (not applied).** Treat "ran to completion, verifier disabled /
no reward expected" as terminal (non-retryable) with an explanatory message in
`_store_trial_results`; carry a "verification-disabled" signal on
`HarborOutcome` so a deliberately scoreless run isn't confused with a transient
failure.

## 3. Observation — PR-preview workflow didn't dispatch for some PRs

New PRs #444/#449 never triggered the PR Preview workflow (no run created; not
approval-gated), while other PRs deployed normally. Cause unconfirmed
(GitHub-side). Noted for follow-up.
