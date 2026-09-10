import type {
  DeliveryBoardResponse,
  DeliveryTaskBoardRow,
  TaskQAHistoryResponse,
} from "../src/lib/types";
export function taskRow(version = 7): DeliveryTaskBoardRow {
  return {
    delivery_task_id: "member-a",
    task_id: "task-a",
    task_name: "Task A",
    version_id: `version-${version}`,
    version,
    pinned_version_id: null,
    newer_version_exists: false,
    is_visible: true,
    sort_order: 0,
    qa: {
      status: "never",
      trial_id: null,
      finished_at: null,
      detail: `No QA for v${version}`,
    },
    qa_work: {
      owner_user_id: "maya",
      claimed_at: null,
      issue_categories: [],
      note: "",
    },
    qa_owner_name: "Maya",
    checks: [
      {
        key: "audit",
        kind: "automated",
        label: "Audit complete",
        status: "fail",
        detail: `Audit missing for v${version}`,
      },
      {
        key: "signoff",
        kind: "manual",
        label: "Task sign-off",
        status: "fail",
        detail: "",
      },
    ],
    defects: [],
    ready: false,
  };
}
export function board(version = 7): DeliveryBoardResponse {
  return {
    delivery: {
      id: "refresh-test",
      name: "Maya delivery",
      customer_name: "Acme",
      status: "active",
      is_public: false,
      created_at: "2026-09-09T00:00:00Z",
      updated_at: "2026-09-09T00:00:00Z",
    },
    check_config: { automated: {}, manual: [] },
    tasks: [taskRow(version)],
    delivery_checks: [],
    ready: false,
    ready_task_count: 0,
    task_count: 1,
    frozen: false,
    qa_as_of: null,
    qa_viewer_user_id: "maya",
  };
}
export function history(
  current = 7,
  latest = current,
  status = "running"
): TaskQAHistoryResponse {
  return {
    task_id: "task-a",
    task_name: "Task A",
    current_version_id: `version-${current}`,
    versions: Array.from({ length: latest }, (_, i) => {
      const n = latest - i;
      return {
        version_id: `version-${n}`,
        version: n,
        created_at: "2026-09-09T00:00:00Z",
        message: `Version ${n} notes`,
        is_current: n === current,
        pre_trial_status: n >= 8 ? null : "success",
        must_fix: 0,
        pre_trial_should_fix: 0,
        rollout_count: n >= 8 ? 0 : 5,
        rollout_agents: n >= 8 ? 0 : 3,
        qa_runs: n >= 8 ? [] : [{ trial_id: `qa-${n}`, kind: "qa", status }],
        findings: [
          {
            tier: "should_fix",
            title: `v${n} historical finding`,
            source: "pre_trial",
          },
        ],
      };
    }),
  };
}
