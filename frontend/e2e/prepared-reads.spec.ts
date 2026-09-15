import { expect, test } from "@playwright/test";

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
