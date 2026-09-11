import { pageFixture, selectionFixture } from "./delivery-page-fixtures";
import { expect, test, type Page } from "@playwright/test";
import { board, history, taskRow, reviewTaskRow } from "./delivery-fixtures";

async function controlledAPI(page: Page) {
  const state = {
    board: board(),
    history: history(),
    failBoard: false,
    failHistory: false,
    reads: { board: 0, history: 0 },
    writes: [] as { path: string; body: Record<string, unknown> }[],
  };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET") {
      const body = request.postDataJSON() ?? {};
      state.writes.push({ path, body });
      if (path.endsWith("/checks")) {
        const check = state.board.tasks[0].checks.find(
          (c) => c.key === body.check_key
        );
        if (check) check.status = body.checked ? "pass" : "fail";
        const task = state.board.tasks[0];
        if (String(body.check_key).startsWith("ack:")) {
          const defect = task.defects.find(
            (defect) => `ack:${defect.id}` === body.check_key
          )!;
          defect.acknowledged = true;
          defect.acknowledged_by_name = "Maya";
          task.checks.find((check) => check.key === "no_must_fix")!.status =
            task.defects.every((defect) => defect.acknowledged)
              ? "pass"
              : "fail";
          task.ready = task.checks.every((check) => check.status !== "fail");
        }
      } else if (path.endsWith("/qa-work")) {
        state.board.tasks[0].qa_work.note = body.note;
      } else if (path.endsWith("/tasks") && request.method() === "POST") {
        state.board.tasks.push({
          ...taskRow(),
          task_id: "task-b",
          task_name: "Task B",
          delivery_task_id: "member-b",
        });
      } else if (request.method() === "DELETE") {
        state.board.tasks = state.board.tasks.filter(
          (row) => !path.endsWith(row.task_id)
        );
      } else if (!path.endsWith("/qa/retry")) {
        throw new Error(`Unexpected mutation: ${request.method()} ${path}`);
      }
      return route.fulfill({ json: {} });
    }
    if (path.endsWith("/qa-history")) {
      state.reads.history++;
      return route.fulfill({
        status: state.failHistory ? 503 : 200,
        json: state.failHistory ? { detail: "history offline" } : state.history,
      });
    }
    if (path === "/api/deliveries/refresh-test/selection") {
      return route.fulfill({
        json: selectionFixture(
          state.board,
          new URL(request.url()).searchParams
        ),
      });
    }
    if (path === "/api/deliveries/refresh-test/view") {
      state.reads.board++;
      return route.fulfill({
        status: state.failBoard ? 503 : 200,
        json: state.failBoard
          ? { detail: "board offline" }
          : pageFixture(state.board, new URL(request.url()).searchParams),
      });
    }
    if (path.startsWith("/api/tasks/browse"))
      return route.fulfill({ json: { items: [] } });
    throw new Error(`Unexpected read: ${path}`);
  });
  await page.clock.install();
  return state;
}

const current = (page: Page, version = 7) =>
  page.locator("summary").filter({ hasText: new RegExp(`v${version}current`) });
async function openBoard(page: Page) {
  await page.goto("/?task=task-a");
  await page.getByText("QA history", { exact: true }).click();
  await expect(current(page)).toBeVisible();
}
async function tick(page: Page) {
  await page.clock.fastForward(15000);
}

test("expanded history sees completed review on the board refresh", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  await openBoard(page);
  await expect(current(page)).toContainText("qa (running)");
  state.history = history(7, 7, "success");
  await tick(page);
  await expect(current(page)).toContainText("qa (success)");
  expect(state.writes).toEqual([]);
});

test("non-default creation keeps v7; default switch shows v8 with v7 history still expanded", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  await openBoard(page);
  await page.getByRole("button", { name: "Show all 7 versions" }).click();
  await current(page).click();
  await expect(
    page.getByText("v7 historical finding", { exact: true })
  ).toBeVisible();
  state.history = history(7, 8);
  await tick(page);
  await expect(
    page.locator("summary").filter({ hasText: /v8Version 8/ })
  ).toBeVisible();
  await expect(current(page)).toBeVisible();
  state.board = board(8);
  state.history = history(8);
  await tick(page);
  await expect(current(page, 8)).toBeVisible();
  await expect(
    page.getByText("Audit missing for v8", { exact: true })
  ).toBeVisible();
  await expect(
    page.getByText("v7 historical finding", { exact: true })
  ).toBeVisible();
  await expect(
    page.locator("summary").filter({ hasText: /v1Version 1/ })
  ).toBeVisible();
  await expect(page).toHaveURL(/task=task-a/);
  expect(state.writes).toEqual([]);
  // QA only starts after Maya explicitly selects a task and requests it.
  await page
    .getByRole("checkbox", { name: "Select Task A", exact: true })
    .click();
  await page.getByRole("button", { name: /Rerun QA/ }).click();
  await expect
    .poll(() => state.writes.filter((w) => w.path.endsWith("/qa/retry")).length)
    .toBe(1);
});

test("external membership, sign-off, acknowledgment and assignment appear without QA", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  await openBoard(page);
  state.board.tasks.push({
    ...taskRow(),
    task_id: "task-b",
    task_name: "Task B",
    delivery_task_id: "member-b",
  });
  state.board.tasks[0].checks[1].status = "pass";
  state.board.tasks[0].qa_owner_name = "Teammate";
  state.board.tasks[0].defects = [
    {
      id: "finding",
      title: "Known defect",
      source: "pre_trial",
      acknowledged: true,
      acknowledged_by_user_id: "teammate",
    },
  ];
  await tick(page);
  await expect(
    page.getByRole("link", { name: "Task B", exact: true })
  ).toBeVisible();
  await expect(
    page.getByRole("table").getByText("Teammate", { exact: true })
  ).toBeVisible();
  await expect(page.getByText("Known defect")).toBeHidden();
  await page
    .locator("summary")
    .filter({ hasText: /^Acknowledged/ })
    .click();
  await expect(page.getByText("Known defect")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Acknowledge for v7", exact: true })
  ).toHaveCount(0);
  await expect(
    page
      .getByRole("listitem")
      .filter({ hasText: "Known defect" })
      .getByRole("button", { name: "Acknowledge for v7", exact: true })
  ).toHaveCount(0);
  state.board.tasks.pop();
  state.board.tasks[0].checks[1].status = "fail";
  state.board.tasks[0].qa_owner_name = null;
  state.board.tasks[0].qa_work.owner_user_id = null;
  state.board.tasks[0].defects[0].acknowledged = false;
  await tick(page);
  await expect(
    page.getByRole("button", { name: "Acknowledge for v7", exact: true })
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Task B", exact: true })
  ).toHaveCount(0);
  await expect(
    page
      .getByRole("listitem")
      .filter({ hasText: "Known defect" })
      .getByRole("button", { name: "Acknowledge for v7", exact: true })
  ).toBeVisible();
  expect(state.writes).toEqual([]);
});

