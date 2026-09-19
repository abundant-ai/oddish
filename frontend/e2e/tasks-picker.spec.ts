import { expect, test } from "@playwright/test";

const storageKey = "oddish.tasks.selection.org-1";
const ids = Array.from({ length: 1201 }, (_, i) => `task-${i}`);

test.beforeEach(async ({ page }) => {
  await page.addInitScript(
    ({ storageKey, ids }) => {
      if (!localStorage.getItem(storageKey)) {
        localStorage.setItem(
          storageKey,
          JSON.stringify({
            v: 1,
            entries: ids.map((id) => [id, { cost: null, estimated: false }]),
          })
        );
      }
      const writes: string[] = [];
      Object.assign(window, { selectionWrites: writes });
      const remove = Storage.prototype.removeItem;
      const set = Storage.prototype.setItem;
      Storage.prototype.removeItem = function (key) {
        if (key === storageKey) writes.push("removed");
        return remove.call(this, key);
      };
      Storage.prototype.setItem = function (key, value) {
        if (key === storageKey) writes.push(value);
        return set.call(this, key, value);
      };
    },
    { storageKey, ids }
  );
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const data =
      path === "/api/customers"
        ? [{ id: "lab", name: "Lab" }]
        : path === "/api/deliveries"
          ? [
              {
                id: "existing",
                name: "Existing",
                status: "active",
                task_count: 0,
              },
            ]
          : path.endsWith("/facets")
            ? { delivery_customers: ["Mapped Lab", "Legacy Lab"], categories: ["coding"], agent_models: [{ agent: "test-agent", model: "test-model" }] }
            : path.endsWith("/count")
              ? { total: 0 }
              : { items: [], total: 0, has_more: false };
    await route.fulfill({ json: data });
  });
  await page.goto("/picker");
  await expect(page.getByText("1,201 selected", { exact: true })).toBeVisible({ timeout: 30_000 });
});

test("restoration never removes stored picks, including Strict Mode and remount", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Remount picker" }).click();
  await expect(page.getByText("1,201 selected", { exact: true })).toBeVisible();
  const writes = await page.evaluate(
    () => (window as unknown as { selectionWrites: string[] }).selectionWrites
  );
  expect(writes).not.toContain("removed");
  for (const value of writes)
    expect(JSON.parse(value).entries).toHaveLength(1201);
});

test("rapid author and sort changes preserve each other and pending search", async ({
  page,
}) => {
  await page
    .getByRole("textbox", { name: "Search tasks", exact: true })
    .fill("some task");
  await page.evaluate(() => {
    const author = document.querySelector<HTMLSelectElement>(
      'select[aria-label="Author"]'
    )!;
    const sort = document.querySelector<HTMLSelectElement>(
      'select[aria-label="Sort tasks"]'
    )!;
    author.value = "me";
    author.dispatchEvent(new Event("change", { bubbles: true }));
    sort.value = "total_trials_desc";
    sort.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await expect
    .poll(() => new URL(page.url()).searchParams.get("q"))
    .toBe("some task");
  expect(new URL(page.url()).searchParams.get("author")).toBe("me");
  expect(new URL(page.url()).searchParams.get("sort")).toBe(
    "total_trials_desc"
  );
  await expect(
    page.getByRole("button", { name: "Ready to ship", exact: true })
  ).toHaveCount(0);
});

test("creation sends the entire selection once and keeps it on failure", async ({
  page,
}) => {
  const requests: string[][] = [];
  await page.route("**/api/deliveries", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    requests.push(route.request().postDataJSON().task_ids);
    await route.fulfill({
      status: 404,
      json: { detail: "tasks not found: task-1200" },
    });
  });
  await page.getByRole("button", { name: "Add 1,201 to delivery" }).click();
  await page.getByRole("menuitem", { name: "New delivery" }).click();
  await page.getByLabel("Name", { exact: true }).fill("Batch");
  await page.getByRole("combobox", { name: "Customer", exact: true }).click();
  await page.getByRole("option", { name: "Lab", exact: true }).click();
  await page.getByRole("button", { name: "Create and add 1,201" }).click();
  await expect(page.getByRole("alert")).toHaveText(
    "tasks not found: task-1200"
  );
  expect(requests).toEqual([ids]);
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByText("1,201 selected", { exact: true })).toBeVisible();
});

