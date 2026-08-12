import { expect, test, type Page } from "@playwright/test";
import { clerk, setupClerkTestingToken } from "@clerk/testing/playwright";

const CLERK_EMAIL = process.env.E2E_CLERK_EMAIL;
const CLERK_SECRET = process.env.CLERK_SECRET_KEY;
const CLERK_PUBLISHABLE = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;
const TASK_ID = process.env.E2E_TASK_ID;

const hasClerkEnv = !!CLERK_EMAIL && !!CLERK_SECRET && !!CLERK_PUBLISHABLE;

const READER_TASK_ID = "task-open-view";
const DEFAULT_VERSION_ID = "version-2";
const HISTORICAL_VERSION_ID = "version-1";

function versionSummary(versionId: string) {
  const historical = versionId === HISTORICAL_VERSION_ID;
  const trialCount = historical ? 4 : 25;
  const experiments = historical
    ? [{ id: "experiment-history", name: "Historical experiment" }]
    : [{ id: "experiment-current", name: "Current experiment" }];
  return {
    id: versionId,
    version: historical ? 1 : 2,
    message: historical ? "historical reader" : "current reader",
    created_at: historical ? "2026-08-01T12:00:00Z" : "2026-08-10T12:00:00Z",
    is_current: !historical,
    trial_count: trialCount,
    completed_count: trialCount,
    failed_count: 0,
    skipped_count: 0,
    pass_count: historical ? 3 : 20,
    partial_count: 0,
    fail_count: historical ? 1 : 5,
    pending_count: 0,
    reward_sum: historical ? 3 : 20,
    reward_total: trialCount,
    cost_usd: trialCount,
    cost_trial_count: trialCount,
    cost_has_estimated: false,
    cost_has_native: true,
    billed_cost_usd: trialCount,
    billed_trial_count: trialCount,
    billed_has_estimated: false,
    billed_has_native: true,
    last_run_at: "2026-08-11T12:00:00Z",
    user_tags: [],
    experiments,
    agent_models: [
      {
        agent: "codex",
        model: historical ? "gpt-history" : "gpt-current",
        providers: ["openai"],
        is_probe: false,
        trial_count: trialCount,
        completed_count: trialCount,
        failed_count: 0,
        skipped_count: 0,
        pending_count: 0,
        pass_count: historical ? 3 : 20,
        partial_count: 0,
        fail_count: historical ? 1 : 5,
        reward_sum: historical ? 3 : 20,
        reward_total: trialCount,
        cost_usd: trialCount,
        cost_trial_count: trialCount,
        cost_has_estimated: false,
        cost_has_native: true,
        billed_cost_usd: trialCount,
        billed_trial_count: trialCount,
        billed_has_estimated: false,
        billed_has_native: true,
        last_run_at: "2026-08-11T12:00:00Z",
        duration_sum_seconds: historical ? 240 : 2500,
        duration_trial_count: trialCount,
      },
    ],
  };
}

