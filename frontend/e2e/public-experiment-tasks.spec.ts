import { expect, test } from "@playwright/test";

import { fetchTrajectorySummary } from "../src/lib/use-trajectory-summary";
import type { Task, Trial } from "../src/lib/types";

const emptyCostTotals = {
  cost_usd: 0,
  cost_trial_count: 0,
  cost_has_estimated: false,
  cost_has_native: false,
  token_count: 0,
  token_trial_count: 0,
  owned_cost_usd: 0,
  owned_trial_count: 0,
  owned_has_estimated: false,
  owned_has_native: false,
  owned_token_count: 0,
  owned_token_trial_count: 0,
  billed_cost_usd: 0,
  billed_trial_count: 0,
  billed_has_estimated: false,
  billed_has_native: false,
  billed_token_count: 0,
  billed_token_trial_count: 0,
  total_trials: 0,
};

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

function publicOpenResponse(publicTask: Task, hasActiveTrials = false) {
  return {
    experiment_id: "exp-1",
    name: "Public experiment",
    created_at: "2026-07-14T00:00:00Z",
    revision: "2026-07-14T00:00:00Z",
    has_active_trials: hasActiveTrials,
    summary: {
      task_count: 1,
      trial_count: 1,
      completed: hasActiveTrials ? 0 : 1,
      failed: 0,
      skipped: 0,
      active: hasActiveTrials ? 1 : 0,
      reward_sum: hasActiveTrials ? 0 : 1,
      reward_total: hasActiveTrials ? 0 : 1,
      pass_count: hasActiveTrials ? 0 : 1,
      partial_count: 0,
      fail_count: 0,
      harness_error_count: 0,
      average_score: hasActiveTrials ? null : 1,
      qa_accepted: 0,
      qa_rejected: 0,
      qa_running: 0,
      qa_failed: 0,
    },
    tasks: [publicTask],
  };
}

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

