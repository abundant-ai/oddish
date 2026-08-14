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
const THIRD_VERSION_ID = "version-3";

function versionSummary(
  versionId: string,
  defaultVersionId = DEFAULT_VERSION_ID
) {
  const historical = versionId === HISTORICAL_VERSION_ID;
  const third = versionId === THIRD_VERSION_ID;
  const trialCount = historical ? 4 : third ? 6 : 25;
  const experiments = historical
    ? [{ id: "experiment-history", name: "Historical experiment" }]
    : third
      ? [{ id: "experiment-third", name: "Third experiment" }]
      : [{ id: "experiment-current", name: "Current experiment" }];
  return {
    id: versionId,
    version: historical ? 1 : third ? 3 : 2,
    message: historical
      ? "historical reader"
      : third
        ? "third reader"
        : "current reader",
    created_at: historical
      ? "2026-08-01T12:00:00Z"
      : third
        ? "2026-08-11T12:00:00Z"
        : "2026-08-10T12:00:00Z",
    is_current: versionId === defaultVersionId,
    trial_count: trialCount,
    completed_count: trialCount,
    failed_count: 0,
    skipped_count: 0,
    pass_count: historical ? 3 : third ? 6 : 20,
    partial_count: 0,
    fail_count: historical ? 1 : third ? 0 : 5,
    pending_count: 0,
    reward_sum: historical ? 3 : third ? 6 : 20,
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
        model: historical ? "gpt-history" : third ? "gpt-third" : "gpt-current",
        providers: ["openai"],
        is_probe: false,
        trial_count: trialCount,
        completed_count: trialCount,
        failed_count: 0,
        skipped_count: 0,
        pending_count: 0,
        pass_count: historical ? 3 : third ? 6 : 20,
        partial_count: 0,
        fail_count: historical ? 1 : third ? 0 : 5,
        reward_sum: historical ? 3 : third ? 6 : 20,
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
        duration_sum_seconds: historical ? 240 : third ? 360 : 2500,
        duration_trial_count: trialCount,
      },
    ],
  };
}