test("organization changes never overwrite either organization's picks", async ({
  page,
}) => {
  await page.evaluate(() =>
    localStorage.setItem(
      "oddish.tasks.selection.org-2",
      JSON.stringify({
        v: 1,
        entries: [["other-task", { cost: null, estimated: false }]],
      })
    )
  );
  await page.getByRole("button", { name: "Switch organization" }).click();
  await expect(page.getByText("1 selected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Switch organization" }).click();
  await expect(page.getByText("1,201 selected", { exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () =>
        JSON.parse(localStorage.getItem("oddish.tasks.selection.org-2")!)
          .entries
    )
  ).toEqual([["other-task", { cost: null, estimated: false }]]);
});

test("adding to an existing delivery uses one request for all picks", async ({
  page,
}) => {
  const requests: string[][] = [];
  await page.route("**/api/deliveries/existing/tasks", async (route) => {
    requests.push(route.request().postDataJSON().task_ids);
    await route.fulfill({ json: { added: ids.length } });
  });
  await page.getByRole("button", { name: "Add 1,201 to delivery" }).click();
  await page.getByRole("menuitem", { name: /Existing/ }).click();
  await expect(
    page.getByRole("button", { name: "Clear selection", exact: true })
  ).toHaveCount(0);
  expect(requests).toEqual([ids]);
});

test("filter presets and delivery context survive clearing filters", async ({
  page,
}) => {
  await page.goto("/picker?delivery=existing&not_delivered_to=Lab");
  await expect(
    page.getByRole("heading", { name: "Add tasks to Existing" })
  ).toBeVisible();
  await page.getByRole("button", { name: /^Filters/ }).click();
  await page.getByRole("textbox", { name: "Find a filter" }).fill("trial");
  await page
    .getByRole("combobox", { name: "Trial count", exact: true })
    .selectOption("5");
  await expect
    .poll(() => new URL(page.url()).searchParams.get("total_trials_min"))
    .toBe("5");
  await page.keyboard.press("Escape");
  await page
    .getByRole("button", { name: "Clear filters", exact: true })
    .first()
    .click();
  await expect
    .poll(() => new URL(page.url()).searchParams.get("total_trials_min"))
    .toBe(null);
  expect(new URL(page.url()).searchParams.get("delivery")).toBe("existing");
});

test("reviewing selection exposes picks outside the current empty results", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Review selection" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(
    page.getByRole("link", { name: "task-1200", exact: true })
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Remove", exact: true })
    .first()
    .click();
  await expect(
    page.getByRole("heading", { name: "1,200 selected tasks" })
  ).toBeVisible();
});

const task = {
  id: "visible-task",
  name: "Queue recovery",
  current_version: 3,
  current_version_id: "version-3",
  version_count: 3,
  qa_outcome: "accepted",
  total_trials: 40,
  completed_trials: 29,
  failed_trials: 1,
  reward_success: 29,
  reward_sum: 29,
  reward_total: 30,
  pass_count: 29,
  partial_count: 0,
  fail_count: 1,
  harness_count: 0,
  pending_count: 10,
  agent_count: 3,
  cost_usd: 4.5,
  cost_trial_count: 30,
  latest_trials: [],
  latest_trials_truncated: true,
  experiments: [],
  user_tags: [],
  deliveries: [],
};

