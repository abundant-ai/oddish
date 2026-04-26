import type { JobStatus, Task, Trial, VisibleWorkerJob } from "@/lib/types";

export const ACTIVE_TRIAL_STATUSES = [
  "running",
  "queued",
  "retrying",
  "pending",
] as const;
export const ACTIVE_PIPELINE_STATUSES = [
  "pending",
  "queued",
  "running",
] as const;
export const ACTIVE_VISIBLE_JOB_STATUSES = [
  "queued",
  "running",
  "retrying",
  "blocked",
] as const;

export function isActiveTrialStatus(
  status: string | null | undefined,
): boolean {
  return ACTIVE_TRIAL_STATUSES.includes(
    status as (typeof ACTIVE_TRIAL_STATUSES)[number],
  );
}

export function isActivePipelineStatus(
  status: JobStatus | string | null | undefined,
): boolean {
  return ACTIVE_PIPELINE_STATUSES.includes(
    status as (typeof ACTIVE_PIPELINE_STATUSES)[number],
  );
}

export function isActiveVisibleJob(job: VisibleWorkerJob): boolean {
  return ACTIVE_VISIBLE_JOB_STATUSES.includes(
    job.status as (typeof ACTIVE_VISIBLE_JOB_STATUSES)[number],
  );
}

export function taskHasCancellableWork(task: Task | null | undefined): boolean {
  if (!task) return false;
  if (task.jobs?.some(isActiveVisibleJob)) return true;
  return (
    task.status === "analyzing" ||
    task.status === "verdict_pending" ||
    isActivePipelineStatus(task.verdict_status) ||
    (task.trials ?? []).some(
      (trial) =>
        isActiveTrialStatus(trial.status) ||
        isActivePipelineStatus(trial.analysis_status) ||
        trial.jobs?.some(isActiveVisibleJob),
    )
  );
}

export function getActiveTrialCount(task: Task | null | undefined): number {
  return (task?.trials ?? []).filter((trial) =>
    isActiveTrialStatus(trial.status),
  ).length;
}

export function getActiveAnalysisCount(task: Task | null | undefined): number {
  return (task?.trials ?? []).filter((trial) =>
    isActivePipelineStatus(trial.analysis_status),
  ).length;
}

export function getCancelActionLabel(task: Task | null | undefined): string {
  const activeTrials = getActiveTrialCount(task);
  if (activeTrials > 0) return `Cancel (${activeTrials})`;
  if (
    task?.status === "verdict_pending" ||
    isActivePipelineStatus(task?.verdict_status)
  ) {
    return "Cancel verdict";
  }
  return "Cancel analysis";
}

export function trialHasActiveAnalysis(trial: Trial): boolean {
  return (
    isActivePipelineStatus(trial.analysis_status) ||
    Boolean(
      trial.jobs?.some(
        (job) => job.kind === "analysis" && isActiveVisibleJob(job),
      ),
    )
  );
}