test("refresh failures keep history, filters, pagination, scroll, expansion and draft; retry recovers", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  state.board.tasks = Array.from({ length: 30 }, (_, i) => ({
    ...taskRow(),
    task_id: `task-${i}`,
    task_name: `Task ${i}`,
    delivery_task_id: `member-${i}`,
  }));
  state.board.tasks[25] = taskRow();
  await page.goto(
    "/?page=2&per_page=25&filter=qa_incomplete&task=task-a&source=agent"
  );
  await page.getByText("QA history", { exact: true }).click();
  await expect(current(page)).toBeVisible();
  await page.getByRole("button", { name: "Show all 7 versions" }).click();
  await current(page).click();
  await page.evaluate(() => window.scrollTo(0, 300));
  const scroll = await page.evaluate(() => window.scrollY);
  const url = page.url();
  state.failBoard = true;
  await tick(page);
  await expect(page.locator("main").getByRole("alert")).toContainText(
    "board offline"
  );
  await expect(
    page.getByText("v7 historical finding", { exact: true })
  ).toBeVisible();
  expect(page.url()).toBe(url);
  expect(
    Math.abs((await page.evaluate(() => window.scrollY)) - scroll)
  ).toBeLessThan(100);
  state.failBoard = false;
  state.failHistory = true;
  await page.getByRole("button", { name: "Retry delivery" }).click();
  await expect(page.locator("main").getByRole("alert")).toContainText(
    "history offline"
  );
  await expect(
    page.getByText("v7 historical finding", { exact: true })
  ).toBeVisible();
  state.failHistory = false;
  state.history = history(7, 7, "success");
  await page.getByRole("button", { name: "Retry history" }).click();
  await expect(page.locator("main").getByRole("alert")).toHaveCount(0);
  await expect(current(page)).toContainText("qa (success)");
  await expect(
    page.locator("summary").filter({ hasText: /v1Version 1/ })
  ).toBeVisible();
  expect(page.url()).toBe(url);
  expect(state.writes).toEqual([]);
});

test("task pagination stays put when the next page has fewer rows", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  state.board.tasks = Array.from({ length: 30 }, (_, i) => ({
    ...taskRow(),
    task_id: `task-${i}`,
    task_name: `Task ${i}`,
    delivery_task_id: `member-${i}`,
  }));
  await page.goto("/");

  const taskRegion = page.getByRole("region", { name: "Delivery tasks" });
  const pagination = page.getByRole("navigation", { name: "Task pages" });
  const regionHeight = await taskRegion.evaluate(
    (element) => element.clientHeight
  );
  const paginationOffset = await pagination.evaluate(
    (element) =>
      element.getBoundingClientRect().top -
      element.previousElementSibling!.getBoundingClientRect().top
  );

  await taskRegion.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.getByText("Page 2 of 2 · 30 tasks")).toBeVisible();
  await expect(taskRegion).toHaveJSProperty("scrollTop", 0);
  expect(await taskRegion.evaluate((element) => element.clientHeight)).toBe(
    regionHeight
  );
  expect(
    await pagination.evaluate(
      (element) =>
        element.getBoundingClientRect().top -
        element.previousElementSibling!.getBoundingClientRect().top
    )
  ).toBe(paginationOffset);
});

for (const direction of ["Previous", "Next"] as const) {
  test(`${direction} waits for the requested page before allowing another page change`, async ({
    page,
  }) => {
    const state = await controlledAPI(page);
    state.board.tasks = Array.from({ length: 40 }, (_, i) => ({
      ...taskRow(),
      task_id: `task-${i}`,
      task_name: `Task ${i}`,
      delivery_task_id: `member-${i}`,
    }));
    await page.goto("/?page=2&per_page=10&source=agent#tasks");
    await expect(page.getByText("Page 2 of 4 · 40 tasks")).toBeVisible();
    const previous = page.getByRole("button", {
      name: "Previous",
      exact: true,
    });
    const next = page.getByRole("button", { name: "Next", exact: true });
    const target = direction === "Previous" ? 1 : 3;
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const requests: string[] = [];
    await page.route("**/api/deliveries/refresh-test/view?*", async (route) => {
      requests.push(route.request().url());
      await gate;
      return route.fallback();
    });
    await page.getByRole("button", { name: direction, exact: true }).click();
    await expect(page.getByText("Updating delivery view…")).toBeVisible();
    await expect(previous).toBeDisabled();
    await expect(next).toBeDisabled();
    // Native clicks on either disabled button cannot overwrite the pending URL.
    await previous.evaluate((button: HTMLButtonElement) => button.click());
    await next.evaluate((button: HTMLButtonElement) => button.click());
    expect(new URL(page.url()).searchParams.get("page")).toBe(
      target === 1 ? null : String(target)
    );
    expect(new URL(page.url()).searchParams.get("source")).toBe("agent");
    expect(new URL(page.url()).hash).toBe("#tasks");
    await expect.poll(() => requests.length).toBe(1);
    release();
    await expect(
      page.getByText(`Page ${target} of 4 · 40 tasks`)
    ).toBeVisible();
    await expect(next).toBeEnabled();
    if (target === 1) await expect(previous).toBeDisabled();
    else await expect(previous).toBeEnabled();
    expect(state.reads).toEqual({ board: 2, history: 0 });

    // Returning to an already loaded page stays immediate and allows navigation.
    await page
      .getByRole("button", {
        name: direction === "Previous" ? "Next" : "Previous",
        exact: true,
      })
      .click();
    await expect(page.getByText("Page 2 of 4 · 40 tasks")).toBeVisible();
    await expect(previous).toBeEnabled();
    await expect(next).toBeEnabled();
    expect(state.reads).toEqual({ board: 2, history: 0 });
    expect(state.writes).toEqual([]);
  });
}

