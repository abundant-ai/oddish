import { expect, test } from "@playwright/test";

import { resolveExperimentTaskVersion } from "../src/lib/experiment-task-version";
import type { Task, Trial } from "../src/lib/types";

function task(overrides: Partial<Task>): Task {
  return {
    id: "task-1",
    name: "Task one",
    status: "completed",
    priority: "low",
    user: "tester",
    task_path: "tasks/task-1",
    experiment_id: "exp-1",
    experiment_name: "Experiment one",
    experiment_is_public: false,
    total: 0,
    completed: 0,
    failed: 0,
    created_at: "2026-07-14T00:00:00Z",
    updated_at: "2026-07-14T00:00:00Z",
    trials: null,
    ...overrides,
  };
}

test("uses the experiment trial version instead of the displayed default", () => {
  const historicalTask = task({
    current_version: 4,
    current_version_id: "task-1-v4",
    trial_version: 2,
    trial_version_id: "task-1-v2",
    trials: [
      { id: "legacy", task_version: null } as Trial,
      { id: "trial-v2", task_version: 2 } as Trial,
    ],
  });

  expect(resolveExperimentTaskVersion(historicalTask)).toBe(2);
});

test("uses the server trial version before trial rows finish loading", () => {
  const shell = task({
    current_version: 4,
    current_version_id: "task-1-v4",
    trial_version: 2,
    trial_version_id: "task-1-v2",
    trials: null,
  });

  expect(resolveExperimentTaskVersion(shell)).toBe(2);
});

test("falls back to the selected default when no trial version is available", () => {
  expect(resolveExperimentTaskVersion(task({ current_version: 4 }))).toBe(4);
});