test("public experiment exposes and retries an initial cost failure", async ({
  page,
}) => {
  const token = "public-cost-retry";
  const publicTask = task({
    current_version: 1,
    current_version_id: "task-1-v1",
  });
  let allowCostSuccess = false;
  let costRequests = 0;

  await page.route(`**/api/public/experiments/${token}`, (route) =>
    route.fulfill({
      json: {
        name: "Public cost retry",
        public_token: token,
        description: null,
      },
    })
  );
  await page.route(
    `**/api/public/experiments/${token}/cost-totals`,
    (route) => {
      costRequests += 1;
      return allowCostSuccess
        ? route.fulfill({
            json: {
              ...emptyCostTotals,
              cost_usd: 12.34,
              cost_trial_count: 1,
              cost_has_native: true,
              token_count: 100,
              token_trial_count: 1,
            },
          })
        : route.fulfill({
            status: 503,
            json: { detail: "cost endpoint unavailable" },
          });
    }
  );
  await page.route(`**/api/public/experiments/${token}/open?*`, (route) =>
    route.fulfill({ json: publicOpenResponse(publicTask) })
  );
  await page.route(`**/api/public/experiments/${token}/trial-page?*`, (route) =>
    route.fulfill({
      json: { revision: "2026-07-14T00:00:00Z", trials: [] },
    })
  );

  await page.goto(`/share/${token}`);

  await expect(
    page.getByRole("heading", { name: "Failed to load experiment spend" })
  ).toBeVisible();
  await expect(page.getByText("cost endpoint unavailable")).toBeVisible();
  await expect(page.getByText("Unavailable", { exact: true })).toBeVisible();
  expect(costRequests).toBeGreaterThan(0);

  allowCostSuccess = true;
  await page.getByRole("button", { name: "Retry" }).click();

  await expect(page.getByText("$12.34", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Failed to load experiment spend" })
  ).toHaveCount(0);
});

test("public experiment keeps task rows visible when trial pagination fails", async ({
  page,
}) => {
  const token = "public-trial-page-retry";
  const publicTask = task({
    current_version: 1,
    current_version_id: "task-1-v1",
    reward_success: 0,
    reward_sum: 0,
    reward_total: 0,
  });
  let trialPageRequests = 0;

  await page.route(`**/api/public/experiments/${token}`, (route) =>
    route.fulfill({
      json: {
        name: "Public retry test",
        public_token: token,
        description: null,
      },
    })
  );
  await page.route(
    `**/api/public/experiments/${token}/cost-totals`,
    (route) => route.fulfill({ json: emptyCostTotals })
  );
  await page.route(`**/api/public/experiments/${token}/open?*`, (route) =>
    route.fulfill({
      json: {
        experiment_id: "exp-1",
        name: "Public retry test",
        created_at: "2026-07-14T00:00:00Z",
        revision: "2026-07-14T00:00:00Z",
        has_active_trials: false,
        summary: {
          task_count: 1,
          trial_count: 1,
          completed: 0,
          failed: 0,
          skipped: 0,
          active: 1,
          reward_sum: 0,
          reward_total: 0,
          pass_count: 0,
          partial_count: 0,
          fail_count: 0,
          harness_error_count: 0,
          average_score: null,
          qa_accepted: 0,
          qa_rejected: 0,
          qa_running: 0,
          qa_failed: 0,
        },
        tasks: [publicTask],
      },
    })
  );
  await page.route(
    `**/api/public/experiments/${token}/trial-page?*`,
    (route) => {
      trialPageRequests += 1;
      if (trialPageRequests === 1) {
        return route.fulfill({
          status: 503,
          json: { detail: "trial page unavailable" },
        });
      }
      return route.fulfill({
        json: {
          revision: "2026-07-14T00:00:00Z",
          trials: [],
        },
      });
    }
  );

  await page.goto(`/share/${token}`);

  await expect(page.getByText("Task one")).toBeVisible();
  await expect(
    page.getByText("The share link may be invalid or no longer public.")
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Some trial results failed to load" })
  ).toBeVisible();

  await page.getByRole("button", { name: "Retry" }).click();

  await expect.poll(() => trialPageRequests).toBe(2);
  await expect(
    page.getByRole("heading", { name: "Some trial results failed to load" })
  ).toHaveCount(0);
  await expect(page.getByText("Task one")).toBeVisible();
});

test("later trial-page failure waits for an explicit retry", async ({ page }) => {
  const token = "public-later-trial-page-retry";
  const publicTask = task({ trials: undefined });
  let trialPageRequests = 0;

  await page.route(`**/api/public/experiments/${token}`, (route) =>
    route.fulfill({
      json: { name: "Bounded trials", public_token: token, description: null },
    })
  );
  await page.route(
    `**/api/public/experiments/${token}/cost-totals`,
    (route) => route.fulfill({ json: emptyCostTotals })
  );
  await page.route(`**/api/public/experiments/${token}/open?*`, (route) =>
    route.fulfill({
      json: {
        experiment_id: "exp-1",
        name: "Bounded trials",
        created_at: "2026-07-14T00:00:00Z",
        revision: "2026-07-14T00:00:00Z",
        has_active_trials: false,
        summary: {
          task_count: 1,
          trial_count: 501,
          completed: 501,
          failed: 0,
          skipped: 0,
          active: 0,
          reward_sum: 0,
          reward_total: 0,
          pass_count: 0,
          partial_count: 0,
          fail_count: 0,
          harness_error_count: 0,
          average_score: null,
          qa_accepted: 0,
          qa_rejected: 0,
          qa_running: 0,
          qa_failed: 0,
        },
        tasks: [publicTask],
      },
    })
  );
  await page.route(
    `**/api/public/experiments/${token}/trial-page?*`,
    (route) => {
      trialPageRequests += 1;
      if (trialPageRequests === 1) {
        return route.fulfill({
          json: {
            revision: "2026-07-14T00:00:00Z",
            trials: [],
            next_created_at: "2026-07-13T00:00:00Z",
            next_trial_id: "trial-250",
          },
        });
      }
      if (trialPageRequests === 2) {
        return route.fulfill({
          status: 503,
          json: { detail: "later page unavailable" },
        });
      }
      return route.fulfill({
        json: { revision: "2026-07-14T00:00:00Z", trials: [] },
      });
    }
  );

  await page.goto(`/share/${token}`);
  await expect.poll(() => trialPageRequests).toBe(1);
  await page.waitForTimeout(750);
  expect(trialPageRequests).toBe(1);

  await page.getByRole("button", { name: "Load next 250 trial results" }).click();
  await expect.poll(() => trialPageRequests).toBe(2);
  await expect(
    page.getByRole("heading", { name: "Some trial results failed to load" })
  ).toBeVisible();
  await page.waitForTimeout(750);
  expect(trialPageRequests).toBe(2);

  await page.getByRole("button", { name: "Retry" }).click();
  await expect.poll(() => trialPageRequests).toBe(3);
});

test("public deep link resolves a trial outside the loaded pages", async ({
  page,
}) => {
  const token = "public-focused-trial";
  const focusedTask = task({
    id: "task-101",
    name: "Task one hundred one",
    task_path: "tasks/task-101",
    current_version: 1,
    current_version_id: "task-101-v1",
    trials: undefined,
  });
  const focusedTrial: Trial = {
    id: "task-101-1",
    name: "Focused trial",
    task_id: focusedTask.id,
    task_path: focusedTask.task_path,
    experiment_id: "exp-1",
    agent: "claude-code",
    provider: "anthropic",
    model: "masked-model",
    status: "success",
    attempts: 1,
    max_attempts: 1,
    harbor_stage: "completed",
    reward: 1,
    task_version_id: "task-101-v1",
    has_trajectory: false,
    created_at: "2026-07-14T00:00:00Z",
  };
  let focusRequests = 0;

  await page.route(`**/api/public/experiments/${token}`, (route) =>
    route.fulfill({
      json: { name: "Focused links", public_token: token, description: null },
    })
  );
  await page.route(
    `**/api/public/experiments/${token}/cost-totals`,
    (route) =>
      route.fulfill({
        json: {
          ...emptyCostTotals,
          cost_usd: 12.34,
          cost_trial_count: 101,
          token_count: 123456,
          token_trial_count: 101,
        },
      })
  );
  await page.route(`**/api/public/experiments/${token}/open?*`, (route) =>
    route.fulfill({
      json: {
        experiment_id: "exp-1",
        name: "Focused links",
        created_at: "2026-07-14T00:00:00Z",
        revision: "2026-07-14T00:00:00Z",
        has_active_trials: false,
        summary: {
          task_count: 101,
          trial_count: 1,
          completed: 1,
          failed: 0,
          skipped: 0,
          active: 0,
          reward_sum: 1,
          reward_total: 1,
          pass_count: 1,
          partial_count: 0,
          fail_count: 0,
          harness_error_count: 0,
          average_score: 1,
          qa_accepted: 0,
          qa_rejected: 0,
          qa_running: 0,
          qa_failed: 0,
        },
        tasks: [task({ id: "task-1", name: "First page task" })],
      },
    })
  );
  await page.route(
    `**/api/public/experiments/${token}/trial-page?*`,
    (route) =>
      route.fulfill({
        json: { revision: "2026-07-14T00:00:00Z", trials: [] },
      })
  );
  await page.route(`**/api/public/experiments/${token}/focus?*`, (route) => {
    focusRequests += 1;
    const includeTrial = new URL(route.request().url()).searchParams.has("trial");
    return route.fulfill({
      json: {
        revision: "2026-07-14T00:00:00Z",
        task: focusedTask,
        trial: includeTrial ? { ...focusedTrial, analysis: {} } : null,
      },
    });
  });
  await page.route(
    `**/api/public/experiments/${token}/tasks/task-101?*`,
    (route) => route.fulfill({ json: { ...focusedTask, trials: [] } })
  );
  await page.route(
    `**/api/public/experiments/${token}/tasks/task-101/files?*`,
    (route) => route.fulfill({ json: { files: [] } })
  );

  await page.goto(`/share/${token}?task=task-101`);

  await expect.poll(() => focusRequests).toBe(1);
  await expect(page).toHaveURL(/task=task-101/);
  await expect(page).not.toHaveURL(/trial=/);
  await expect(page.getByText("$12.34", { exact: true })).toBeVisible();

  await page.goto(`/share/${token}?task=task-101&trial=task-101-1`);

  await expect.poll(() => focusRequests).toBe(2);
  await expect(page.getByRole("tab", { name: "Summary" })).toBeVisible();
  await expect(page).toHaveURL(/task=task-101&trial=task-101-1/);
});

test("retryable focus errors preserve the trial-page deep-link fallback", async ({
  page,
}) => {
  const token = "public-focus-retry";
  const publicTask = task({
    current_version: 1,
    current_version_id: "task-1-v1",
    trials: undefined,
  });
  const focusedTrial: Trial = {
    id: "task-1-2",
    name: "Focused trial",
    task_id: publicTask.id,
    task_path: publicTask.task_path,
    experiment_id: "exp-1",
    agent: "claude-code",
    provider: "anthropic",
    model: "masked-model",
    status: "success",
    attempts: 1,
    max_attempts: 1,
    harbor_stage: "completed",
    reward: 1,
    task_version_id: "task-1-v1",
    has_trajectory: false,
    created_at: "2026-07-14T00:00:00Z",
  };
  let focusRequests = 0;

  await page.route(`**/api/public/experiments/${token}`, (route) =>
    route.fulfill({
      json: { name: "Focus retry", public_token: token, description: null },
    })
  );
  await page.route(
    `**/api/public/experiments/${token}/cost-totals`,
    (route) => route.fulfill({ json: emptyCostTotals })
  );
  await page.route(`**/api/public/experiments/${token}/open?*`, (route) =>
    route.fulfill({ json: publicOpenResponse(publicTask) })
  );
  await page.route(
    `**/api/public/experiments/${token}/trial-page?*`,
    async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 300));
      await route.fulfill({
        json: {
          revision: "2026-07-14T00:00:00Z",
          trials: [{ ...focusedTrial, analysis: {} }],
        },
      });
    }
  );
  await page.route(`**/api/public/experiments/${token}/focus?*`, (route) => {
    focusRequests += 1;
    return route.fulfill({
      status: 503,
      json: { detail: "focus temporarily unavailable" },
    });
  });
  await page.route(
    `**/api/public/experiments/${token}/tasks/task-1?*`,
    (route) =>
      route.fulfill({ json: { ...publicTask, trials: [focusedTrial] } })
  );

  await page.goto(`/share/${token}?task=task-1&trial=task-1-2`);

  await expect.poll(() => focusRequests).toBe(1);
  await expect(page.getByRole("tab", { name: "Summary" })).toBeVisible();
  await expect(page).toHaveURL(/task=task-1&trial=task-1-2/);
});

