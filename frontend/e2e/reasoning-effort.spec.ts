import { expect, test } from "@playwright/test";

test("effort columns separate five trials and retain the model typography", async ({
  page,
}) => {
  await page.goto("/effort");
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
  await expect(page.getByLabel("Selected effort")).toHaveText("high: 5 trials");
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
  await page.goto("/effort");
  await page.getByRole("button", { name: "Run trials", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("checkbox", { name: "Agent default", exact: true })
    .uncheck();
  await dialog.getByRole("checkbox", { name: "medium", exact: true }).check();
  await dialog.getByRole("checkbox", { name: "high", exact: true }).check();
  await dialog.getByRole("spinbutton", { name: "Trials per effort" }).fill("3");
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
      body.configs.map((config) => config.agent_config?.kwargs.reasoning_effort)
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