test("an old version draft stays copyable and cannot save against the replacement", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  await openBoard(page);
  await page.getByRole("button", { name: "Edit QA work" }).click();
  await page
    .getByRole("textbox", { name: "Handoff note" })
    .fill("Notes about v7");
  state.failBoard = true;
  await tick(page);
  await expect(page.getByRole("textbox", { name: "Handoff note" })).toHaveValue(
    "Notes about v7"
  );
  state.failBoard = false;
  state.board = board(8);
  state.history = history(8);
  await tick(page);
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText(
    "draft belongs to the previous version"
  );
  await expect(page.getByRole("textbox", { name: "Handoff note" })).toHaveValue(
    "Notes about v7"
  );
  await expect(
    page.getByRole("button", { name: "Save", exact: true })
  ).toBeDisabled();
  expect(state.writes).toEqual([]);
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await expect(current(page, 8)).toBeVisible();
  await page.getByRole("button", { name: "Edit QA work" }).click();
  await page
    .getByRole("textbox", { name: "Handoff note" })
    .fill("Notes about v8");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect(page.getByText("Notes about v8", { exact: true })).toBeVisible();
  expect(state.writes.at(-1)?.body).toMatchObject({
    version_id: "version-8",
    note: "Notes about v8",
  });
});

test("local add, remove and sign-off refresh immediately and checks carry the viewed version", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  state.board.tasks[0].checks[0].status = "pass";
  await openBoard(page);
  // The expanded sign-off section has the sole unchecked checkbox besides row selectors.
  await page.getByRole("checkbox").last().click();
  await expect(page.getByRole("checkbox").last()).toBeChecked();
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0].body).toMatchObject({
    check_key: "signoff",
    expected_version_id: "version-7",
    checked: true,
  });
  await expect.poll(() => state.reads.history).toBeGreaterThan(1);
  await page.getByRole("button", { name: "Add tasks", exact: true }).click();
  await page.getByRole("button", { name: "Paste list" }).click();
  await page.getByRole("textbox").fill("task-b");
  await page.getByRole("button", { name: "Add pasted tasks" }).click();
  await expect(
    page.getByRole("link", { name: "Task B", exact: true })
  ).toBeVisible();
  await page.getByText("Task actions", { exact: true }).click();
  await page.getByRole("button", { name: "Remove from delivery" }).click();
  await page.getByRole("button", { name: "Remove", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "Task A", exact: true })
  ).toHaveCount(0);
  expect(state.writes.some((w) => w.path.endsWith("/qa/retry"))).toBe(false);
});

test("reads are bounded to one board and one expanded history per interval", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  await openBoard(page);
  for (let i = 0; i < 4; i++) {
    const reads = { ...state.reads };
    await tick(page);
    await expect.poll(() => state.reads.history).toBe(reads.history + 1);
    expect(state.reads.board).toBe(reads.board + 1);
  }
  await page
    .getByRole("row")
    .filter({ has: page.getByRole("link", { name: "Task A", exact: true }) })
    .click();
  const reads = { ...state.reads };
  await tick(page);
  await expect.poll(() => state.reads.board).toBe(reads.board + 1);
  expect(state.reads.history).toBe(reads.history);
  expect(state.writes).toEqual([]);
});

test("finalized delivery does not poll and distinguishes live history from shipped version", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  state.board.frozen = true;
  state.board.delivery.status = "finalized";
  state.board.finalized_at = "2026-09-09T00:00:00Z";
  state.history = history(8);
  await page.goto("/?task=task-a");
  await expect(
    page.getByText("Live task history · delivery shipped v7")
  ).toBeVisible();
  await page.getByText("Live task history · delivery shipped v7").click();
  await expect(current(page, 8)).toBeVisible();
  const reads = { ...state.reads };
  await page.clock.fastForward(60000);
  expect(state.reads).toEqual(reads);
  expect(state.writes).toEqual([]);
});

test("a delayed pre-mutation board response cannot overwrite a saved note", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  await openBoard(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let delayed = false;
  const oldBoard = structuredClone(state.board);
  await page.route("**/api/deliveries/refresh-test/view?*", async (route) => {
    if (delayed) return route.fallback();
    delayed = true;
    await gate;
    await route.fulfill({
      json: pageFixture(oldBoard, new URL(route.request().url()).searchParams),
    });
  });
  await tick(page);
  await expect.poll(() => delayed).toBe(true);
  await page.getByRole("button", { name: "Edit QA work" }).click();
  await page
    .getByRole("textbox", { name: "Handoff note" })
    .fill("Saved during a slow refresh");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect(
    page.getByText("Saved during a slow refresh", { exact: true })
  ).toBeVisible();
  release();
  await tick(page);
  await expect(
    page.getByText("Saved during a slow refresh", { exact: true })
  ).toBeVisible();
});

test("a failed first board load can recover without navigating away", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  state.failBoard = true;
  await page.goto("/?filter=qa_incomplete&task=task-a");
  await expect(page.locator("main").getByRole("alert")).toContainText(
    "board offline"
  );
  state.failBoard = false;
  await page.getByRole("button", { name: "Retry delivery" }).click();
  await page.getByText("QA history", { exact: true }).click();
  await expect(current(page)).toBeVisible();
  await expect(page).toHaveURL(/filter=qa_incomplete&task=task-a/);
  expect(state.writes).toEqual([]);
});

