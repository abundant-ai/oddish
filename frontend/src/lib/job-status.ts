import type { JobStatus, Task, Trial, VisibleWorkerJob } from "@/lib/types";
import { isAgentTrial } from "@/lib/types";

const ACTIVE_TRIAL_STATUSES = [
  "running",
  "paused",
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

const WORKER_OWNED_TRIAL_STATUSES = ["running", "paused"] as const;

export function isActiveTrialStatus(
  status: string | null | undefined
): boolean {
  return ACTIVE_TRIAL_STATUSES.includes(
    status as (typeof ACTIVE_TRIAL_STATUSES)[number]
  );
}

export function isWorkerOwnedTrialStatus(
  status: string | null | undefined
): boolean {
  return WORKER_OWNED_TRIAL_STATUSES.includes(
    status as (typeof WORKER_OWNED_TRIAL_STATUSES)[number]
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
  kind: "trial" | "qa" | "analysis"
): boolean {
  return job.kind === kind && isActiveVisibleJob(job);
}

export function taskHasActiveTrials(task: Task | null | undefined): boolean {
  // Agent trials only. A live qa/audit trial counts as active QA (see
  // taskHasLiveAnalysisTrial), so the cancel path picks the QA cancel
  // endpoint instead of the whole-task one.
  return (
    task?.trials?.some(
      (trial) =>
        isAgentTrial(trial) &&
        (isActiveTrialStatus(trial.status) ||
          trial.jobs?.some((job) => isActiveVisibleJobKind(job, "trial")))
    ) === true
  );
}

export function taskHasActiveAnalysis(task: Task | null | undefined): boolean {
  return taskAnalysisProgress(task) !== null;
}

/** Active job/trial records take precedence over aggregate task flags. */
export function taskAnalysisProgress(
  task: Task | null | undefined
): "queued" | "running" | null {
  if (!task) return null;
  const jobs =
    task.jobs?.filter((job) => isActiveVisibleJobKind(job, "analysis")) ?? [];
  if (jobs.some((job) => job.status === "running")) return "running";
  let queued = jobs.length > 0;
  for (const trial of task.trials ?? []) {
    if (trial.superseded_by_trial_id) continue;
    const trialJobs =
      trial.jobs?.filter((job) => isActiveVisibleJobKind(job, "analysis")) ??
      [];
    if (trialJobs.length > 0) {
      if (trialJobs.some((job) => job.status === "running")) return "running";
      queued = true;
    } else if (trial.analysis_status === "running") {
      return "running";
    } else if (isActivePipelineStatus(trial.analysis_status)) {
      queued = true;
    }
  }
  if (queued) return "queued";
  return task.status === "analyzing" ? "running" : null;
}

// QA and the source audit run as qa/audit-kind trials now. A live one means
// analysis is in progress no matter what the status flags say -- after a
// crash the flags can be stale. The qa/cancel endpoint cancels both kinds.
export function isLiveAnalysisTrial(trial: Trial): boolean {
  return (
    (trial.kind === "qa" || trial.kind === "audit") &&
    !trial.superseded_by_trial_id &&
    isActiveTrialStatus(trial.status)
  );
}

export function taskHasLiveAnalysisTrial(
  task: Task | null | undefined
): boolean {
  return (
    (task?.active_qa_trial != null &&
      isLiveAnalysisTrial(task.active_qa_trial)) ||
    task?.trials?.some(isLiveAnalysisTrial) === true
  );
}

// The qa kind specifically: verdict presentation must not read a live
// pre-trial audit as "QA is running" (the audit is a per-version source
// check, not the task verdict), so anything that DISPLAYS verdict progress
// keys off these, while cancellation keeps using the analysis-wide
// predicates above.
export function isLiveQaTrial(trial: Trial): boolean {
  return (
    trial.kind === "qa" &&
    !trial.superseded_by_trial_id &&
    isActiveTrialStatus(trial.status)
  );
}

export function taskHasActiveVerdict(task: Task | null | undefined): boolean {
  return taskVerdictProgress(task) !== null;
}

export function taskVerdictProgress(
  task: Task | null | undefined
): "queued" | "running" | null {
  if (!task) return null;
  const qaTrials = [
    ...(task.active_qa_trial ? [task.active_qa_trial] : []),
    ...(task.trials ?? []),
  ].filter((trial) => trial.kind === "qa" && !trial.superseded_by_trial_id);
  const jobs = [
    ...(task.jobs?.filter((job) => isActiveVisibleJobKind(job, "qa")) ?? []),
    ...qaTrials.flatMap(
      (trial) =>
        trial.jobs?.filter(
          (job) =>
            (job.kind === "qa" || job.kind === "trial") &&
            isActiveVisibleJob(job)
        ) ?? []
    ),
  ];
  if (jobs.length > 0)
    return jobs.some((job) => job.status === "running") ? "running" : "queued";
  if (task.active_qa_trial && isLiveQaTrial(task.active_qa_trial))
    return isWorkerOwnedTrialStatus(task.active_qa_trial.status)
      ? "running"
      : "queued";
  const trials = qaTrials.filter(isLiveQaTrial);
  if (trials.length > 0)
    return trials.some((trial) => isWorkerOwnedTrialStatus(trial.status))
      ? "running"
      : "queued";
  if (isActivePipelineStatus(task.verdict_status))
    return task.verdict_status === "running" ? "running" : "queued";
  // This stage is entered as the job is queued; it does not prove execution.
  return task.status === "verdict_pending" && task.verdict_status == null
    ? "queued"
    : null;
}

/** Short experiment-row copy for a rejected task. */
export function rejectedMustFixLabel(task: Task): string {
  const count = task.must_fix_count ?? 0;
  if (count > 0) {
    return `Rejected: ${count} Must Fix`;
  }
  return "Rejected";
}

export function taskHasCancellableWork(task: Task | null | undefined): boolean {
  if (!task) return false;
  if (task.jobs?.some(isActiveVisibleJob)) return true;
  return (
    taskHasActiveTrials(task) ||
    taskHasActiveAnalysis(task) ||
    taskHasActiveVerdict(task) ||
    // A live audit is cancellable work (qa/cancel covers both kinds) even
    // though it no longer counts as an active verdict above.
    taskHasLiveAnalysisTrial(task)
  );
}

function getActiveTrialCount(task: Task | null | undefined): number {
  // Agent trials only: verdict generation and pre-trial audits have named
  // cancellation actions rather than contributing to the agent-run count.
  return (task?.trials ?? []).filter(
    (trial) => isAgentTrial(trial) && isActiveTrialStatus(trial.status)
  ).length;
}

export function getCancelActionLabel(task: Task | null | undefined): string {
  const activeTrials = getActiveTrialCount(task);
  if (activeTrials > 0) return `Cancel (${activeTrials})`;
  // Run analysis can be an independent job, not verdict generation. Its
  // cancellation (or work with no known kind) uses the generic action label.
  if (taskHasActiveAnalysis(task)) return "Cancel";
  const verdictActive = taskHasActiveVerdict(task);
  const auditActive =
    isActivePipelineStatus(task?.pre_trial_status) ||
    (task?.active_qa_trial?.kind === "audit" &&
      isLiveAnalysisTrial(task.active_qa_trial)) ||
    task?.trials?.some(
      (trial) => trial.kind === "audit" && isLiveAnalysisTrial(trial)
    );
  if (verdictActive && auditActive)
    return "Cancel audits and verdict generation";
  if (verdictActive) return "Cancel verdict generation";
  if (auditActive) return "Cancel pre-trial audit";
  return "Cancel";
}
