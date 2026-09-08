import type { JobUsage, ModelUsage, QueueStats } from "./types";

function inferProviderFromQueueKey(queueKey: string): string {
  const [provider] = queueKey.split("/", 1);
  return provider || "unknown";
}

// Reserved presentation buckets in the queues payload for the
// trajectory-analysis / verdict pipelines (mirrors ANALYSIS_PIPELINE_QUEUE_KEY
// / VERDICT_PIPELINE_QUEUE_KEY in oddish.config). They carry ALL-TIME
// trials.analysis_status / tasks.verdict_status counts, not windowed model
// usage, so rendering them as model rows would pin a pseudo-model with
// hundreds of thousands of "jobs" to the top of the Usage table and inflate
// the running/queued totals with pipeline state. Pipeline counts surface via
// the per-kind breakdown instead; keep them out of the model rows.
const PIPELINE_QUEUE_KEYS = new Set(["analysis", "verdict"]);

function getQueueTotalJobs(stats?: QueueStats[string]): number {
  if (!stats) return 0;
  return (
    (Number(stats.pending) || 0) +
    (Number(stats.queued) || 0) +
    (Number(stats.running) || 0) +
    (Number(stats.retrying) || 0) +
    (Number(stats.success) || 0) +
    (Number(stats.failed) || 0) +
    // Gate-skipped trials are terminal and now land in their own queue bucket;
    // include them or the queue's total job count drops by every skipped trial.
    (Number(stats.skipped) || 0)
  );
}

type UsageRow = {
  key: string;
  queueKey: string;
  model: string;
  provider: string;
  jobCount: number;
  inputTokens: number;
  outputTokens: number;
  cacheTokens: number;
  costUsd: number;
  costEstimatedUsd: number;
  running: number;
  queued: number;
  retrying: number;
  avgDurationS: number | null;
  hasUsageMetrics: boolean;
};

// Merge per-model usage, per-queue worker-job stats, and raw queue stats into
// one row per queue key. Shared by the full Usage table and the Dashboard
// summary so the running/queued/retrying counts stay identical across both.
export function buildUsageRows(
  jobUsage: JobUsage[],
  modelUsage: ModelUsage[],
  queues: QueueStats | null
): UsageRow[] {
  const mergedRows = new Map<string, UsageRow>();
  const jobUsageByQueue = new Map<
    string,
    {
      jobCount: number;
      running: number;
      queued: number;
      retrying: number;
      durationTotalS: number;
      durationCount: number;
      avgDurationS: number | null;
    }
  >();

  for (const job of jobUsage) {
    const existing = jobUsageByQueue.get(job.queue_key) ?? {
      jobCount: 0,
      running: 0,
      queued: 0,
      retrying: 0,
      durationTotalS: 0,
      durationCount: 0,
      avgDurationS: null,
    };
    existing.jobCount += job.job_count;
    existing.running += job.running;
    existing.queued += job.queued;
    existing.retrying += job.retrying;
    if (job.avg_duration_s != null && job.job_count > 0) {
      existing.durationTotalS += job.avg_duration_s * job.job_count;
      existing.durationCount += job.job_count;
      existing.avgDurationS =
        existing.durationCount > 0
          ? existing.durationTotalS / existing.durationCount
          : null;
    }
    jobUsageByQueue.set(job.queue_key, existing);
  }

  for (const usage of modelUsage) {
    const queueKey = usage.model || usage.provider || "unknown";
    const jobsForQueue = jobUsageByQueue.get(queueKey);
    const queueStats = queues?.[queueKey];
    mergedRows.set(queueKey, {
      key: queueKey,
      queueKey,
      model: usage.model,
      provider: usage.provider,
      jobCount:
        jobsForQueue?.jobCount ||
        getQueueTotalJobs(queueStats) ||
        usage.trial_count,
      inputTokens: usage.input_tokens,
      outputTokens: usage.output_tokens,
      cacheTokens: usage.cache_tokens,
      costUsd: usage.cost_usd,
      costEstimatedUsd: usage.cost_estimated_usd ?? 0,
      // Live status comes only from worker_jobs, including jobs older than
      // the cost window. Historical trial mirrors can outlive their jobs.
      running: jobsForQueue?.running ?? 0,
      queued: jobsForQueue?.queued ?? 0,
      retrying: jobsForQueue?.retrying ?? 0,
      avgDurationS: jobsForQueue?.avgDurationS ?? usage.avg_duration_s,
      hasUsageMetrics: true,
    });
  }

  for (const [queueKey, jobsForQueue] of jobUsageByQueue) {
    if (mergedRows.has(queueKey)) continue;

    mergedRows.set(queueKey, {
      key: queueKey,
      queueKey,
      model: queueKey,
      provider: inferProviderFromQueueKey(queueKey),
      jobCount: jobsForQueue.jobCount,
      inputTokens: 0,
      outputTokens: 0,
      cacheTokens: 0,
      costUsd: 0,
      costEstimatedUsd: 0,
      running: jobsForQueue.running,
      queued: jobsForQueue.queued,
      retrying: jobsForQueue.retrying,
      avgDurationS: jobsForQueue.avgDurationS,
      hasUsageMetrics: false,
    });
  }

  for (const [queueKey, queueStats] of Object.entries(queues ?? {})) {
    if (PIPELINE_QUEUE_KEYS.has(queueKey)) continue;
    const totalJobs = getQueueTotalJobs(queueStats);
    if (mergedRows.has(queueKey) || totalJobs === 0) continue;

    mergedRows.set(queueKey, {
      key: queueKey,
      queueKey,
      model: queueKey,
      provider: inferProviderFromQueueKey(queueKey),
      jobCount: totalJobs,
      inputTokens: 0,
      outputTokens: 0,
      cacheTokens: 0,
      costUsd: 0,
      costEstimatedUsd: 0,
      running: 0,
      queued: 0,
      retrying: 0,
      avgDurationS: null,
      hasUsageMetrics: false,
    });
  }

  return Array.from(mergedRows.values());
}
