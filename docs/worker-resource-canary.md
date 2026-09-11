# Worker reservation canary

The migration seeds the database row with `fraction=0`. Staging and production deployments enable
a 1% CPU-only sample after their worker deploy succeeds. Merging into `staging`
enables staging; promoting to `main` enables production. Preview deployments
leave admission under manual control.
The existing worker remains 1 physical CPU core and 3,072 MiB RAM. The candidate
uses the same `_run_one_job` body, Harbor image, secrets, timeout, interruption
protection, and task sandbox resource configuration. Only the worker reservation
changes. Candidate RAM is a scalar request, with no new hard cap.

| Stage | CPU request | CPU ceiling | RAM request | Concurrent candidates |
| --- | --- | --- | --- | --- |
| Base | 1 core | Modal default, currently 17 cores | 3,072 MiB | Existing base limit |
| CPU-only, first | 0.6 cores | 17 cores, explicit | 3,072 MiB | 2 |
| Lower RAM, after CPU comparison passes | 0.6 cores | 17 cores, explicit | 1,536 MiB | 2 |

[Modal documents](https://modal.com/docs/guide/resources) the default CPU ceiling
as the request plus 16 physical cores. It charges the greater of requests and
actual usage; Oddish's estimates use the requests. [Non-preemptible functions](https://modal.com/docs/guide/preemption)
retain their 3× CPU/memory price multiplier.

## Deployment and live controls

Apply the core migration `worker_resources_001` before deploying the worker code.
The migration initializes `worker_resource_rollout` row 1 with fraction 0,
max_workers 2, and configuration `candidate-cpu0.6-mem3072`.

These deployment environment variables define the candidate function:

```text
ODDISH_MODAL_WORKER_CANDIDATE_CPU=0.6
ODDISH_MODAL_WORKER_CANDIDATE_MEMORY_MB=3072
ODDISH_MODAL_WORKER_CANDIDATE_MAX_CONTAINERS=2
```

Set them in the environment that runs `modal deploy deploy.py`. Their values are
also delivered in the final deployment-owned secret, so older provider secrets
cannot change the resource values used for cost recording inside a worker.

The deployment workflows pin the candidate to 0.6 cores, 3,072 MiB, and two
containers, then call the control command after a successful worker deployment.
Each deploy applies a fraction of `0.01` unless its GitHub configuration variable
overrides it:

| Workflow | Modal app / environment | Fraction override |
| --- | --- | --- |
| `.github/workflows/staging-deploy.yml` | `oddish-staging` / `staging` | `STAGING_WORKER_RESOURCE_FRACTION` in the GitHub `staging` environment |
| `.github/workflows/modal-deploy.yml` | `oddish` / `main` | Repository variable `PRODUCTION_WORKER_RESOURCE_FRACTION` |

The production job runs only from `main`. Staging uses the `oddish-staging-db`
secret for its database; production uses its existing production secret.
Set the corresponding GitHub variable to `0` to keep subsequent deploys stopped.
For an immediate stop, also run the live stop command below; changing the GitHub
variable alone does not update the live database. Removing the override restores
the 1% default on the next deploy. The migration still seeds fraction zero.

From `backend/`, display staging's live state (read-only):

```bash
MODAL_APP_NAME=oddish-staging MODAL_ENVIRONMENT=staging MODAL_SECRET_ENVIRONMENT=main \
  uv run modal run --env staging worker_resource_rollout.py
```

After copied-task validation passes, enable the CPU-only candidate on approximately
1% of eligible job IDs, with at most two concurrent candidate workers:

```bash
MODAL_APP_NAME=oddish-staging MODAL_ENVIRONMENT=staging MODAL_SECRET_ENVIRONMENT=main \
  uv run modal run --env staging worker_resource_rollout.py --fraction 0.01 --max-workers 2
```

Stop new candidate claims without interrupting running jobs:

```bash
MODAL_APP_NAME=oddish-staging MODAL_ENVIRONMENT=staging MODAL_SECRET_ENVIRONMENT=main \
  uv run modal run --env staging worker_resource_rollout.py --fraction 0
```

For production, stop new candidate claims with:

```bash
MODAL_APP_NAME=oddish MODAL_ENVIRONMENT=main MODAL_SECRET_ENVIRONMENT=main \
  uv run modal run --env main worker_resource_rollout.py --fraction 0
```

Omit `--fraction 0` to read production's live state without changing it.

The equivalent SQL, run against the intended deployment's database, is:

```sql
-- Enable only after validating the CPU-only candidate.
UPDATE worker_resource_rollout
SET fraction = 0.01, max_workers = 2, configuration = 'candidate-cpu0.6-mem3072'
WHERE id = 1;

-- Stop. Once COMMIT returns, subsequent candidate claims see zero.
UPDATE worker_resource_rollout SET fraction = 0 WHERE id = 1;
SELECT * FROM worker_resource_rollout WHERE id = 1;
```

Production uses `MODAL_APP_NAME=oddish`, `MODAL_ENVIRONMENT=main`, and `--env main`.
Preview apps use their exact `oddish-pr-N` app name and environment `preview`.
All three borrow provider secrets from `main`; staging and preview append their
own database override. Never use a production app name when testing copied jobs.

To test lower RAM: stop, wait for active candidate jobs to finish, redeploy with
`ODDISH_MODAL_WORKER_CANDIDATE_MEMORY_MB=1536`, and repeat copied tests before
setting fraction 0.01 with configuration `candidate-cpu0.6-mem1536`. Pass that same
environment variable to the control command. A configuration mismatch stops old
candidates from taking additional jobs. The live max_workers can lower the launch
cap (zero stops claims); increasing above the deployed Modal cap is refused by
the control command. Reducing a positive cap does not interrupt current jobs.

## Eligibility and attribution

The stable sample is computed from the first 32 bits of MD5(worker_job_id), below
fraction × 2^32. It samples jobs, not launches or batch iterations. Only TRIAL
worker jobs attached to ordinary `kind='agent'` trials, excluding probes, with
priority <= 0, `harbor_variant_id='default'`, and `execution_lane='default'` qualify.
Deleted tasks/trials and delayed jobs remain unclaimable. Every candidate claim
keeps the launch's organization and priority class. Positive-priority analysis,
other worker job kinds, ephemeral/blessed images, and EC2 lanes stay on base workers.

While enabled, base claims exclude the sampled jobs. If both candidates are busy,
selected jobs wait; they do not consume repeated base launches. Candidate and base
workers share the existing model queue slots, organization fairness, and approval
checks. The two-candidate cap includes workers starting up and workers between
jobs. An unreserved candidate invocation cannot claim. Retries retain the job's
sample bucket, while every attempt records the configuration that actually ran.
Existing pre-deployment workers can still claim without the new partition; start
measurement after those workers have drained.

Disabling returns queued selected jobs to the base pool. Each claim holds a shared
lock on the rollout row only for its database transaction. A stop waits for those
claims to commit, then prevents new ones. No connection or rollout lock remains
held during Harbor execution.

`worker_resource_attempts` records configuration, CPU request and ceiling, RAM,
interruption protection, invocation ID, and claim timestamp atomically with each
claim. This survives retries and a disabled/failing cost recorder. Existing logs
include configuration and invocation ID. Cost spans receive the worker's actual
billing specification; sandbox cost recording is unchanged. Join attempt records
to cost spans as follows:

```sql
SELECT a.worker_job_id, a.attempt, a.configuration, a.modal_function_call_id,
       a.cpu_request, a.cpu_limit, a.memory_mb, a.nonpreemptible,
       c.started_at, c.finished_at, c.cost_usd
FROM worker_resource_attempts a
LEFT JOIN modal_costs c ON c.worker_job_id = a.worker_job_id
 AND c.worker_job_attempt = a.attempt AND c.component_role = 'worker_function'
ORDER BY a.claimed_at DESC;
```

One Modal invocation can run multiple jobs, so do not sum the same invocation's
actual charge once for each attempt. Compare total function charges, including
cold starts and idle container time, against the sum of its Oddish spans. Costs
based on resource requests are estimates, not measurements of Modal charges.

## Validation checklist and results

- [x] Read the five requested files in order.
- [x] Add the disabled candidate, stable job sampling, every-claim checks, and shared concurrency limits.
- [x] Persist actual resources and invocation IDs for each attempt.
- [x] Core regressions: `289 passed, 2 skipped` (the two skips require a Kubernetes cluster).
- [x] Backend regressions: `12 passed` (resource wiring, live controls, deployment values, dispatcher integration, EC2 release).
- [x] PostgreSQL coverage includes overlapping dispatchers, 1% selection over 10,000 job IDs, eligibility, retries, stop races, and cost recording at both RAM values.
- [x] Core migrations reached `worker_resources_001` on disposable PostgreSQL 16; downgrade/upgrade also passed.
- [x] Compare copied normal tasks and largest available archived log conversions in Modal; results below.
- [x] Formatting/lint hooks passed. Mypy reports the same 35 errors in 19 files as unmodified staging `21ebfe0b2`; zero new error messages after normalizing line numbers.
- [x] Prepare staging PR with rollout disabled, measurements, and exact controls.

The baseline Modal UI was verified on September 10, 2026: 1 core, 3,072 MiB,
no memory limit, non-preemptible True, timeout 43,200 seconds. The six largest
historical trial step counts had no stored trial artifact prefix, including the
457,573-step trial. They cannot establish a readable-file comparison. The largest
available archived case found by step count has 68,792 steps, an 86,189,692-byte
trajectory JSON, and a 35,733,313-byte Codex stdout log.

`backend/benchmark_worker_resources.py` replays archived conversions and optionally
runs copied task inputs with the oracle agent in independent Modal sandboxes. It
never claims live jobs; its database URL is deliberately unusable. For reproducible
measurements, retain the manifest of S3 keys/sizes and returned JSON, compare the
same tasks/versions, and run CPU-only before `--lower-memory`. Its 100 ms event-loop
heartbeat measures stalls; it does not simulate the production database heartbeat.
Oracle comparisons cover Harbor orchestration and verifier execution, not a fresh
model-driven solver session. Missing source artifacts or either kind of new failure
must be investigated before enabling or expanding the rollout.

### September 10 isolated comparison

The [input manifest](worker-resource-canary-manifest.json) identifies the exact
task versions and archived S3 objects. The [raw measurements](worker-resource-canary-results.json)
include configuration, invocation ID, output sizes, per-case estimates, and Modal
call duration. CPU-only completed before the lower-memory invocation started.

| Measurement | 1 CPU / 3,072 MiB | 0.6 CPU / 3,072 MiB | 0.6 CPU / 1,536 MiB |
| --- | ---: | ---: | ---: |
| Modal invocation execution | 59.036 s | 54.801 s | 52.916 s |
| Copied hashids task, oracle | 11.562 s | 10.703 s | 10.869 s |
| Copied data-quality task, oracle | 29.417 s | 25.588 s | 25.140 s |
| 68,792-step raw Codex conversion + JSON round trip | 3.731 s | 3.764 s | 3.510 s |
| Largest observed 100 ms heartbeat gap | 3.768 s | 3.857 s | 3.626 s |
| Process peak resident memory | 969.5 MiB | 994.5 MiB | 1,009.8 MiB |
| Estimated invocation execution cost | $0.003500 | $0.002387 | $0.001776 |
| Modal displayed function charge, cents precision | $0.01 | $0.00 | $0.00 |

All three function histories contain one call marked `Succeeded`; no retry or
memory death appeared. All six copied oracle task runs returned reward 1.0,
`error=null`, and readable results. Each configuration replayed 12 artifacts;
the output byte sizes and step counts matched across all three configurations.
The archived hashids trajectory has 23 steps, while the raw Claude conversion
produces 22 on every configuration, including the baseline. This is an existing
converter/input difference, not a change caused by the reservation.

The baseline/CPU [Modal run](https://modal.com/apps/abundant-ai/main/ap-4MjXnmf45pTWjWfBc1PVDa)
and lower-memory [Modal run](https://modal.com/apps/abundant-ai/main/ap-RNcSL85JnwL5hQJx8I2arA)
have stopped. The displayed charges are rounded to cents, include container
overhead, and may settle later; `$0.00` does not mean free. The estimates apply
Oddish's existing request-based rates and interruption-protection multiplier to
Modal's execution durations. They exclude startup, idle time, task sandbox
charges, and measured usage above the request. This sample is too small to infer
actual savings from the rounded charges. Compare a longer CPU-only cohort with
Modal's usage export before expanding or moving the live cohort to lower RAM.

No delivery job was claimed and no staging or production rollout was enabled by
these tests. At that point the candidate was CPU-only, fraction 0, cap 2. A 1% live
CPU-only comparison and production database-heartbeat measurements remain the
next deployment step; the isolated lower-RAM result does not skip that gate.

Reproduce from `backend/` with access to the source artifact storage:

```bash
MODAL_APP_NAME=oddish-worker-resource-comparison uv run modal run benchmark_worker_resources.py \
  --manifest ../docs/worker-resource-canary-manifest.json --output /tmp/cpu-results.json --run-tasks
# Only after reviewing CPU-only results:
MODAL_APP_NAME=oddish-worker-resource-comparison uv run modal run benchmark_worker_resources.py \
  --manifest ../docs/worker-resource-canary-manifest.json --output /tmp/memory-results.json --run-tasks --lower-memory
```

Local regressions were run as separate core/backend processes, with a disposable
PostgreSQL database in `ODDISH_DATABASE_URL` and `ODDISH_TEST_DATABASE_URL`:

```bash
PYTHONPATH=oddish/src:backend python -m pytest -q -rs \
  oddish/tests/test_dispatch*.py oddish/tests/test_worker_jobs*.py \
  oddish/tests/test_worker_job_dispatcher.py oddish/tests/test_worker_batch_drain.py \
  oddish/tests/test_queue_launch_reservations.py oddish/tests/test_modal_cost*.py
PYTHONPATH=oddish/src:backend python -m pytest -q \
  backend/tests/test_worker_resource_candidate.py backend/tests/test_modal_app_worker_limits.py \
  backend/tests/test_dispatch_metrics.py backend/tests/test_ec2_worker_capacity_release.py
```

Combining both suites in one process registered the backend quota reader against
the core-only test database and failed three cost-breakdown tests with
`relation "quotas" does not exist`. Both suites passed separately. The type-check
comparison used the same existing file set on an archived staging checkout.
