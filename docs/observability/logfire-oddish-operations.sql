-- Oddish Operations dashboard query catalog
--
-- Dashboard variables:
--   $deployment_environment: exact metrics.deployment_environment value
--   $service_name: exact metrics.service_name value
--   $queue_key: a queue key, or __all__ to include every non-aggregate queue
--   $worker_job_kind: TRIAL, TASK_EXPAND, TAG_PROJECT, or __all__
--
-- Each numbered query is one Logfire time-series panel. Paste one query at a
-- time; this file is a reviewable source for the UI dashboard that is exported
-- to logfire-oddish-operations.json after the metrics are present in Logfire.

-- 1. Queued and running jobs by queue key
WITH ranked AS (
    SELECT
        time_bucket($resolution, recorded_timestamp) AS x,
        attributes->>'queue_key' AS queue_key,
        attributes->>'state' AS state,
        metric_avg(value) AS jobs,
        ROW_NUMBER() OVER (
            PARTITION BY
                time_bucket($resolution, recorded_timestamp),
                attributes->>'queue_key',
                attributes->>'state'
            ORDER BY recorded_timestamp DESC
        ) AS observation_rank
    FROM metrics
    WHERE metric_name = 'oddish.queue.jobs'
      AND deployment_environment = $deployment_environment
      AND service_name = $service_name
      AND attributes->>'queue_key' <> '__all__'
      AND (
          $queue_key = '__all__'
          OR attributes->>'queue_key' = $queue_key
      )
)
SELECT
    x,
    jobs,
    queue_key || ' ' || state AS series
FROM ranked
WHERE observation_rank = 1
ORDER BY x;

-- 2. Held queue slots versus configured limits
WITH ranked AS (
    SELECT
        time_bucket($resolution, recorded_timestamp) AS x,
        attributes->>'queue_key' AS queue_key,
        attributes->>'state' AS state,
        metric_avg(value) AS slots,
        ROW_NUMBER() OVER (
            PARTITION BY
                time_bucket($resolution, recorded_timestamp),
                attributes->>'queue_key',
                attributes->>'state'
            ORDER BY recorded_timestamp DESC
        ) AS observation_rank
    FROM metrics
    WHERE metric_name = 'oddish.queue.slots'
      AND deployment_environment = $deployment_environment
      AND service_name = $service_name
      AND attributes->>'queue_key' <> '__all__'
      AND (
          $queue_key = '__all__'
          OR attributes->>'queue_key' = $queue_key
      )
)
SELECT
    x,
    slots,
    queue_key || ' ' || state AS series
FROM ranked
WHERE observation_rank = 1
ORDER BY x;

-- 3. Worker transitions per minute by outcome
SELECT
    time_bucket($resolution, recorded_timestamp) AS x,
    attributes->>'outcome' AS outcome,
    60 * metric_rate(value, recorded_timestamp) AS transitions_per_minute
FROM metrics
WHERE metric_name = 'oddish.worker_job.transitions'
  AND deployment_environment = $deployment_environment
  AND service_name = $service_name
  AND ($queue_key = '__all__' OR attributes->>'queue_key' = $queue_key)
  AND (
      $worker_job_kind = '__all__'
      OR attributes->>'kind' = $worker_job_kind
  )
GROUP BY x, outcome
ORDER BY x;

-- 4. Retrying attempt share
-- One job may contribute multiple RETRYING attempts followed by one SUCCESS.
-- This is the share of persisted attempt outcomes, not the share of unique jobs.
WITH transitions AS (
    SELECT
        time_bucket($resolution, recorded_timestamp) AS x,
        attributes->>'outcome' AS outcome,
        metric_increase(value, recorded_timestamp) AS transition_count
    FROM metrics
    WHERE metric_name = 'oddish.worker_job.transitions'
      AND deployment_environment = $deployment_environment
      AND service_name = $service_name
      AND ($queue_key = '__all__' OR attributes->>'queue_key' = $queue_key)
      AND (
          $worker_job_kind = '__all__'
          OR attributes->>'kind' = $worker_job_kind
      )
    GROUP BY x, outcome
)
SELECT
    x,
    100 * SUM(CASE WHEN outcome = 'RETRYING' THEN transition_count ELSE 0 END)
        / NULLIF(SUM(transition_count), 0) AS retrying_attempt_share
FROM transitions
GROUP BY x
ORDER BY x;