for (const query of ["page=2", "filter=ready&group=owner&owner=unassigned"]) {
  test(`failed navigation preserves usable displayed rows and selection: ${query}`, async ({
    page,
  }) => {
    const state = await controlledAPI(page);
    state.board.tasks = Array.from({ length: 12 }, (_, i) => ({
      ...taskRow(),
      task_id: `task-${i}`,
      task_name: `Task ${i}`,
      delivery_task_id: `member-${i}`,
      version_id: `version-${i}`,
    }));
    await page.goto("/?per_page=10&source=agent");
    const first = page.getByRole("checkbox", {
      name: "Select Task 0",
      exact: true,
    });
    await expect(first).toBeEnabled();
    const selectionQueries: string[] = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.pathname.endsWith("/selection"))
        selectionQueries.push(url.search);
    });
    let release!: () => void;
    let gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    await page.route("**/api/deliveries/refresh-test/view?*", async (route) => {
      await gate;
      return route.fallback();
    });
    state.failBoard = true;
    await page.evaluate(
      (query) =>
        window.history.pushState(
          null,
          "",
          `?per_page=10&source=agent&${query}`
        ),
      query
    );
    await expect(page.getByRole("status")).toHaveText(
      "Updating delivery view…"
    );
    await expect(first).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "Next", exact: true })
    ).toBeDisabled();
    release();
    await expect(page.locator("main").getByRole("alert")).toContainText(
      "Showing the previously loaded tasks"
    );
    await expect(page.getByText("Updating delivery view…")).toHaveCount(0);
    await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
    await expect(first).toBeEnabled();
    await expect(
      page.getByRole("button", { name: "Next", exact: true })
    ).toBeEnabled();
    await expect(
      page.getByRole("columnheader", { name: "Owner", exact: true })
    ).toBeVisible();
    await page
      .getByRole("checkbox", { name: "Select all tasks in this view" })
      .click();
    await expect(page.getByText("12 selected", { exact: true })).toBeVisible();
    expect(selectionQueries).toEqual(["?per_page=10"]);
    expect(state.reads).toEqual({ board: 2, history: 0 });
    expect(state.writes).toEqual([]);
    gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    state.failBoard = false;
    await page.getByRole("button", { name: "Retry delivery" }).click();
    await expect(page.getByText("Updating delivery view…")).toBeVisible();
    await expect(first).toBeDisabled();
    release();
    await expect(page.locator("main").getByRole("alert")).toHaveCount(0);
    await expect(page.getByText("Updating delivery view…")).toHaveCount(0);
    await expect(first).toHaveCount(0);
    if (query === "page=2") {
      await expect(
        page.getByRole("checkbox", { name: "Select Task 10", exact: true })
      ).toBeEnabled();
    } else {
      await expect(page.getByText("No tasks match this filter.")).toBeVisible();
    }
    expect(state.reads.board).toBe(3);
    await expect(page).toHaveURL(new RegExp(`source=agent&${query}`));
  });
}

test("bulk sign-off keeps the versions shown when confirmation opened", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  state.board.tasks[0].checks[0].status = "pass";
  await openBoard(page);
  await page
    .getByRole("checkbox", { name: "Select Task A", exact: true })
    .click();
  await page.getByRole("button", { name: "Sign off", exact: true }).click();
  state.board = board(8);
  state.board.tasks[0].checks[0].status = "pass";
  state.history = history(8);
  await tick(page);
  await page
    .getByRole("alertdialog")
    .getByRole("button", {
      name: "Sign off",
      exact: true,
    })
    .click();
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0].body).toMatchObject({
    check_key: "signoff",
    expected_version_id: "version-7",
  });
});

test("a fresh server board avoids the initial read and keeps history, URL navigation, and edits live", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  await page.goto("/?seed=fresh&filter=all&task=task-a&source=agent");
  await page.getByText("QA history", { exact: true }).click();
  await expect(current(page)).toBeVisible();
  expect(state.reads).toEqual({ board: 0, history: 1 });
  await page.evaluate(() =>
    window.history.pushState(
      null,
      "",
      "?seed=fresh&filter=all&group=owner&source=agent"
    )
  );
  await expect(page).toHaveURL(/group=owner/);
  await page.goBack();
  // The shared URL restores the open history panel.
  await expect(current(page)).toBeVisible();
  expect(state.reads.board).toBe(1);
  await page.goForward();
  await expect(page).toHaveURL(/group=owner/);
  expect(state.reads.board).toBe(1);
  await page.goBack();
  // The shared URL restores the open history panel.
  state.history = history(7, 7, "success");
  await tick(page);
  await expect(current(page)).toContainText("qa (success)");
  expect(state.reads.board).toBe(2);
  await page.getByRole("button", { name: "Edit QA work" }).click();
  await page
    .getByRole("textbox", { name: "Handoff note" })
    .fill("Saved after server load");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect.poll(() => state.reads.board).toBe(3);
  await expect(
    page.getByText("Saved after server load", { exact: true })
  ).toBeVisible();
  expect(state.writes).toHaveLength(1);
  await page.clock.setSystemTime(new Date());
  await page.reload();
  // The shared URL restores the open history panel.
  await expect(current(page)).toBeVisible();
  expect(state.reads.board).toBe(3);
  await expect(page).toHaveURL(/source=agent/);
});

test("an old server snapshot refreshes on arrival", async ({ page }) => {
  const state = await controlledAPI(page);
  state.board.tasks[0].task_name = "Updated task";
  await page.goto("/?seed=stale&filter=all");
  await expect(
    page.getByRole("link", { name: "Updated task", exact: true })
  ).toBeVisible();
  expect(state.reads.board).toBe(1);
});

test("a write invalidates inactive pages before browser Forward restores them", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  await page.goto("/?task=task-a&source=agent");
  await expect(
    page.getByRole("button", { name: "Edit QA work" })
  ).toBeVisible();
  await page.evaluate(() => {
    const url = new URL(window.location.href);
    url.searchParams.set("group", "owner");
    window.history.pushState(null, "", url);
  });
  await expect.poll(() => state.reads.board).toBe(2);
  await expect(page.getByRole("status")).toHaveCount(0);
  await page.goBack();
  await page.getByRole("button", { name: "Edit QA work" }).click();
  await page
    .getByRole("textbox", { name: "Handoff note" })
    .fill("Fresh across pages");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect(
    page.getByText("Fresh across pages", { exact: true })
  ).toBeVisible();
  expect(state.reads.board).toBe(3);
  await page.goForward();
  await expect.poll(() => state.reads.board).toBe(4);
  await expect(
    page.getByText("Fresh across pages", { exact: true })
  ).toBeVisible();
  await expect(page).toHaveURL(/source=agent&group=owner/);
});

