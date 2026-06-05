import type { JobStatus, Task, Trial, VisibleWorkerJob } from "@/lib/types";

const ACTIVE_TRIAL_STATUSES = [
  "running",
  "queued",
  "retrying",
  "pending",
] as const;
const ACTIVE_PIPELINE_STATUSES = ["pending", "queued", "running"] as const;
const ACTIVE_VISIBLE_JOB_STATUSES = [
  "queued",
  "running",
  "retrying",
  "blocked",
] as const;

function isActiveTrialStatus(status: string | null | undefined): boolean {
  return ACTIVE_TRIAL_STATUSES.includes(
    status as (typeof ACTIVE_TRIAL_STATUSES)[number]
  );
}

export function isActivePipelineStatus(
  status: JobStatus | string | null | undefined
): boolean {
  return ACTIVE_PIPELINE_STATUSES.includes(
    status as (typeof ACTIVE_PIPELINE_STATUSES)[number]
  );
}

function isActiveVisibleJob(job: VisibleWorkerJob): boolean {
  return ACTIVE_VISIBLE_JOB_STATUSES.includes(
    job.status as (typeof ACTIVE_VISIBLE_JOB_STATUSES)[number]
  );
}

function isActiveVisibleJobKind(
  job: VisibleWorkerJob,
  kind: "trial" | "analysis" | "verdict"
): boolean {
  return job.kind === kind && isActiveVisibleJob(job);
}

export function trialHasActiveAnalysis(
  trial: Trial | null | undefined
): boolean {
  if (!trial) return false;
  return (
    isActivePipelineStatus(trial.analysis_status) ||
    trial.jobs?.some((job) => isActiveVisibleJobKind(job, "analysis")) === true
  );
}

export function taskHasActiveTrials(task: Task | null | undefined): boolean {
  return (
    task?.trials?.some(
      (trial) =>
        isActiveTrialStatus(trial.status) ||
        trial.jobs?.some((job) => isActiveVisibleJobKind(job, "trial"))
    ) === true
  );
}

export function taskHasActiveAnalysis(task: Task | null | undefined): boolean {
  if (!task) return false;
  return (
    task.status === "analyzing" ||
    task.trials?.some((trial) => trialHasActiveAnalysis(trial)) === true
  );
}

export function taskHasActiveVerdict(task: Task | null | undefined): boolean {
  if (!task) return false;
  return (
    task.status === "verdict_pending" ||
    isActivePipelineStatus(task.verdict_status) ||
    task.jobs?.some((job) => isActiveVisibleJobKind(job, "verdict")) === true
  );
}

export function taskHasCancellableWork(task: Task | null | undefined): boolean {
  if (!task) return false;
  if (task.jobs?.some(isActiveVisibleJob)) return true;
  return (
    taskHasActiveTrials(task) ||
    taskHasActiveAnalysis(task) ||
    taskHasActiveVerdict(task)
  );
}

function getActiveTrialCount(task: Task | null | undefined): number {
  return (task?.trials ?? []).filter((trial) =>
    isActiveTrialStatus(trial.status)
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
