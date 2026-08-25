# Oddish Logfire operations metrics

Postgres remains the canonical record of jobs and queue leases. Python workers
and dispatchers emit operational observations after reading or changing that
state. Logfire stores the metric time series and renders their graphs. The
Oddish React application does not copy, poll, aggregate, or graph these values.

## Telemetry contract

| Metric | Instrument and unit | Definition |
|---|---|---|
| `oddish.worker_job.transitions` | Counter, `{transition}` | Guarded `worker_jobs` updates that changed one row to `SUCCESS`, `RETRYING`, or `FAILED`. |
| `oddish.worker_job.duration` | Histogram, `s` | Seconds from the accepted job claim to the same accepted durable transition. Negative clock differences are recorded as zero. |
| `oddish.queue.jobs` | Gauge, `{job}` | Queued and running `worker_jobs` rows observed in one dispatch plan. |
| `oddish.queue.slots` | Gauge, `{slot}` | Held `queue_slots` leases and configured concurrency limits observed in that plan. |
| `oddish.dispatch.workers_spawned` | Counter, `{worker}` | Workers whose host spawn operation completed successfully. |
| `oddish.dispatch.cycles` | Counter, `{cycle}` | Dispatcher cycles labeled `success` or `error`. |
| `oddish.dispatch.duration` | Histogram, `s` | Wall-clock duration of the same completed or failed dispatch cycle. |

`oddish.dispatch.duration` is included because a cycle-duration panel requires
a histogram; the original six-name contract did not provide an instrument that
could store that requested value.

## Dimensions

Metric attributes are limited to the following fields:

- `kind`: the `TRIAL`, `TASK_EXPAND`, or `TAG_PROJECT` worker-job kind.
- `outcome`: `SUCCESS`, `RETRYING`, or `FAILED` for worker transitions; `success`
  or `error` for dispatch cycles.
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

Create one custom **Oddish Operations** dashboard. The eight numbered queries in
`logfire-oddish-operations.sql` define these time-series panels:

1. Queued and running jobs by queue key.
2. Held queue slots versus configured limits.
3. Worker transitions per minute by outcome.
4. Retry percentage.
5. Terminal-failure percentage.
6. P50 and P95 worker-job duration by kind.
7. Workers successfully spawned per successful cycle.
8. Dispatch cycles reaching the spawn cap.

Configure four list variables:

| Variable | Values |
|---|---|
| `deployment_environment` | The project's deployed environments, such as `production` and `preview-pr-123`. |
| `service_name` | `oddish-worker` for hosted workers or `oddish-dispatcher` for the standalone dispatcher. |
| `queue_key` | `__all__` plus the queue keys present in the project. |
| `worker_job_kind` | `__all__`, `TRIAL`, `TASK_EXPAND`, and `TAG_PROJECT`. |

For the two multi-series gauge panels, select the query's `series` column as the
chart dimension. For worker transitions, select `outcome`; for duration, select
`kind`. The percentage and dispatch panels have no dimension.

## Create and export the custom dashboard

1. Deploy this instrumentation to an environment with `LOGFIRE_TOKEN` set and
   run at least one dispatcher cycle and one worker job.
2. In Logfire's **Explore** view, run each query from
   `logfire-oddish-operations.sql` with concrete values replacing dashboard
   variables. This confirms the deployed project has the expected metric and
   attribute names.
3. Open **Dashboards**, choose **Custom**, and create **Oddish Operations**.
4. Add the four list variables above, then add the eight time-series panels and
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
