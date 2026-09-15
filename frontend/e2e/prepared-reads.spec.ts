import { expect, test } from "@playwright/test";

const completedCounts = {
  task_count: 1,
  total_trials: 1,
  completed_trials: 1,
  failed_trials: 0,
  skipped_trials: 0,
  retrying_trials: 0,
  active_trials: 0,
  avg_score: 1,
  author: null,
  last_runner: null,
  last_author: null,
  user_tags: [],
  summary_pending: false,
};

test("Org → Mine → Org reuses the completed list without a blocking request", async ({
  page,
}) => {
  const calls: string[] = [];
  await page.route("**/api/dashboard?**", async (route) => {
    const params = new URL(route.request().url()).searchParams;
    const author = params.get("experiments_author") || "all";
    if (params.get("include_experiments") === "true") calls.push(author);
    await route.fulfill({
      json: {
        experiments: [
          {
            id: author,
            name:
              author === "all" ? "Organization experiment" : "My experiment",
            ...completedCounts,
          },
        ],
        experiments_has_more: false,
      },
    });
  });
  await page.goto("/prepared-dashboard");
  await expect(
    page.getByRole("link", { name: "My experiment", exact: true })
  ).toBeVisible();
  await page.getByRole("button", { name: "Org", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Organization experiment", exact: true })
  ).toBeVisible();
  await page.getByRole("button", { name: "Mine", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "My experiment", exact: true })
  ).toBeVisible();
  await page.getByRole("button", { name: "Org", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Organization experiment", exact: true })
  ).toBeVisible();
  expect(calls).toEqual(["me", "all"]);
  const search = page.getByPlaceholder("Search anything...");
  await search.fill("alpha");
  await expect
    .poll(() => new URL(page.url()).searchParams.get("q"))
    .toBe("alpha");
  await page.getByRole("button", { name: "Mine", exact: true }).click();
  await search.fill("");
  await expect.poll(() => new URL(page.url()).searchParams.get("q")).toBeNull();
  await page.goBack();
  await expect(search).toHaveValue("alpha");
});

for (const state of ["summary_pending", "active_trials"] as const) {
  test(`dashboard polls ${state} after five seconds and slows once complete`, async ({
    page,
  }) => {
    await page.clock.install();
    let calls = 0;
    await page.route("**/api/dashboard?**", async (route) => {
      if (
        new URL(route.request().url()).searchParams.get(
          "include_experiments"
        ) === "true"
      )
        calls += 1;
      await route.fulfill({
        json: {
          experiments: [
            {
              ...completedCounts,
              id: "polling",
              name: calls === 1 ? "Waiting experiment" : "Completed experiment",
              ...(calls === 1
                ? { [state]: state === "summary_pending" ? true : 1 }
                : {}),
            },
          ],
          experiments_has_more: false,
        },
      });
    });
    await page.goto("/prepared-dashboard");
    await expect(
      page.getByRole("link", { name: "Waiting experiment", exact: true })
    ).toBeVisible();
    await page.clock.runFor(5_100);
    await expect(
      page.getByRole("link", { name: "Completed experiment", exact: true })
    ).toBeVisible();
    expect(calls).toBe(2);
    await page.clock.runFor(10_000);
    expect(calls).toBe(2);
    await page.clock.runFor(20_100);
    await expect.poll(() => calls).toBe(3);
  });
}

test("deleting an experiment invalidates unmounted Org and Mine lists", async ({
  page,
}) => {
  const calls: string[] = [];
  let deleted = false;
  await page.route("**/api/dashboard?**", async (route) => {
    const author =
      new URL(route.request().url()).searchParams.get("experiments_author") ||
      "all";
    if (
      new URL(route.request().url()).searchParams.get("include_experiments") ===
      "true"
    )
      calls.push(author);
    await route.fulfill({
      json: {
        experiments: deleted
          ? []
          : [
              {
                ...completedCounts,
                id: "delete-me",
                name: "Shared experiment",
              },
            ],
        experiments_has_more: false,
      },
    });
  });
  await page.route("**/api/experiments/delete-me", async (route) => {
    expect(route.request().method()).toBe("DELETE");
    deleted = true;
    await route.fulfill({ json: { success: true } });
  });
  await page.goto("/prepared-dashboard");
  const row = page.getByRole("link", {
    name: "Shared experiment",
    exact: true,
  });
  await expect(row).toBeVisible();
  await page.getByRole("button", { name: "Org", exact: true }).click();
  await expect.poll(() => calls).toEqual(["me", "all"]);
  await expect(row).toBeVisible();
  await page
    .getByRole("button", { name: "Delete Shared experiment", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Delete experiment", exact: true })
    .click();
  await expect.poll(() => calls).toEqual(["me", "all", "all"]);
  await expect(row).toHaveCount(0);
  await page.getByRole("button", { name: "Mine", exact: true }).click();
  await expect.poll(() => calls).toEqual(["me", "all", "all", "me"]);
  await expect(row).toHaveCount(0);
  await page.goBack();
  await expect(
    page.getByRole("button", { name: "Mine", exact: true })
  ).toBeVisible();
  await expect(row).toHaveCount(0);
  expect(calls).toEqual(["me", "all", "all", "me"]);
});
