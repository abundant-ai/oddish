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
    if (path === "/api/deliveries/refresh-test") {
      state.reads.board++;
      return route.fulfill({
        status: state.failBoard ? 503 : 200,
        json: state.failBoard ? { detail: "board offline" } : state.board,
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
  await expect(page.getByText("Teammate", { exact: true })).toBeVisible();
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
  await page.goto("/?page=2&filter=blocked&task=task-a");
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
  await page.route("**/api/deliveries/refresh-test", async (route) => {
    if (delayed) return route.fallback();
    delayed = true;
    await gate;
    await route.fulfill({ json: oldBoard });
  });
  await tick(page);
  await expect.poll(() => delayed).toBe(true);
  await page.getByRole("button", { name: "Edit QA work" }).click();
  await page
    .getByRole("textbox", { name: "Handoff note" })
    .fill("Saved during a slow refresh");
  await page.getByRole("button", { name: "Save", exact: true }).click();
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
  await page.goto("/?filter=blocked&task=task-a");
  await expect(page.locator("main").getByRole("alert")).toContainText(
    "board offline"
  );
  state.failBoard = false;
  await page.getByRole("button", { name: "Retry delivery" }).click();
  await page.getByText("QA history", { exact: true }).click();
  await expect(current(page)).toBeVisible();
  await expect(page).toHaveURL(/filter=blocked&task=task-a/);
  expect(state.writes).toEqual([]);
});

for (const scope of ["all", "selected"] as const) {
  test(`bulk ${scope} sign-off keeps the versions shown when confirmation opened`, async ({
    page,
  }) => {
    const state = await controlledAPI(page);
    state.board.tasks[0].checks[0].status = "pass";
    await openBoard(page);
    if (scope === "selected")
      await page
        .getByRole("checkbox", { name: "Select Task A", exact: true })
        .click();
    await page
      .getByRole("button", { name: `Sign off ${scope} (1)`, exact: true })
      .click();
    state.board = board(8);
    state.board.tasks[0].checks[0].status = "pass";
    state.history = history(8);
    await tick(page);
    await page
      .getByRole("alertdialog")
      .getByRole("button", {
        name: scope === "all" ? "Sign off all" : "Sign off",
        exact: true,
      })
      .click();
    await expect.poll(() => state.writes.length).toBe(1);
    expect(state.writes[0].body).toMatchObject({
      check_key: "signoff",
      expected_version_id: "version-7",
    });
  });
}

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
  await retained.getByText("Review evidence", { exact: true }).click();
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
    page.getByText("Ready to deliver", { exact: true })
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