function openResponse(
  versionId = DEFAULT_VERSION_ID,
  verdictStatus: string | null = null
) {
  const selected = versionSummary(versionId);
  const trials = Array.from(
    { length: Math.min(selected.trial_count, 20) },
    (_, index) => ({
      id: `${READER_TASK_ID}-${index + 1}`,
      name: `Recent trial ${index + 1}`,
      experiment_id: selected.experiments[0].id,
      task_version_id: versionId,
      agent: " CODEX ",
      provider: "openai",
      model: selected.agent_models[0].model?.toUpperCase() ?? null,
      status: "success",
      reward: index < selected.pass_count ? 1 : 0,
      error_kind: null,
      is_probe: false,
      cost_usd: 1,
      cost_is_estimated: false,
      is_billed: true,
      created_at: `2026-08-11T11:${String(index).padStart(2, "0")}:00Z`,
      started_at: null,
      finished_at: null,
    })
  );
  return {
    task: {
      id: READER_TASK_ID,
      name: "Bounded task reader",
      status: verdictStatus ? "verdict_pending" : "completed",
      priority: "low",
      user: "reader@example.com",
      github_username: null,
      github_meta: null,
      link: null,
      task_path: "tasks/bounded-reader",
      experiments: selected.experiments,
      current_version: 2,
      current_version_id: DEFAULT_VERSION_ID,
      user_tags: [],
      run_analysis: verdictStatus !== null,
      verdict_status: verdictStatus,
      verdict: null,
      verdict_error: null,
      created_at: "2026-08-01T12:00:00Z",
      updated_at: "2026-08-11T12:00:00Z",
    },
    default_version: {
      id: DEFAULT_VERSION_ID,
      version: 2,
      message: "current reader",
      created_at: "2026-08-10T12:00:00Z",
      is_current: true,
    },
    selected_version: selected,
    totals: {
      cost_usd: 29,
      cost_trial_count: 29,
      cost_has_estimated: false,
      cost_has_native: true,
      billed_cost_usd: 29,
      billed_trial_count: 29,
      billed_has_estimated: false,
      billed_has_native: true,
      total_trials: 29,
      qa_cost_usd: 0,
      token_count: 2900,
      token_trial_count: 29,
    },
    trials,
    trials_has_more: selected.trial_count > trials.length,
  };
}

function linkedTrial(taskId = READER_TASK_ID) {
  return {
    id: `${READER_TASK_ID}-99`,
    name: "Old linked trial",
    task_id: taskId,
    task_path: "tasks/bounded-reader",
    experiment_id: "experiment-history",
    agent: "codex",
    provider: "openai",
    model: "gpt-history",
    status: "success",
    attempts: 1,
    max_attempts: 1,
    harbor_stage: "completed",
    reward: 1,
    task_version: 1,
    task_version_id: HISTORICAL_VERSION_ID,
    cost_usd: 1,
    is_probe: false,
    is_billed: true,
    created_at: "2026-08-01T12:00:00Z",
    started_at: "2026-08-01T12:00:00Z",
    finished_at: "2026-08-01T12:01:00Z",
  };
}

function detailResponse(versionId = DEFAULT_VERSION_ID) {
  const open = openResponse(versionId);
  return {
    task: {
      ...open.task,
      experiment_id: "",
      experiment_name: "",
      experiment_is_public: false,
      total: open.selected_version.trial_count,
      completed: open.selected_version.completed_count,
      failed: 0,
      skipped: 0,
      trials: [],
    },
    versions: [open.selected_version],
    totals: open.totals,
  };
}

async function signIn(page: Page) {
  await setupClerkTestingToken({ page });
  await page.goto("/");
  await clerk.signIn({ page, emailAddress: CLERK_EMAIL! });
}