-- 5. Terminal-failure attempt share
-- The denominator is every persisted SUCCESS, RETRYING, or FAILED attempt
-- outcome in the bucket; it is not a unique-job failure percentage.
WITH transitions AS (
    SELECT
        time_bucket($resolution, recorded_timestamp) AS x,
        attributes->>'outcome' AS outcome,
        metric_increase(value, recorded_timestamp) AS transition_count
    FROM metrics
    WHERE metric_name = 'oddish.worker_job.transitions'
      AND deployment_environment = $deployment_environment
      AND service_name = $service_name
      AND ($queue_key = '__all__' OR attributes->>'queue_key' = $queue_key)
      AND (
          $worker_job_kind = '__all__'
          OR attributes->>'kind' = $worker_job_kind
      )
    GROUP BY x, outcome
)
SELECT
    x,
    100 * SUM(CASE WHEN outcome = 'FAILED' THEN transition_count ELSE 0 END)
        / NULLIF(SUM(transition_count), 0) AS terminal_failure_attempt_share
FROM transitions
GROUP BY x
ORDER BY x;

-- 6. P50 and P95 worker-job duration by kind
SELECT
    time_bucket($resolution, recorded_timestamp) AS x,
    attributes->>'kind' AS kind,
    metric_quantile(0.50, value) AS p50_seconds,
    metric_quantile(0.95, value) AS p95_seconds
FROM metrics
WHERE metric_name = 'oddish.worker_job.duration'
  AND deployment_environment = $deployment_environment
  AND service_name = $service_name
  AND ($queue_key = '__all__' OR attributes->>'queue_key' = $queue_key)
  AND (
      $worker_job_kind = '__all__'
      OR attributes->>'kind' = $worker_job_kind
  )
GROUP BY x, kind
ORDER BY x;

-- 7. Workers successfully spawned per successful cycle
WITH increases AS (
    SELECT
        time_bucket($resolution, recorded_timestamp) AS x,
        metric_name,
        metric_increase(value, recorded_timestamp) AS metric_count
    FROM metrics
    WHERE metric_name IN (
        'oddish.dispatch.workers_spawned',
        'oddish.dispatch.cycles'
    )
      AND deployment_environment = $deployment_environment
      AND service_name = $service_name
      AND attributes->>'outcome' = 'success'
    GROUP BY x, metric_name
)
SELECT
    x,
    SUM(
        CASE
            WHEN metric_name = 'oddish.dispatch.workers_spawned'
            THEN metric_count
            ELSE 0
        END
    ) / NULLIF(
        SUM(
            CASE
                WHEN metric_name = 'oddish.dispatch.cycles'
                THEN metric_count
                ELSE 0
            END
        ),
        0
    ) AS workers_per_cycle
FROM increases
GROUP BY x
ORDER BY x;

-- 8. Dispatch cycles reaching the spawn cap
SELECT
    time_bucket($resolution, recorded_timestamp) AS x,
    metric_increase(value, recorded_timestamp) AS capped_cycles
FROM metrics
WHERE metric_name = 'oddish.dispatch.cycles'
  AND deployment_environment = $deployment_environment
  AND service_name = $service_name
  AND attributes->>'spawn_cap_reached' = 'true'
GROUP BY x
ORDER BY x;

-- 9. Dispatch cycles by outcome
SELECT
    time_bucket($resolution, recorded_timestamp) AS x,
    attributes->>'outcome' AS outcome,
    metric_increase(value, recorded_timestamp) AS cycles
FROM metrics
WHERE metric_name = 'oddish.dispatch.cycles'
  AND deployment_environment = $deployment_environment
  AND service_name = $service_name
GROUP BY x, outcome
ORDER BY x;

-- 10. Dispatcher error percentage
-- The denominator includes successful, transiently skipped, and errored cycles.
WITH cycles AS (
    SELECT
        time_bucket($resolution, recorded_timestamp) AS x,
        attributes->>'outcome' AS outcome,
        metric_increase(value, recorded_timestamp) AS cycle_count
    FROM metrics
    WHERE metric_name = 'oddish.dispatch.cycles'
      AND deployment_environment = $deployment_environment
      AND service_name = $service_name
    GROUP BY x, outcome
)
SELECT
    x,
    100 * SUM(CASE WHEN outcome = 'error' THEN cycle_count ELSE 0 END)
        / NULLIF(SUM(cycle_count), 0) AS dispatcher_error_percentage
FROM cycles
GROUP BY x
ORDER BY x;
