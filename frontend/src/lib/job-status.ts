import type { JobStatus, Task, Trial, VisibleWorkerJob } from "@/lib/types";
import { isAgentTrial } from "@/lib/types";

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

function isActiveVisibleJob(job: VisibleWorkerJob): boolean {
  return ACTIVE_VISIBLE_JOB_STATUSES.includes(
    job.status as (typeof ACTIVE_VISIBLE_JOB_STATUSES)[number],
  );
}

function isActiveVisibleJobKind(
  job: VisibleWorkerJob,
  kind: "trial" | "qa" | "analysis",
): boolean {
  return job.kind === kind && isActiveVisibleJob(job);
}

function trialHasActiveAnalysis(trial: Trial | null | undefined): boolean {
  if (!trial) return false;
  return (
    isActivePipelineStatus(trial.analysis_status) ||
    trial.jobs?.some((job) => isActiveVisibleJobKind(job, "analysis")) === true
  );
}

export function taskHasActiveTrials(task: Task | null | undefined): boolean {
  // Agent trials only. A live qa/audit trial counts as active QA (see
  // taskHasLiveQaTrial), so the cancel path picks the QA cancel endpoint
  // instead of the whole-task one.
  return (
    task?.trials?.some(
      (trial) =>
        isAgentTrial(trial) &&
        (isActiveTrialStatus(trial.status) ||
          trial.jobs?.some((job) => isActiveVisibleJobKind(job, "trial"))),
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

// QA and the source audit run as qa/audit-kind trials now. A live one means
// analysis is in progress no matter what the status flags say -- after a
// crash the flags can be stale. The qa/cancel endpoint cancels both kinds.
export function taskHasLiveAnalysisTrial(
  task: Task | null | undefined,
): boolean {
  return (
    task?.trials?.some(
      (trial) =>
        !isAgentTrial(trial) &&
        !trial.superseded_by_trial_id &&
        isActiveTrialStatus(trial.status),
    ) === true
  );
}

export function taskHasActiveVerdict(task: Task | null | undefined): boolean {
  if (!task) return false;
  return (
    task.status === "verdict_pending" ||
    isActivePipelineStatus(task.verdict_status) ||
    taskHasLiveAnalysisTrial(task) ||
    task.jobs?.some((job) => isActiveVisibleJobKind(job, "qa")) === true
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
  // Agent trials only: a running qa/audit trial should read as "Cancel QA",
  // not as a mystery "Cancel (1)".
  return (task?.trials ?? []).filter(
    (trial) => isAgentTrial(trial) && isActiveTrialStatus(trial.status),
  ).length;
}

export function getCancelActionLabel(task: Task | null | undefined): string {
  const activeTrials = getActiveTrialCount(task);
  if (activeTrials > 0) return `Cancel (${activeTrials})`;
  // Trajectory analysis + verdict are one task-level QA job now.
  return "Cancel QA";
}
