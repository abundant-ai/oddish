import { expect, test } from "@playwright/test";

const finding =
  "/tasks/task-a?version=7&drawer=task&finding=empty-answer&taskPane=file&taskFile=tests%2Ftest.sh&taskLines=L7";
const fileList = "**/api/tasks/task-a/files?**";

function batch(version = 7) {
  return {
    version,
    source_hash: `fixture-v${version}`,
    directories: {
      "": { files: [], dirs: [{ path: "tests" }], cursor: null },
      tests: {
        files: [{ path: `tests/v${version}.sh`, key: "test", size: 7 }],
        dirs: [],
        cursor: "page-2",
      },
      solution: { files: [], dirs: [], cursor: null },
      environment: { files: [], dirs: [], cursor: null },
    },
  };
}

test("URL-selected file paints before the directory batch and retains its line address", async ({
  page,
}) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const requests: URL[] = [];
  await page.route(fileList, async (route) => {
    const url = new URL(route.request().url());
    requests.push(url);
    await gate;
    await route.continue();
  });
  try {
    await page.goto(finding);
    await expect(page.getByText("exit 0", { exact: true })).toBeVisible();
    await expect.poll(() => requests.length).toBe(1);
    expect(requests[0].searchParams.getAll("directories")).toEqual([
      "",
      "solution",
      "tests",
      "environment",
    ]);
    expect(requests[0].searchParams.get("version")).toBe("7");
    expect(new URL(page.url()).searchParams.get("taskLines")).toBe("L7");
  } finally {
    release();
  }
  await expect(page.getByRole("button", { name: /test.sh/ })).toBeVisible();
  expect(new URL(page.url()).searchParams.get("taskFile")).toBe(
    "tests/test.sh"
  );
  expect(new URL(page.url()).searchParams.get("taskLines")).toBe("L7");
});

test("closing and reopening a task reuses its directory batch", async ({
  page,
}) => {
  let batches = 0;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (
      url.pathname === "/api/tasks/task-a/files" &&
      url.searchParams.has("directories")
    )
      batches++;
  });
  await page.goto(finding);
  await expect(page.getByRole("button", { name: /test.sh/ })).toBeVisible();
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await page
    .getByRole("button", { name: "View task files", exact: true })
    .click();
  await expect(page.getByRole("button", { name: /test.sh/ })).toBeVisible();
  expect(batches).toBe(1);
});

for (const intent of ["hover", "focus"] as const) {
  test(`${intent} prefetch is consumed by the experiment drawer without changing its URL`, async ({
    page,
  }) => {
    let batches = 0;
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (
        url.pathname === "/api/tasks/task-a/files" &&
        url.searchParams.has("directories")
      )
        batches++;
    });
    await page.goto("/experiments/review-demo");
    const task = page.getByRole("button", { name: "Task A", exact: true });
    await task[intent]();
    await expect.poll(() => batches).toBe(1);
    expect(new URL(page.url()).searchParams.has("task")).toBe(false);
    await task.click();
    await expect(page.getByRole("button", { name: /test.sh/ })).toBeVisible();
    expect(batches).toBe(1);
  });
}

test("a file outside the first directory page remains directly addressable", async ({
  page,
}) => {
  await page.route(fileList, (route) => route.fulfill({ json: batch() }));
  await page.goto(finding);
  await expect(page.getByText("exit 0", { exact: true })).toBeVisible();
  expect(new URL(page.url()).searchParams.get("taskLines")).toBe("L7");
  expect(new URL(page.url()).searchParams.get("taskFile")).toBe(
    "tests/test.sh"
  );
});

test("a delayed previous-version page cannot append to the next version", async ({
  page,
}) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let pageStarted = false;
  await page.route(fileList, async (route) => {
    const params = new URL(route.request().url()).searchParams;
    const version = Number(params.get("version"));
    if (params.has("cursor")) {
      pageStarted = true;
      await gate;
      return route.fulfill({
        json: {
          source_hash: "fixture-v7",
          files: [{ path: "tests/old-page.sh", key: "old" }],
          cursor: null,
        },
      });
    }
    return route.fulfill({ json: batch(version) });
  });
  await page.goto("/file-cache");
  await expect(page.getByTestId("tree")).toContainText("tests/v7.sh");
  await page.getByRole("button", { name: "More tests" }).click();
  await expect.poll(() => pageStarted).toBe(true);
  await page.getByRole("button", { name: "Version 6" }).click();
  await expect(page.getByTestId("tree")).toContainText("tests/v6.sh");
  release();
  await page.getByRole("button", { name: "Version 7" }).click();
  await expect(page.getByTestId("tree")).toContainText("old-page.sh");
  await page.getByRole("button", { name: "Version 6" }).click();
  await expect(page.getByTestId("tree")).not.toContainText("old-page.sh");
});

test("directory cache is isolated by both account and organization", async ({
  page,
}) => {
  let reads = 0;
  await page.route(fileList, (route) => {
    reads++;
    const data = batch();
    data.directories.tests.files[0].path = `tests/read-${reads}.sh`;
    return route.fulfill({ json: data });
  });
  await page.goto("/file-cache");
  await expect(page.getByTestId("tree")).toContainText("read-1.sh");
  await page.getByRole("button", { name: "Alice A" }).click();
  await expect(page.getByTestId("tree")).toContainText("read-2.sh");
  await page.getByRole("button", { name: "Bob A" }).click();
  await expect(page.getByTestId("tree")).toContainText("read-3.sh");
  await page.getByRole("button", { name: "Alice B" }).click();
  await expect(page.getByTestId("tree")).toContainText("read-4.sh");
  await page.getByRole("button", { name: "Alice A" }).click();
  await expect(page.getByTestId("tree")).toContainText("read-2.sh");
  expect(reads).toBe(4);
});

