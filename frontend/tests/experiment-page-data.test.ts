import assert from "node:assert/strict";
import test from "node:test";

import {
  buildExperimentTasks,
  trialFromExperimentCell,
} from "../src/lib/experiment-page-data.ts";
import type {
  ExperimentOpenResponse,
  ExperimentOpenTask,
  ExperimentTrialCell,
  ExperimentTrialPageResponse,
} from "../src/lib/types.ts";

const REVISION = "2026-09-02T00:00:00Z";

function task(overrides: Partial<ExperimentOpenTask> = {}): ExperimentOpenTask {
  return {
    id: "task-1",
    name: "Task one",
    status: "completed",
    priority: "low",
    user: "tester",
    task_path: "tasks/task-1",
    current_version: 2,
    current_version_id: "task-1-v2",
    trial_version: 2,
    trial_version_id: "task-1-v2",
    total: 1,
    completed: 1,
    failed: 0,
    created_at: REVISION,
    updated_at: REVISION,
    ...overrides,
  };
}

function openPage(taskRow: ExperimentOpenTask): ExperimentOpenResponse {
  return {
    experiment_id: "experiment-1",
    name: "Experiment one",
    created_at: REVISION,
    owner: "tester",
    link: null,
    revision: REVISION,
    has_active_trials: false,
    summary: null,
    tasks: [taskRow],
  };
}

function trial(
  overrides: Partial<ExperimentTrialCell> = {}
): ExperimentTrialCell {
  return {
    id: "trial-1",
    name: "trial-1",
    task_id: "task-1",
    task_path: "tasks/task-1",
    experiment_id: "experiment-1",
    task_version_id: "task-1-v2",
    agent: "codex",
    provider: "openai",
    queue_key: "openai/gpt-5.6",
    model: "openai/gpt-5.6",
    status: "success",
    attempts: 1,
    max_attempts: 1,
    harbor_stage: "completed",
    reward: 1,
    is_billed: true,
    has_trajectory: true,
    analysis: {},
    created_at: REVISION,
    ...overrides,
  };
}

function trialPage(cells: ExperimentTrialCell[]): ExperimentTrialPageResponse {
  return { revision: REVISION, trials: cells };
}

test("attaches only trial cells for the experiment-selected task version", () => {
  const [result] = buildExperimentTasks(
    [openPage(task())],
    [
      trialPage([
        trial({ id: "trial-v1", task_version_id: "task-1-v1" }),
        trial({ id: "trial-v2", task_version_id: "task-1-v2" }),
      ]),
    ],
    false
  );

  assert.deepEqual(
    result.trials?.map((item) => item.id),
    ["trial-v2"]
  );
  assert.equal(result.trial_version_id, "task-1-v2");
});

test("keeps legacy versionless task and trial rows together", () => {
  const versionlessTask = task({
    current_version: null,
    current_version_id: null,
    trial_version: null,
    trial_version_id: null,
  });
  const versionlessTrial = trial({ task_version_id: null });

  const [result] = buildExperimentTasks(
    [openPage(versionlessTask)],
    [trialPage([versionlessTrial])],
    false
  );

  assert.deepEqual(
    result.trials?.map((item) => item.id),
    ["trial-1"]
  );
});

test("preserves a historical classification whose subtype is absent", () => {
  const result = trialFromExperimentCell(
    trial({
      analysis: {
        status: "success",
        classification: "GOOD_FAILURE",
        subtype: null,
        evidence: "Historical grade",
      },
    })
  );

  assert.equal(result.analysis_status, "success");
  assert.deepEqual(result.analysis, {
    classification: "GOOD_FAILURE",
    subtype: undefined,
    evidence: "Historical grade",
  });
});

test("experiment rows retain the rejection preview before trial pages load", () => {
  const primaryIssue = "The verifier accepts an empty solution.";
  const page = openPage(
    task({
      verdict_status: "success",
      verdict: {
        verdict: "reject",
        is_good: false,
        confidence: "high",
        primary_issue: primaryIssue,
      },
    })
  );
  const [row] = buildExperimentTasks([page], undefined, false);
  assert.equal(row.verdict?.primary_issue, primaryIssue);
  assert.equal(row.trials, undefined);
});
