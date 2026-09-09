import { expect as baseExpect, test, type Page } from "@playwright/test";

import { fetchTrajectorySummary } from "../src/lib/use-trajectory-summary";
import type { Task, Trial } from "../src/lib/types";

// CI runs Next.js in dev mode; compiling and loading the public page can exceed
// the default five-second assertion budget before any mocked API request starts.
const expect = baseExpect.configure({ timeout: 15_000 });
test.setTimeout(60_000);

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

function resultRecords(
  tasks: Task[],
  trials: Partial<Trial>[],
  active = false
) {
  const header = publicOpenResponse(tasks[0], active);
  header.tasks = [];
  header.summary.task_count = tasks.length;
  header.summary.trial_count = trials.length;
  return [
    { type: "experiment", experiment: header },
    ...tasks.map((task) => ({
      type: "task",
      task: { ...task, trials: undefined },
    })),
    ...trials.map((trial) => ({
      type: "trial",
      trial: { ...trial, analysis: { status: null } },
    })),
    { type: "complete" },
  ];
}
const streamBody = (records: unknown[]) =>
  records.map((record) => JSON.stringify(record)).join("\n") + "\n";
async function mockResults(
  page: Page,
  token: string,
  tasks: Task[],
  trials: Trial[] = []
) {
  await page.route(`**/api/public/experiments/${token}/results`, (route) =>
    route.fulfill({
      contentType: "application/x-ndjson",
      body: streamBody(resultRecords(tasks, trials)),
    })
  );
}
async function mockInfo(page: Page, token: string) {
  await page.route(`**/api/public/experiments/${token}`, (route) =>
    route.fulfill({
      json: { name: "Streamed experiment", public_token: token },
    })
  );
  await page.route(`**/api/public/experiments/${token}/cost-totals`, (route) =>
    route.fulfill({ json: emptyCostTotals })
  );
}

async function mockContinuousResults(
  page: Page,
  token: string,
  initial: unknown[],
  remaining: unknown[]
) {
  await page.addInitScript(
    ({ token, initial, remaining }) => {
      const originalFetch = window.fetch.bind(window);
      const state = window as typeof window & {
        finishResults: () => void;
        resultRequests: number;
      };
      state.resultRequests = 0;
      window.fetch = async (input, init) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        if (!url.endsWith(`/experiments/${token}/results`))
          return originalFetch(input, init);
        state.resultRequests++;
        return new Response(
          new ReadableStream({
            start(controller) {
              const encoder = new TextEncoder();
              controller.enqueue(encoder.encode(initial));
              state.finishResults = () => {
                controller.enqueue(encoder.encode(remaining));
                controller.close();
              };
              init?.signal?.addEventListener("abort", () =>
                controller.error(new DOMException("Aborted", "AbortError"))
              );
            },
          }),
          { headers: { "Content-Type": "application/x-ndjson" } }
        );
      };
    },
    { token, initial: streamBody(initial), remaining: streamBody(remaining) }
  );
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
  await mockResults(page, token, [publicTask]);

  await page.goto(`/share/${token}`, { waitUntil: "domcontentloaded" });

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