test("a snapshot from another organization is discarded", async ({ page }) => {
  const state = await controlledAPI(page);
  await page.goto("/?seed=wrong-org&filter=all");
  await expect(
    page.getByRole("link", { name: "Task A", exact: true })
  ).toBeVisible();
  await expect(page.getByText("OTHER_ORG_PRIVATE_TASK")).toHaveCount(0);
  expect(state.reads.board).toBe(1);
});

test("a frozen server board has no initial or periodic board requests", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  await page.goto("/?seed=frozen&filter=all&task=task-a");
  await page.getByText("Live task history · delivery shipped v7").click();
  await expect(current(page)).toBeVisible();
  await page.clock.fastForward(60000);
  expect(state.reads).toEqual({ board: 0, history: 1 });
});

test("review shows outstanding decisions first and acknowledgment retains the version and evidence", async ({
  page,
}, testInfo) => {
  const state = await controlledAPI(page);
  state.board.tasks = [
    reviewTaskRow(),
    {
      ...taskRow(),
      task_id: "task-b",
      task_name: "Next task",
      delivery_task_id: "member-b",
    },
  ];
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto("/?task=task-a");
  const pending = page
    .locator("details")
    .filter({
      has: page.locator("summary").filter({ hasText: /^Needs a decision/ }),
    })
    .first();
  const accepted = page
    .locator("details")
    .filter({
      has: page.locator("summary").filter({ hasText: /^Acknowledged/ }),
    })
    .first();
  await expect(pending.locator("summary").first()).toHaveText(
    "Needs a decision · 2 findings · v1"
  );
  await expect(accepted.locator("summary").first()).toHaveText(
    "Acknowledged · 3 findings, 2 checks · v1"
  );
  await expect(
    page.getByRole("link", {
      name: state.board.tasks[0].task_name,
      exact: true,
    })
  ).toHaveCount(1);
  await expect(
    page.getByRole("button", { name: "Acknowledge for v1", exact: true })
  ).toHaveCount(2);
  await expect(
    page.getByText(
      "Sign-off is recorded. Outstanding finding or check decisions still block delivery."
    )
  ).toBeVisible();
  await expect(page.getByText("Enough rollouts", { exact: true })).toHaveCount(
    0
  );
  await expect(
    page.getByText("The image build leaves", { exact: false })
  ).toBeHidden();
  await expect(current(page, 1)).toBeHidden();
  await page.evaluate(() => {
    document.documentElement.classList.add("dark");
    window.scrollTo(0, 0);
  });
  await page.clock.runFor(300);
  await page.screenshot({
    animations: "disabled",
    path: testInfo.outputPath("delivery-review-dark.png"),
    fullPage: true,
  });
  await page.evaluate(() => document.documentElement.classList.remove("dark"));
  await page.clock.runFor(300);
  await page.screenshot({
    animations: "disabled",
    path: testInfo.outputPath("delivery-review-light.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth
    )
  ).toBe(true);
  await page.setViewportSize({ width: 1440, height: 1100 });
  const verifier = pending
    .getByRole("listitem")
    .filter({ hasText: "The verifier does not check" });
  await verifier.getByText("Review evidence", { exact: true }).click();
  await expect(
    verifier.getByText("Fixture evidence:", { exact: false })
  ).toBeVisible();
  const evidenceHref = await verifier.getByRole("link").getAttribute("href");
  expect(evidenceHref).toContain("version=1");
  expect(evidenceHref).toContain("finding=verifier");
  expect(evidenceHref).toContain("taskFile=tests%2Ftest.sh");
  expect(state.writes).toEqual([]);
  await verifier
    .getByRole("button", { name: "Acknowledge for v1", exact: true })
    .click();
  await expect(pending.locator("summary").first()).toHaveText(
    "Needs a decision · 1 finding · v1"
  );
  await expect(accepted.locator("summary").first()).toHaveText(
    "Acknowledged · 4 findings, 2 checks · v1"
  );
  expect(state.writes[0].body).toMatchObject({
    check_key: "ack:verifier",
    expected_version_id: "version-1",
    checked: true,
  });
  await expect(page).toHaveURL(/task=task-a/);
  await accepted.locator("summary").first().click();
  const retained = accepted
    .getByRole("listitem")
    .filter({ hasText: "The verifier does not check" });
  await expect(retained).toContainText("Recorded severity: should_fix");
  await expect(retained).toContainText(
    "Acknowledged by Maya for v1; finding retained"
  );
  await expect(retained.getByRole("link")).toHaveAttribute(
    "href",
    evidenceHref!
  );
  // Evidence stays expanded when acknowledgment moves it between sections.
  await expect(
    retained.getByText("Fixture evidence:", { exact: false })
  ).toBeVisible();
  await pending
    .getByRole("button", { name: "Acknowledge for v1", exact: true })
    .click();
  await expect(
    page.getByText("Needs a decision", { exact: false })
  ).toHaveCount(0);
  await expect(
    page.getByRole("table").getByText("Ready", { exact: true })
  ).toBeVisible();
  await expect(
    page.getByText("Review could not complete", { exact: true })
  ).toBeVisible();
  expect(state.writes.map(({ body }) => body.check_key)).toEqual([
    "ack:verifier",
    "ack:environment",
  ]);
});

test("finalized review exposes evidence but cannot acknowledge outstanding findings", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  state.board.tasks = [reviewTaskRow()];
  state.board.frozen = true;
  await page.goto("/?task=task-a");
  for (const button of await page
    .getByRole("button", { name: "Acknowledge for v1", exact: true })
    .all()) {
    await expect(button).toBeDisabled();
  }
  await page.getByText("Review evidence", { exact: true }).click();
  await expect(
    page.getByText("Fixture evidence:", { exact: false })
  ).toBeVisible();
  expect(state.writes).toEqual([]);
});

