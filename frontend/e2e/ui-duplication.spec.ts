import { expect, test } from "@playwright/test";
import { openFor, tasks } from "./review-app/records";

test("single group statistics only collapse when their trial scope matches", async ({
  page,
}) => {
  for (const scenario of [
    "single",
    "two-models",
    "different-scope",
    "historical",
  ] as const) {
    await page.route("**/api/tasks/task-a/open**", (route) => {
      const open = openFor(tasks[0], scenario === "historical" ? 6 : 7);
      const version = open.selected_version!;
      if (scenario === "two-models") {
        version.agent_models.push({
          ...version.agent_models[0],
          model: "openai/another-model",
        });
        version.trial_count = 2;
      }
      if (scenario === "different-scope") version.trial_count = 2;
      return route.fulfill({ json: open });
    });
    await page.goto(
      `/tasks/task-a${scenario === "historical" ? "?version=6" : ""}`
    );
    await expect(
      page.getByRole("heading", { name: "Agents", exact: true })
    ).toBeVisible();
    await expect(page.getByText(/^avg score$/i)).toHaveCount(
      scenario === "two-models" ? 3 : scenario === "different-scope" ? 2 : 1
    );
    await expect(
      page.getByText("Total cost (all versions)", { exact: true })
    ).toBeVisible();
    await expect(page.getByText("Billed spend", { exact: true })).toBeVisible();
    await expect(page.getByText(/0\/1 pass/)).toHaveCount(0);
    await page.unroute("**/api/tasks/task-a/open**");
  }
});

test("drawer keeps one verdict count and generation control, restoring the page control on close", async ({
  page,
}) => {
  await page.goto("/tasks/task-a?version=7&drawer=task&taskPane=overview");
  await expect(
    page.getByRole("heading", { name: "Findings", exact: true })
  ).toBeVisible();
  await expect(page.getByText("1 Must fix", { exact: true })).toHaveCount(1);
  await expect(
    page.getByRole("button", { name: /Generate QA verdict for v7/i })
  ).toHaveCount(1);
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await expect(
    page.getByRole("button", {
      name: "Generate QA verdict for v7",
      exact: true,
    })
  ).toBeVisible();
});

test("usage, action counts and experiment groups retain their meaning at desktop and narrow widths", async ({
  page,
}) => {
  await page.goto("/duplication");
  await expect(page.getByText("Input: 1.2M,", { exact: true })).toBeVisible();
  await expect(page.getByText("Output: 300.0K", { exact: true })).toBeVisible();
  await expect(
    page.getByText("(Total: 1.5M tokens)", { exact: true })
  ).toBeVisible();
  await expect(
    page.getByText(/Statuses include trial and task-QA/)
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", {
      name: "ACTION ITEMS: 2 MUST FIX TASK DEFECTS",
      exact: true,
    })
  ).toBeVisible();
  await page.getByLabel("Defect count").selectOption("1");
  await expect(
    page.getByRole("heading", {
      name: "ACTION ITEMS: 1 MUST FIX TASK DEFECT",
      exact: true,
    })
  ).toBeVisible();
  await page.getByLabel("Defect count").selectOption("0");
  await expect(
    page.getByRole("heading", { name: /ACTION ITEMS:/ })
  ).toHaveCount(0);
  const sourceA = page
    .getByRole("heading", { name: "Dependency baseline", exact: true })
    .locator("..");
  const sourceB = page
    .getByRole("heading", { name: "Verifier comparison", exact: true })
    .locator("..");
  await expect(
    sourceA.getByRole("button", { name: "View trial", exact: true })
  ).toHaveCount(2);
  await expect(
    sourceB.getByRole("button", { name: "View trial", exact: true })
  ).toHaveCount(1);
  await expect(sourceA.getByRole("link")).toHaveAttribute(
    "href",
    "/experiments/source-a"
  );
  const runs = sourceA.locator("details");
  await runs.nth(0).locator("summary").click();
  await expect(runs.nth(0).getByText("CAUSE", { exact: true })).toBeVisible();
  await expect(runs.nth(0).getByText("EVIDENCE", { exact: true })).toHaveCount(
    0
  );
  await runs.nth(1).locator("summary").click();
  await expect(
    runs.nth(1).getByText("EVIDENCE", { exact: true })
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByText("Output: 300.0K", { exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth
    )
  ).toBe(true);
});

for (const id of ["unreviewed", "review-failed"]) {
  test(`verdict action stays with the ${id} result and explanation`, async ({
    page,
  }) => {
    await page.goto(`/tasks/${id}?drawer=task&taskPane=overview`);
    const action = page.getByRole("button", {
      name: /Generate QA verdict for v/i,
    });
    await expect(action).toHaveCount(1);
    const card = action.locator("..");
    await expect(card).toContainText(
      id === "unreviewed" ? "No QA verdict" : "QA verdict failed"
    );
    if (id === "review-failed")
      await expect(card).toContainText("Evidence could not be read.");
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(action).toBeVisible();
  });
}
