import type {
  TaskBrowseItem,
  TaskOpenResponse,
  TaskOpenTrialRef,
} from "@/lib/types";
import { summarizeTrials } from "@/lib/trial-aggregation";

export type TaskOpenResource =
  | TaskOpenResponse
  | { source: "browse"; open: TaskOpenResponse };

export function taskOpenKey(taskId: string, versionId?: string | null): string {
  const path = `/api/tasks/${encodeURIComponent(taskId)}/open`;
  return versionId
    ? `${path}?version_id=${encodeURIComponent(versionId)}`
    : path;
}

export function taskOpenValue(
  resource: TaskOpenResource | undefined
): TaskOpenResponse | undefined {
  return resource && "source" in resource ? resource.open : resource;
}

export function isBrowseTaskOpen(
  resource: TaskOpenResource | undefined
): resource is { source: "browse"; open: TaskOpenResponse } {
  return resource !== undefined && "source" in resource;
}

export function taskOpenFromBrowse(browse: TaskBrowseItem): TaskOpenResource {
  const trials: TaskOpenTrialRef[] = browse.latest_trials.map((trial) => ({
    ...trial,
    provider: "",
    experiment_id: null,
    task_version_id: browse.current_version_id,
    error_kind: trial.error_message ? "error" : null,
    is_probe: false,
    is_billed: false,
    created_at: browse.last_run_at ?? "",
  }));
  const preview = summarizeTrials(
    trials.map((trial) => ({
      ...trial,
      task_id: browse.id,
      task_path: "",
      attempts: 0,
      max_attempts: 0,
      harbor_stage: null,
      error_message: trial.error_kind ? "Trial error" : null,
    }))
  );
  const active = Math.max(
    browse.total_trials -
      browse.completed_trials -
      browse.failed_trials -
      preview.skipped,
    0
  );
  const status =
    browse.total_trials === 0
      ? "pending"
      : active > 0
        ? "running"
        : browse.completed_trials === 0 && browse.failed_trials > 0
          ? "failed"
          : "completed";
  const version =
    browse.current_version_id != null && browse.current_version != null
      ? {
          id: browse.current_version_id,
          version: browse.current_version,
          created_at: browse.last_run_at ?? "",
          is_current: true,
        }
      : null;

  return {
    source: "browse",
    open: {
      task: {
        id: browse.id,
        name: browse.name,
        status,
        priority: "low",
        user: "",
        github_meta: browse.github_meta,
        link: browse.link,
        task_path: "",
        experiments: browse.experiments,
        current_version: browse.current_version,
        current_version_id: browse.current_version_id,
        user_tags: browse.user_tags,
        run_analysis: false,
        created_at: "",
        updated_at: browse.last_run_at ?? "",
      },
      default_version: version,
      selected_version: version
        ? {
            ...version,
            trial_count: browse.total_trials,
            completed_count: browse.completed_trials,
            failed_count: browse.failed_trials,
            skipped_count: preview.skipped,
            pass_count: preview.passCount,
            partial_count: preview.partialCount,
            fail_count: preview.failCount,
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
            agent_models: [],
          }
        : null,
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
        token_count: 0,
        token_trial_count: 0,
      },
      trials,
      trials_has_more: browse.total_trials > trials.length,
    },
  };
}
