import { expect, test, type Page } from "@playwright/test";

// The "Avg score" KPI tile prints the same percentage as a single grouped
// leaderboard row, so an unscoped getByText matches one element before the
// leaderboard renders and two afterwards (a strict-mode violation).
const leaderboard = (page: Page) =>
  page.getByRole("region", { name: "Leaderboard" });

test.beforeEach(async ({ page }) => {
  await page.route("**/api/skills", (route) =>
    route.fulfill({
      json: [
        {
          id: "test-skill",
          name: "Queue check",
          is_seed: false,
          operator_prompt: "Check the queue behavior.",
          result_focus: null,
          evaluation_metric: null,
          files: [],
        },
      ],
    })
  );
});

test.describe("hydrated effort fixture", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/effort");
    // The table and launch button server-render before their handlers attach.
    // Recharts' client-rendered plot confirms the fixture has hydrated.
    await expect(page.getByRole("application")).toBeVisible({
      timeout: 30_000,
    });
  });

  test("effort columns separate five trials and retain the model typography", async ({
    page,
  }) => {
    for (const effort of ["low", "medium", "high", "xhigh"]) {
      await expect(
        page.getByText(`global.anthropic.claude-opus-5/${effort}`, {
          exact: true,
        })
      ).toBeVisible();
    }
    const modelLabel = page.getByText("global.anthropic.claude-opus-5/high", {
      exact: true,
    });
    const typography = await modelLabel.evaluate((element) => {
      const suffix = element.querySelector("span")!;
      const model = getComputedStyle(element),
        effort = getComputedStyle(suffix);
      return [
        model.fontSize === effort.fontSize,
        model.fontWeight === effort.fontWeight,
        model.color === effort.color,
      ];
    });
    expect(typography).toEqual([true, true, true]);
    const firstRow = page.getByRole("row").filter({ hasText: "repair-queue" });
    const cells = firstRow.locator("td");
    await expect(cells).toHaveCount(5);
    for (let i = 1; i < 5; i++)
      await expect(cells.nth(i).getByRole("button")).toHaveCount(5);
    await cells.nth(3).getByRole("button").first().click();
    await expect(page.getByLabel("Selected effort")).toHaveText(
      "high: 5 trials"
    );
  });

  test("run dialog counts efforts and submits independent retryable requests", async ({
    page,
  }) => {
    const submissions: {
      headers: Record<string, string>;
      body: {
        task_id: string;
        add_trials: boolean;
        configs: {
          model: string;
          n_trials: number;
          agent_config?: { kwargs: { reasoning_effort: string } };
        }[];
      };
    }[] = [];
    let fail = true;
    await page.route("**/api/tasks/sweep", async (route) => {
      const request = route.request();
      const body = request.postDataJSON();
      submissions.push({ headers: request.headers(), body });
      await route.fulfill({
        status: body.task_id === "repair-writes" && fail ? 503 : 200,
        json: { detail: "test interruption" },
      });
    });
    await page.getByRole("button", { name: "Run trials", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await dialog
      .getByRole("checkbox", { name: "Agent default", exact: true })
      .uncheck();
    await dialog.getByRole("checkbox", { name: "medium", exact: true }).check();
    await dialog.getByRole("checkbox", { name: "high", exact: true }).check();
    await dialog
      .getByRole("spinbutton", { name: "Trials per effort" })
      .fill("3");
    await dialog
      .getByRole("button", { name: "Run 18 trials", exact: true })
      .click();
    await expect(dialog.getByRole("alert")).toContainText(
      "1 task submissions failed"
    );
    expect(submissions).toHaveLength(3);
    for (const { body } of submissions) {
      expect(body.add_trials).toBe(true);
      expect(
        body.configs.map(
          (config) => config.agent_config?.kwargs.reasoning_effort
        )
      ).toEqual(["medium", "high"]);
      expect(
        body.configs.every(
          (config) =>
            config.model === "global.anthropic.claude-opus-5" &&
            config.n_trials === 3
        )
      ).toBe(true);
    }
    fail = false;
    await dialog
      .getByRole("button", { name: "Retry 1 tasks", exact: true })
      .click();
    await expect(dialog).not.toBeVisible();
    expect(submissions).toHaveLength(4);
    expect(submissions[3]).toEqual(
      submissions.find((item) => item.body.task_id === "repair-writes")
    );
  });

  for (const [agent, model, effort] of [
    ["codex", "openai/gpt-5.6", "max"],
    ["gemini-cli", "google/gemini-3.5-flash", "minimal"],
    ["antigravity-cli", "google/gemini-3.7-flash", "medium"],
    ["cursor-cli", "anthropic/claude-opus-5", "max"],
    ["grok-build", "xai/vendor-latest-learnability", "xhigh"],
    ["mini-swe-agent", "openai/gpt-5.6", "max"],
  ]) {
    test(`launch selector submits ${agent} with ${effort}`, async ({
      page,
    }) => {
      const configs: {
        model: string;
        agent_config: { kwargs: { reasoning_effort: string } };
      }[] = [];
      await page.route("**/api/tasks/sweep", async (route) => {
        configs.push(...route.request().postDataJSON().configs);
        await route.fulfill({ status: 200, json: {} });
      });
      await page
        .getByRole("button", { name: "Run trials", exact: true })
        .click();
      const dialog = page.getByRole("dialog");
      await dialog
        .getByRole("combobox", { name: "Agent", exact: true })
        .click();
      await page.getByRole("option", { name: agent, exact: true }).click();
      await dialog
        .getByRole("combobox", { name: "Model", exact: true })
        .fill(model);
      await dialog
        .getByRole("checkbox", { name: "Agent default", exact: true })
        .uncheck();
      await dialog.getByRole("checkbox", { name: effort, exact: true }).check();
      await dialog
        .getByRole("button", { name: "Run 15 trials", exact: true })
        .click();
      await expect(dialog).not.toBeVisible();
      expect(configs).toHaveLength(3);
      expect(
        configs.every(
          (config) =>
            config.model === model &&
            config.agent_config.kwargs.reasoning_effort === effort
        )
      ).toBe(true);
    });
  }

  test("changing Gemini model clears Flash-only effort and excludes Gemini 2.5", async ({
    page,
  }) => {
    await page.getByRole("button", { name: "Run trials", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("combobox", { name: "Agent", exact: true }).click();
    await page.getByRole("option", { name: "gemini-cli", exact: true }).click();
    const model = dialog.getByRole("combobox", { name: "Model", exact: true });
    await model.fill("google/gemini-3.5-flash");
    await dialog.getByRole("checkbox", { name: "medium", exact: true }).check();
    await model.fill("google/gemini-3.1-pro-preview");
    await expect(
      dialog.getByRole("checkbox", { name: "medium", exact: true })
    ).toHaveCount(0);
    await expect(
      dialog.getByRole("checkbox", { name: "high", exact: true })
    ).not.toBeChecked();
    await expect(
      dialog.getByRole("checkbox", { name: "Agent default", exact: true })
    ).toBeChecked();
    await model.fill("google/gemini-2.5-flash");
    await expect(dialog.getByRole("checkbox")).toHaveCount(1);
  });

  test("new runs leave effort unset without an effort interaction", async ({
    page,
  }) => {
    const configs: Record<string, unknown>[] = [];
    await page.route("**/api/tasks/sweep", async (route) => {
      configs.push(...route.request().postDataJSON().configs);
      await route.fulfill({ status: 200, json: {} });
    });
    await page.getByRole("button", { name: "Run trials", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await expect(
      dialog.getByRole("checkbox", { name: "high", exact: true })
    ).not.toBeChecked();
    await expect(
      dialog.getByRole("checkbox", { name: "Agent default", exact: true })
    ).toBeChecked();
    await dialog
      .getByRole("button", { name: "Run 15 trials", exact: true })
      .click();
    await expect(dialog).not.toBeVisible();
    expect(configs).toHaveLength(3);
    for (const config of configs)
      expect(config).not.toHaveProperty("agent_config");
  });

  for (const effort of ["default", "high"]) {
    test(`probe submits ${effort} effort`, async ({ page }) => {
      const requests: {
        agent_config?: { kwargs: { reasoning_effort: string } };
      }[] = [];
      await page.route("**/api/tasks/sweep", async (route) => {
        requests.push(...route.request().postDataJSON().configs);
        await route.fulfill({ status: 200, json: {} });
      });
      const form = page.getByRole("region", { name: "Probe launch" });
      await form.getByRole("combobox", { name: "Skill", exact: true }).click();
      await page
        .getByRole("option", { name: "Queue check", exact: true })
        .click();
      const selector = form.getByRole("combobox", { name: "Reasoning effort" });
      await expect(selector).toHaveText("Agent default");
      if (effort === "high") {
        await selector.click();
        await page.getByRole("option", { name: "high", exact: true }).click();
      }
      await form
        .getByRole("textbox", { name: "Instructions" })
        .fill("Check the queue behavior.");
      await form.getByRole("button", { name: "Submit probe run" }).click();
      await expect.poll(() => requests.length).toBe(1);
      if (effort === "default")
        expect(requests[0]).not.toHaveProperty("agent_config");
      else
        expect(requests[0].agent_config?.kwargs.reasoning_effort).toBe("high");
    });
  }

  test("grouping efforts combines cells and charts, survives reload, and restores columns", async ({
    page,
  }) => {
    const toggle = page.getByRole("checkbox", { name: "Group effort levels" });
    const firstRow = page.getByRole("row").filter({ hasText: "repair-queue" });
    await expect(toggle).not.toBeChecked();
    await toggle.check();
    await expect(page).toHaveURL(/groupEfforts=1/);
    await expect(firstRow.locator("td")).toHaveCount(2);
    await expect(firstRow.locator("td").nth(1).getByRole("button")).toHaveCount(
      20
    );
    await expect(
      page.getByText("n = 20 · 3 tasks · 1 configurations")
    ).toBeVisible();
    await expect(
      leaderboard(page).getByText("65.0%", { exact: true })
    ).toBeVisible();
    await firstRow.locator("td").nth(1).getByRole("button").nth(10).click();
    await expect(page.getByLabel("Selected effort")).toHaveText(
      "high: 20 trials"
    );
    await page.reload();
    await expect(toggle).toBeChecked();
    await expect(firstRow.locator("td")).toHaveCount(2);
    await toggle.uncheck();
    await expect(page).not.toHaveURL(/groupEfforts=1/);
    await expect(firstRow.locator("td")).toHaveCount(5);
    await page.goBack();
    await expect(toggle).toBeChecked();
    await expect(firstRow.locator("td")).toHaveCount(2);
  });

  test("mixed unspecified and high trials share totals and row filtering", async ({
    page,
  }) => {
    await page.goto("/effort?sample=mixed");
    await expect(page.getByRole("application")).toBeVisible({
      timeout: 30_000,
    });
    const row = page.getByRole("row").filter({ hasText: "repair-queue" });
    const filter = page.getByRole("group", { name: "Row filter" });
    await filter
      .getByRole("button", { name: "Any failed", exact: true })
      .click();
    await expect(row).toBeVisible();
    await page.getByRole("checkbox", { name: "Group effort levels" }).check();
    await expect(row).toHaveCount(0);
    await filter.getByRole("button", { name: "All", exact: true }).click();
    await expect(row.locator("td")).toHaveCount(2);
    await expect(row.locator("td").nth(1).getByRole("button")).toHaveCount(5);
    await expect(
      leaderboard(page).getByText("20.0%", { exact: true })
    ).toBeVisible();
    await row.locator("td").nth(1).getByRole("button").nth(2).click();
    await expect(page.getByLabel("Selected effort")).toHaveText(
      "high: 5 trials"
    );
    await expect(page).toHaveURL(/sample=mixed/);
  });

  test("experiment view refreshes open drawer groups when the URL grouping changes", async ({
    page,
  }) => {
    await page.route("**/api/**", (route) => route.fulfill({ json: {} }));
    await page.goto(
      "/effort?sample=mixed&detail=1&task=repair-queue&trial=repair-queue-low-2"
    );
    // The development server compiles the lazy-loaded trial panel on first open.
    await expect(
      page.getByRole("button", { name: "Next trial", exact: true })
    ).toBeVisible({ timeout: 30_000 });
    const trials = page.getByRole("button", { name: /^Trial \d+ / });
    await expect(trials).toHaveCount(8); // Five in the table, three high-effort in the drawer.
    await page.evaluate(() => {
      const url = new URL(window.location.href);
      url.searchParams.set("groupEfforts", "1");
      window.history.pushState(null, "", url);
    });
    await expect(trials).toHaveCount(10); // Five in each view.
    await expect(page).toHaveURL(/trial=repair-queue-low-2/);
    await page.goBack();
    await expect(trials).toHaveCount(8);
  });
});

test("public experiments always group efforts and hide the grouping control", async ({
  page,
}) => {
  await page.route("**/api/**", (route) => route.fulfill({ json: {} }));
  await page.goto("/effort?sample=mixed&detail=1&public=1&groupEfforts=0");
  const row = page.getByRole("row").filter({ hasText: "repair-queue" });
  await expect(row.locator("td")).toHaveCount(2);
  await expect(row.locator("td").nth(1).getByRole("button")).toHaveCount(5);
  await expect(
    page.getByRole("checkbox", { name: "Group effort levels" })
  ).toHaveCount(0);
  await expect(
    leaderboard(page).getByText("20.0%", { exact: true })
  ).toBeVisible();
  await page.goto("/effort?sample=mixed&detail=1&public=1");
  await expect(row.locator("td")).toHaveCount(2);
  await expect(
    page.getByRole("checkbox", { name: "Group effort levels" })
  ).toHaveCount(0);
});
