# Oddish Logfire operations metrics

Postgres remains the canonical record of jobs and queue leases. Python workers
and dispatchers emit operational observations after reading or changing that
state. Logfire stores the metric time series and renders their graphs. The
Oddish React application does not copy, poll, aggregate, or graph these values.

## Telemetry contract

| Metric | Instrument and unit | Definition |
|---|---|---|
| `oddish.worker_job.transitions` | Counter, `{transition}` | Guarded `worker_jobs` updates that changed one row to `SUCCESS`, `RETRYING`, or `FAILED`. |
| `oddish.worker_job.duration` | Histogram, `s` | Seconds on the worker's monotonic clock from receiving the accepted claim through `_record_outcome` returning the accepted durable transition. |
| `oddish.queue.jobs` | Gauge, `{job}` | Queued and running `worker_jobs` rows observed in one dispatch plan. A queue absent from the next plan receives one final zero-valued observation. |
| `oddish.queue.slots` | Gauge, `{slot}` | Held `queue_slots` leases and configured concurrency limits observed in that plan. |
| `oddish.dispatch.workers_spawned` | Counter, `{worker}` | Workers returned by dispatch cycles whose complete host spawn operation succeeded. Skipped and error cycles emit cycle metrics but no worker count because the dispatcher interface does not expose partial results. |
| `oddish.dispatch.cycles` | Counter, `{cycle}` | Dispatcher cycles labeled `success`, `skipped`, or `error`. |
| `oddish.dispatch.duration` | Histogram, `s` | Wall-clock duration of the same successful, skipped, or failed dispatch cycle. |

`oddish.dispatch.duration` is included because a cycle-duration panel requires
a histogram; the original six-name contract did not provide an instrument that
could store that requested value.

## Dimensions

Metric attributes are limited to the following fields:

- `kind`: the `TRIAL`, `TASK_EXPAND`, or `TAG_PROJECT` worker-job kind.
- `outcome`: `SUCCESS`, `RETRYING`, or `FAILED` for worker transitions;
  `success`, `skipped`, or `error` for dispatch cycles. `skipped` means a
  recognized transient `OSError` prevented completion and the polling host will
  retry. `error` means an unexpected failure prevented completion.
- `state`: `queued`, `running`, `held`, or `limit` for queue gauges.
- `queue_key`: the configured worker queue key. The reserved value `__all__`
  identifies the aggregate observation emitted on every cycle, including an
  empty cycle whose value is zero.
- `execution_lane`: the worker's bounded runtime lane, such as `default` or
  `ec2_trial`.
- `spawn_cap_reached`: whether the cycle's plan filled its configured per-cycle
  worker cap.

Do not attach worker-job IDs, trial IDs, organization IDs, user IDs, exception
messages, or other per-record values to these metrics.

## Dashboards

Enable Logfire's built-in **Web Server Metrics** dashboard for FastAPI request
counts and latency and **Basic System Metrics** for CPU and memory. Oddish
already instruments FastAPI in `backend/api/app.py` and system metrics in
`backend/observability.py`; defining replacement application metrics would give
the same operational number two meanings.

Create one custom **Oddish Operations** dashboard. The ten numbered queries in
`logfire-oddish-operations.sql` define these time-series panels:

1. Queued and running jobs by queue key.
2. Held queue slots versus configured limits.
3. Worker transitions per minute by outcome.
4. Retrying attempt share.
5. Terminal-failure attempt share.
6. P50 and P95 worker-job duration by kind.
7. Workers successfully spawned per successful cycle.
8. Dispatch cycles reaching the spawn cap.
9. Dispatch cycles by `success`, `skipped`, and `error`.
10. Dispatcher error percentage, where the denominator is every observed cycle.

The two attempt-share panels count persisted attempt outcomes, not unique jobs.
For example, one job that records `RETRYING`, `RETRYING`, then `SUCCESS`
contributes two retrying outcomes and one successful outcome, so its retrying
attempt share is 66.7%. This does not mean 66.7% of jobs retried.

Configure four list variables:

| Variable | Values |
|---|---|
| `deployment_environment` | The project's deployed environments, such as `production` and `preview-pr-123`. |
| `service_name` | `oddish-worker` for hosted workers or `oddish-dispatcher` for the standalone dispatcher. |
| `queue_key` | `__all__` plus the queue keys present in the project. |
| `worker_job_kind` | `__all__`, `TRIAL`, `TASK_EXPAND`, and `TAG_PROJECT`. |

Use the following panel configuration after pasting each numbered query:

