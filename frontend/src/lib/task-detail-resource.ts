import type {
  Task,
  TaskBrowseItem,
  TaskDetailResponse,
  TaskVersionSummary,
  Trial,
} from "@/lib/types";
import { summarizeTrials } from "@/lib/trial-aggregation";

export type TaskDetailResource =
  | TaskDetailResponse
  | { source: "browse"; detail: TaskDetailResponse };

export function taskDetailKey(taskId: string, apiBaseUrl = "/api"): string {
  return `${apiBaseUrl.replace(/\/$/, "")}/tasks/${encodeURIComponent(taskId)}/detail`;
}

export function taskDetailValue(
  resource: TaskDetailResource | undefined
): TaskDetailResponse | undefined {
  return resource && "source" in resource ? resource.detail : resource;
}

export function isBrowseTaskDetail(
  resource: TaskDetailResource | undefined
): boolean {
  return resource !== undefined && "source" in resource;
}

export function taskDetailFromBrowse(
  browse: TaskBrowseItem
): TaskDetailResource {
  const trials: Trial[] = browse.latest_trials.map((trial) => ({
    ...trial,
    task_id: browse.id,
    task_path: "",
    provider: "",
    attempts: 0,
    max_attempts: 0,
    harbor_stage: null,
    task_version: browse.current_version,
    task_version_id: browse.current_version_id,
    created_at: browse.last_run_at ?? "",
  }));
  const knownTrials = summarizeTrials(trials);
  const active = Math.max(
    browse.total_trials -
      browse.completed_trials -
      browse.failed_trials -
      knownTrials.skipped,
    0
  );
  const primaryExperiment = browse.experiments[0];

  const task: Task = {
    id: browse.id,
    name: browse.name,
    status:
      browse.total_trials === 0
        ? "pending"
        : active > 0
          ? "running"
          : browse.completed_trials === 0 && browse.failed_trials > 0
            ? "failed"
            : "completed",
    priority: "low",
    user: "",
    link: browse.link,
    github_meta: browse.github_meta,
    task_path: "",
    experiment_id: primaryExperiment?.id ?? "",
    experiment_name: primaryExperiment?.name ?? "",
    experiment_is_public: false,
    experiments: browse.experiments,
    total: browse.total_trials,
    completed: browse.completed_trials,
    failed: browse.failed_trials,
    skipped: knownTrials.skipped,
    reward_success: browse.reward_success,
    reward_sum: browse.reward_sum,
    reward_total: browse.reward_total,
    current_version: browse.current_version,
    current_version_id: browse.current_version_id,
    trial_version: browse.current_version,
    trial_version_id: browse.current_version_id,
    trials,
    user_tags: browse.user_tags,
    created_at: "",
    updated_at: browse.last_run_at ?? "",
  };

  const version: TaskVersionSummary | null =
    browse.current_version_id == null || browse.current_version == null
      ? null
      : {
          id: browse.current_version_id,
          version: browse.current_version,
          created_at: browse.last_run_at ?? "",
          is_current: true,
          trial_count: browse.total_trials,
          completed_count: browse.completed_trials,
          failed_count: browse.failed_trials,
          skipped_count: knownTrials.skipped,
          pass_count: knownTrials.passCount,
          partial_count: knownTrials.partialCount,
          fail_count: knownTrials.failCount,
          pending_count: active,
          reward_sum: browse.reward_sum,
          reward_total: browse.reward_total,
          cost_usd: browse.cost_usd,
          cost_trial_count: browse.cost_trial_count,
          cost_has_estimated: browse.cost_has_estimated,
          cost_has_native: browse.cost_has_native,
          billed_cost_usd: browse.billed_cost_usd,
          billed_trial_count: browse.billed_trial_count,
          billed_has_estimated: browse.billed_has_estimated,
          billed_has_native: browse.billed_has_native,
          last_run_at: browse.last_run_at,
          user_tags: browse.user_tags,
          experiments: browse.experiments,
        };

  return {
    source: "browse",
    detail: {
      task,
      versions: version ? [version] : [],
      // Browse totals are current-version scoped. Keep all-version totals
      // unknown until /detail replaces this snapshot.
      totals: {
        cost_usd: 0,
        cost_trial_count: 0,
        cost_has_estimated: false,
        cost_has_native: false,
        billed_cost_usd: 0,
        billed_trial_count: 0,
        billed_has_estimated: false,
        billed_has_native: false,
        total_trials: 0,
      },
    },
  };
}
