import { expect, test } from "@playwright/test";
import { openFor, tasks } from "./review-app/records";

test.skip(
  process.env.E2E_REVIEW_FIXTURES !== "1",
  "Requires the isolated review fixture app"
);

const fixtureTask = tasks[0];
const fixture = openFor(fixtureTask, fixtureTask.current_version!);

test("flat task summary stays scoped, aligned and readable at narrow widths", async ({
  page,
}) => {
  const summary = fixture.selected_version!;
  const agent = summary.agent_models[0];
  await page.route("**/api/tasks/*/open*", (route) =>
    route.fulfill({
      json: {
        ...fixture,
        selected_version: {
          ...summary,
          trial_count: 304,
          completed_count: 86,
          failed_count: 218,
          reward_total: 86,
          reward_sum: 41,
          pass_count: 41,
          fail_count: 45,
          agent_models: [
            {
              ...agent,
              agent: "codex",
              model: "openai/long-model-name-for-layout-checking",
              trial_count: 151,
            },
            { ...agent, agent: "oracle", trial_count: 153 },
          ],
        },
        trials: [],
        trials_has_more: true,
      },
    })
  );
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`/tasks/${fixtureTask.id}`);
  const stats = page.getByLabel("Selected version summary");
  await expect(stats.getByText("304", { exact: true })).toBeVisible();
  await expect(stats.getByText("47.7%", { exact: true })).toBeVisible();
  await expect(stats.getByText("218", { exact: true })).toBeVisible();
  const metricTops = await stats
    .locator(":scope > div > span:last-of-type")
    .evaluateAll((nodes) =>
      nodes.map((node) => Math.round(node.getBoundingClientRect().top))
    );
  expect(new Set(metricTops).size).toBe(1);
  const agentColumns = await page
    .locator("section")
    .filter({ has: page.getByRole("heading", { level: 3 }) })
    .evaluateAll((sections) =>
      sections.map((section) =>
        Math.round(
          section.firstElementChild!.children[1].getBoundingClientRect().right
        )
      )
    );
  expect(agentColumns).toHaveLength(2);
  expect(new Set(agentColumns).size).toBe(1);
  await expect(page.getByText("Billed spend", { exact: true })).toBeHidden();
  await page.getByText("Cost details", { exact: true }).click();
  await expect(page.getByText("Billed spend", { exact: true })).toBeVisible();
  await page.getByText("Cost details", { exact: true }).click();
  for (const width of [1440, 1024, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await expect(stats).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth
      )
    ).toBe(true);
  }
});

test("all trials load only on request, preserve version scope, and retry after failure", async ({
  page,
}) => {
  await page.route("**/api/tasks/*/open*", (route) =>
    route.fulfill({ json: { ...fixture, trials: [], trials_has_more: true } })
  );
  let reads = 0;
  await page.route("**/api/tasks/*/detail", (route) => {
    reads++;
    return reads === 1
      ? route.fulfill({ status: 500, json: { detail: "Unavailable" } })
      : route.fulfill({
          json: {
            task: {
              ...fixtureTask,
              trials: [
                {
                  ...fixtureTask.trials![0],
                  name: "selected-version-trial",
                  task_version_id: fixture.selected_version!.id,
                },
                {
                  ...fixtureTask.trials![0],
                  id: "other-version",
                  name: "other-version-trial",
                  task_version_id: "other-version",
                },
              ],
            },
            versions: [],
            totals: fixture.totals,
          },
        });
  });
  await page.goto(`/tasks/${fixtureTask.id}`);
  const load = page.getByRole("button", {
    name: "View all trials",
    exact: true,
  });
  await expect(load).toBeVisible();
  expect(reads).toBe(0);
  await load.click();
  await expect(page.getByText("Could not load all trials.")).toBeVisible();
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(
    page.getByRole("button", { name: /selected-version-trial/ })
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /other-version-trial/ })
  ).toHaveCount(0);
  await expect(load).toHaveCount(0);
});
