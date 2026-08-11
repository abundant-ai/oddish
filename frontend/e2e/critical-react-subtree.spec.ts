import { expect, test } from "@playwright/test";
import { clerk, setupClerkTestingToken } from "@clerk/testing/playwright";

import type {
  TaskBrowseResponse,
  TaskDetailResponse,
  Trial,
} from "../src/lib/types";

const CLERK_EMAIL = process.env.E2E_CLERK_EMAIL;
const CLERK_SECRET = process.env.CLERK_SECRET_KEY;
const CLERK_PUBLISHABLE = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;
const hasClerkEnv = !!CLERK_EMAIL && !!CLERK_SECRET && !!CLERK_PUBLISHABLE;

const TASK_ID = "task-p1-snapshot";
const TRIAL_ID = "trial-p1-snapshot";
const NOW = "2026-08-10T20:00:00Z";

const trial: Trial = {
  id: TRIAL_ID,
  name: "trial-p1",
  task_id: TASK_ID,
  task_path: "tasks/p1",
  experiment_id: "experiment-p1",
  agent: "codex",
  provider: "openai",
  model: "gpt-5",
  status: "success",
  attempts: 1,
  max_attempts: 3,
  harbor_stage: "completed",
  reward: 0,
  result: {},
  analysis_status: "success",
  analysis: {
    classification: "GOOD_FAILURE",
    subtype: "task_issue",
    root_cause: "The known trial report paints from the selected row.",
    recommendation: "Keep the row visible while details revalidate.",
  },
  analysis_started_at: "2026-08-10T20:00:01Z",
  analysis_finished_at: "2026-08-10T20:00:03Z",
  task_version: 1,
  task_version_id: "version-p1",
  cost_usd: 0.12,
  cost_is_estimated: false,
  has_trajectory: true,
  created_at: NOW,
  started_at: "2026-08-10T20:00:00Z",
  finished_at: "2026-08-10T20:00:04Z",
};

const browseResponse: TaskBrowseResponse = {
  items: [
    {
      id: TASK_ID,
      name: "P1 snapshot task",
      current_version: 1,
      current_version_id: "version-p1",
      version_count: 1,
      total_trials: 1,
      completed_trials: 1,
      failed_trials: 0,
      reward_success: 0,
      reward_sum: 0,
      reward_total: 1,
      last_run_at: NOW,
      cost_usd: 0.12,
      cost_trial_count: 1,
      cost_has_estimated: false,
      cost_has_native: true,
      billed_cost_usd: 0.12,
      billed_trial_count: 1,
      billed_has_estimated: false,
      billed_has_native: true,
      latest_trials: [
        {
          id: TRIAL_ID,
          name: trial.name,
          status: trial.status,
          reward: trial.reward,
          agent: trial.agent,
          model: trial.model,
        },
      ],
      experiments: [{ id: "experiment-p1", name: "P1 experiment" }],
      user_tags: [],
    },
  ],
  limit: 25,
  offset: 0,
  has_more: false,
};

const taskDetail: TaskDetailResponse = {
  task: {
    id: TASK_ID,
    name: "P1 snapshot task",
    status: "completed",
    priority: "high",
    user: "test-user",
    task_path: "tasks/p1",
    experiment_id: "experiment-p1",
    experiment_name: "P1 experiment",
    experiment_is_public: false,
    experiments: [{ id: "experiment-p1", name: "P1 experiment" }],
    total: 1,
    completed: 1,
    failed: 0,
    skipped: 0,
    reward_success: 0,
    reward_sum: 0,
    reward_total: 1,
    run_analysis: true,
    verdict_status: "success",
    current_version: 1,
    current_version_id: "version-p1",
    trial_version: 1,
    trial_version_id: "version-p1",
    trials: [trial],
    user_tags: [],
    created_at: NOW,
    updated_at: NOW,
  },
  versions: [
    {
      id: "version-p1",
      version: 1,
      created_at: NOW,
      is_current: true,
      trial_count: 1,
      completed_count: 1,
      failed_count: 0,
      skipped_count: 0,
      pass_count: 0,
      partial_count: 0,
      fail_count: 1,
      pending_count: 0,
      reward_sum: 0,
      reward_total: 1,
      cost_usd: 0.12,
      cost_trial_count: 1,
      cost_has_estimated: false,
      cost_has_native: true,
      billed_cost_usd: 0.12,
      billed_trial_count: 1,
      billed_has_estimated: false,
      billed_has_native: true,
      last_run_at: NOW,
      user_tags: [],
      experiments: [{ id: "experiment-p1", name: "P1 experiment" }],
    },
  ],
  totals: {
    cost_usd: 0.12,
    cost_trial_count: 1,
    cost_has_estimated: false,
    cost_has_native: true,
    billed_cost_usd: 0.12,
    billed_trial_count: 1,
    billed_has_estimated: false,
    billed_has_native: true,
    total_trials: 1,
  },
};

function deferred() {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { pending, release };
}

function requestCount(requests: string[], pattern: RegExp): number {
  return requests.filter((url) => pattern.test(url)).length;
}

