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

/** Five retained findings, two accepted check exceptions, and an earlier sign-off. */
export function reviewTaskRow(): DeliveryTaskBoardRow {
  const row = taskRow(1);
  row.task_name = "qa-golden-source-vadimdemedes__ink-303-927e39b0-88267eec";
  row.qa = {
    status: "error",
    detail: "QA produced no current verdict",
    trial_id: "qa-1",
    finished_at: null,
  };
  row.checks = [
    {
      key: "pre_trial_passed",
      kind: "automated",
      label: "Source review completed",
      status: "pass",
      detail: "Source review completed on v1",
    },
    {
      key: "min_rollouts",
      kind: "automated",
      label: "Enough rollouts",
      status: "waived",
      detail: "6/5 trials, 1/3 agents on v1",
      checked_by_name: "Kyle",
    },
    {
      key: "verdict_ok",
      kind: "automated",
      label: "No blocking defects in verdict",
      status: "waived",
      detail: "No completed execution-review verdict on v1",
      checked_by_name: "Kyle",
    },
    {
      key: "no_must_fix",
      kind: "automated",
      label: "Every defect resolved or acknowledged",
      status: "fail",
      detail: "2 of 5 task defects unacknowledged on v1",
    },
    {
      key: "signoff",
      kind: "manual",
      label: "Signed off",
      status: "pass",
      detail: "",
      checked_by_name: "Kyle",
      checked_by_user_id: "kyle",
    },
  ];
  row.defects = [
    {
      id: "build",
      title:
        "The image build leaves the compiled fixed ErrorOverview component in the build output.",
      source: "pre_trial",
      acknowledged: true,
      recorded_tier: "must_fix",
      acknowledged_by_name: "Kyle",
    },
    {
      id: "network",
      title:
        "Internet access lets the agent fetch the real upstream fix for this public repository.",
      source: "pre_trial",
      acknowledged: true,
      recorded_tier: "must_fix",
      acknowledged_by_name: "Kyle",
    },
    {
      id: "verifier",
      finding_id: "verifier",
      title:
        "The verifier does not check that errors avoid unhandled promise rejections.",
      source: "pre_trial",
      acknowledged: false,
      recorded_tier: "should_fix",
      file: "tests/test.sh",
      line_start: 7,
      line_end: 9,
      finding: {
        id: "verifier",
        tier: "should_fix",
        title:
          "The verifier does not check that errors avoid unhandled promise rejections.",
        file: "tests/test.sh",
        line_start: 7,
        line_end: 9,
        detail: "Fixture evidence: the shell script only checks the exit code.",
        recommendation:
          "Fixture recommendation: assert that the rejection is handled.",
      },
    },
    {
      id: "tsconfig",
      title:
        "Dockerfile leaves the agent's tsconfig.json in a permanently failing state",
      source: "trial",
      acknowledged: true,
      recorded_tier: "must_fix",
      acknowledged_by_name: "Kyle",
    },
    {
      id: "environment",
      title:
        "Agent environment is missing the global.self polyfill that the verifier defines.",
      source: "trial",
      acknowledged: false,
      recorded_tier: "should_fix",
    },
  ];
  return row;
}
