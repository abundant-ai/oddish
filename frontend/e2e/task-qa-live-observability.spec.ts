import { expect, test, type Page } from "@playwright/test";
import { clerk, setupClerkTestingToken } from "@clerk/testing/playwright";

const CLERK_EMAIL = process.env.E2E_CLERK_EMAIL;
const CLERK_SECRET = process.env.CLERK_SECRET_KEY;
const CLERK_PUBLISHABLE = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;
const hasClerkEnv = !!CLERK_EMAIL && !!CLERK_SECRET && !!CLERK_PUBLISHABLE;

const TASK_ID = "qa-live-task";
const VERSION_ID = `${TASK_ID}-v2`;
const AGENT_TRIAL_ID = `${TASK_ID}-1`;
const QA_TRIAL_ID = `${TASK_ID}-qa`;
const CREATED_AT = "2026-08-27T12:00:00Z";

type QaStatus = "queued" | "running";

function compactTrial(id: string, kind: "agent" | "qa", status: string) {
  return {
    id,
    name: id,
    experiment_id: kind === "qa" ? "qa-shadow" : "experiment-1",
    task_version_id: VERSION_ID,
    agent: kind === "qa" ? "claude-code" : "codex",
    provider: kind === "qa" ? "anthropic" : "openai",
    model: kind === "qa" ? "anthropic/claude-sonnet-4-6" : "openai/gpt-5.6",
    kind,
    status,
    reward: kind === "agent" ? 0 : null,
    error_kind: null,
    is_probe: false,
    cost_usd: kind === "agent" ? 1 : null,
    cost_is_estimated: false,
    is_billed: kind === "agent",
    has_trajectory: status === "success",
    created_at: CREATED_AT,
    started_at: status === "running" ? CREATED_AT : null,
    finished_at: status === "success" ? CREATED_AT : null,
  };
}

function taskOpenResponse(qaStatus: QaStatus | null) {
  return {
    task: {
      id: TASK_ID,
      name: "QA live observability",
      status: "completed",
      priority: "low",
      user: "qa@example.com",
      task_path: "tasks/qa-live",
      experiments: [{ id: "experiment-1", name: "Experiment 1" }],
      current_version: 2,
      current_version_id: VERSION_ID,
      user_tags: [],
      run_analysis: true,
      verdict_status: null,
      verdict: null,
      verdict_error: null,
      created_at: CREATED_AT,
      updated_at: CREATED_AT,
    },
    default_version: {
      id: VERSION_ID,
      version: 2,
      created_at: CREATED_AT,
      is_current: true,
    },
    selected_version: {
      id: VERSION_ID,
      version: 2,
      created_at: CREATED_AT,
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
      cost_usd: 1,
      cost_trial_count: 1,
      cost_has_estimated: false,
      cost_has_native: true,
      billed_cost_usd: 1,
      billed_trial_count: 1,
      billed_has_estimated: false,
      billed_has_native: true,
      last_run_at: CREATED_AT,
      duration_sum_seconds: 60,
      duration_trial_count: 1,
      user_tags: [],
      experiments: [{ id: "experiment-1", name: "Experiment 1" }],
      agent_models: [
        {
          agent: "codex",
          model: "openai/gpt-5.6",
          providers: ["openai"],
          is_probe: false,
          trial_count: 1,
          completed_count: 1,
          failed_count: 0,
          skipped_count: 0,
          pending_count: 0,
          pass_count: 0,
          partial_count: 0,
          fail_count: 1,
          reward_sum: 0,
          reward_total: 1,
          cost_usd: 1,
          cost_trial_count: 1,
          cost_has_estimated: false,
          cost_has_native: true,
          billed_cost_usd: 1,
          billed_trial_count: 1,
          billed_has_estimated: false,
          billed_has_native: true,
          last_run_at: CREATED_AT,
          duration_sum_seconds: 60,
          duration_trial_count: 1,
        },
      ],
    },
    totals: {
      cost_usd: 1,
      cost_trial_count: 1,
      cost_has_estimated: false,
      cost_has_native: true,
      billed_cost_usd: 1,
      billed_trial_count: 1,
      billed_has_estimated: false,
      billed_has_native: true,
      total_trials: 1,
      token_count: 100,
      token_trial_count: 1,
    },
    active_qa_trial:
      qaStatus === null ? null : compactTrial(QA_TRIAL_ID, "qa", qaStatus),
    trials: [compactTrial(AGENT_TRIAL_ID, "agent", "success")],
    trials_has_more: false,
  };
}

function trialDetail(id: string, status: string) {
  const compact = compactTrial(id, id === QA_TRIAL_ID ? "qa" : "agent", status);
  const isAgent = id === AGENT_TRIAL_ID;
  return {
    ...compact,
    task_id: TASK_ID,
    task_path: "tasks/qa-live",
    task_version: 2,
    attempts: 1,
    max_attempts: 6,
    harbor_stage: status === "success" ? "completed" : status,
    error_message: null,
    result: null,
    analysis_status: isAgent ? "success" : null,
    analysis: isAgent
      ? {
          classification: "GOOD_FAILURE",
          subtype: "correct",
          evidence: "The verifier correctly rejected the output.",
          _graded_by: QA_TRIAL_ID,
          _graded_at_steps: [15],
        }
      : null,
    jobs: [],
    has_trajectory: status === "success",
    input_tokens: status === "success" ? 100 : 10,
    output_tokens: status === "success" ? 20 : 2,
  };
}