test("an older server's root-only response still loads directory pages", async ({
  page,
}) => {
  const requested: string[] = [];
  await page.route(fileList, async (route) => {
    const prefix =
      new URL(route.request().url()).searchParams.get("prefix") ?? "";
    requested.push(prefix);
    await route.fulfill({
      json:
        prefix === "tests"
          ? {
              files: [{ path: "tests/test.sh", key: "tests/test.sh" }],
              cursor: null,
            }
          : { files: [], dirs: [{ path: "tests" }], cursor: null },
    });
  });
  await page.goto(finding);
  await expect(
    page.getByRole("button", { name: "test.sh", exact: true })
  ).toBeVisible();
  await expect(page.getByText("exit 0", { exact: true })).toBeVisible();
  expect(new Set(requested)).toEqual(new Set(["", "tests"]));
  expect(requested.filter((path) => path === "").length).toBeLessThanOrEqual(2);
  // Late metadata can revalidate a legacy response once, without a retry loop.
  expect(new URL(page.url()).searchParams.get("taskLines")).toBe("L7");
});

test("an overwrite between pages replaces the inventory without mixing revisions", async ({
  page,
}) => {
  let replaced = false;
  await page.route(fileList, async (route) => {
    const params = new URL(route.request().url()).searchParams;
    if (params.has("cursor")) {
      replaced = true;
      return route.fulfill({
        json: {
          source_hash: "replacement",
          files: [{ path: "tests/partial-new.sh", key: "new" }],
        },
      });
    }
    const data = batch();
    if (replaced) {
      data.source_hash = "replacement";
      data.directories.tests.files[0].path = "tests/replacement.sh";
    }
    return route.fulfill({ json: data });
  });
  await page.goto("/file-cache");
  await expect(page.getByTestId("tree")).toContainText("tests/v7.sh");
  await page.getByRole("button", { name: "More tests" }).click();
  await expect(page.getByTestId("tree")).toContainText("tests/replacement.sh");
  await expect(page.getByTestId("tree")).not.toContainText("tests/v7.sh");
  await expect(page.getByTestId("tree")).not.toContainText("partial-new.sh");
});

for (const partialContent of [
  "one\ntwo",
  "one\ntwo\nthree\nfour\nfive\nsix\nTARGET",
]) {
  test(`an addressed line outside the complete preview loads full content (${partialContent.split("\n").length} preview lines)`, async ({
    page,
  }) => {
    const reads: string[] = [];
    await page.route(
      "**/api/tasks/task-a/files/tests%2Ftest.sh?**",
      async (route) => {
        const params = new URL(route.request().url()).searchParams;
        reads.push(params.get("max_bytes") ?? "full");
        const partial = params.has("max_bytes");
        await route.fulfill({
          json: {
            content: partial
              ? partialContent
              : "one\ntwo\nthree\nfour\nfive\nsix\nTARGET LINE\n",
            size: 150000,
            is_truncated: partial,
          },
        });
      }
    );
    await page.goto(finding);
    await expect(page.getByText("TARGET LINE", { exact: true })).toBeVisible();
    expect(reads).toEqual(["102400", "full"]);
    expect(new URL(page.url()).searchParams.get("taskLines")).toBe("L7");
    expect(new URL(page.url()).searchParams.get("taskFile")).toBe(
      "tests/test.sh"
    );
  });
}

test("a delayed full file stays attached to its original version", async ({
  page,
}) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let fullReadStarted = false;
  await page.route(
    "**/api/tasks/task-a/files/tests%2Ftest.sh?**",
    async (route) => {
      const params = new URL(route.request().url()).searchParams;
      const version = params.get("version");
      const partial = version === "7" && params.has("max_bytes");
      if (version === "7" && !partial) {
        fullReadStarted = true;
        await gate;
      }
      await route.fulfill({
        json: {
          content: partial ? "preview" : `COMPLETE VERSION ${version}`,
          size: 150000,
          is_truncated: partial,
        },
      });
    }
  );
  try {
    await page.goto(finding);
    await expect.poll(() => fullReadStarted).toBe(true);
    await page.evaluate(() => {
      const url = new URL(window.location.href);
      url.searchParams.set("version", "task-a-v6");
      url.searchParams.delete("taskLines");
      window.history.pushState(null, "", url);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await expect(
      page.getByText("COMPLETE VERSION 6", { exact: true })
    ).toBeVisible();
    const finished = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        url.pathname.endsWith("files/tests%2Ftest.sh") &&
        url.searchParams.get("version") === "7" &&
        !url.searchParams.has("max_bytes")
      );
    });
    release();
    await (await finished).finished();
    await page.goBack();
    await expect(
      page.getByText("COMPLETE VERSION 7", { exact: true })
    ).toBeVisible();
    await page.goForward();
    await expect(
      page.getByText("COMPLETE VERSION 6", { exact: true })
    ).toBeVisible();
    await expect(
      page.getByText("COMPLETE VERSION 7", { exact: true })
    ).not.toBeVisible();
  } finally {
    release();
  }
});
