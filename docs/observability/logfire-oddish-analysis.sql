-- Oddish Analysis Performance dashboard query catalog
--
-- Dashboard variables:
--   $deployment_environment: exact metrics.deployment_environment value
--   $service_name: usually oddish-worker
--   $analysis_kind: qa, audit, summarize, or __all__
--   $queue_key: a model queue key, or __all__
--   $outcome: success, error, cancelled, or __all__
--   $source: worker, cleanup, or __all__
--
-- The metric has bounded attributes. Trial, task, and worker-job IDs exist on
-- the corresponding analysis.* spans for drill-down and never on this metric.

-- 1. Creation-to-import duration by analysis kind
SELECT
    time_bucket($resolution, recorded_timestamp) AS x,
    attributes->>'analysis_kind' AS analysis_kind,
    metric_quantile(0.50, value) AS p50_seconds,
    metric_quantile(0.95, value) AS p95_seconds
FROM metrics
WHERE metric_name = 'oddish.analysis.stage.duration'
  AND deployment_environment = $deployment_environment
  AND service_name = $service_name
  AND attributes->>'stage' = 'end_to_end'
  AND ($analysis_kind = '__all__' OR attributes->>'analysis_kind' = $analysis_kind)
  AND ($queue_key = '__all__' OR attributes->>'queue_key' = $queue_key)
  AND ($outcome = '__all__' OR attributes->>'outcome' = $outcome)
  AND ($source = '__all__' OR attributes->>'source' = $source)
GROUP BY x, analysis_kind
ORDER BY x;

-- 2. Runtime and persistence duration by stage
SELECT
    time_bucket($resolution, recorded_timestamp) AS x,
    attributes->>'stage' AS stage,
    metric_quantile(0.50, value) AS p50_seconds,
    metric_quantile(0.95, value) AS p95_seconds
FROM metrics
WHERE metric_name = 'oddish.analysis.stage.duration'
  AND deployment_environment = $deployment_environment
  AND service_name = $service_name
  AND attributes->>'stage' NOT LIKE 'activity.%'
  AND attributes->>'stage' <> 'end_to_end'
  AND ($analysis_kind = '__all__' OR attributes->>'analysis_kind' = $analysis_kind)
  AND ($queue_key = '__all__' OR attributes->>'queue_key' = $queue_key)
  AND ($outcome = '__all__' OR attributes->>'outcome' = $outcome)
  AND ($source = '__all__' OR attributes->>'source' = $source)
GROUP BY x, stage
ORDER BY x;

-- 3. Deterministic activity duration inside the analysis agent
SELECT
    time_bucket($resolution, recorded_timestamp) AS x,
    replace(attributes->>'stage', 'activity.', '') AS activity,
    metric_quantile(0.50, value) AS p50_seconds,
    metric_quantile(0.95, value) AS p95_seconds
FROM metrics
WHERE metric_name = 'oddish.analysis.stage.duration'
  AND deployment_environment = $deployment_environment
  AND service_name = $service_name
  AND attributes->>'stage' LIKE 'activity.%'
  AND ($analysis_kind = '__all__' OR attributes->>'analysis_kind' = $analysis_kind)
  AND ($queue_key = '__all__' OR attributes->>'queue_key' = $queue_key)
  AND ($outcome = '__all__' OR attributes->>'outcome' = $outcome)
  AND ($source = '__all__' OR attributes->>'source' = $source)
GROUP BY x, activity
ORDER BY x;

-- 4. Creation-to-import duration by workload bucket and retry state
SELECT
    time_bucket($resolution, recorded_timestamp) AS x,
    attributes->>'target_bucket'
        || CASE
            WHEN attributes->>'retried' = 'true' THEN ' retried'
            ELSE ' first attempt'
        END AS workload,
    metric_quantile(0.50, value) AS p50_seconds,
    metric_quantile(0.95, value) AS p95_seconds
FROM metrics
WHERE metric_name = 'oddish.analysis.stage.duration'
  AND deployment_environment = $deployment_environment
  AND service_name = $service_name
  AND attributes->>'stage' = 'end_to_end'
  AND ($analysis_kind = '__all__' OR attributes->>'analysis_kind' = $analysis_kind)
  AND ($queue_key = '__all__' OR attributes->>'queue_key' = $queue_key)
  AND ($outcome = '__all__' OR attributes->>'outcome' = $outcome)
  AND ($source = '__all__' OR attributes->>'source' = $source)
GROUP BY x, workload
ORDER BY x;
