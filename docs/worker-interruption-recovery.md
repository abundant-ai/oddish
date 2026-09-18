# Worker interruption recovery (ABT-1208 / ABT-1209 / ABT-1223)

Implemented on a separate checkout based on staging commit `10fb1d34`.
No cloud migration, deployment, rollout expansion, or deliberate cloud worker
termination has been performed as part of the local verification below.

## Recovery contract

Each hosted claim records job ID, attempt, worker ID, Modal invocation ID,
container ID (`MODAL_TASK_ID`), reservation token, and unchanged CPU/RAM requests.
The invocation can survive a container replacement, so it cannot prove liveness.
Only a positive original-container `TaskGetInfo.info.finished_at` permits early
recovery. The SDK's own `modal container stop` uses this same check. API errors,
missing identity, and a zero finish time leave the current ownership intact.

The original worker/attempt/container must still match under a database row lock.
A competing recovery loses that lock or finds the job already settled. A locked
trial defers recovery instead of waiting in the inverse cancellation lock order.
A cancelled or superseded trial is never converted into a new runnable attempt.
Remaining job and trial attempt budgets determine RETRYING versus FAILED.

The transaction records INTERRUPTED and the original container's end, mirrors
trial state, and releases only the old worker's queue slot. It saves the old
sandbox handle on the attempt. Dispatch and claim both exclude cleanup-pending
jobs. Provider teardown happens outside transactions and is retried until it
succeeds; an unresolved provisioning record stays blocked. The ordinary
scheduler must obtain a fresh reservation for the retry. No replacement directly
claims work using the consumed reservation.

A database trigger records normal, cancelled, and stale-heartbeat outcomes in the
same transaction as their job transition. Heartbeat-only records have an unknown
container end (`finished_at IS NULL`); detection time must not be billed as an
observed stop time. Old rows are left unknown, rather than reconstructed from a
later successful retry. Worker billing uses the attempt end when known, including
when another attempt is already running. Sandbox billing retains its own lifetime.

## Local verification

Run against an explicitly disposable PostgreSQL instance:

```sh
ODDISH_TEST_DATABASE_URL=postgresql+asyncpg://USER@localhost:PORT/DB \
  python -m pytest oddish/tests/test_interrupted_worker_recovery.py \
  oddish/tests/test_queue_launch_reservations.py
```

The integration fixture uses a unique schema per test and deletes only that
schema. It exercises two competing replacements, failed and retried sandbox
teardown, a fresh second claim, successful outcome persistence, cancellation,
exhausted allowances, wrong containers, newer owners, and a full capacity slot.
Backend tests cover live/unknown/stopped Modal responses and require both the
reservation and invocation when identifying a replacement.

Apply `worker_interruptions_001` before running the new worker version. Deploying
code first would make claim SQL reference absent columns. Old workers remain
compatible with the added nullable columns and false cleanup default.

## Required cloud validation before production

Use copied short tasks in non-production with the existing CPU/RAM reservations.
Record the exact deployment revision and migration revision first. Stop only the
identified test container after its job and attempt record are visible. Verify
one INTERRUPTED row, the actual original-container finish time, completed sandbox
teardown, a closed worker billing interval, and exactly one fresh claim using a
new reservation and container. Let that claim finish and verify its saved result.
Repeat with competing replacements, user cancellation, and max_attempts exhausted.
Verify queue and candidate caps throughout. A provider failure must retain
cleanup_pending and keep the retry out of dispatch demand.

No cloud interruption test has been run yet. Local provider/API doubles establish
database behavior, not Modal's replacement timing or the service identity's
permission to call TaskGetInfo. Do not expand ABT-1210 until that test passes.

## September 15–16 retry investigation

ABT-1223's historical cutoff is September 15 21:00 UTC through September 16
21:00 UTC, compared with the preceding 24 hours. The issue records 15,931 RETRYING
outcomes out of 20,436 (78.0%), versus 804 / 7,242 (11.1%). These count attempts,
not unique tasks. The issue does not establish a connection to worker stops.

A September 18 read of Logfire reproduced 10,642 GetObject ClientError records
across 395 traces. It also found 1,208 blank-message CancelledError records across
470 traces and 33 records explicitly saying `Input was cancelled by user`.
Nested exceptions are not interrupted-worker counts. No results were returned
for the `metric=worker_job_retry_requeued` console-message query in this window;
that query cannot supply a complete attempt/reason census.

```sql
SELECT exception_type, left(exception_message,250) AS error,
       count(*) AS records, count(DISTINCT trace_id) AS traces
FROM records
WHERE start_timestamp >= '2026-09-15T21:00:00Z'
  AND start_timestamp < '2026-09-16T21:00:00Z'
  AND service_name='oddish-worker' AND deployment_environment='production'
  AND is_exception
GROUP BY 1,2 ORDER BY 3 DESC LIMIT 15;
```

The remaining causal evidence is original container IDs with stop timestamps and
termination reasons, joined to interrupted job attempts and subsequent retries.
Send Modal only those verified IDs and relevant timestamps when available; no
original container IDs were recovered from the reviewed Linear issues. CPU/RAM
usage measurements and billed savings remain unverified (ABT-1209); the persisted
request values alone do not answer either question.

A fresh metric query on September 18 reproduced the historical issue totals and
compared a fixed newer 24-hour window (September 17 21:00 UTC inclusive through
September 18 21:00 UTC exclusive): 6,420 SUCCESS, 2,175 RETRYING, 275 FAILED.
Retry share is 2,175 / 8,870 = 24.5%, lower than 78.0% during the surge but still
above the 11.1% earlier baseline. This does not identify a cause or prove that
any recovery code is deployed. These values were read directly from Logfire's
SQL Workbench using the following metric query:

```sql
SELECT CASE WHEN recorded_timestamp < '2026-09-15T21:00:00Z' THEN 'before'
            WHEN recorded_timestamp < '2026-09-16T21:00:00Z' THEN 'surge'
            ELSE 'Sep17-18' END AS period,
       attributes->>'outcome' AS outcome,
       metric_increase(value, recorded_timestamp) AS attempts
FROM metrics
WHERE metric_name='oddish.worker_job.transitions'
  AND deployment_environment='production' AND service_name='oddish-worker'
  AND attributes->>'kind'='TRIAL'
  AND ((recorded_timestamp >= '2026-09-14T21:00:00Z'
        AND recorded_timestamp < '2026-09-16T21:00:00Z')
    OR (recorded_timestamp >= '2026-09-17T21:00:00Z'
        AND recorded_timestamp < '2026-09-18T21:00:00Z'))
GROUP BY 1,2 ORDER BY 1,2;
```

Final local results (September 18): core recovery/reservation/dispatcher/runner/
retry/billing suite `135 passed in 11.51s`; hosted startup, liveness, resource,
dispatch and EC2 capacity tests `19 passed in 2.57s`; final recovery-only run after
adding the pending-cleanup demand assertion `9 passed in 2.56s`. The migration
upgrade/downgrade round trip passed on a disposable PostgreSQL schema. Ruff's
undefined-name/import checks and `git diff --check` passed. Cloud validation,
original-container support evidence, and measured CPU/RAM remain outstanding.