function openResponse(
  versionId = DEFAULT_VERSION_ID,
  verdictStatus: string | null = null,
  defaultVersionId = DEFAULT_VERSION_ID
) {
  const selected = versionSummary(versionId, defaultVersionId);
  const defaultVersion = versionSummary(defaultVersionId, defaultVersionId);
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
      current_version: defaultVersion.version,
      current_version_id: defaultVersion.id,
      user_tags: [],
      run_analysis: verdictStatus !== null,
      verdict_status: verdictStatus,
      verdict: null,
      verdict_error: null,
      created_at: "2026-08-01T12:00:00Z",
      updated_at: "2026-08-11T12:00:00Z",
    },
    default_version: {
      id: defaultVersion.id,
      version: defaultVersion.version,
      message: defaultVersion.message,
      created_at: defaultVersion.created_at,
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

function reviewResponse({
  active = false,
  findingCursor = false,
  trialCursor = false,
}: {
  active?: boolean;
  findingCursor?: boolean;
  trialCursor?: boolean;
} = {}) {
  const run = {
    id: active ? "qa-run-active" : "qa-run-published",
    disposition: active ? null : "published",
    task_version_id: DEFAULT_VERSION_ID,
    worker_job_id: active ? "qa-job-active" : "qa-job-published",
    input_trial_count: 2,
    input_set_sha256: "a".repeat(64),
    input_analysis_changed_count: active ? 0 : 1,
    pre_trial_block_id: "pre-trial-block",
    verdict_block_id: active ? null : "verdict-block",
    started_at: "2026-08-11T12:00:00Z",
    finished_at: active ? null : "2026-08-11T12:02:00Z",
  };
  const findings = findingCursor
    ? [
        {
          id: "finding-extra",
          source: "post_trial",
          problem_type: "incompleteness",
          dimension: "verifier",
          file: "verifier/tests.py",
          line_start: 99,
          line_end: 99,
          title: "Extra paginated finding",
          detail: "This finding came from the next cursor page.",
          recommendation: "Add the missing assertion.",
          tier: "optional",
          links_to: null,
          exploited: false,
          exploit_evidence: null,
          causal: false,
          from_pre_trial: false,
          trial_ids: ["review-trial-good-failure"],
          experiment_ids: ["experiment-review"],
        },
      ]
    : trialCursor
      ? []
      : [
          {
            id: "finding-optional",
            source: "pre_trial",
            problem_type: "incompleteness",
            dimension: "info_leakage",
            file: "instruction.md",
            line_start: 8,
            line_end: 8,
            title: "Optional clarification",
            detail: "The wording could be clearer.",
            recommendation: "Clarify the sentence.",
            tier: "optional",
            links_to: null,
            exploited: false,
            exploit_evidence: null,
            causal: false,
            from_pre_trial: true,
            trial_ids: [],
            experiment_ids: ["experiment-review"],
          },
          {
            id: "finding-must-fix",
            source: "post_trial",
            problem_type: "mismatch",
            dimension: "verifier",
            file: "verifier/tests.py",
            line_start: 12,
            line_end: 14,
            title: "Verifier rejects the requested behavior",
            detail: "The verifier checks the opposite outcome.",
            recommendation: "Align the verifier with instruction.md.",
            tier: "must_fix",
            links_to: null,
            exploited: true,
            exploit_evidence: "Trial step 4 follows the written requirement.",
            causal: true,
            from_pre_trial: false,
            trial_ids: ["review-trial-good-failure"],
            experiment_ids: ["experiment-review"],
          },
          {
            id: "finding-should-fix",
            source: "pre_trial",
            problem_type: "incompleteness",
            dimension: "oracle",
            file: "solution/solve.py",
            line_start: 3,
            line_end: 3,
            title: "Oracle omits an edge case",
            detail: "The reference path never exercises empty input.",
            recommendation: "Cover empty input in the oracle.",
            tier: "should_fix",
            links_to: null,
            exploited: false,
            exploit_evidence: null,
            causal: false,
            from_pre_trial: true,
            trial_ids: [],
            experiment_ids: ["experiment-review"],
          },
        ];
  const trials = trialCursor
    ? [
        {
          id: "review-trial-extra",
          role: "model",
          experiment_id: "experiment-review",
          agent: "codex",
          model: "gpt-next",
          config_fingerprint: "config-extra",
          environment: "docker",
          harbor_sha: "harbor-extra",
          status: "success",
          reward: 1,
          cost_usd: 0.02,
          duration_seconds: 9,
          included_in_result_run: false,
          result_run_analysis_fingerprint: null,
          analysis_matches_result_run: null,
          analysis_status: null,
          analysis: null,
        },
      ]
    : findingCursor
      ? []
      : [
          {
            id: "review-trial-good-failure",
            role: "model",
            experiment_id: "experiment-review",
            agent: "codex",
            model: "gpt-current",
            config_fingerprint: "config-main",
            environment: "docker",
            harbor_sha: "harbor-main",
            status: "success",
            reward: 0,
            cost_usd: 0.12,
            duration_seconds: 61,
            included_in_result_run: true,
            result_run_analysis_fingerprint: "analysis-old",
            analysis_matches_result_run: false,
            analysis_status: "success",
            analysis: {
              classification: "GOOD_FAILURE",
              subtype: "Implementation Bugs",
              evidence: "The agent failed without exposing a task defect.",
              root_cause: "The implementation was incomplete.",
              recommendation: "N/A",
              action_items: [],
              exploitation: [],
            },
          },
        ];
  return {
    schema_version: 1,
    task: {
      id: READER_TASK_ID,
      name: "Bounded task reader",
      version: 2,
      version_id: DEFAULT_VERSION_ID,
      content_hash: "content-review",
    },
    scope: {
      experiment_id: "experiment-review",
      tiers: ["must_fix", "should_fix", "optional"],
      same_version_across_experiments: false,
    },
    qa: {
      status: active ? "queued" : "success",
      result_run: active ? null : run,
      active_run: active ? run : null,
      is_task_published_run: !active,
      legacy_unscoped_verdict_available: !active,
      input_analysis_changed_after_run: !active,
    },
    baselines: {
      outcome: "valid",
      nop: {
        expected_reward: 0,
        valid: true,
        trial_count: 1,
        unexpected_count: 0,
      },
      oracle: {
        expected_reward: 1,
        valid: true,
        trial_count: 1,
        unexpected_count: 0,
      },
    },
    verdict: active
      ? null
      : {
          verdict: "accept",
          is_good: true,
          confidence: "high",
          primary_issue: null,
          reasoning: "The task behaves as specified.",
          recommendations: [],
          task_problem_count: 0,
          agent_problem_count: 1,
          success_count: 0,
          harness_error_count: 0,
        },
    finding_counts: {
      unfiltered_total: 4,
      filtered_total: 4,
      must_fix: 1,
      should_fix: 1,
      optional: 2,
    },
    findings,
    findings_page: {
      has_more: !findingCursor && !trialCursor,
      next_cursor: !findingCursor && !trialCursor ? "finding-next" : null,
    },
    trial_counts: {
      eligible: 2,
      analyzed: 1,
      unanalyzed: 1,
      classifications: {
        GOOD_FAILURE: 1,
        BAD_FAILURE: 0,
        GOOD_SUCCESS: 0,
        BAD_SUCCESS: 0,
        HARNESS_ERROR: 0,
      },
    },
    trials,
    trials_page: {
      has_more: !findingCursor && !trialCursor,
      next_cursor: !findingCursor && !trialCursor ? "trial-next" : null,
    },
  };
}

function capabilitiesResponse() {
  return {
    schema_version: 4,
    task_version_id: DEFAULT_VERSION_ID,
    cohort_success: [],
    cohort_failure: ["trial-1", "trial-2"],
    mode: "single",
    summary: "Agents found the failure but stopped before applying the fix.",
    models: {
      successful: [],
      failing: [{ model: "gpt-current", trials: 2 }],
    },
    trial_models: {
      "trial-1": "gpt-current",
      "trial-2": "gpt-current",
    },
    categories: [
      {
        category: "debugging",
        label: null,
        successful: [],
        failing: [
          {
            behavior_description: "Agents reproduced the reported failure.",
            evidence: [
              {
                trial_id: "trial-1",
                step_id: 7,
                quote: "The failing case reproduces consistently.",
              },
            ],
          },
        ],
      },
    ],
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

  test("capabilities load only when their pane is selected", async ({
    page,
  }) => {
    await signIn(page);
    let capabilityRequests = 0;

    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/open(?:\\?|$)`),
      (route) => route.fulfill({ json: openResponse() })
    );
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/detail(?:\\?|$)`),
      (route) => route.fulfill({ json: detailResponse() })
    );
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/trials(?:\\?|$)`),
      (route) => route.fulfill({ json: [] })
    );
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/files(?:\\?|$)`),
      (route) => route.fulfill({ json: { files: [] } })
    );
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/agent-capabilities(?:\\?|$)`),
      (route) => {
        expect(new URL(page.url()).searchParams.get("taskPane")).toBe(
          "capabilities"
        );
        capabilityRequests += 1;
        route.fulfill({ json: capabilitiesResponse() });
      }
    );

    await page.goto(`/tasks/${READER_TASK_ID}?drawer=task`);
    await expect(
      page.getByRole("button", { name: "Overview" })
    ).toHaveAttribute("aria-current", "page");
    await expect(
      page.getByRole("button", { name: "Capabilities" })
    ).toBeVisible();

    await page.getByRole("button", { name: "Capabilities" }).click();
    await expect.poll(() => capabilityRequests).toBe(1);
    await expect(
      page.getByRole("heading", { name: "Capabilities" })
    ).toBeVisible();
    await expect(page.getByText("Agents found the failure")).toBeVisible();
    const debuggingCategory = page
      .getByText("Debugging", { exact: true })
      .locator("..");
    await expect(debuggingCategory.getByText("gpt-current ×1")).toBeVisible();
    await debuggingCategory.getByText("1 example").click();
    await expect(
      debuggingCategory.getByRole("link", { name: /step 7/ })
    ).toHaveAttribute(
      "href",
      new RegExp(`trial=trial-1.*tab=trajectory#step-7$`)
    );
    await expect(page).toHaveURL(/taskPane=capabilities/);
    expect(capabilityRequests).toBe(1);

    await page.reload();
    await expect(
      page.getByRole("heading", { name: "Capabilities" })
    ).toBeVisible();
    expect(capabilityRequests).toBe(2);

    await page.goBack();
    await expect(
      page.getByRole("button", { name: "Overview" })
    ).toHaveAttribute("aria-current", "page");
    await page.goForward();
    await expect(
      page.getByRole("heading", { name: "Capabilities" })
    ).toBeVisible();
  });

  test("task review is bounded, cursor-paginated, provenance-aware, and polls only while active", async ({
    page,
  }) => {
    test.setTimeout(45_000);
    await signIn(page);
    let active = false;
    const reviewUrls: URL[] = [];
    const trialCollectionUrls: string[] = [];
    const initialReview = reviewResponse();
    expect(Buffer.byteLength(JSON.stringify(initialReview))).toBeLessThan(
      50_000,
    );

    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.pathname === `/api/tasks/${READER_TASK_ID}/trials`) {
        trialCollectionUrls.push(request.url());
      }
    });
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/open(?:\\?|$)`),
      (route) => route.fulfill({ json: openResponse() }),
    );
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/detail(?:\\?|$)`),
      (route) => route.fulfill({ json: detailResponse() }),
    );
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/files(?:\\?|$)`),
      (route) => route.fulfill({ json: { files: [] } }),
    );
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/review(?:\\?|$)`),
      (route) => {
        const url = new URL(route.request().url());
        reviewUrls.push(url);
        route.fulfill({
          json: reviewResponse({
            active,
            findingCursor: url.searchParams.has("finding_cursor"),
            trialCursor: url.searchParams.has("trial_cursor"),
          }),
        });
      },
    );

    await page.goto(`/tasks/${READER_TASK_ID}?drawer=task`);
    await expect(
      page.getByRole("heading", { name: "Task QA review" }),
    ).toBeVisible();
    await expect(page.getByText("Experiment experiment-review")).toBeVisible();
    await expect(
      page.getByText(
        "A legacy unscoped verdict exists. This view shows only version-owned QA evidence.",
      ),
    ).toBeVisible();
    await expect(
      page.getByText(
        "Trial analysis changed after the published QA run. Rerun QA before relying on this verdict.",
      ),
    ).toBeVisible();
    await expect(page.getByText("Fail · 0", { exact: true })).toBeVisible();
    await expect(page.getByText("Good failure", { exact: true })).toBeVisible();

    const tierOrder = (await page.locator("details > summary").allTextContents())
      .filter((text) => /MUST FIX|SHOULD FIX|OPTIONAL/.test(text))
      .map((text) => text.match(/MUST FIX|SHOULD FIX|OPTIONAL/)?.[0]);
    expect(tierOrder).toEqual(["MUST FIX", "SHOULD FIX", "OPTIONAL"]);
    await page.locator("summary").filter({ hasText: "MUST FIX" }).click();
    await expect(
      page.getByText("The verifier checks the opposite outcome."),
    ).toBeVisible();
    await expect(
      page.getByText("Align the verifier with instruction.md."),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: /codex · gpt-current/ }),
    ).toBeVisible();
    await expect(
      page.locator('a[href="/experiments/experiment-review"]'),
    ).toHaveAttribute("href", "/experiments/experiment-review");

    const initialRequests = () =>
      reviewUrls.filter(
        (url) =>
          !url.searchParams.has("finding_cursor") &&
          !url.searchParams.has("trial_cursor"),
      );
    await expect.poll(() => initialRequests().length).toBe(1);
    const first = initialRequests()[0];
    expect(first.searchParams.get("finding_limit")).toBe("20");
    expect(first.searchParams.get("trial_limit")).toBe("20");
    await page.waitForTimeout(5_500);
    expect(initialRequests()).toHaveLength(1);
    expect(trialCollectionUrls).toEqual([]);

    await page.getByRole("button", { name: "Show more findings" }).click();
    await expect
      .poll(
        () =>
          reviewUrls.filter((url) =>
            url.searchParams.has("finding_cursor"),
          ).length,
      )
      .toBe(1);
    await expect(page.getByText("Extra paginated finding")).toBeAttached();
    await page.getByRole("button", { name: "Show more trials" }).click();
    await expect
      .poll(
        () =>
          reviewUrls.filter((url) => url.searchParams.has("trial_cursor"))
            .length,
      )
      .toBe(1);
    await expect(page.getByText("codex · gpt-next")).toBeVisible();

    active = true;
    await page.reload();
    await expect(
      page.getByRole("heading", { name: "Task QA review" }),
    ).toBeVisible();
    const activeStart = initialRequests().length;
    await expect
      .poll(() => initialRequests().length, { timeout: 7_000 })
      .toBeGreaterThan(activeStart);
    expect(trialCollectionUrls).toEqual([]);
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
        await route.fulfill({
          json: openResponse(requested, null, currentDefault),
        });
      }
    );
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/versions$`),
      (route) =>
        route.fulfill({
          json: [
            versionSummary(DEFAULT_VERSION_ID),
            versionSummary(HISTORICAL_VERSION_ID),
            versionSummary(THIRD_VERSION_ID),
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
    await page.getByText("v3", { exact: true }).click();
    await expect(
      page.getByRole("link", { name: "Third experiment" }).last()
    ).toBeVisible();
    await page.getByRole("button", { name: /v3/ }).click();
    await page.getByText("v1", { exact: true }).click();
    await historicalRequestStarted;
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

    // Revisit a resource cached before the mutation, then select the former
    // default. Every versioned cache must agree that v1 is now the default;
    // otherwise v3 treats v2 as bare/default and incorrectly jumps back to v1.
    await page.getByRole("button", { name: /v1/ }).click();
    await page.getByText("v3", { exact: true }).click();
    await expect(
      page.getByRole("link", { name: "Third experiment" }).last()
    ).toBeVisible();
    await page.getByRole("button", { name: /v3/ }).click();
    await page.getByText("v2", { exact: true }).click();
    await expect(
      page.getByRole("link", { name: "Current experiment" }).last()
    ).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`version=${DEFAULT_VERSION_ID}`));
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

  for (const { label, trialParam } of [
    { label: "full-id", trialParam: `${READER_TASK_ID}-99` },
    { label: "short-id", trialParam: "99" },
  ]) {
    test(`an older ${label} trial link shares the single-trial resource and selects its version`, async ({
      page,
    }) => {
      await signIn(page);
      let trialRequests = 0;
      let unexpandedTrialRequests = 0;
      await page.route(
        new RegExp(`/api/tasks/${READER_TASK_ID}/open(?:\\?|$)`),
        (route) => {
          const requested =
            new URL(route.request().url()).searchParams.get("version_id") ??
            DEFAULT_VERSION_ID;
          route.fulfill({ json: openResponse(requested) });
        }
      );
      await page.route(new RegExp(`/api/trials/99$`), (route) => {
        unexpandedTrialRequests += 1;
        route.fulfill({ status: 404, json: { error: "Trial not found" } });
      });
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
        page.getByRole("heading", { name: /Old linked trial/ })
      ).toBeVisible();
      await expect(page).toHaveURL(new RegExp(`trial=${READER_TASK_ID}-99`));
      await expect(page).toHaveURL(
        new RegExp(`version=${HISTORICAL_VERSION_ID}`)
      );
      expect(trialRequests).toBe(1);
      expect(unexpandedTrialRequests).toBe(0);
    });
  }

  test("a hand-shortened trial link addresses this page's task", async ({
    page,
  }) => {
    await signIn(page);
    const trialPaths: string[] = [];
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/open(?:\\?|$)`),
      (route) => {
        const requested =
          new URL(route.request().url()).searchParams.get("version_id") ??
          DEFAULT_VERSION_ID;
        route.fulfill({ json: openResponse(requested) });
      }
    );
    // Every single-trial id, not just the expanded one: a request for the bare
    // index has to 404 the way it does in production, or the short link looks
    // resolved while addressing a trial that doesn't exist.
    await page.route(/\/api\/trials\/[^/?]+$/, (route) => {
      const path = new URL(route.request().url()).pathname;
      trialPaths.push(path);
      if (path === `/api/trials/${READER_TASK_ID}-99`) {
        route.fulfill({ json: linkedTrial() });
        return;
      }
      route.fulfill({ status: 404, json: { error: "Trial not found" } });
    });
    await page.route(
      new RegExp(`/api/tasks/${READER_TASK_ID}/detail(?:\\?|$)`),
      (route) => route.fulfill({ json: detailResponse(HISTORICAL_VERSION_ID) })
    );

    await page.goto(`/tasks/${READER_TASK_ID}?trial=99`);
    await expect(
      page.getByRole("heading", { name: /Old linked trial/ })
    ).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`trial=${READER_TASK_ID}-99`));
    expect([...new Set(trialPaths)]).toEqual([
      `/api/trials/${READER_TASK_ID}-99`,
    ]);
  });

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

    await page.goto(`/tasks/${READER_TASK_ID}?trial=${READER_TASK_ID}-99`);
    await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`trial=${READER_TASK_ID}-99`));
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