async function installRoutes(
  page: Page,
  qaStatus: QaStatus | null,
  qaDetailStatus: () => string
) {
  await page.route(new RegExp(`/api/tasks/${TASK_ID}/open(?:\\?|$)`), (route) =>
    route.fulfill({ json: taskOpenResponse(qaStatus) })
  );
  await page.route(
    new RegExp(`/api/trials/${QA_TRIAL_ID}/live(?:\\?|$)`),
    (route) =>
      route.fulfill({
        json: {
          attempt: 1,
          events: [],
          next_seq: 0,
          usage: { input_tokens: 10, output_tokens: 2, cost_usd: 0.01 },
          harbor_stage: "agent_running",
          done: false,
        },
      })
  );
  await page.route(new RegExp(`/api/trials/${QA_TRIAL_ID}$`), (route) =>
    route.fulfill({ json: trialDetail(QA_TRIAL_ID, qaDetailStatus()) })
  );
  await page.route(new RegExp(`/api/trials/${AGENT_TRIAL_ID}$`), (route) =>
    route.fulfill({ json: trialDetail(AGENT_TRIAL_ID, "success") })
  );
  await page.route(
    new RegExp(`/api/trials/${QA_TRIAL_ID}/trajectory/summary$`),
    (route) => route.fulfill({ status: 404, json: { detail: "No summary" } })
  );
  await page.route(
    new RegExp(`/api/trials/${QA_TRIAL_ID}/trajectory$`),
    (route) =>
      route.fulfill({
        json: {
          schema_version: "1.0",
          session_id: "qa-session",
          agent: {
            name: "claude-code",
            version: "1",
            model_name: "anthropic/claude-sonnet-4-6",
          },
          steps: [
            {
              step_id: 15,
              timestamp: CREATED_AT,
              source: "agent",
              model_name: "anthropic/claude-sonnet-4-6",
              message: "graded this trial",
              reasoning_content: null,
              tool_calls: null,
              observation: null,
              metrics: null,
            },
          ],
          notes: null,
          final_metrics: null,
        },
      })
  );
}

async function openAgentDrawer(page: Page, expectActiveQaButton = true) {
  await page.goto(`/tasks/${TASK_ID}?trial=${AGENT_TRIAL_ID}`);
  if (expectActiveQaButton) {
    await expect(
      page.getByRole("button", { name: "view the QA run" })
    ).toBeVisible();
  }
}

test.describe("task QA live observability", () => {
  test.skip(
    !hasClerkEnv,
    "needs E2E_CLERK_EMAIL + CLERK_SECRET_KEY + NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY"
  );

  test.beforeEach(async ({ page }) => {
    await setupClerkTestingToken({ page });
    await page.goto("/");
    await clerk.signIn({ page, emailAddress: CLERK_EMAIL! });
  });

  test("running QA opens Live, shares one detail request, and settles on Summary", async ({
    page,
  }) => {
    test.setTimeout(30_000);
    let qaDetailRequests = 0;
    await installRoutes(page, "running", () => {
      qaDetailRequests += 1;
      return qaDetailRequests === 1 ? "running" : "success";
    });
    await openAgentDrawer(page);

    await page.getByRole("button", { name: "view the QA run" }).click();
    await expect
      .poll(() => new URL(page.url()).searchParams.get("trial"))
      .toBe(QA_TRIAL_ID);
    await expect
      .poll(() => new URL(page.url()).searchParams.get("tab"))
      .toBe("live");
    await expect(page.getByRole("tab", { name: "Live" })).toHaveAttribute(
      "data-state",
      "active"
    );
    await page.waitForTimeout(1_000);
    expect(qaDetailRequests).toBe(1);

    await expect(page.getByRole("tab", { name: "Summary" })).toHaveAttribute(
      "data-state",
      "active",
      { timeout: 10_000 }
    );
    expect(qaDetailRequests).toBe(2);
    await expect(page.getByRole("tab", { name: "Live" })).toHaveCount(0);
  });

  test("queued QA opens Summary", async ({ page }) => {
    let qaDetailRequests = 0;
    await installRoutes(page, "queued", () => {
      qaDetailRequests += 1;
      return "queued";
    });
    await openAgentDrawer(page);

    await page.getByRole("button", { name: "view the QA run" }).click();
    await expect
      .poll(() => new URL(page.url()).searchParams.get("trial"))
      .toBe(QA_TRIAL_ID);
    await expect
      .poll(() => new URL(page.url()).searchParams.get("tab"))
      .toBe("summary");
    await expect(page.getByRole("tab", { name: "Summary" })).toHaveAttribute(
      "data-state",
      "active"
    );
    await page.waitForTimeout(1_000);
    expect(qaDetailRequests).toBe(1);
  });

  test("graded-step link opens the QA trajectory at step 15", async ({
    page,
  }) => {
    await installRoutes(page, null, () => "success");
    await openAgentDrawer(page, false);

    const gradedStepLink = page.getByRole("link", { name: "at step 15" });
    await expect(gradedStepLink).toHaveAttribute("href", /#step-15$/);
    await gradedStepLink.click();

    await expect
      .poll(() => new URL(page.url()).searchParams.get("trial"))
      .toBe(QA_TRIAL_ID);
    await expect(page.getByRole("tab", { name: "Trajectory" })).toHaveAttribute(
      "data-state",
      "active"
    );
    await expect(page.getByText("#15", { exact: true })).toBeVisible();
    await expect(
      page.getByText("graded this trial", { exact: true })
    ).toBeVisible();
  });
});
