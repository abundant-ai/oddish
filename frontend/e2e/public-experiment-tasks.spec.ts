import { expect, test } from "@playwright/test";

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

function summary(overrides: Record<string, number | null> = {}) {
  return {
    task_count: 1,
    trial_count: 1,
    success_count: 1,
    failed_count: 0,
    skipped_count: 0,
    active_count: 0,
    reward_success: 0,
    reward_sum: 0.5,
    reward_total: 1,
    pass_count: 0,
    partial_count: 1,
    fail_count: 0,
    harness_error_count: 0,
    avg_score: 0.5,
    qa_accepted: 0,
    qa_rejected: 0,
    qa_running: 0,
    qa_failed: 0,
    ...overrides,
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
  let openRequests = 0;
  let trialPageRequests = 0;
  let revisionRequests = 0;

  await page.route(`**/api/public/experiments/${token}/open`, (route) => {
    openRequests += 1;
    return route.fulfill({
      json: {
        experiment_id: "exp-1",
        name: "Public drawer test",
        description: null,
        description_truncated: false,
        revision: "2026-07-14T00:01:00Z",
        has_active_trials: false,
        summary: summary(),
        tasks: [{ ...publicTask, trials: null }],
        next_cursor: null,
      },
    });
  });
  await page.route(`**/api/public/experiments/${token}/trial-page`, (route) => {
    trialPageRequests += 1;
    return route.fulfill({
      json: {
        revision: "2026-07-14T00:01:00Z",
        tasks: [publicTask],
        trial_count: 1,
        next_cursor: null,
      },
    });
  });
  await page.route(`**/api/public/experiments/${token}/revision`, (route) => {
    revisionRequests += 1;
    return route.fulfill({
      json: {
        revision: "2026-07-14T00:01:00Z",
        has_active_trials: false,
      },
    });
  });
  await page.route(
    `**/api/public/experiments/${token}/trials/task-1-2`,
    (route) => route.fulfill({ json: publicTrial })
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
  // The local credential-less dev shell mounts Clerk's configuration warning
  // over public pages. The public page itself is still fully rendered, so the
  // regression test bypasses that unrelated overlay.
  await page
    .getByRole("button", { name: "Trial 1 Partial" })
    .click({ force: true });

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

  // Wait longer than ExperimentPageClient's 5-second active revision interval.
  // A completed experiment must remain at its one open + one trial-page read.
  await page.waitForTimeout(5_500);
  expect(openRequests).toBe(1);
  expect(trialPageRequests).toBe(1);
  expect(revisionRequests).toBe(0);
});

test("public revision polling stops when active work finishes without a new revision", async ({
  page,
}) => {
  const token = "public-finished-poll";
  const publicTask = task({
    current_version: 1,
    current_version_id: "task-1-v1",
    trial_version: 1,
    trial_version_id: "task-1-v1",
  });
  let openRequests = 0;
  let revisionRequests = 0;

  await page.route(`**/api/public/experiments/${token}/open`, (route) => {
    openRequests += 1;
    const active = openRequests === 1;
    return route.fulfill({
      json: {
        experiment_id: "exp-1",
        name: "Polling completion test",
        description: null,
        description_truncated: false,
        revision: "unchanged-revision",
        has_active_trials: active,
        summary: summary({
          trial_count: active ? 1 : 0,
          success_count: 0,
          active_count: active ? 1 : 0,
        }),
        tasks: [{ ...publicTask, trials: null }],
        next_cursor: null,
      },
    });
  });
  await page.route(`**/api/public/experiments/${token}/trial-page`, (route) =>
    route.fulfill({
      json: {
        revision: "unchanged-revision",
        tasks: [{ ...publicTask, trials: [] }],
        trial_count: 0,
        next_cursor: null,
      },
    })
  );
  await page.route(`**/api/public/experiments/${token}/revision`, (route) => {
    revisionRequests += 1;
    return route.fulfill({
      json: {
        revision: "unchanged-revision",
        has_active_trials: false,
      },
    });
  });

  await page.goto(`/share/${token}`);
  await expect(
    page.getByRole("heading", { name: "Polling completion test" })
  ).toBeVisible();
  await expect.poll(() => revisionRequests).toBe(1);
  await expect.poll(() => openRequests).toBe(2);

  await page.waitForTimeout(5_500);
  expect(revisionRequests).toBe(1);
});

test("a failed later public trial page exposes Retry and preserves loaded rows", async ({
  page,
}) => {
  const token = "public-page-retry";
  const firstTrial = {
    id: "trial-1",
    name: "First trial",
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
    reward: 1,
    created_at: "2026-07-14T00:00:00Z",
  } satisfies Trial;
  const secondTrial = {
    ...firstTrial,
    id: "trial-2",
    name: "Second trial",
    created_at: "2026-07-14T00:01:00Z",
  } satisfies Trial;
  const publicTask = task({
    current_version: 1,
    current_version_id: "task-1-v1",
    trial_version: 1,
    trial_version_id: "task-1-v1",
    total: 2,
    completed: 2,
  });
  let laterPageRequests = 0;

  await page.route(`**/api/public/experiments/${token}/open`, (route) =>
    route.fulfill({
      json: {
        experiment_id: "exp-1",
        name: "Later page retry test",
        description: null,
        description_truncated: false,
        revision: "settled-revision",
        has_active_trials: false,
        summary: summary({
          trial_count: 2,
          success_count: 2,
          pass_count: 2,
          partial_count: 0,
          avg_score: 1,
        }),
        tasks: [{ ...publicTask, trials: null }],
        next_cursor: null,
      },
    })
  );
  await page.route(
    new RegExp(`/api/public/experiments/${token}/trial-page(?:\\?|$)`),
    (route) => {
      const cursor = new URL(route.request().url()).searchParams.get("cursor");
      if (!cursor) {
        return route.fulfill({
          json: {
            revision: "settled-revision",
            tasks: [{ ...publicTask, trials: [firstTrial] }],
            trial_count: 1,
            next_cursor: "later-page",
          },
        });
      }
      laterPageRequests += 1;
      if (laterPageRequests === 1) {
        return route.fulfill({
          status: 503,
          json: { detail: "temporary later-page failure" },
        });
      }
      return route.fulfill({
        json: {
          revision: "settled-revision",
          tasks: [{ ...publicTask, trials: [secondTrial] }],
          trial_count: 1,
          next_cursor: null,
        },
      });
    }
  );

  await page.goto(`/share/${token}`);
  await expect(
    page.getByRole("heading", { name: "Later page retry test" })
  ).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  const alert = page
    .getByRole("alert")
    .filter({ hasText: "Some trial results failed to load" });
  await expect(alert).toContainText("Some trial results failed to load");
  const firstTrialButton = page.getByRole("button", { name: /^Trial 1/ });
  await expect(firstTrialButton).toBeVisible();

  await alert.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByRole("button", { name: /^Trial 2/ })).toBeVisible();
  await expect(alert).toHaveCount(0);
  expect(laterPageRequests).toBe(2);
});

