import { expect, test } from "@playwright/test";
import { DEFAULT_TRIAL_DRAWER_LAYOUT as defaults } from "../src/lib/user-ui-layout";

for (const target of ["taskFile=tests%2Ftest.sh", "taskPane=file"]) {
  test(`experiment ${target} reveals task content across a delayed preference read`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1600, height: 1000 });
    let releaseRead!: () => void;
    const readGate = new Promise<void>((resolve) => {
      releaseRead = resolve;
    });
    let readStarted = false;
    let saved = { ...defaults, preferredWidthPx: 1234, showTask: false };
    const writes: (typeof defaults)[] = [];
    await page.route(
      "**/api/users/me/ui-layouts/experiment.trial-drawer",
      async (route) => {
        if (route.request().method() === "PUT") {
          saved = route.request().postDataJSON();
          writes.push(saved);
        } else {
          readStarted = true;
          await readGate;
        }
        await route.fulfill({ json: saved });
      }
    );
    await page.goto(
      `/experiments/review-demo?scenario=layout-deep-link&task=task-a&trial=task-a-trial&${target}`
    );
    const hideTask = page.getByRole("button", {
      name: "Hide task",
      exact: true,
    });
    await expect(hideTask).toBeVisible();
    await expect.poll(() => readStarted).toBe(true);
    releaseRead();
    const drawer = page.locator('[data-slot="resizable-drawer"]');
    await expect
      .poll(() =>
        drawer.evaluate((element) => element.getBoundingClientRect().width)
      )
      .toBe(1234);
    await expect(hideTask).toBeVisible();
    await expect(page.locator('[data-panel-id="task-pane"]')).toBeVisible();
    if (target.startsWith("taskFile")) {
      await expect(
        page.getByText("# verifier fixture", { exact: false })
      ).toBeVisible();
    }
    expect(writes).toHaveLength(0);
    expect(saved.showTask).toBe(false);

    // Ordinary navigation clears the link's temporary reveal.
    await page.getByRole("button", { name: "Close", exact: true }).click();
    await page
      .getByRole("row")
      .filter({
        has: page.getByRole("button", { name: "Task A", exact: true }),
      })
      .getByRole("button", { name: "Trial 1 Fail", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "Show task", exact: true })
    ).toBeVisible();
    expect(writes).toHaveLength(0);
    await page.goto(
      `/experiments/review-demo?scenario=layout-deep-link&task=task-a&trial=task-a-trial&${target}`
    );
    await expect(hideTask).toBeVisible();

    // A visibility gesture adopts the displayed pane before hiding trials,
    // so the persisted pair can never become { showTask: false, showTrial: false }.
    await page
      .getByRole("button", { name: "Hide trials", exact: true })
      .click();
    await expect.poll(() => saved.showTrial).toBe(false);
    expect(saved.showTask).toBe(true);
    await page
      .getByRole("button", { name: "Show trials", exact: true })
      .click();
    await hideTask.click();
    await expect.poll(() => saved.showTask).toBe(false);
    await expect(
      page.getByRole("button", { name: "Show task", exact: true })
    ).toBeVisible();
  });
}

