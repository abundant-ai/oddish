import records from "../fixtures/review-records.json";
import type {
  Task,
  Trial,
  TaskOpenResponse,
  TaskVersionSummary,
  TaskOpenVersionSummary,
  DeliveryBoardResponse,
} from "@/lib/types";
export const now = "2026-09-09T12:00:00Z";
export const tasks: Task[] = records.map((record) => ({
  id: record.id,
  name: record.name,
  status: "completed",
  priority: "low",
  user: "maya",
  task_path: `tasks/${record.id}`,
  experiment_id: "review-demo",
  experiment_name: "Review meaning fixtures",
  experiment_is_public: false,
  total: record.classification ? 1 : 0,
  completed: record.classification ? 1 : 0,
  failed: 0,
  created_at: now,
  updated_at: now,
  current_version: record.version,
  current_version_id: `${record.id}-v${record.version}`,
  trial_version: record.version,
  trial_version_id: `${record.id}-v${record.version}`,
  run_analysis: true,
  verdict_status: record.verdict_status as Task["verdict_status"],
  verdict:
    record.is_good == null
      ? null
      : {
          is_good: record.is_good,
          confidence: "high",
          primary_issue: record.finding?.title,
          reasoning: record.is_good
            ? "The current review found no blocking defects."
            : record.finding?.detail,
        },
  verdict_error: record.error,
  must_fix_count: record.finding ? 1 : 0,
  review_version_matches: !record.historical_version,
  trials: record.classification
    ? [
        {
          id: `${record.id}-trial`,
          name: `${record.id}-trial`,
          task_id: record.id,
          task_version: record.version,
          task_version_id: `${record.id}-v${record.version}`,
          kind: "agent",
          agent: "codex",
          model: "openai/gpt-5.6",
          provider: "openai",
          status:
            record.classification === "HARNESS_ERROR" ? "failed" : "success",
          reward: record.classification === "GOOD_SUCCESS" ? 1 : 0,
          created_at: now,
          finished_at: now,
          analysis_status: "success",
          analysis: {
            classification: record.classification,
            root_cause:
              record.classification === "GOOD_FAILURE"
                ? "The agent made a reasoning mistake. The unrelated source defect did not cause this failure."
                : "Recorded execution evidence",
            evidence: "The verifier recorded the result.",
            action_items: [],
          },
          has_trajectory: false,
          harbor_stage: null,
          error_message: null,
          attempts: 1,
          max_attempts: 1,
          task_path: `tasks/${record.id}`,
          experiment_id: "review-demo",
        } as Trial,
      ]
    : [],
}));
export function versionFor(
  task: Task,
  version: number
): TaskVersionSummary & TaskOpenVersionSummary {
  const record = records.find((item) => item.id === task.id)!;
  const summary = {
    id: `${task.id}-v${version}`,
    version,
    is_current: version === task.current_version,
    created_at: now,
    trial_count: task.total,
    completed_count: task.completed,
    failed_count: 0,
    skipped_count: 0,
    pending_count: 0,
    pass_count: 0,
    partial_count: 0,
    fail_count: task.total,
    reward_sum: 0,
    reward_total: task.total,
    cost_usd: 0,
    cost_trial_count: 0,
    cost_has_estimated: false,
    cost_has_native: false,
    billed_cost_usd: 0,
    billed_trial_count: 0,
    billed_has_estimated: false,
    billed_has_native: false,
    user_tags: [],
    experiments: [],
    agent_models: [],
    pre_trial_status:
      version === record.historical_version ? "success" : record.source_status,
    pre_trial_error: record.error,
    pre_trial_findings: record.finding ? [record.finding] : [],
  } as TaskVersionSummary & TaskOpenVersionSummary;
  if (task.total)
    summary.agent_models = [
      {
        ...summary,
        agent: "codex",
        model: "openai/gpt-5.6",
        providers: ["openai"],
        is_probe: false,
        duration_sum_seconds: 0,
        duration_trial_count: 0,
      },
    ];
  return summary;
}
export function openFor(task: Task, version: number): TaskOpenResponse {
  return {
    task: {
      ...task,
      review_version_matches:
        !records.find((item) => item.id === task.id)?.historical_version ||
        version ===
          records.find((item) => item.id === task.id)?.historical_version,
      experiments: [],
      user_tags: [],
    },
    default_version: versionFor(task, task.current_version!),
    selected_version: versionFor(task, version),
    totals: {
      cost_usd: 0,
      cost_trial_count: 0,
      cost_has_estimated: false,
      cost_has_native: false,
      billed_cost_usd: 0,
      billed_trial_count: 0,
      billed_has_estimated: false,
      billed_has_native: false,
      total_trials: task.total,
      token_count: 0,
      token_trial_count: 0,
    },
    trials: task.trials ?? [],
    trials_has_more: false,
    active_qa_trial: null,
  } as TaskOpenResponse;
}
export const board: DeliveryBoardResponse = {
  delivery: {
    id: "review-demo",
    name: "September delivery",
    status: "active",
    is_public: false,
    created_at: now,
    updated_at: now,
    customer: "Fixture customer",
    description: "Representative review records",
  },
  qa_as_of: now,
  qa_viewer_user_id: "maya",
  frozen: false,
  ready: false,
  ready_task_count: 1,
  task_count: tasks.length,
  delivery_checks: [],
  check_config: { automated: {}, manual: [] },
  tasks: tasks.map((task) => {
    const record = records.find((item) => item.id === task.id)!;
    return {
      task_id: task.id,
      task_name: task.name,
      delivery_task_id: `delivery-${task.id}`,
      version: record.version,
      version_id: task.current_version_id,
      is_visible: true,
      newer_version_exists: false,
      sort_order: 0,
      ready: record.signed_off,
      qa_owner_name: null,
      qa_work: {
        owner_user_id: null,
        claimed_at: null,
        issue_categories: [],
        note: "",
      },
      qa: {
        status: record.qa_status,
        trial_id: null,
        finished_at: record.verdict_status === "success" ? now : null,
        detail: record.error ?? "Review evidence belongs to this version.",
      },
      defects: record.finding
        ? [
            {
              ...record.finding,
              id: "delivery-empty-answer",
              finding_id: record.finding.id,
              source: "pre_trial",
              acknowledged: false,
            },
          ]
        : [],
      checks: [
        {
          key: "pre_trial_passed",
          kind: "automated",
          label: "Source review completed",
          status: record.source_status === "success" ? "pass" : "fail",
          detail: `Source review ${record.source_status ?? "not run"} on v${record.version}; defect checks are separate.`,
        },
        {
          key: "verdict_ok",
          kind: "automated",
          label: "No blocking defects in verdict",
          status: record.is_good === true ? "pass" : "fail",
          detail:
            record.finding?.title ??
            record.error ??
            "No completed review verdict for this version.",
        },
        {
          key: "signoff",
          kind: "manual",
          label: "Signed off",
          status: record.signed_off ? "pass" : "fail",
          detail: record.signed_off
            ? `Signed off on v${record.version}`
            : `Awaiting sign-off on v${record.version}`,
        },
      ],
    };
  }),
} as DeliveryBoardResponse;
export { records };