test("public pages fetch full metadata only when the open description is truncated", async ({
  page,
}) => {
  const token = "public-full-description";
  const truncated = "a".repeat(8_000);
  const fullDescription = `${truncated}\nFULL_DESCRIPTION_TAIL`;
  let metadataRequests = 0;

  await page.route(`**/api/public/experiments/${token}/open`, (route) =>
    route.fulfill({
      json: {
        experiment_id: "exp-description",
        name: "Long description test",
        description: truncated,
        description_truncated: true,
        revision: "settled-revision",
        has_active_trials: false,
        summary: summary({ task_count: 0, trial_count: 0 }),
        tasks: [],
        next_cursor: null,
      },
    })
  );
  await page.route(
    `**/api/public/experiments/${token}/trial-page`,
    (route) =>
      route.fulfill({
        json: {
          revision: "settled-revision",
          tasks: [],
          trial_count: 0,
          next_cursor: null,
        },
      })
  );
  await page.route(
    new RegExp(`/api/public/experiments/${token}$`),
    (route) => {
      metadataRequests += 1;
      return route.fulfill({
        json: {
          name: "Long description test",
          public_token: token,
          description: fullDescription,
        },
      });
    }
  );

  await page.goto(`/share/${token}`);
  await expect(page.getByText("FULL_DESCRIPTION_TAIL")).toBeVisible();
  expect(metadataRequests).toBe(1);
});