test("owner, state, and history share one scope across server pages; finalize stays delivery-wide", async ({
  page,
}, testInfo) => {
  const state = await controlledAPI(page);
  const task = taskRow();
  state.board.tasks = [
    {
      ...task,
      task_name: "Missing review",
      task_id: "incomplete",
      delivery_task_id: "incomplete",
    },
    {
      ...task,
      task_name: "Needs a fix",
      task_id: "defect",
      delivery_task_id: "defect",
      defects: [
        {
          id: "finding",
          title: "Known defect",
          source: "pre_trial",
          acknowledged: false,
        },
      ],
    },
    {
      ...task,
      task_name: "Awaiting approval",
      task_id: "waiting",
      delivery_task_id: "waiting",
      checks: [{ ...task.checks[0], status: "pass" }, task.checks[1]],
    },
    {
      ...task,
      task_name: "Ready task",
      task_id: "ready",
      delivery_task_id: "ready",
      ready: true,
      checks: task.checks.map((check) => ({ ...check, status: "pass" })),
      qa: { ...task.qa, status: "accepted" },
    },
    {
      ...task,
      task_name: "Jules complete",
      task_id: "jules",
      delivery_task_id: "jules",
      ready: true,
      checks: task.checks.map((check) => ({ ...check, status: "pass" })),
      qa_work: { ...task.qa_work, owner_user_id: "jules" },
      qa_owner_name: "Jules",
    },
    {
      ...task,
      task_name: "Unassigned task",
      task_id: "unassigned",
      delivery_task_id: "unassigned",
      qa_work: { ...task.qa_work, owner_user_id: null },
      qa_owner_name: null,
    },
  ];
  state.board.task_count = 6;
  state.board.ready_task_count = 2;
  state.board.progress_history = Array.from({ length: 10 }, (_, day) => {
    const maya = day < 3 ? 2 : 4;
    const jules = day < 6 ? 0 : 1;
    const unassigned = day < 8 ? 0 : 1;
    const ready = (day < 4 ? 0 : 1) + jules;
    return {
      recorded_at: `2026-09-${String(day + 1).padStart(2, "0")}T12:00:00Z`,
      task_count: maya + jules + unassigned,
      ready,
      blocked: 3,
      awaiting_signoff: 1,
      unassigned,
      open_findings: 1,
      acknowledged_findings: 0,
      owners: {
        maya: { task_count: maya, ready: day < 4 ? 0 : 1 },
        jules: { task_count: jules, ready: jules },
        unassigned: { task_count: unassigned, ready: 0 },
      },
    };
  });
  await page.goto("/");
  const overview = page.getByLabel("Delivery overview");
  for (const [label, count] of [
    ["Needs work", "1"],
    ["QA incomplete", "2"],
    ["Needs sign-off", "1"],
    ["Ready", "2"],
  ]) {
    await expect(
      overview.getByText(label, { exact: true }).locator("..").locator("dd")
    ).toHaveText(count);
  }
  await expect(overview.getByRole("button")).toHaveCount(0);
  const filters = page.getByRole("group", {
    name: "Task filters",
    exact: true,
  });
  expect(
    await filters
      .getByRole("combobox")
      .evaluateAll((elements) =>
        elements.map((element) => element.getAttribute("aria-label"))
      )
  ).toEqual([
    "State filter",
    "Owner filter",
    "Issue category filter",
    "Group tasks",
  ]);
  await expect(overview.locator(".recharts-line-curve")).toHaveCount(2);
  expect(
    await overview
      .locator(".recharts-line-curve")
      .first()
      .evaluate((element) => getComputedStyle(element).stroke)
  ).not.toBe("none");
  const reads = state.reads.board;
  await page
    .getByRole("combobox", { name: "State filter", exact: true })
    .click();
  await page.getByRole("option", { name: "Ready", exact: true }).click();
  await expect(
    page
      .getByRole("table")
      .getByRole("link", { name: "Ready task", exact: true })
  ).toBeVisible();
  await expect(page.getByRole("table")).not.toContainText("Missing review");
  await page
    .getByRole("combobox", { name: "State filter", exact: true })
    .click();
  await page.getByRole("option", { name: "All states", exact: true }).click();
  await page.getByRole("combobox", { name: "Owner filter" }).click();
  await page.getByRole("option", { name: "Jules", exact: true }).click();
  await expect(
    overview.getByText("Ready", { exact: true }).locator("..").locator("dd")
  ).toHaveText("1");
  await expect(overview.locator("dt")).toHaveCount(3);
  await expect(
    overview.getByText("Needs sign-off", { exact: true })
  ).toHaveCount(0);
  await expect(overview.getByRole("img")).toHaveAttribute(
    "aria-label",
    /Jules: 1 ready of 1 tasks/
  );
  await expect(page.getByRole("table")).toContainText("Jules complete");
  await expect(
    page.getByRole("button", { name: "Finalize", exact: true })
  ).toBeDisabled();
  await page.goBack();
  await expect(page.getByRole("table")).toContainText("Missing review");
  expect(state.reads.board).toBe(reads + 2);
  expect(state.writes).toEqual([]);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({
    path: testInfo.outputPath("delivery-overview-desktop.png"),
    fullPage: true,
  });
  await page.evaluate(() => document.documentElement.classList.add("dark"));
  await page.clock.runFor(300);
  await page.screenshot({
    animations: "disabled",
    path: testInfo.outputPath("delivery-overview-dark.png"),
    fullPage: true,
  });
  await page.mouse.move(0, 0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.clock.runFor(100);
  await expect
    .poll(() =>
      filters.evaluate((element) => element.scrollWidth <= element.clientWidth)
    )
    .toBe(true);
  await expect
    .poll(() =>
      overview.evaluate((element) => element.scrollWidth <= element.clientWidth)
    )
    .toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("delivery-overview-mobile.png"),
    fullPage: true,
  });
});

test("older owner observations show no history rather than delivery totals", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  state.board.progress_history = [
    {
      recorded_at: "2026-09-09T12:00:00Z",
      task_count: 10,
      ready: 8,
      blocked: 2,
      awaiting_signoff: 0,
      unassigned: 1,
      open_findings: 0,
      acknowledged_findings: 0,
    },
  ];
  await page.goto("/?owner=maya");
  await expect(
    page.getByLabel("Delivery overview").getByText("No history yet")
  ).toBeVisible();
  await expect(
    page.getByLabel("Delivery overview").getByRole("img")
  ).toHaveCount(0);
});