test("drawer gestures survive reload, account switches and a smaller screen", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1600, height: 1000 });
  let account = "alice-a";
  const saved = new Map<string, typeof defaults>([
    [account, { ...defaults, preferredWidthPx: 1200, taskPanePercent: 60 }],
  ]);
  const writes: (typeof defaults)[] = [];
  await page.route(
    "**/api/users/me/ui-layouts/experiment.trial-drawer",
    async (route) => {
      if (route.request().method() === "PUT") {
        const value = route.request().postDataJSON();
        saved.set(account, value);
        writes.push(value);
      }
      await route.fulfill({ json: saved.get(account) ?? defaults });
    }
  );
  await page.goto("/layouts");
  const drawer = page.locator('[data-slot="resizable-drawer"]');
  const width = () =>
    drawer.evaluate((element) => element.getBoundingClientRect().width);
  await expect.poll(width).toBe(1200);
  await expect(page.getByTestId("save-status")).toHaveText("ready");
  expect(writes).toHaveLength(0);

  // Drag the outer edge 100px to the right (narrower).
  // Hover waits for the drawer's opening animation before grabbing its edge.
  const edge = drawer.locator(".cursor-ew-resize");
  await edge.hover({ position: { x: 1, y: 294 } });
  const edgeBounds = await edge.boundingBox();
  await page.mouse.down();
  await page.mouse.move(edgeBounds!.x + 101, edgeBounds!.y + 294, { steps: 8 });
  await page.mouse.up();
  await expect.poll(() => saved.get(account)?.preferredWidthPx).toBe(1100);

  const divider = page.locator("[data-panel-resize-handle-id]");
  await divider.hover();
  const bounds = await divider.boundingBox();
  const dividerX = bounds!.x + bounds!.width / 2;
  await page.mouse.move(dividerX, bounds!.y + 300);
  await page.mouse.down();
  await page.mouse.move(dividerX - 110, bounds!.y + 300, { steps: 8 });
  await page.mouse.up();
  await expect
    .poll(() => saved.get(account)!.taskPanePercent)
    .toBeCloseTo(50, 0);
  await divider.focus();
  await page.keyboard.press("ArrowRight");
  await expect
    .poll(() => saved.get(account)!.taskPanePercent)
    .toBeGreaterThan(50);
  const chosenRatio = saved.get(account)!.taskPanePercent;

  // Collapsing by drag must not replace the remembered expanded ratio.
  const collapseBounds = await divider.boundingBox();
  await page.mouse.move(collapseBounds!.x, collapseBounds!.y + 300);
  await page.mouse.down();
  await page.mouse.move(502, collapseBounds!.y + 300, { steps: 8 });
  await page.mouse.up();
  await expect(page.locator('[data-panel-id="task-pane"]')).toHaveAttribute(
    "data-panel-size",
    "0.0"
  );
  expect(saved.get(account)!.taskPanePercent).toBe(chosenRatio);
  await page.reload();
  await expect
    .poll(async () =>
      Number(
        await page
          .locator('[data-panel-id="task-pane"]')
          .getAttribute("data-panel-size")
      )
    )
    .toBeCloseTo(chosenRatio, 0);

  await page.getByRole("button", { name: "Expand to full screen" }).click();
  await expect.poll(width).toBe(1600);
  await expect.poll(() => saved.get(account)!.maximized).toBe(true);
  await page.reload();
  await expect.poll(width).toBe(1600);
  await page.getByRole("button", { name: "Restore width" }).click();
  await expect.poll(width).toBe(1100);

  await page.getByRole("button", { name: "Hide task", exact: true }).click();
  await expect.poll(() => saved.get(account)!.showTask).toBe(false);
  await page.reload();
  await expect(page.getByText("Task definition contents")).toHaveCount(0);
  await page.getByRole("button", { name: "Show task", exact: true }).click();
  await expect
    .poll(async () =>
      Number(
        await page
          .locator('[data-panel-id="task-pane"]')
          .getAttribute("data-panel-size")
      )
    )
    .toBeCloseTo(chosenRatio, 0);

  account = "bob-a";
  await page.getByRole("button", { name: "Bob A" }).click();
  await expect.poll(width).toBe(1500);
  await expect(page.getByTestId("save-status")).toHaveText("ready");
  account = "alice-b";
  await page.getByRole("button", { name: "Alice B" }).click();
  await expect.poll(width).toBe(1500);
  await expect(page.getByTestId("save-status")).toHaveText("ready");
  account = "alice-a";
  await page.getByRole("button", { name: "Alice A" }).click();
  await expect.poll(width).toBe(1100);

  const beforeResize = writes.length;
  await page.setViewportSize({ width: 800, height: 900 });
  await expect.poll(width).toBe(800);
  await page.waitForTimeout(600);
  expect(writes).toHaveLength(beforeResize);
  expect(saved.get(account)!.preferredWidthPx).toBe(1100);
  await page.setViewportSize({ width: 1600, height: 1000 });
  await expect.poll(width).toBe(1100);
});

test("save failure leaves the drawer usable and Retry persists the latest choice", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1600, height: 1000 });
  let fail = true;
  let saved = { ...defaults, preferredWidthPx: 1200 };
  await page.route(
    "**/api/users/me/ui-layouts/experiment.trial-drawer",
    async (route) => {
      if (route.request().method() === "PUT") {
        if (fail) {
          await route.fulfill({ status: 503, json: { detail: "unavailable" } });
          return;
        }
        saved = route.request().postDataJSON();
      }
      await route.fulfill({ json: saved });
    }
  );
  await page.goto("/layouts");
  await expect(page.getByTestId("save-status")).toHaveText("ready");
  await page.getByRole("button", { name: "Hide task", exact: true }).click();
  await expect(
    page.getByText("Layout preferences could not sync.")
  ).toBeVisible();
  await expect(page.getByText("Task definition contents")).toHaveCount(0);
  expect(saved.showTask).toBe(true);
  fail = false;
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect.poll(() => saved.showTask).toBe(false);
  await expect(
    page.getByText("Layout preferences could not sync.")
  ).toHaveCount(0);
});