test("a deep-linked trial loads task pages until its host shell arrives", async ({
  page,
}) => {
  const token = "public-deep-link-pages";
  const firstTask = task({ id: "task-first", name: "First task" });
  const hostTask = task({ id: "task-late", name: "Late host task" });
  const deepLinkedTrial = {
    id: "trial-late",
    name: "Late trial",
    task_id: hostTask.id,
    task_path: hostTask.task_path,
    experiment_id: "exp-deep-link",
    agent: "codex",
    provider: "openai",
    model: "masked-model",
    status: "success",
    attempts: 1,
    max_attempts: 1,
    harbor_stage: "completed",
    reward: 1,
    created_at: "2026-07-14T00:00:00Z",
  } satisfies Trial;
  let laterOpenRequests = 0;

  await page.addInitScript(() => {
    window.IntersectionObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof IntersectionObserver;
  });
  await page.route(
    new RegExp(`/api/public/experiments/${token}/open(?:\\?|$)`),
    (route) => {
      const cursor = new URL(route.request().url()).searchParams.get("cursor");
      if (!cursor) {
        return route.fulfill({
          json: {
            experiment_id: "exp-deep-link",
            name: "Deep link pagination test",
            description: null,
            description_truncated: false,
            revision: "settled-revision",
            has_active_trials: false,
            summary: summary({ task_count: 2, trial_count: 1 }),
            tasks: [{ ...firstTask, trials: null }],
            next_cursor: "late-task-page",
          },
        });
      }
      laterOpenRequests += 1;
      return route.fulfill({
        json: {
          experiment_id: "exp-deep-link",
          name: "Deep link pagination test",
          description: null,
          description_truncated: false,
          revision: "settled-revision",
          has_active_trials: false,
          summary: summary({ task_count: 2, trial_count: 1 }),
          tasks: [{ ...hostTask, trials: null }],
          next_cursor: null,
        },
      });
    }
  );
  await page.route(`**/api/public/experiments/${token}/trial-page`, (route) =>
    route.fulfill({
      json: {
        revision: "settled-revision",
        tasks: [{ ...firstTask, trials: [] }],
        trial_count: 0,
        next_cursor: null,
      },
    })
  );
  await page.route(
    `**/api/public/experiments/${token}/trials/${deepLinkedTrial.id}`,
    (route) => route.fulfill({ json: deepLinkedTrial })
  );
  await page.route(
    `**/api/public/experiments/${token}/tasks/${hostTask.id}/files?*`,
    (route) => route.fulfill({ json: { files: [] } })
  );
  await page.route(
    `**/api/public/experiments/${token}/trials/${deepLinkedTrial.id}/trajectory/summary`,
    (route) => route.fulfill({ status: 404, json: { detail: "not found" } })
  );

  await page.goto(`/share/${token}?trial=${deepLinkedTrial.id}`);
  await expect(page.getByRole("tab", { name: "Summary" })).toBeVisible();
  expect(laterOpenRequests).toBe(1);
});

test("dataset links use the bounded public experiment reader", async ({ page }) => {
  const token = "bounded-dataset-page";
  let removedTaskListRequests = 0;

  await page.route(`**/api/public/experiments/${token}/open`, (route) =>
    route.fulfill({
      json: {
        experiment_id: "exp-dataset",
        name: "Bounded dataset test",
        description: null,
        description_truncated: false,
        revision: "settled-revision",
        has_active_trials: false,
        summary: summary({ task_count: 0, trial_count: 0 }),
        tasks: [],
        next_cursor: null,
      },
    })
  );
  await page.route(
    `**/api/public/experiments/${token}/trial-page`,
    (route) =>
      route.fulfill({
        json: {
          revision: "settled-revision",
          tasks: [],
          trial_count: 0,
          next_cursor: null,
        },
      })
  );
  await page.route(
    `**/api/public/experiments/${token}/tasks?*`,
    (route) => {
      removedTaskListRequests += 1;
      return route.fulfill({ json: [] });
    }
  );

  await page.goto(`/datasets/${token}`);
  await expect(
    page.getByRole("heading", { name: "Bounded dataset test" })
  ).toBeVisible();
  expect(removedTaskListRequests).toBe(0);
});