test("review disclosures restore from a shared link and browser Back", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  await openBoard(page);
  await page.getByRole("button", { name: "Show all 7 versions" }).click();
  await current(page).click();
  const url = page.url();
  await page.reload();
  await expect(
    page.getByText("v7 historical finding", { exact: true })
  ).toBeVisible();
  await current(page).click();
  await expect(
    page.getByText("v7 historical finding", { exact: true })
  ).toBeHidden();
  await page.goBack();
  await expect(
    page.getByText("v7 historical finding", { exact: true })
  ).toBeVisible();
  expect(page.url()).toBe(url);
  expect(state.writes).toEqual([]);
});

test("mixed selections cannot be partly signed off and grouping keeps claim available", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  const waiting = taskRow();
  waiting.checks[0].status = "pass";
  waiting.task_name = "Awaiting approval";
  state.board.tasks = [
    waiting,
    {
      ...taskRow(),
      task_id: "unassigned",
      delivery_task_id: "unassigned",
      task_name: "Unassigned task",
      qa_work: { ...taskRow().qa_work, owner_user_id: null },
      qa_owner_name: null,
    },
  ];
  await page.goto("/");
  await page
    .getByRole("checkbox", { name: "Select all tasks in this view" })
    .click();
  await expect(
    page.getByRole("button", { name: "Sign off", exact: true })
  ).toBeDisabled();
  await page
    .getByRole("checkbox", { name: "Select Unassigned task", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Sign off", exact: true })
  ).toBeEnabled();
  await page.getByRole("combobox", { name: "Group tasks" }).click();
  await page
    .getByRole("option", { name: "Group by owner", exact: true })
    .click();
  await expect(
    page.getByRole("columnheader", { name: "Owner", exact: true })
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Review Unassigned task", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Claim task", exact: true })
  ).toBeVisible();
  expect(state.writes).toEqual([]);
});

test("history remains above counts with only one observation", async ({
  page,
}, testInfo) => {
  const state = await controlledAPI(page);
  await page.goto("/");
  const overview = page.getByLabel("Delivery overview");
  await expect(overview.getByText("No history yet")).toBeVisible();
  await expect(overview.locator("svg")).toHaveCount(0);
  state.board.qa_as_of = "2026-09-10T12:00:00Z";
  state.board.progress_history = [
    {
      recorded_at: "2026-09-09T12:00:00Z",
      task_count: 24,
      ready: 3,
      blocked: 21,
      awaiting_signoff: 0,
      unassigned: 0,
      open_findings: 0,
      acknowledged_findings: 0,
    },
  ];
  await tick(page);
  const chart = overview.getByRole("img");
  await expect(chart.getByText("24 total", { exact: true })).toBeVisible();
  await expect(chart.getByText("3 ready", { exact: true })).toBeVisible();
  await expect(overview.getByLabel("Progress snapshot")).toHaveCount(0);
  expect(
    await chart
      .locator(".recharts-line-dots circle")
      .evaluateAll(
        (elements) =>
          elements.filter((e) => Number(e.getAttribute("r")) > 0).length
      )
  ).toBe(2);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.clock.runFor(100);
    const graph = (await chart.boundingBox())!;
    const counts = (await overview.locator("dl").boundingBox())!;
    expect(graph.y + graph.height).toBeLessThanOrEqual(counts.y);
    await page.screenshot({
      path: testInfo.outputPath(`delivery-single-observation-${width}.png`),
      fullPage: true,
    });
  }
  state.board.progress_history[0].recorded_at = "2026-09-10T12:00:00Z";
  await tick(page);
  await expect(chart.locator("time")).toHaveCount(1);
  await expect(chart.getByText("24 total", { exact: true })).toBeVisible();
  expect(state.writes).toEqual([]);
});

test("history keeps gaps visible and separates equal endpoint labels", async ({
  page,
}, testInfo) => {
  const state = await controlledAPI(page);
  state.board.qa_as_of = "2026-09-10T12:00:00Z";
  state.board.progress_history = [7, 8, 10].map((day) => ({
    recorded_at: `2026-09-${String(day).padStart(2, "0")}T12:00:00Z`,
    task_count: 24,
    ready: 24,
    blocked: 0,
    awaiting_signoff: 0,
    unassigned: 0,
    open_findings: 0,
    acknowledged_findings: 0,
  }));
  await page.goto("/");
  const overview = page.getByLabel("Delivery overview");
  await expect(overview.getByText("24 total", { exact: true })).toBeVisible();
  await expect(overview.getByText("24 ready", { exact: true })).toBeVisible();
  await expect(
    overview.locator(".recharts-cartesian-grid, .recharts-yAxis")
  ).toHaveCount(0);
  await expect(overview.getByRole("img").locator("time")).toHaveText([
    "Sep 7",
    "Today",
  ]);
  const paths = await overview
    .locator(".recharts-line-curve")
    .evaluateAll((elements) => elements.map((e) => e.getAttribute("d")));
  for (const path of paths) expect(path!.match(/M/g)).toHaveLength(2);
  const dots = await overview
    .locator(".recharts-line-dots circle")
    .evaluateAll(
      (elements) =>
        elements.filter((e) => Number(e.getAttribute("r")) > 0).length
    );
  expect(dots).toBe(2);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    // ResizeObserver and React render asynchronously; wait for the actual label
    // position while allowing the fake animation clock to advance.
    await expect
      .poll(async () => {
        await page.clock.runFor(16);
        return overview
          .getByText("24 ready", { exact: true })
          .evaluate((element) => element.getBoundingClientRect().right);
      })
      .toBeLessThanOrEqual(width);
    const total = (await overview
      .getByText("24 total", { exact: true })
      .boundingBox())!;
    const ready = (await overview
      .getByText("24 ready", { exact: true })
      .boundingBox())!;
    expect(total.y + total.height).toBeLessThanOrEqual(ready.y);
    expect(ready.x + ready.width).toBeLessThanOrEqual(width);
    await page.screenshot({
      path: testInfo.outputPath(`delivery-history-gap-${width}.png`),
      fullPage: true,
    });
  }
  expect(state.writes).toEqual([]);
});