test("delivery picker preserves trial counts and only adds to its named destination", async ({
  page,
}) => {
  await page.route("**/api/tasks/browse?**", (route) =>
    route.fulfill({
      json: { items: [task], offset: 0, limit: 24, has_more: false },
    })
  );
  await page.route("**/api/tasks/browse/count?**", (route) =>
    route.fulfill({ json: { total: 1 } })
  );
  let submitted: string[] = [];
  await page.route("**/api/deliveries/existing/tasks", (route) => {
    submitted = route.request().postDataJSON().task_ids;
    return route.fulfill({ json: { added: 1 } });
  });
  await page.goto("/picker?delivery=existing");
  await expect(
    page.getByRole("heading", { name: "Add tasks to Existing" })
  ).toBeVisible();
  await expect(page.getByText("29/40 completed")).toBeVisible();
  await expect(page.getByText("10 pending · 1 failed")).toBeVisible();
  await page
    .getByRole("button", { name: "Trial details", exact: true })
    .click();
  await expect(page.getByText("Avg score", { exact: true })).toBeVisible();
  await page
    .getByRole("checkbox", { name: "Select Queue recovery", exact: true })
    .first()
    .check();
  await page
    .getByRole("button", { name: "Add 1 to Existing", exact: true })
    .click();
  await expect(page).toHaveURL(/\/deliveries\/existing$/, { timeout: 30_000 });
  expect(submitted).toEqual(["visible-task"]);
});

test("numeric custom ranges apply once and reject inverted bounds", async ({
  page,
}) => {
  await page.getByRole("button", { name: /^Filters/ }).click();
  await page.getByRole("textbox", { name: "Find a filter" }).fill("median");
  await page
    .getByRole("combobox", { name: "Median steps", exact: true })
    .selectOption("custom");
  await page
    .getByRole("spinbutton", { name: "Median steps minimum" })
    .fill("100");
  await page
    .getByRole("spinbutton", { name: "Median steps maximum" })
    .fill("50");
  await expect(page.getByText("Minimum cannot exceed maximum")).toBeVisible();
  expect(new URL(page.url()).searchParams.get("steps_p50_min")).toBe(null);
  await page
    .getByRole("spinbutton", { name: "Median steps maximum" })
    .fill("200");
  await page.getByRole("button", { name: "Apply", exact: true }).click();
  await expect
    .poll(() => new URL(page.url()).searchParams.get("steps_p50_min"))
    .toBe("100");
  expect(new URL(page.url()).searchParams.get("steps_p50_max")).toBe("200");
});



test("sorting legacy only-mine links keeps the author filter", async ({page}) => {
  await page.goto("/picker?mine=only");
  await expect(page.getByText("1,201 selected", { exact: true })).toBeVisible();
  await page.getByRole("combobox", {name: "Sort tasks"}).selectOption("mine");
  await expect.poll(() => new URL(page.url()).searchParams.get("author")).toBe("me");
  await page.evaluate(() => {
    const sort = document.querySelector<HTMLSelectElement>('select[aria-label="Sort tasks"]')!;
    const author = document.querySelector<HTMLSelectElement>('select[aria-label="Author"]')!;
    sort.value = "recent"; sort.dispatchEvent(new Event("change", {bubbles: true}));
    author.value = "all"; author.dispatchEvent(new Event("change", {bubbles: true}));
  });
  await expect.poll(() => new URL(page.url()).searchParams.get("mine")).toBe("off");
  expect(new URL(page.url()).searchParams.has("author")).toBe(false);
});

for (const [filter, option, key, value] of [
  ["QA outcome", "Accepted", "qa_outcomes", "accepted"],
  ["Category", "coding", "categories", "coding"],
  ["Agent · Model", "test-model", "agent_models", "test-agent:test-model"],
]) {
  test(`nested ${filter} accepts pointer clicks above its parent`, async ({ page }) => {
    await page.getByRole("button", { name: /^Filters/ }).click();
    await page.getByRole("textbox", { name: "Find a filter" }).fill(filter);
    await page.getByRole("dialog", { name: "Task filters" }).getByRole("button", { name: "Any", exact: true }).click();
    await page.getByRole("checkbox", { name: option, exact: true }).click();
    await expect.poll(() => new URL(page.url()).searchParams.get(key)).toBe(value);
  });
}