test.describe("authenticated task view", () => {
  test.skip(
    !hasClerkEnv,
    "needs E2E_CLERK_EMAIL + CLERK_SECRET_KEY + NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY"
  );

  test("dashboard task detail renders for a signed-in user", async ({
    page,
  }) => {
    // Testing Token first: it bypasses Clerk bot detection so the programmatic
    // sign-in below is accepted. Then load an unprotected page that boots Clerk.
    await setupClerkTestingToken({ page });
    await page.goto("/");

    await clerk.signIn({
      page,
      // Non-null: the describe-level skip guarantees CLERK_EMAIL is set.
      emailAddress: CLERK_EMAIL!,
    });

    if (TASK_ID) {
      await page.goto(`/tasks/${TASK_ID}`);
    } else {
      await page.goto("/tasks");
      // CardTitle renders a styled <div>, not an <h*>, so this is text — not a
      // heading role.
      await expect(
        page.getByText("Recent Tasks", { exact: true })
      ).toBeVisible();
      await page.locator('a[href^="/tasks/"]').first().click();
    }

    // The task-detail client always renders an "Agents" section once the detail
    // payload resolves (frontend/src/app/(app)/tasks/[task_id]/
    // task-detail-client.tsx), so it's a stable, non-brittle readiness anchor.
    await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible({
      timeout: 15_000,
    });
  });

  test("task drawer requests a metadata-only file tree", async ({ page }) => {
    test.skip(!TASK_ID, "needs E2E_TASK_ID");

    await setupClerkTestingToken({ page });
    await page.goto("/");
    await clerk.signIn({ page, emailAddress: CLERK_EMAIL! });

    const listingResponse = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === `/api/tasks/${TASK_ID}/files`;
    });
    await page.goto(`/tasks/${TASK_ID}?drawer=task`);

    const response = await listingResponse;
    const url = new URL(response.url());
    expect(url.searchParams.get("recursive")).toBe("1");
    expect(url.searchParams.get("inline")).toBe("0");
    expect(url.searchParams.get("presign")).toBe("0");

    const listing = (await response.json()) as {
      files?: Array<{ content?: string; url?: string }>;
    };
    expect(
      (listing.files ?? []).every(
        (file) => file.content === undefined && file.url === undefined
      )
    ).toBe(true);
  });

  test("binary preview waits for its own presigned URL", async ({ page }) => {
    test.skip(!TASK_ID, "needs E2E_TASK_ID");

    await setupClerkTestingToken({ page });
    await page.goto("/");
    await clerk.signIn({ page, emailAddress: CLERK_EMAIL! });

    let signalBinaryRequest!: () => void;
    const binaryRequestStarted = new Promise<void>((resolve) => {
      signalBinaryRequest = resolve;
    });
    let releaseBinaryRequest!: () => void;
    const binaryRequestGate = new Promise<void>((resolve) => {
      releaseBinaryRequest = resolve;
    });
    const binaryRequests: URL[] = [];
    const filesPath = `/api/tasks/${TASK_ID}/files`;

    await page.route(`**${filesPath}**`, async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === filesPath) {
        await route.fulfill({
          contentType: "application/json",
          body: JSON.stringify({
            files: [
              { path: "readme.txt", key: "readme.txt", size: 200_000 },
              { path: "preview.png", key: "preview.png", size: 43 },
            ],
          }),
        });
        return;
      }
      if (url.pathname === `${filesPath}/readme.txt`) {
        expect(url.searchParams.get("presign")).toBeNull();
        expect(url.searchParams.get("max_bytes")).toBe("102400");
        await route.fulfill({
          contentType: "application/json",
          body: JSON.stringify({
            content: "text preview loaded",
            is_truncated: true,
          }),
        });
        return;
      }
      if (url.pathname === `${filesPath}/preview.png`) {
        binaryRequests.push(url);
        if (url.searchParams.get("presign") === "1") {
          signalBinaryRequest();
          await binaryRequestGate;
          await route.fulfill({
            contentType: "application/json",
            body: JSON.stringify({
              url: "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==",
            }),
          });
        } else {
          await route.fulfill({ status: 500, body: "unexpected bare request" });
        }
        return;
      }
      await route.continue();
    });

    await page.goto(`/tasks/${TASK_ID}?drawer=task`);
    await page.getByRole("button", { name: "readme.txt" }).click();
    await expect(page.getByText("text preview loaded")).toBeVisible();
    await expect(page.getByText(/Showing first 100\.0 KB/)).toBeVisible();

    await page.getByRole("button", { name: "preview.png" }).click();
    await binaryRequestStarted;
    await expect(page.getByRole("img", { name: "preview.png" })).toHaveCount(0);
    expect(
      binaryRequests.filter((url) => url.searchParams.get("presign") !== "1")
    ).toEqual([]);

    releaseBinaryRequest();
    await expect(page.getByRole("img", { name: "preview.png" })).toBeVisible();
    expect(binaryRequests).toHaveLength(1);
  });

  test("bounded open renders exact metrics, recent previews, historical experiments, and default switching", async ({
    page,
  }) => {
    await signIn(page);
    let currentDefault = DEFAULT_VERSION_ID;
    let defaultMutationCount = 0;
    let signalHistoricalRequest!: () => void;
    const historicalRequestStarted = new Promise<void>((resolve) => {
      signalHistoricalRequest = resolve;
    });
    let releaseHistoricalRequest!: () => void;
    const historicalRequestGate = new Promise<void>((resolve) => {
      releaseHistoricalRequest = resolve;
    });
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/open(?:\\?|$)`),
      async (route) => {
        const requested =
          new URL(route.request().url()).searchParams.get("version_id") ??
          currentDefault;
        if (requested === HISTORICAL_VERSION_ID) {
          signalHistoricalRequest();
          await historicalRequestGate;
        }
        await route.fulfill({ json: openResponse(requested) });
      }
    );
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/versions$`),
      (route) =>
        route.fulfill({
          json: [
            versionSummary(DEFAULT_VERSION_ID),
            versionSummary(HISTORICAL_VERSION_ID),
          ],
        })
    );
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/versions/1/default$`),
      (route) => {
        defaultMutationCount += 1;
        currentDefault = HISTORICAL_VERSION_ID;
        route.fulfill({ json: { ok: true } });
      }
    );

    await page.goto(`/tasks/${READER_TASK_ID}`);
    await expect(page.getByText("1 agent · 25 trials")).toBeVisible();
    await expect(
      page.getByText("Showing 20 most recent of 25 trials")
    ).toBeVisible();
    await expect(page.getByText("80% (20/25)")).toBeVisible();
    await expect(page.getByText("1m 40s")).toBeVisible();
    await expect(page.getByText("$25.00").last()).toBeVisible();
    await expect(page.getByText("$1.00")).toBeVisible();

    await page.getByRole("button", { name: /v2/ }).click();
    await page.getByText("v1", { exact: true }).click();
    await historicalRequestStarted;
    await expect(page.getByText("$25.00").last()).toBeVisible();
    releaseHistoricalRequest();
    await expect(
      page.getByRole("link", { name: "Historical experiment" }).last()
    ).toBeVisible();
    await expect(page).toHaveURL(
      new RegExp(`version=${HISTORICAL_VERSION_ID}`)
    );
    await page.getByRole("button", { name: "Make default" }).click();
    await expect.poll(() => defaultMutationCount).toBe(1);
    await expect(page).not.toHaveURL(/version=/);
  });

  test("verdict controls preserve Run and Cancel requests while revalidating open", async ({
    page,
  }) => {
    await signIn(page);
    let verdictStatus: string | null = null;
    let backfillBody: unknown;
    let cancelCount = 0;
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/open(?:\\?|$)`),
      (route) =>
        route.fulfill({ json: openResponse(DEFAULT_VERSION_ID, verdictStatus) })
    );
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/qa/backfill$`),
      async (route) => {
        backfillBody = route.request().postDataJSON();
        verdictStatus = "queued";
        await route.fulfill({ json: { ok: true } });
      }
    );
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/qa/cancel$`),
      (route) => {
        cancelCount += 1;
        verdictStatus = null;
        route.fulfill({ json: { ok: true } });
      }
    );

    await page.goto(`/tasks/${READER_TASK_ID}`);
    await page.getByRole("button", { name: "Run QA" }).click();
    await expect
      .poll(() => backfillBody)
      .toEqual({
        force: false,
        enable_analysis: true,
      });
    await expect(page.getByRole("button", { name: "Cancel QA" })).toBeVisible();
    await page.getByRole("button", { name: "Cancel QA" }).click();
    await expect.poll(() => cancelCount).toBe(1);
  });

  for (const trialParam of ["99", `${READER_TASK_ID}-99`]) {
    test(`older ${trialParam === "99" ? "short" : "legacy"} trial link shares the single-trial resource and selects its version`, async ({
      page,
    }) => {
      await signIn(page);
      let trialRequests = 0;
      await page.route(
        new RegExp(`/api/tasks/${READER_TASK_ID}/open(?:\\?|$)`),
        (route) => {
          const requested =
            new URL(route.request().url()).searchParams.get("version_id") ??
            DEFAULT_VERSION_ID;
          route.fulfill({ json: openResponse(requested) });
        }
      );
      await page.route(
        new RegExp(`/api/trials/${READER_TASK_ID}-99$`),
        (route) => {
          trialRequests += 1;
          route.fulfill({ json: linkedTrial() });
        }
      );
      await page.route(
        new RegExp(`/api/tasks/${READER_TASK_ID}/detail(?:\\?|$)`),
        (route) =>
          route.fulfill({ json: detailResponse(HISTORICAL_VERSION_ID) })
      );

      await page.goto(`/tasks/${READER_TASK_ID}?trial=${trialParam}`);
      await expect(
        page.getByRole("heading", { name: /task-open-view #99/ })
      ).toBeVisible();
      await expect(page).toHaveURL(/trial=99/);
      await expect(page).toHaveURL(
        new RegExp(`version=${HISTORICAL_VERSION_ID}`)
      );
      expect(trialRequests).toBe(1);
    });
  }

  test("an older trial owned by another task is rejected without destroying its URL", async ({
    page,
  }) => {
    await signIn(page);
    let detailRequests = 0;
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/open(?:\\?|$)`),
      (route) => route.fulfill({ json: openResponse() })
    );
    await page.route(new RegExp(`/api/trials/${READER_TASK_ID}-99$`), (route) =>
      route.fulfill({ json: linkedTrial("another-task") })
    );
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/detail(?:\\?|$)`),
      (route) => {
        detailRequests += 1;
        route.fulfill({ json: detailResponse() });
      }
    );

    await page.goto(`/tasks/${READER_TASK_ID}?trial=99`);
    await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
    await expect(page).toHaveURL(/trial=99/);
    await expect(page.getByText("Old linked trial")).toHaveCount(0);
    expect(detailRequests).toBe(0);
  });

  test("invalid explicit version proves the default and clears only version state", async ({
    page,
  }) => {
    await signIn(page);
    const openUrls: string[] = [];
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/open(?:\\?|$)`),
      (route) => {
        const url = new URL(route.request().url());
        openUrls.push(url.pathname + url.search);
        if (url.searchParams.get("version_id") === "missing-version") {
          route.fulfill({ status: 404, json: { error: "Version not found" } });
          return;
        }
        route.fulfill({ json: openResponse() });
      }
    );
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/detail(?:\\?|$)`),
      (route) => route.fulfill({ json: detailResponse() })
    );

    await page.goto(
      `/tasks/${READER_TASK_ID}?version=missing-version&drawer=task&taskFile=README.txt&taskLines=L1-L2`
    );
    await expect(
      page.getByRole("heading", { name: "Bounded task reader", exact: true })
    ).toBeVisible();
    await expect(page).not.toHaveURL(/version=/);
    await expect(page).toHaveURL(/drawer=task/);
    await expect(page).toHaveURL(/taskFile=README.txt/);
    await expect(page).toHaveURL(/taskLines=L1-L2/);
    expect(openUrls.filter((url) => !url.includes("version_id")).length).toBe(
      1
    );
  });

  test("a missing default remains a genuine task failure", async ({ page }) => {
    await signIn(page);
    let defaultRequests = 0;
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/open(?:\\?|$)`),
      (route) => {
        const url = new URL(route.request().url());
        if (!url.searchParams.has("version_id")) defaultRequests += 1;
        route.fulfill({ status: 404, json: { error: "Task not found" } });
      }
    );

    await page.goto(`/tasks/${READER_TASK_ID}`);
    await expect(page.getByText("Failed to load task")).toBeVisible();
    expect(defaultRequests).toBe(1);
  });
});