test("acknowledgment shows saving and refreshing, and a failed save can be retried", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  state.board.tasks = [reviewTaskRow()];
  await page.goto("/?task=task-a");
  const finding = page
    .getByRole("listitem")
    .filter({ hasText: "The verifier does not check" });
  const acknowledge = finding.getByRole("button", {
    name: "Acknowledge for v1",
    exact: true,
  });
  let releaseSave!: () => void;
  let saveGate = new Promise<void>((resolve) => {
    releaseSave = resolve;
  });
  let failSave = true;
  await page.route("**/api/deliveries/refresh-test/checks", async (route) => {
    await saveGate;
    if (failSave)
      return route.fulfill({
        status: 409,
        json: { detail: "The selected task version changed." },
      });
    return route.fallback();
  });
  await acknowledge.click();
  await expect(
    finding.getByRole("button", { name: "Saving…", exact: true })
  ).toBeDisabled();
  expect(state.writes).toHaveLength(0);
  releaseSave();
  await expect(
    page.getByText("The selected task version changed.", { exact: false })
  ).toBeVisible();
  await expect(acknowledge).toBeEnabled();
  await page.clock.runFor(100);
  failSave = false;
  saveGate = new Promise<void>((resolve) => {
    releaseSave = resolve;
  });
  let releaseRefresh!: () => void;
  const refreshGate = new Promise<void>((resolve) => {
    releaseRefresh = resolve;
  });
  await page.route("**/api/deliveries/refresh-test/view?*", async (route) => {
    await refreshGate;
    return route.fallback();
  });
  await acknowledge.click();
  await expect(
    finding.getByRole("button", { name: "Saving…", exact: true })
  ).toBeDisabled();
  releaseSave();
  await expect(
    finding.getByRole("button", { name: "Updating…", exact: true })
  ).toBeDisabled();
  expect(state.writes).toHaveLength(1);
  // A slow refresh only disables the finding just saved, not the next action.
  const nextFinding = page
    .getByRole("listitem")
    .filter({ hasText: "Agent environment is missing" });
  await expect(
    nextFinding.getByRole("button", { name: "Acknowledge for v1", exact: true })
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "Release task", exact: true })
  ).toBeEnabled();
  await page.clock.runFor(100);
  await nextFinding
    .getByRole("button", { name: "Acknowledge for v1", exact: true })
    .click();
  await expect(
    nextFinding.getByRole("button", { name: "Updating…", exact: true })
  ).toBeDisabled();
  await expect(
    finding.getByRole("button", { name: "Updating…", exact: true })
  ).toBeDisabled();
  expect(state.writes).toHaveLength(2);
  releaseRefresh();
  await expect(
    page.getByRole("button", { name: "Updating…", exact: true })
  ).toHaveCount(0);
  await expect(
    finding.getByRole("button", { name: "Acknowledge for v1", exact: true })
  ).toHaveCount(0);
  await expect(
    nextFinding.getByRole("button", { name: "Acknowledge for v1", exact: true })
  ).toHaveCount(0);
});

test("legacy blocked links include defects and incomplete QA but exclude signoff and ready tasks", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  const defect = reviewTaskRow();
  defect.task_id = "defect";
  defect.delivery_task_id = "member-defect";
  defect.task_name = "Defect task";
  const incomplete = taskRow();
  incomplete.task_name = "Incomplete task";
  const awaiting = taskRow();
  awaiting.task_id = "awaiting";
  awaiting.delivery_task_id = "member-awaiting";
  awaiting.task_name = "Awaiting task";
  awaiting.checks[0].status = "pass";
  const ready = taskRow();
  ready.task_id = "ready";
  ready.delivery_task_id = "member-ready";
  ready.task_name = "Ready task";
  ready.checks.forEach((check) => (check.status = "pass"));
  ready.ready = true;
  state.board.tasks = [defect, incomplete, awaiting, ready];
  await page.goto("/?filter=blocked");
  await expect(page.getByRole("combobox", { name: "State filter" })).toHaveText(
    "Blocked"
  );
  for (const name of ["Defect task", "Incomplete task"])
    await expect(page.getByRole("link", { name, exact: true })).toBeVisible();
  for (const name of ["Awaiting task", "Ready task"])
    await expect(page.getByRole("link", { name, exact: true })).toHaveCount(0);
});

test("select all includes matching tasks beyond the rendered page", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  state.board.tasks = Array.from({ length: 32 }, (_, i) => {
    const row = taskRow();
    row.task_id = `bulk-${i}`;
    row.task_name = `Bulk ${i}`;
    row.delivery_task_id = `bulk-member-${i}`;
    row.version_id = `bulk-version-${i}`;
    row.checks[0].status = "pass";
    return row;
  });
  await page.goto("/?per_page=10&filter=awaiting_signoff&source=agent");
  await expect(
    page.getByRole("checkbox", { name: /^Select Bulk/ })
  ).toHaveCount(10);
  await page
    .getByRole("checkbox", { name: "Select all tasks in this view" })
    .click();
  await expect(page.getByText("32 selected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Sign off", exact: true }).click();
  await expect(page.getByRole("alertdialog")).toContainText(
    "Sign off 32 tasks?"
  );
  await page
    .getByRole("alertdialog")
    .getByRole("button", { name: "Sign off", exact: true })
    .click();
  await expect.poll(() => state.writes.length).toBe(32);
  expect(new Set(state.writes.map((w) => w.body.expected_version_id))).toEqual(
    new Set(state.board.tasks.map((r) => r.version_id))
  );
  await expect(page).toHaveURL(/source=agent/);
});

test("history starts on intent and opening consumes the same pending read", async ({
  page,
}) => {
  const state = await controlledAPI(page);
  await page.goto("/?panels=history");
  await expect(
    page.getByRole("link", { name: "Task A", exact: true })
  ).toBeVisible();
  expect(state.reads.history).toBe(0);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let requests = 0;
  await page.route("**/api/tasks/task-a/qa-history", async (route) => {
    requests++;
    await gate;
    return route.fallback();
  });
  try {
    const row = page
      .getByRole("row")
      .filter({ has: page.getByRole("link", { name: "Task A", exact: true }) });
    await row.hover();
    await page.clock.runFor(200);
    await expect.poll(() => requests).toBe(1);
    await row.click();
    await expect(page).toHaveURL(/task=task-a/);
    await page.clock.runFor(200);
    expect(requests).toBe(1);
  } finally {
    release();
  }
  await expect(current(page)).toBeVisible();
  expect(requests).toBe(1);
});