test("public deep link resolves a trial outside the streamed results", async ({
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
  await page.route(`**/api/public/experiments/${token}/cost-totals`, (route) =>
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
  await mockResults(page, token, [
    task({ id: "task-1", name: "First loaded task" }),
  ]);
  await page.route(`**/api/public/experiments/${token}/focus?*`, (route) => {
    focusRequests += 1;
    const includeTrial = new URL(route.request().url()).searchParams.has(
      "trial"
    );
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

  await page.goto(`/share/${token}?task=task-101`, {
    waitUntil: "domcontentloaded",
  });

  await expect.poll(() => focusRequests).toBe(1);
  await expect(page).toHaveURL(/task=task-101/);
  await expect(page).not.toHaveURL(/trial=/);
  await expect(page.getByText("$12.34", { exact: true })).toBeVisible();

  await page.goto(`/share/${token}?task=task-101&trial=task-101-1`, {
    waitUntil: "domcontentloaded",
  });

  await expect.poll(() => focusRequests).toBe(2);
  await expect(page.getByRole("tab", { name: "Summary" })).toBeVisible();
  await expect(page).toHaveURL(/task=task-101&trial=task-101-1/);
});

test("retryable focus errors preserve the streamed-trial deep-link fallback", async ({
  page,
}) => {
  const token = "public-focus-retry";
  const publicTask = task({
    current_version: 1,
    current_version_id: "task-1-v1",
    trial_version_id: "task-1-v1",
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
  await page.route(`**/api/public/experiments/${token}/cost-totals`, (route) =>
    route.fulfill({ json: emptyCostTotals })
  );
  const records = resultRecords([publicTask], [focusedTrial]);
  await mockContinuousResults(
    page,
    token,
    records.slice(0, 2),
    records.slice(2)
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

  await page.goto(`/share/${token}?task=task-1&trial=task-1-2`, {
    waitUntil: "domcontentloaded",
  });

  await expect.poll(() => focusRequests).toBe(1);
  await page.evaluate(() =>
    (window as typeof window & { finishResults: () => void }).finishResults()
  );
  await expect(page.getByRole("tab", { name: "Summary" })).toBeVisible();
  await expect(page).toHaveURL(/task=task-1&trial=task-1-2/);
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
    trial_version_id: "task-1-v1",
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
  await page.route(`**/api/public/experiments/${token}/cost-totals`, (route) =>
    route.fulfill({ json: emptyCostTotals })
  );
  await mockResults(page, token, [publicTask], [publicTrial]);
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

  await page.goto(`/share/${token}`, { waitUntil: "domcontentloaded" });
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

test("one continuous response shows 101 tasks and 505 trials, then enables graphs", async ({
  page,
}) => {
  const token = "continuous-results";
  const tasks = Array.from({ length: 101 }, (_, i) =>
    task({
      id: `task-${i + 1}`,
      name: `Task ${i + 1}`,
      total: 5,
      completed: 5,
      trials: undefined,
    })
  );
  const trials = Array.from({ length: 505 }, (_, i) => ({
    id: `trial-${i + 1}`,
    task_id: `task-${Math.floor(i / 5) + 1}`,
    agent: "codex",
    model: "gpt-5",
    provider: "openai",
    status: "success" as const,
    reward: i < 250 ? 1 : 0,
    created_at: "2026-07-14T00:00:00Z",
  }));
  const records = resultRecords(tasks, trials);
  await mockInfo(page, token);
  const pageRequests: string[] = [];
  page.on("request", (request) => {
    if (/\/(open|trial-page)(\?|$)/.test(request.url()))
      pageRequests.push(request.url());
  });
  // A real ReadableStream lets the test keep one response open after records arrive.
  await mockContinuousResults(
    page,
    token,
    records.slice(0, 352),
    records.slice(352)
  );
  await page.goto(`/share/${token}`, { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("button", { name: "Task 1", exact: true })
  ).toBeVisible();
  const waiting = page.getByText(
    "Graphs will appear once all task and trial results have loaded."
  );
  await expect(waiting).toBeVisible();
  await expect(page.getByText("49.5%", { exact: true })).toHaveCount(0);
  await page.evaluate(() =>
    (window as typeof window & { finishResults: () => void }).finishResults()
  );
  await expect(waiting).toHaveCount(0);
  await expect(page.getByText("49.5%", { exact: true })).toBeVisible({
    timeout: 15000,
  });
  expect(
    await page.evaluate(
      () =>
        (window as typeof window & { resultRequests: number }).resultRequests
    )
  ).toBe(1);
  expect(pageRequests).toEqual([]);
  await expect(page.getByRole("button", { name: /Load next/ })).toHaveCount(0);
});

test("an interrupted stream keeps loaded rows, hides graphs, and retries the full response", async ({
  page,
}) => {
  const token = "interrupted-results";
  await mockInfo(page, token);
  const records = resultRecords([task({})], []);
  let requests = 0;
  await page.route(`**/api/public/experiments/${token}/results`, (route) => {
    requests++;
    return route.fulfill({
      contentType: "application/x-ndjson",
      body: streamBody(requests === 1 ? records.slice(0, -1) : records),
    });
  });
  await page.goto(`/share/${token}`, { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("button", { name: "Task one", exact: true })
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Some trial results failed to load" })
  ).toBeVisible();
  await page.waitForTimeout(750);
  expect(requests).toBe(1);
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Some trial results failed to load" })
  ).toHaveCount(0);
  expect(requests).toBe(2);
});

test("active experiments refresh one complete response and cost totals", async ({
  page,
}) => {
  const token = "active-results";
  await mockInfo(page, token);
  await page.clock.install();
  let requests = 0,
    costRequests = 0;
  await page.route(`**/api/public/experiments/${token}/results`, (route) => {
    requests++;
    return route.fulfill({
      contentType: "application/x-ndjson",
      body: streamBody(resultRecords([task({})], [], true)),
    });
  });
  await page.route(`**/api/public/experiments/${token}/cost-totals`, (route) =>
    route.fulfill({
      json: {
        ...emptyCostTotals,
        cost_usd: ++costRequests,
        cost_trial_count: 1,
      },
    })
  );
  await page.goto(`/share/${token}`, { waitUntil: "domcontentloaded" });
  await expect(page.getByText("$1.00", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Task one", exact: true })
  ).toBeVisible();
  await expect(
    page.getByRole("status").filter({ hasText: "Loading trials" })
  ).toHaveCount(0);
  expect(requests).toBe(1);
  await page.clock.runFor(30_100);
  await expect.poll(() => requests).toBe(2);
  await expect(page.getByText("$2.00", { exact: true })).toBeVisible();
});

test("cost totals remain visible without not-real labels", async ({ page }) => {
  const token = "cost-labels";
  await mockInfo(page, token);
  await mockResults(page, token, [task({})]);
  await page.route(`**/api/public/experiments/${token}/cost-totals`, (route) =>
    route.fulfill({
      json: {
        ...emptyCostTotals,
        cost_usd: 1190,
        cost_trial_count: 1,
        excluded_cost_usd: 1190,
        owned_cost_usd: 1190,
        owned_trial_count: 1,
        owned_excluded_cost_usd: 1190,
        experiment_cost_excluded: true,
      },
    })
  );
  await page.goto(`/share/${token}`, { waitUntil: "domcontentloaded" });
  await expect(page.getByText("$1,190", { exact: true })).toBeVisible();
  await expect(page.getByText(/not real/i)).toHaveCount(0);
});

test("initial results request failure offers Retry before any rows arrive", async ({
  page,
}) => {
  const token = "initial-stream-failure";
  await mockInfo(page, token);
  let requests = 0;
  await page.route(`**/api/public/experiments/${token}/results`, (route) => {
    requests++;
    return requests === 1
      ? route.fulfill({
          status: 503,
          json: { detail: "Temporarily unavailable" },
        })
      : route.fulfill({
          contentType: "application/x-ndjson",
          body: streamBody(resultRecords([task({})], [])),
        });
  });
  await page.goto(`/share/${token}`, { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("button", { name: "Retry", exact: true })
  ).toBeVisible();
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Task one", exact: true })
  ).toBeVisible();
  expect(requests).toBe(2);
});