test("public experiment resources refresh while the experiment is active", async ({
  page,
}) => {
  const token = "public-active-cost";
  const publicTask = task({ trials: undefined });
  let costRequests = 0;
  let openRequests = 0;
  let trialPageRequests = 0;

  await page.clock.install();
  await page.route(`**/api/public/experiments/${token}`, (route) =>
    route.fulfill({
      json: { name: "Active cost", public_token: token, description: null },
    })
  );
  await page.route(
    `**/api/public/experiments/${token}/cost-totals`,
    (route) => {
      costRequests += 1;
      return route.fulfill({
        json: {
          ...emptyCostTotals,
          cost_usd: costRequests,
          cost_trial_count: 1,
        },
      });
    }
  );
  await page.route(`**/api/public/experiments/${token}/open?*`, (route) => {
    openRequests += 1;
    return route.fulfill({ json: publicOpenResponse(publicTask, true) });
  });
  await page.route(
    `**/api/public/experiments/${token}/trial-page?*`,
    (route) => {
      trialPageRequests += 1;
      return route.fulfill({
        json: { revision: "2026-07-14T00:00:00Z", trials: [] },
      });
    }
  );

  await page.goto(`/share/${token}`);
  await expect.poll(() => costRequests).toBe(1);
  await expect.poll(() => openRequests).toBe(1);
  await expect.poll(() => trialPageRequests).toBe(1);
  await expect(page.getByText("$1.00", { exact: true })).toBeVisible();

  await page.clock.runFor(30_100);

  await expect.poll(() => costRequests).toBe(2);
  await expect.poll(() => openRequests).toBe(2);
  await expect.poll(() => trialPageRequests).toBe(2);
  await expect(page.getByText("$2.00", { exact: true })).toBeVisible();
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
  await page.route(
    `**/api/public/experiments/${token}/cost-totals`,
    (route) => route.fulfill({ json: emptyCostTotals })
  );
  await page.route(`**/api/public/experiments/${token}/open?*`, (route) =>
    route.fulfill({
      json: {
        experiment_id: "exp-1",
        name: "Public drawer test",
        created_at: "2026-07-14T00:00:00Z",
        revision: "2026-07-14T00:00:00Z",
        has_active_trials: false,
        summary: {
          task_count: 1,
          trial_count: 1,
          completed: 1,
          failed: 0,
          skipped: 0,
          active: 0,
          reward_sum: 0.5,
          reward_total: 1,
          pass_count: 0,
          partial_count: 1,
          fail_count: 0,
          harness_error_count: 0,
          average_score: 0.5,
          qa_accepted: 0,
          qa_rejected: 0,
          qa_running: 0,
          qa_failed: 0,
        },
        tasks: [publicTask],
      },
    })
  );
  await page.route(`**/api/public/experiments/${token}/trial-page?*`, (route) =>
    route.fulfill({
      json: {
        revision: "2026-07-14T00:00:00Z",
        trials: [{ ...publicTrial, analysis: {} }],
      },
    })
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