test.describe("critical task and trial subtree", () => {
  test.skip(
    !hasClerkEnv,
    "needs E2E_CLERK_EMAIL + CLERK_SECRET_KEY + NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY"
  );

  test("paints snapshots before revalidation and starts tab resources only on intent", async ({
    page,
  }) => {
    test.setTimeout(120_000);

    await setupClerkTestingToken({ page });
    await page.goto("/");
    await clerk.signIn({ page, emailAddress: CLERK_EMAIL! });

    const taskDetailGate = deferred();
    const trialDetailGate = deferred();
    const requests: string[] = [];
    page.on("request", (request) => requests.push(request.url()));

    await page.route(/\/api\/tasks\/browse(?:\?|$)/, async (route) => {
      await route.fulfill({ json: browseResponse });
    });
    await page.route(/\/api\/tasks\/browse\/facets(?:\?|$)/, async (route) => {
      await route.fulfill({
        json: {
          agents: [],
          models: [],
          agent_models: [],
          providers: [],
          environments: [],
          harbor_stages: [],
          analysis_classifications: [],
        },
      });
    });
    await page.route(/\/api\/tags(?:\?|$)/, async (route) => {
      await route.fulfill({ json: { items: [] } });
    });
    await page.route(
      new RegExp(`/api/tasks/${TASK_ID}/detail(?:\\?|$)`),
      async (route) => {
        await taskDetailGate.pending;
        await route.fulfill({ json: taskDetail });
      }
    );
    await page.route(
      new RegExp(`/api/tasks/${TASK_ID}/files(?:\\?|$)`),
      async (route) => {
        await route.fulfill({ json: { files: [] } });
      }
    );
    await page.route(
      new RegExp(`/api/tasks/${TASK_ID}/trials(?:\\?|$)`),
      async (route) => {
        await route.fulfill({ json: [trial] });
      }
    );
    await page.route(
      new RegExp(`/api/trials/${TRIAL_ID}/analysis-log(?:\\?|$)`),
      async (route) => {
        await route.fulfill({
          json: { log: "terminal analyzer output", queue_position: null },
        });
      }
    );
    await page.route(
      new RegExp(`/api/trials/${TRIAL_ID}/files(?:/|\\?|$)`),
      async (route) => {
        await route.fulfill({ json: { files: [] } });
      }
    );
    await page.route(
      new RegExp(`/api/trials/${TRIAL_ID}/trajectory(?:/|\\?|$)`),
      async (route) => {
        await route.fulfill({ json: null });
      }
    );
    await page.route(
      new RegExp(`/api/trials/${TRIAL_ID}(?:\\?|$)`),
      async (route) => {
        await trialDetailGate.pending;
        await route.fulfill({ json: trial });
      }
    );

    await page.goto("/tasks");
    const taskLink = page.getByRole("link", { name: "P1 snapshot task" });
    await expect(taskLink).toBeVisible();

    const taskDetailPattern = new RegExp(
      `/api/tasks/${TASK_ID}/detail(?:\\?|$)`
    );
    const taskDetailRequest = page.waitForRequest(taskDetailPattern);
    await taskLink.click();
    await taskDetailRequest;
    expect(requestCount(requests, taskDetailPattern)).toBe(1);
    // The detail response is still blocked: this heading can only be the
    // browse snapshot preserved on the detail resource key.
    await expect(
      page.getByRole("heading", { name: "P1 snapshot task" })
    ).toBeVisible();

    taskDetailGate.release();
    const trialButton = page.getByRole("button", { name: "trial-p1 Fail" });
    await expect(trialButton).toBeVisible();

    const trialDetailPattern = new RegExp(`/api/trials/${TRIAL_ID}(?:\\?|$)`);
    const trialDetailRequest = page.waitForRequest(trialDetailPattern);
    await trialButton.click();
    await trialDetailRequest;
    expect(requestCount(requests, trialDetailPattern)).toBe(1);
    // The full trial response is also blocked. Summary and the known report
    // must render from the selected row, without mounting sibling tabs.
    await expect(page.getByRole("tab", { name: "Summary" })).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "GOOD FAILURE" })
    ).toBeVisible();
    const analysisLogDisclosure = page
      .locator("summary")
      .filter({ hasText: "Analysis log" });
    await expect(analysisLogDisclosure).toBeVisible();

    await page.waitForTimeout(300);
    const analysisLogPattern = new RegExp(
      `/api/trials/${TRIAL_ID}/analysis-log(?:\\?|$)`
    );
    const trialFilesPattern = new RegExp(
      `/api/trials/${TRIAL_ID}/files(?:/|\\?|$)`
    );
    const trajectoryPattern = new RegExp(
      `/api/trials/${TRIAL_ID}/trajectory(?:/|\\?|$)`
    );
    expect(requestCount(requests, analysisLogPattern)).toBe(0);
    expect(requestCount(requests, trialFilesPattern)).toBe(0);
    expect(requestCount(requests, trajectoryPattern)).toBe(0);

    trialDetailGate.release();
    await analysisLogDisclosure.click();
    await expect(page.getByText("terminal analyzer output")).toBeVisible();
    expect(requestCount(requests, analysisLogPattern)).toBe(1);

    const trialFilesRequest = page.waitForRequest(trialFilesPattern);
    await page.getByRole("tab", { name: "Files" }).click();
    await trialFilesRequest;
    expect(requestCount(requests, trialFilesPattern)).toBeGreaterThan(0);
    expect(requestCount(requests, trajectoryPattern)).toBe(0);

    const trajectoryRequest = page.waitForRequest(trajectoryPattern);
    await page.getByRole("tab", { name: "Trajectory" }).click();
    await trajectoryRequest;
    expect(requestCount(requests, trajectoryPattern)).toBeGreaterThan(0);
  });
});