| Panel | Metric column(s) | Dimension |
|---|---|---|
| 1 | `jobs` | `series` |
| 2 | `slots` | `series` |
| 3 | `transitions_per_minute` | `outcome` |
| 4 | `retrying_attempt_share` | none |
| 5 | `terminal_failure_attempt_share` | none |
| 6 | `p50_seconds`, `p95_seconds` | `kind` |
| 7 | `workers_per_cycle` | none |
| 8 | `capped_cycles` | none |
| 9 | `cycles` | `outcome` |
| 10 | `dispatcher_error_percentage` | none |

## Configure ingestion before deployment

The hosted worker package configures Logfire with `service_name=oddish-worker`.
`backend/observability.py` derives `deployment_environment=production` when
`MODAL_APP_NAME=oddish` and `deployment_environment=preview-pr-N` when the app
name is `oddish-pr-N`. The staging app's `oddish-staging-db` secret sets the
explicit override `LOGFIRE_ENVIRONMENT=staging`. The standalone dispatcher configures
`service_name=oddish-dispatcher`.

The hosted Modal functions load their environment variables from the
`oddish-prod` secret declared in `backend/modal_runtime.py`. In Modal's `main`
environment, add `LOGFIRE_TOKEN` to that secret. The value must be a write token
for the Logfire project that should receive these metrics. Do not store or paste
the token in a repository file.

After deploying and running at least one dispatcher cycle and one worker job,
run this query in Logfire's SQL Workbench:

```sql
SELECT
    metric_name,
    service_name,
    deployment_environment,
    COUNT(*) AS data_points
FROM metrics
WHERE metric_name LIKE 'oddish.%'
GROUP BY metric_name, service_name, deployment_environment
ORDER BY metric_name;
```

The result must contain these seven names:

```text
oddish.dispatch.cycles
oddish.dispatch.duration
oddish.dispatch.workers_spawned
oddish.queue.jobs
oddish.queue.slots
oddish.worker_job.duration
oddish.worker_job.transitions
```

Record the observed `service_name`, `deployment_environment`, all seven metric
names, and any SQL error in `logfire-rollout-checklist.md`. Do not record the
token value. A missing `oddish.dispatch.workers_spawned` row can mean that no
fully successful cycle has spawned a worker yet; run a workload with queued
capacity before treating that absence as an export failure.

## Create and export the custom dashboard

1. Deploy this instrumentation to an environment with `LOGFIRE_TOKEN` set and
   run at least one dispatcher cycle and one worker job.
2. In Logfire's **Explore** view, run each query from
   `logfire-oddish-operations.sql` with concrete values replacing dashboard
   variables. This confirms the deployed project has the expected metric and
   attribute names.
3. Open **Dashboards**, choose **Custom**, and create **Oddish Operations**.
4. Add the four list variables above, then add the ten time-series panels and
   paste their queries.
5. Use **Download dashboard as code**. Save the downloaded file as
   `docs/observability/logfire-oddish-operations.json` without hand-editing its
   schema.
6. To install it in another Logfire project, open **Dashboards**, choose
   **Custom**, select **Import JSON**, and upload that file. Enable the standard
   **Web Server Metrics** and **Basic System Metrics (Logfire)** dashboards from
   the **Standard** tab separately.

The Logfire server owns the dashboard export format and does not publish its
JSON schema. The checked-in JSON therefore comes from **Download dashboard as
code** after the SQL has been exercised against deployed metrics; a handwritten
lookalike is not treated as an importable export.

## Alerts

`logfire-oddish-alerts.sql` contains four Logfire alert queries:

1. Unexpected dispatcher errors.
2. Sustained terminal attempt failures.
3. Sustained queue saturation.
4. Dispatcher telemetry no longer arriving.

The checked-in examples target `service_name=oddish-worker` and
`deployment_environment=production`. Alert queries do not use the dashboard's
list variables, so replace those two SQL literals when monitoring another
environment or the standalone dispatcher. The SQL file includes conservative
starting values for evaluation interval, time window, minimum attempt count,
and failure share. The operator creating the alert selects the final threshold,
notification channel, and notification schedule.

The missing-telemetry query always returns one row whose value changes between
`receiving` and `missing`. Configure that alert to notify when query results
change. The other three queries return rows only when their threshold is met.

Logfire's current UI and SQL behavior are documented in its official
[alerts guide](https://logfire.pydantic.dev/docs/guides/web-ui/alerts/),
[dashboard query guide](https://logfire.pydantic.dev/docs/how-to-guides/write-dashboard-queries/),
and [dashboard guide](https://logfire.pydantic.dev/docs/guides/web-ui/dashboards/).
