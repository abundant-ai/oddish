-- Oddish Operations alert query catalog
--
-- Logfire alert queries do not use dashboard variables. These examples target
-- the hosted production dispatcher, whose service_name is oddish-worker and
-- whose deployment_environment is production. Replace those two literals
-- when creating an alert for preview-pr-1366, staging, or the standalone
-- oddish-dispatcher service.
--
-- Logfire applies the alert's configured time window to the query. The
-- suggested intervals, windows, and thresholds below are starting values;
-- the operator creating the alert owns the final policy.

-- 1. Unexpected dispatcher errors
-- Suggested policy: evaluate every 5 minutes over the last 10 minutes and
-- notify when this query returns a row. One unexpected cycle error is enough.
-- Externally interrupted cycles use outcome='cancelled' and do not match.
SELECT
    service_name,
    deployment_environment,
    metric_increase(value, recorded_timestamp) AS error_cycles
FROM metrics
WHERE metric_name = 'oddish.dispatch.cycles'
  AND service_name = 'oddish-worker'
  AND deployment_environment = 'production'
  AND attributes->>'outcome' = 'error'
GROUP BY service_name, deployment_environment
HAVING metric_increase(value, recorded_timestamp) > 0;

-- 2. Sustained terminal attempt failures
-- Suggested policy: evaluate every 10 minutes over the last 30 minutes and
-- notify when at least 10 attempt outcomes exist and FAILED is at least 20%.
WITH transitions AS (
    SELECT
        service_name,
        deployment_environment,
        attributes->>'outcome' AS outcome,
        metric_increase(value, recorded_timestamp) AS attempt_count
    FROM metrics
    WHERE metric_name = 'oddish.worker_job.transitions'
      AND service_name = 'oddish-worker'
      AND deployment_environment = 'production'
    GROUP BY service_name, deployment_environment, outcome
), totals AS (
    SELECT
        service_name,
        deployment_environment,
        SUM(attempt_count) AS total_attempts,
        SUM(
            CASE WHEN outcome = 'FAILED' THEN attempt_count ELSE 0 END
        ) AS failed_attempts
    FROM transitions
    GROUP BY service_name, deployment_environment
)
SELECT
    service_name,
    deployment_environment,
    total_attempts,
    failed_attempts,
    100 * failed_attempts / NULLIF(total_attempts, 0) AS failed_attempt_share
FROM totals
WHERE total_attempts >= 10
  AND 100 * failed_attempts / NULLIF(total_attempts, 0) >= 20;

-- 3. Sustained queue saturation
-- Suggested policy: evaluate every 5 minutes over the last 20 minutes and
-- notify when at least three 5-minute buckets have queued work and held slots
-- at or above the configured limit.
WITH ranked AS (
    SELECT
        time_bucket('5 minutes', recorded_timestamp) AS bucket,
        service_name,
        deployment_environment,
        attributes->>'queue_key' AS queue_key,
        metric_name,
        attributes->>'state' AS state,
        metric_avg(value) AS observed_value,
        ROW_NUMBER() OVER (
            PARTITION BY
                time_bucket('5 minutes', recorded_timestamp),
                service_name,
                deployment_environment,
                attributes->>'queue_key',
                metric_name,
                attributes->>'state'
            ORDER BY recorded_timestamp DESC
        ) AS observation_rank
    FROM metrics
    WHERE metric_name IN ('oddish.queue.jobs', 'oddish.queue.slots')
      AND service_name = 'oddish-worker'
      AND deployment_environment = 'production'
      AND attributes->>'queue_key' <> '__all__'
), bucketed AS (
    SELECT
        bucket,
        service_name,
        deployment_environment,
        queue_key,
        MAX(
            CASE
                WHEN metric_name = 'oddish.queue.jobs' AND state = 'queued'
                THEN observed_value
            END
        ) AS queued_jobs,
        MAX(
            CASE
                WHEN metric_name = 'oddish.queue.slots' AND state = 'held'
                THEN observed_value
            END
        ) AS held_slots,
        MAX(
            CASE
                WHEN metric_name = 'oddish.queue.slots' AND state = 'limit'
                THEN observed_value
            END
        ) AS slot_limit
    FROM ranked
    WHERE observation_rank = 1
    GROUP BY bucket, service_name, deployment_environment, queue_key
), saturation AS (
    SELECT
        service_name,
        deployment_environment,
        queue_key,
        COUNT(*) AS observed_buckets,
        SUM(
            CASE
                WHEN queued_jobs > 0
                 AND slot_limit > 0
                 AND held_slots >= slot_limit
                THEN 1
                ELSE 0
            END
        ) AS saturated_buckets,
        MAX(queued_jobs) AS max_queued_jobs,
        MAX(held_slots) AS max_held_slots,
        MAX(slot_limit) AS configured_slot_limit
    FROM bucketed
    GROUP BY service_name, deployment_environment, queue_key
)
SELECT
    service_name,
    deployment_environment,
    queue_key,
    observed_buckets,
    saturated_buckets,
    max_queued_jobs,
    max_held_slots,
    configured_slot_limit
FROM saturation
WHERE observed_buckets >= 3
  AND saturated_buckets >= 3;

-- 4. Dispatcher telemetry no longer arriving
-- Suggested policy: evaluate every 5 minutes over the last 10 minutes and use
-- Logfire's "query results change" notification mode. The query always returns
-- one row so it can move from receiving to missing and back to receiving.
-- A change to "missing" means no dispatch-cycle metric row arrived inside the
-- alert's selected time window; a change back to "receiving" is recovery.
SELECT
    CASE
        WHEN COUNT(*) = 0 THEN 'missing'
        ELSE 'receiving'
    END AS dispatcher_telemetry,
    COUNT(*) AS metric_rows
FROM metrics
WHERE metric_name = 'oddish.dispatch.cycles'
  AND service_name = 'oddish-worker'
  AND deployment_environment = 'production';
