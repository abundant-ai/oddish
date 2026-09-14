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

test("Files and Artifacts share one bounded preview across unmounts", async ({
  page,
}) => {
  const listings: URL[] = [];
  const bodies: URL[] = [];
  const file = {
    path: "artifacts/readme.txt",
    key: "attempt-1/artifacts/readme.txt",
    size: 200000,
  };
  await page.route("**/api/trials/prepared-1/files?**", async (route) => {
    const url = new URL(route.request().url());
    listings.push(url);
    expect(url.searchParams.get("indexed")).toBe("true");
    expect(url.searchParams.get("previews")).toBeNull();
    expect(url.searchParams.get("limit")).toBe("100");
    if (url.searchParams.get("artifacts") === "true") {
      await route.fulfill({
        json: { files: [file], source_hash: "fixture-revision", cursor: null },
      });
    } else {
      await route.fulfill({
        json: {
          source_hash: "fixture-revision",
          directories: {
            "": { files: [], dirs: [{ path: "artifacts" }], cursor: null },
            artifacts: { files: [file], dirs: [], cursor: null },
            solution: { files: [], dirs: [] },
            tests: { files: [], dirs: [] },
            environment: { files: [], dirs: [] },
          },
        },
      });
    }
  });
  await page.route("**/api/trials/prepared-1/files/**", async (route) => {
    const url = new URL(route.request().url());
    bodies.push(url);
    expect(url.searchParams.get("max_bytes")).toBe("102400");
    expect(url.searchParams.get("attempt")).toBe("1");
    await route.fulfill({
      contentType: "text/plain",
      body: "Shared prepared preview",
    });
  });
  await page.goto("/prepared-files");
  await expect(
    page.getByText("Shared prepared preview", { exact: true })
  ).toBeVisible();
  const initialBodies = bodies.length;
  await page.getByRole("button", { name: "Show artifacts" }).click();
  await expect(
    page.getByText("Shared prepared preview", { exact: true })
  ).toBeVisible();
  await page.getByRole("button", { name: "Show files" }).click();
  await expect(
    page.getByText("Shared prepared preview", { exact: true })
  ).toBeVisible();
  expect(bodies.length).toBe(initialBodies);
  expect(listings).toHaveLength(2);
});
