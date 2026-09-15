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

test("Files and Artifacts refresh inventories and share one bounded preview across unmounts", async ({
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
  expect(initialBodies).toBe(1);
  await page.getByRole("button", { name: "Show artifacts" }).click();
  await expect(
    page.getByText("Shared prepared preview", { exact: true })
  ).toBeVisible();
  await page.getByRole("button", { name: "Show files" }).click();
  await expect(
    page.getByText("Shared prepared preview", { exact: true })
  ).toBeVisible();
  expect(bodies.length).toBe(initialBodies);
  await expect.poll(() => listings.length).toBe(3);
});

test("nested trial binary URLs preserve separators and escape filename characters", async ({
  page,
}) => {
  const path = "artifacts/charts/chart #1.png";
  const expectedPath =
    "/api/trials/prepared-1/files/artifacts/charts/chart%20%231.png";
  await page.route("**/api/trials/prepared-1/files?**", (route) =>
    route.fulfill({
      json: {
        source_hash: "fixture-revision",
        directories: { "": { files: [{ path, size: 68 }], dirs: [] } },
      },
    })
  );
  const requests: URL[] = [];
  await page.route("**/api/trials/prepared-1/files/**", async (route) => {
    const url = new URL(route.request().url());
    requests.push(url);
    expect(url.pathname).toBe(expectedPath);
    expect(url.searchParams.get("indexed")).toBe("true");
    expect(url.searchParams.get("attempt")).toBe("1");
    await route.fulfill({
      contentType: "image/png",
      body: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7XcAAAAASUVORK5CYII=",
        "base64"
      ),
    });
  });
  await page.goto(`/prepared-files?file=${encodeURIComponent(path)}`);
  const image = page.getByRole("img", { name: "chart #1.png", exact: true });
  await expect(image).toBeVisible();
  await expect
    .poll(() =>
      image.evaluate((element: HTMLImageElement) => element.naturalWidth)
    )
    .toBe(1);
  expect(requests.length).toBeGreaterThan(0);
});

test("nested trial full-file loads use the same encoded path as previews", async ({
  page,
}) => {
  const path = "artifacts/nested/run notes.txt";
  const requests: URL[] = [];
  await page.route("**/api/trials/prepared-1/files?**", (route) =>
    route.fulfill({
      json: {
        source_hash: "fixture-revision",
        directories: { "": { files: [{ path, size: 200000 }], dirs: [] } },
      },
    })
  );
  await page.route("**/api/trials/prepared-1/files/**", async (route) => {
    const url = new URL(route.request().url());
    requests.push(url);
    expect(url.pathname).toBe(
      "/api/trials/prepared-1/files/artifacts/nested/run%20notes.txt"
    );
    expect(url.searchParams.get("attempt")).toBe("1");
    await route.fulfill({
      contentType: "text/plain",
      body: url.searchParams.has("max_bytes")
        ? "preview\n".repeat(12800)
        : "Complete nested trial file",
    });
  });
  await page.goto(`/prepared-files?file=${encodeURIComponent(path)}`);
  await page
    .getByRole("button", { name: "Load full file", exact: true })
    .click();
  await expect(
    page.getByText("Complete nested trial file", { exact: true })
  ).toBeVisible();
  expect(
    requests.filter((url) => !url.searchParams.has("max_bytes"))
  ).toHaveLength(1);
});

function trialListing(
  files: Array<{ path: string; size: number }>,
  revision: string,
  artifacts: boolean
) {
  return artifacts
    ? { files, source_hash: revision, cursor: null }
    : {
        source_hash: revision,
        directories: {
          "": { files: [], dirs: [{ path: "artifacts" }] },
          artifacts: { files, dirs: [], cursor: null },
        },
      };
}

for (const retain of [false, true]) {
  test(`reopening artifacts discovers files after an empty inventory (retain=${retain})`, async ({
    page,
  }) => {
    let published = false;
    let artifactListings = 0;
    await page.route("**/api/trials/prepared-1/files?**", (route) => {
      const artifacts =
        new URL(route.request().url()).searchParams.get("artifacts") === "true";
      if (artifacts) artifactListings++;
      return route.fulfill({
        json: trialListing(
          published ? [{ path: "artifacts/readme.txt", size: 18 }] : [],
          published ? "complete" : "early",
          artifacts
        ),
      });
    });
    await page.route("**/api/trials/prepared-1/files/**", (route) =>
      route.fulfill({ contentType: "text/plain", body: "Published artifact" })
    );
    await page.goto(`/prepared-files?tab=artifacts&retain=${retain}`);
    await expect(page.getByText("No artifacts", { exact: true })).toBeVisible();
    published = true;
    await page.getByRole("button", { name: "Show files" }).click();
    await page.getByRole("button", { name: "Show artifacts" }).click();
    await expect(
      page
        .getByText("Published artifact", { exact: true })
        .filter({ visible: true })
    ).toBeVisible();
    expect(artifactListings).toBe(2);
  });
}

