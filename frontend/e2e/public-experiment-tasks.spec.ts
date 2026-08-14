import { expect, test } from "@playwright/test";

import { preparePublicExperimentTasks } from "../src/lib/public-experiment-tasks";
import { trialDetailDefaultTab } from "../src/lib/trial-detail-tabs";
import { fetchTrajectorySummary } from "../src/lib/use-trajectory-summary";
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
    experiment_is_public: true,
    total: 1,
    completed: 1,
    failed: 0,
    created_at: "2026-07-14T00:00:00Z",
    updated_at: "2026-07-14T00:00:00Z",
    trials: [],
    ...overrides,
  };
}

test("keeps historical experiment trials when the task default differs", () => {
  const historicalTrial = {
    id: "trial-v2",
    task_version: 2,
    status: "success",
    reward: 1,
  } as Trial;
  const historicalTask = task({
    current_version: 4,
    current_version_id: "task-1-v4",
    trials: [historicalTrial],
    reward_success: 1,
    reward_sum: 1,
    reward_total: 1,
  });

  const [prepared] = preparePublicExperimentTasks([historicalTask]);

  expect(prepared).toBe(historicalTask);
  expect(prepared.current_version).toBe(4);
  expect(prepared.trials).toEqual([historicalTrial]);
  expect(prepared.total).toBe(1);
  expect(prepared.reward_total).toBe(1);
});

test("public drawers default back to trajectory after close", () => {
  expect(trialDetailDefaultTab(false)).toBe("trajectory");
  expect(trialDetailDefaultTab(true)).toBe("summary");
});

test("summary polling preserves the durable backend job state", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        status: "running",
        job_id: "job-1",
        retry_after_ms: 1700,
      }),
      { status: 202, headers: { "Content-Type": "application/json" } }
    );
  try {
    await expect(
      fetchTrajectorySummary("https://example.test/summary")
    ).resolves.toEqual({
      status: "running",
      summary: null,
      jobId: "job-1",
      retryAfterMs: 1700,
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});
