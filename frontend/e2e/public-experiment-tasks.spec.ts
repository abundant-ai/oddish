import { expect, test } from "@playwright/test";

import { preparePublicExperimentTasks } from "../src/lib/public-experiment-tasks";
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
      summary: null,
      refresh: {
        status: "running",
        jobId: "job-1",
        retryAfterMs: 1700,
      },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("public trial drawers defer trajectory work", async ({ page }) => {
  const token = "public-drawer-regression";
  const publicTrial: Trial = {
    id: "task-1-2",
    name: "Public trial",
    task_id: "task-1",
    task_path: "tasks/task-1",
    experiment_id: "exp-1",
    agent: "claude-code",
    provider: "anthropic",
    model: "masked-model",
    status: "success",
    attempts: 1,
    max_attempts: 1,
    harbor_stage: "completed",
    reward: 0.5,
    task_version: 1,
    task_version_id: "task-1-v1",
    has_trajectory: true,
    created_at: "2026-07-14T00:00:00Z",
    started_at: "2026-07-14T00:00:00Z",
    finished_at: "2026-07-14T00:01:00Z",
  };
  const publicTask = task({
    current_version: 1,
    current_version_id: "task-1-v1",
    trials: [publicTrial],
    reward_success: 0,
    reward_sum: 0.5,
    reward_total: 1,
  });
  let trajectoryRequests = 0;

  await page.route(`**/api/public/experiments/${token}`, (route) =>
    route.fulfill({
      json: {
        name: "Public drawer test",
        public_token: token,
        description: null,
      },
    })
  );
  await page.route(`**/api/public/experiments/${token}/tasks?*`, (route) =>
    route.fulfill({ json: [publicTask] })
  );
  await page.route(
    `**/api/public/experiments/${token}/tasks/task-1/files?*`,
    (route) => route.fulfill({ json: { files: [] } })
  );
  await page.route(
    `**/api/public/experiments/${token}/trials/task-1-2/trajectory/summary`,
    (route) => route.fulfill({ status: 404, json: { detail: "not found" } })
  );
  await page.route(
    `**/api/public/experiments/${token}/trials/task-1-2/trajectory`,
    (route) => {
      trajectoryRequests += 1;
      return route.fulfill({
        json: {
          schema_version: "1",
          session_id: "session-1",
          agent: {
            name: "claude-code",
            version: "1",
            model_name: "masked-model",
          },
          steps: [
            {
              step_id: 1,
              timestamp: "2026-07-14T00:00:01Z",
              source: "agent",
              model_name: "masked-model",
              message: "Short collapsed preview",
              reasoning_content: "EXPENSIVE_STEP_BODY",
              tool_calls: null,
              observation: null,
              metrics: null,
            },
          ],
          notes: null,
          final_metrics: null,
        },
      });
    }
  );

  await page.goto(`/share/${token}`);
  await expect(
    page.getByRole("heading", { name: "Public drawer test" })
  ).toBeVisible();
  await page.getByRole("button", { name: "Trial 1 Partial" }).click();

  await expect(page.getByRole("tab", { name: "Summary" })).toHaveAttribute(
    "data-state",
    "active"
  );
  await page.waitForTimeout(500);
  expect(trajectoryRequests).toBe(0);

  await page.getByRole("tab", { name: "Trajectory" }).click();
  await expect.poll(() => trajectoryRequests).toBe(1);
  await expect(page.getByText("EXPENSIVE_STEP_BODY")).toHaveCount(0);
  await page.getByRole("button", { name: /^#1/ }).click();
  await expect(page.getByText("EXPENSIVE_STEP_BODY")).toBeVisible();
});