test("advanced conditions accept pointer clicks above the filters panel", async ({ page }) => {
  await page.getByRole("button", { name: /^Filters/ }).click();
  await page.getByRole("textbox", { name: "Find a filter" }).fill("Match any");
  await page.getByRole("button", { name: "Add condition", exact: true }).click();
  await page.getByRole("menuitem", { name: "Trial count", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Task filters" }).getByText("Trial count", { exact: true })).toBeVisible();
});

test("empty customer list supports creation without losing delivery name or picks", async ({ page }) => {
  let customerCreated = false;
  await page.route("**/api/customers", async (route) => {
    if (route.request().method() === "POST") {
      expect(route.request().postDataJSON()).toEqual({ name: "New Lab" });
      customerCreated = true;
      return route.fulfill({ json: { id: "new-lab", name: "New Lab" } });
    }
    await route.fulfill({ json: [] });
  });
  let submitted: unknown;
  await page.route("**/api/deliveries", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    submitted = route.request().postDataJSON();
    await route.fulfill({ status: 400, json: { detail: "Keep dialog open for inspection" } });
  });
  await page.getByRole("button", { name: "Add 1,201 to delivery" }).click();
  await page.getByRole("menuitem", { name: "New delivery" }).click();
  await page.getByLabel("Name", { exact: true }).fill("Preserved batch");
  await expect(page.getByText("No customers yet.")).toBeVisible();
  await page.getByRole("combobox", { name: "Customer", exact: true }).click();
  await page.getByRole("option", { name: "New customer…", exact: true }).click();
  const customerDialog = page.getByRole("dialog", { name: "New customer", exact: true });
  await customerDialog.getByLabel("Name", { exact: true }).fill("New Lab");
  await customerDialog.getByRole("button", { name: "Create", exact: true }).click();
  await expect(customerDialog).toBeHidden();
  expect(customerCreated).toBe(true);
  await expect(page.getByLabel("Name", { exact: true })).toHaveValue("Preserved batch");
  await expect(page.getByRole("combobox", { name: "Customer", exact: true })).toHaveText("New Lab");
  await page.getByRole("button", { name: "Create and add 1,201" }).click();
  await expect(page.getByRole("alert")).toHaveText("Keep dialog open for inspection");
  expect(submitted).toEqual({ name: "Preserved batch", customer: "new-lab", task_ids: ids });
});

test("customer loading and failure offer a working retry", async ({ page }) => {
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  let fail = true;
  await page.route("**/api/customers", async (route) => {
    await held;
    await route.fulfill(fail ? { status: 500, json: { detail: "Unavailable" } } : { json: [{ id: "lab", name: "Lab" }] });
  });
  await page.getByRole("button", { name: "Add 1,201 to delivery" }).click();
  await page.getByRole("menuitem", { name: "New delivery" }).click();
  await expect(page.getByRole("combobox", { name: "Customer", exact: true })).toHaveText("Loading customers…");
  release();
  await expect(page.getByRole("alert")).toContainText("Could not load customers.");
  fail = false;
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByRole("alert")).toBeHidden();
  await page.getByRole("combobox", { name: "Customer", exact: true }).click();
  await page.getByRole("option", { name: "Lab", exact: true }).click();
  await expect(page.getByRole("combobox", { name: "Customer", exact: true })).toHaveText("Lab");
});