for (const tab of ["files", "artifacts"]) {
  test(`${tab} refreshes its inventory and retries a conflicting preview`, async ({
    page,
  }) => {
    let revision = "first";
    let listings = 0;
    const reads: string[] = [];
    const files = [
      { path: "artifacts/readme.txt", size: 4 },
      { path: "artifacts/other.txt", size: 4 },
    ];
    await page.route("**/api/trials/prepared-1/files?**", (route) => {
      listings++;
      return route.fulfill({
        json: trialListing(
          files,
          revision,
          new URL(route.request().url()).searchParams.has("artifacts")
        ),
      });
    });
    await page.route("**/api/trials/prepared-1/files/**", (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith("other.txt"))
        reads.push(url.searchParams.get("revision")!);
      return url.searchParams.get("revision") !== revision
        ? route.fulfill({
            status: 409,
            json: { detail: "File directory changed; reload its contents" },
          })
        : route.fulfill({
            contentType: "text/plain",
            body: url.pathname.endsWith("other.txt")
              ? "Current other contents"
              : "Initial contents",
          });
    });
    await page.goto(`/prepared-files?tab=${tab}&retain=true`);
    await expect(
      page.getByText("Initial contents", { exact: true })
    ).toBeVisible();
    revision = "replacement";
    await page
      .getByRole(tab === "artifacts" ? "treeitem" : "button", {
        name: tab === "artifacts" ? "other.txt" : /other.txt/,
      })
      .click();
    await expect(
      page.getByText("Current other contents", { exact: true })
    ).toBeVisible();
    expect(reads).toEqual(["first", "replacement"]);
    expect(listings).toBe(2);
  });

  test(`${tab} drops loaded full contents when the inventory revision changes`, async ({
    page,
  }) => {
    let revision = "first";
    const files = [{ path: "artifacts/readme.txt", size: 200000 }];
    const reads: URL[] = [];
    await page.route("**/api/trials/prepared-1/files?**", (route) =>
      route.fulfill({
        json: trialListing(
          files,
          revision,
          new URL(route.request().url()).searchParams.has("artifacts")
        ),
      })
    );
    await page.route("**/api/trials/prepared-1/files/**", (route) => {
      const url = new URL(route.request().url());
      reads.push(url);
      expect(url.searchParams.get("revision")).toBe(revision);
      return route.fulfill({
        contentType: "text/plain",
        body:
          revision === "replacement"
            ? "Current preview contents"
            : url.searchParams.has("max_bytes")
              ? "x".repeat(102400)
              : "Initial full contents",
      });
    });
    await page.goto(`/prepared-files?tab=${tab}&retain=true`);
    await page
      .getByRole("button", { name: "Load full file", exact: true })
      .click();
    await expect(
      page.getByText("Initial full contents", { exact: true })
    ).toBeVisible();
    revision = "replacement";
    await page
      .getByRole("button", {
        name: tab === "files" ? "Show artifacts" : "Show files",
      })
      .click();
    await expect(
      page
        .getByText("Current preview contents", { exact: true })
        .filter({ visible: true })
    ).toBeVisible();
    await page.getByRole("button", { name: `Show ${tab}` }).click();
    await expect(
      page
        .getByText("Current preview contents", { exact: true })
        .filter({ visible: true })
    ).toBeVisible();
    await expect(
      page.getByText("Initial full contents", { exact: true })
    ).not.toBeVisible();
    expect(
      reads.filter((url) => url.searchParams.get("revision") === "replacement")
    ).toHaveLength(1);
  });
}

test("a cold trial deep link waits for its revision and downloads one preview", async ({
  page,
}) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let listings = 0;
  const bodies: URL[] = [];
  await page.route("**/api/trials/prepared-1/files?**", async (route) => {
    listings++;
    await gate;
    return route.fulfill({
      json: trialListing(
        [{ path: "artifacts/readme.txt", size: 12 }],
        "settled-index",
        false
      ),
    });
  });
  await page.route("**/api/trials/prepared-1/files/**", (route) => {
    bodies.push(new URL(route.request().url()));
    return route.fulfill({
      contentType: "text/plain",
      body: "Single preview body",
    });
  });
  try {
    await page.goto("/prepared-files");
    await expect.poll(() => listings).toBe(1);
    expect(bodies).toHaveLength(0);
    release();
    await expect(
      page.getByText("Single preview body", { exact: true })
    ).toBeVisible();
    expect(bodies).toHaveLength(1);
    expect(bodies[0].searchParams.get("revision")).toBe("settled-index");
  } finally {
    release();
  }
});

test("an artifact pagination conflict discards all pages from the old revision", async ({
  page,
}) => {
  let replaced = false;
  const cursors: Array<string | null> = [];
  await page.route("**/api/trials/prepared-1/files?**", (route) => {
    const params = new URL(route.request().url()).searchParams;
    const cursor = params.get("cursor");
    cursors.push(cursor);
    if (cursor) expect(params.get("revision")).toBe("first");
    if (replaced && cursor)
      return route.fulfill({
        status: 409,
        json: { detail: "File directory changed; reload its contents" },
      });
    const name = replaced
      ? "current.txt"
      : cursor
        ? "old-page.txt"
        : "readme.txt";
    return route.fulfill({
      json: {
        source_hash: replaced ? "replacement" : "first",
        files: [{ path: `artifacts/${name}`, size: 5 }],
        cursor: replaced ? null : cursor ? "page-3" : "page-2",
      },
    });
  });
  await page.route("**/api/trials/prepared-1/files/**", (route) =>
    route.fulfill({ contentType: "text/plain", body: "File body" })
  );
  await page.goto("/prepared-files?tab=artifacts");
  await page.getByRole("button", { name: "Load more artifacts" }).click();
  await expect(
    page.getByRole("treeitem", { name: "old-page.txt", exact: true })
  ).toBeVisible();
  replaced = true;
  await page.getByRole("button", { name: "Load more artifacts" }).click();
  await expect(
    page.getByRole("treeitem", { name: "current.txt", exact: true })
  ).toBeVisible();
  await expect(
    page.getByRole("treeitem", { name: "old-page.txt", exact: true })
  ).toHaveCount(0);
  expect(cursors).toEqual([null, "page-2", "page-3", null]);
});
