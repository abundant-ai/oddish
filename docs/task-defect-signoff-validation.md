# Task-defect sign-off validation

Validated on 2026-09-09 in the isolated `codex/task-defect-signoff` worktree,
starting at staging commit `fe3381b50`.

## Backend

Python 3.13, PostgreSQL 17.11, disposable local database on port 56487. The full
core migration chain, including `task_defects_001`, applied successfully. The
following affected suites reported `248 passed in 14.82s`:

```sh
python -m pytest \
  oddish/tests/test_task_defect_policy.py \
  oddish/tests/test_deliveries.py \
  oddish/tests/test_delivery_qa.py \
  oddish/tests/test_qa_audit_rejection.py \
  oddish/tests/test_analysis_trials.py \
  oddish/tests/test_verdict_sync.py \
  oddish/tests/test_task_detail_pre_trial.py \
  oddish/tests/test_task_detail_qa_cost.py \
  oddish/tests/test_task_panel.py \
  oddish/tests/test_retry_clears_stale_analysis.py \
  oddish/tests/test_trial_pre_trial_audit.py \
  oddish/tests/test_retry_trial_core.py \
  oddish/tests/test_cli_backfill_analysis.py \
  oddish/tests/analyze -q --tb=short
```

Set `PYTHONPATH=oddish/src` and `ODDISH_DATABASE_URL` to the disposable test
database when invoking this command from the repository root.

The new tests exercise all recorded tiers from both review sources, rejection
of new lower-tier output, direct sign-off and global-waiver attempts, missing
person/version, stale versions, acknowledgment identity and history, retained
execution defects, and causal versus unrelated findings. They also execute the
actual migration against an isolated pre-policy schema and compare the stored
audit and finalized snapshot before/after, asserting an empty worker queue.
Existing retry and review-failure suites pass with retained defects.

## Frontend and browser

`node --test tests/*.test.ts` in `frontend/` reported 97 passing tests.
`tsc --noEmit` and ESLint on all changed frontend files passed. Ruff's fatal-error
checks passed for changed Python files; the new Python files passed full Ruff
checks. `git diff --check` passed.

The Codex inline browser exercised the actual `DeliveryBoardClient` component
and hosted delivery API routes against the disposable database. A temporary
Next.js harness supplied the page shell and a test Clerk hook. FastAPI's auth
dependencies supplied a fixed authenticated actor, Maya; the actual delivery
service, database transactions, user-name lookup, and mutations ran unchanged.
This checks delivery behavior, not the Clerk login flow or production deployment.

The seeded Task A had five executions across three agents and an accepted
historical review for v7. Its source audit recorded `should_fix` finding
`verifier-v7`: “The verifier ignores the exit code,” at `tests/verify.py:4–6`.
The browser observations were:

1. The active delivery showed `0/1 tasks ready`, disabled Finalize, the original
   severity, and the requirement to acknowledge the individual finding.
2. Expanding “Review evidence” exposed the anchor, detail, and recommendation.
3. Clicking Acknowledge displayed “acknowledged by Maya for v7; finding retained.”
4. Signing off displayed `1/1 tasks ready` and enabled Finalize. The original
   finding remained visible.
5. Switching the fixture to v8 returned the delivery to `0/1 tasks ready`,
   disabled Finalize, unchecked sign-off, and displayed missing audit/rollouts
   and “verdict does not cover v8; re-run QA on it.”
6. Opening v7 history on the final build showed the original `should-fix`
   finding and accepted review, plus `ack:verifier-v7` by `maya` for v7 at
   16:37:46 and `signoff` by `maya` for v7 at 16:37:50 (America/Los_Angeles).

No paid analysis was run. Review artifacts used by regression tests were
fixtures. Deploying the migration and reading/acknowledging the delivery do not
create analysis jobs, as asserted by the backend tests.