test("tags load only when their filter is revealed and reuse the result on reopen", async ({ page }) => {
  let tagRequests = 0;
  await page.route("**/api/tags", async (route) => {
    tagRequests++;
    await route.fulfill({ json: { items: [] } });
  });
  await page.getByRole("button", { name: /^Filters/ }).click();
  await expect(page.getByRole("textbox", { name: "Find a filter" })).toBeVisible();
  expect(tagRequests).toBe(0);
  await page.getByRole("textbox", { name: "Find a filter" }).fill("Tags");
  await expect.poll(() => tagRequests).toBe(1);
  await expect(page.getByRole("button", { name: "Has all", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: /^Filters/ }).click();
  await expect(page.getByRole("button", { name: "Has all", exact: true })).toBeVisible();
  expect(tagRequests).toBe(1);
});

for (const initialSearch of ["", "old search"]) {
  for (const source of ["toolbar", "empty results"]) {
    test(`${source} clear cancels pending search with ${initialSearch ? "committed" : "empty"} URL search`, async ({ page }) => {
      const params = new URLSearchParams({
        delivery: "existing",
        qa_outcomes: "rejected",
        sort: "total_trials_desc",
        mine: "off",
      });
      if (initialSearch) params.set("q", initialSearch);
      await page.goto(`/picker?${params}`);
      await expect(page.getByRole("heading", { name: "Add tasks to Existing" })).toBeVisible();
      await expect(page.getByText("No tasks match the current filters.")).toBeVisible();
      await page.clock.install();
      await page.clock.pauseAt(new Date());
      const search = page.getByRole("textbox", { name: "Search tasks", exact: true });
      await search.fill("pending search");
      const clears = page.getByRole("button", { name: "Clear filters", exact: true });
      await clears.nth(source === "toolbar" ? 0 : 1).click();
      await expect(search).toHaveValue("");
      await page.clock.runFor(1000);
      await expect(search).toHaveValue("");
      const cleared = new URL(page.url()).searchParams;
      expect(cleared.has("q")).toBe(false);
      expect(cleared.has("query")).toBe(false);
      expect(cleared.has("qa_outcomes")).toBe(false);
      expect(cleared.get("delivery")).toBe("existing");
      expect(cleared.get("sort")).toBe("total_trials_desc");
      expect(cleared.get("mine")).toBe("off");
      // A reset cancels the old edit, but must not disable subsequent searches.
      await search.fill("new search");
      await page.clock.runFor(300);
      await expect.poll(() => new URL(page.url()).searchParams.get("q")).toBe("new search");
    });
  }
}


for (const destination of ["", "?delivery=existing"]) {
  test(`lab filters are visible and recorded destinations appear in ${destination ? "delivery picking" : "task cards"}`, async ({ page }) => {
    await page.route("**/api/tasks/browse?*", (route) => route.fulfill({
      json: { items: [{ ...task, deliveries: [
        { customer: "Mapped Lab", batch: "September batch", date: "2026-09-01", source: "history" },
        { customer: "Legacy Lab", batch: "August batch", date: "2026-08-01", source: "history" },
      ] }], total: 1, has_more: false },
    }));
    await page.goto(`/picker${destination}`);
    await expect(page.getByText("Mapped Lab", { exact: true })).toBeVisible();
    await expect(page.getByText("Legacy Lab", { exact: true })).toHaveAttribute("title", "August batch · 2026-08-01");
    await page.getByRole("button", { name: /^Filters/ }).click();
    await page.getByRole("group", { name: "Sent to lab", exact: true }).getByRole("button", { name: "Any", exact: true }).click();
    await page.getByRole("checkbox", { name: "Mapped Lab", exact: true }).click();
    await expect.poll(() => new URL(page.url()).searchParams.get("delivered_to")).toBe("Mapped Lab");
    await page.getByRole("checkbox", { name: "Mapped Lab", exact: true }).press("Escape");
    await expect(page.getByRole("checkbox", { name: "Mapped Lab", exact: true })).toBeHidden();
    await page.getByRole("group", { name: "Not sent to lab", exact: true }).getByRole("button", { name: "Any", exact: true }).click();
    await page.getByRole("checkbox", { name: "Legacy Lab", exact: true }).click();
    await expect.poll(() => new URL(page.url()).searchParams.get("not_delivered_to")).toBe("Legacy Lab");
    expect(new URL(page.url()).searchParams.get("delivered_to")).toBe("Mapped Lab");
  });
}
