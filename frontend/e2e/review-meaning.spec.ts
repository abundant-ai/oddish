import { expect, test } from "@playwright/test";
import {
  taskReviewStatus,
  findingHref,
  EXECUTION_LABELS,
} from "../src/lib/review";
import { board, tasks, records } from "./review-app/records";

for (const record of records) {
  test(`review meaning: ${record.name}`, () => {
    const task = tasks.find((task) => task.id === record.id)!;
    expect(taskReviewStatus(task)).toBe(record.qa_status);
    if (record.id === "task-a") {
      expect(EXECUTION_LABELS[task.trials![0].analysis!.classification]).toBe(
        "Fair agent failure"
      );
      expect(task.verdict?.is_good).toBe(false);
    }
  });
}

test("failed replacement cannot publish cached acceptance", () => {
  expect(
    taskReviewStatus({
      ...tasks[0],
      verdict_status: "failed",
      verdict: { is_good: true, confidence: null },
    })
  ).toBe("error");
});

test.describe("real components with local fixture API", () => {
  test.skip(
    process.env.E2E_REVIEW_FIXTURES !== "1",
    "Run with playwright.review.config.ts and the isolated fixture app"
  );

  test("blocker opens exact finding, file, line and preserves browser history", async ({
    page,
    context,
  }) => {
    const writes: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/") && request.method() !== "GET")
        writes.push(request.url());
    });
    await page.goto("/deliveries/review-demo");
    const blocker = page.getByRole("link", {
      name: "Task A · v7 · The verifier accepts an empty answer.",
      exact: true,
    });
    await expect(blocker).toBeVisible();
    await expect(
      page.getByRole("link", { name: "Signed off version", exact: true })
    ).toHaveCount(0);
    await blocker.click();
    await expect(page.locator('[data-finding="empty-answer"]')).toBeVisible();
    await expect(
      page.getByText("FAIR AGENT FAILURE", { exact: true })
    ).toBeVisible();
    await expect(page).toHaveURL(/version=7/);
    await expect(page).toHaveURL(/taskFile=tests%2Ftest.sh/);
    const evidenceLink = page.getByRole("link", {
      name: "Open tests/test.sh:7",
      exact: true,
    });
    await evidenceLink.click();
    await expect(page.getByText("exit 0", { exact: true })).toBeVisible();
    await expect(page).toHaveURL(/taskLines=L?7/);
    const shared = page.url();
    const teammate = await context.newPage();
    await teammate.goto(shared);
    await expect(teammate.getByText("exit 0", { exact: true })).toBeVisible();
    await expect(teammate).toHaveURL(/version=7/);
    await teammate.close();
    await page.goBack();
    await expect(page.locator('[data-finding="empty-answer"]')).toBeVisible();
    await page.goForward();
    await expect(page.getByText("exit 0", { exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByText("exit 0", { exact: true })).toBeVisible();
    expect(writes).toEqual([]);
  });

  test("verdict selections restore URL and results on Back, Forward and reload", async ({
    page,
  }) => {
    const writes: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/") && request.method() !== "GET")
        writes.push(request.url());
    });
    await page.goto("/experiments/review-demo");
    await page
      .getByRole("button", { name: "2 Review could not complete", exact: true })
      .click();
    await expect(page).toHaveURL(/verdict=failed/);
    await expect(
      page.getByRole("button", { name: "Task A", exact: true })
    ).toHaveCount(0);
    await expect(
      page.getByText("Execution could not run", { exact: true })
    ).toBeVisible();
    await page
      .getByRole("button", { name: "1 Blocking defects found", exact: true })
      .click();
    await expect(page).toHaveURL(/verdict=rejected/);
    await expect(page.getByText("Task A", { exact: true })).toBeVisible();
    await page.goBack();
    await expect(page).toHaveURL(/verdict=failed/);
    await expect(
      page.getByText("Execution could not run", { exact: true })
    ).toBeVisible();
    await page.goForward();
    await expect(page).toHaveURL(/verdict=rejected/);
    await page.reload();
    await expect(page.getByText("Task A", { exact: true })).toBeVisible();
    expect(writes).toEqual([]);
  });

  test("missing version, finding and file never substitute current evidence", async ({
    page,
  }) => {
    await page.goto(
      "/tasks/task-a?version=404&drawer=task&finding=empty-answer"
    );
    await expect(
      page.getByText("Historical version unavailable", { exact: true })
    ).toBeVisible();
    await expect(page).toHaveURL(/version=404/);
    await expect(
      page.getByRole("link", { name: "Open the current version" })
    ).toBeVisible();
    await page.goto(
      "/tasks/task-a?version=7&drawer=task&finding=removed-finding&taskPane=overview"
    );
    await expect(
      page
        .getByRole("alert")
        .filter({ hasText: "Finding removed-finding is unavailable for v7" })
    ).toBeVisible();
    await page.goto(
      "/tasks/task-a?version=7&drawer=task&taskPane=file&taskFile=removed.sh&taskLines=7"
    );
    await expect(
      page.getByText(/removed.sh on v7 is unavailable/)
    ).toBeVisible();
    await expect(page).toHaveURL(/taskFile=removed.sh/);
    await expect(page.getByText("exit 0", { exact: true })).toHaveCount(0);
  });

  test("review reruns use existing scoped operations only after explicit clicks", async ({
    page,
  }) => {
    const writes: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/") && request.method() !== "GET")
        writes.push(new URL(request.url()).pathname);
    });
    await page.goto(findingHref("task-a", 7, records[0].finding!));
    const source = page.getByRole("button", {
      name: "Rerun source review",
      exact: true,
    });
    await expect(source).toBeVisible();
    expect(writes).toEqual([]);
    await source.click();
    await expect.poll(() => writes).toEqual(["/api/tasks/task-a/qa/pre-trial"]);
    await page
      .getByRole("button", { name: "Rerun execution review", exact: true })
      .last()
      .click();
    await expect
      .poll(() => writes)
      .toEqual([
        "/api/tasks/task-a/qa/pre-trial",
        "/api/tasks/task-a/qa/retry",
      ]);
  });

  test("delivery inventory and focused row restore from URL", async ({
    page,
  }) => {
    await page.goto("/deliveries/review-demo?filter=all&task=signed-off");
    await expect(
      page.getByRole("link", { name: "Signed off version", exact: true })
    ).toBeVisible();
    await expect(
      page.getByText("Signed off on v1", { exact: true })
    ).toBeVisible();
    await page.getByRole("combobox").filter({ hasText: "All tasks" }).click();
    await page
      .getByRole("option", {
        name: "Blockers and outstanding sign-offs",
        exact: true,
      })
      .click();
    await expect(
      page.getByRole("link", { name: "Signed off version", exact: true })
    ).toHaveCount(0);
    await page.goBack();
    await expect(page).toHaveURL(/filter=all/);
    await expect(
      page.getByText("Signed off on v1", { exact: true })
    ).toBeVisible();
    await page.reload();
    await expect(
      page.getByText("Signed off on v1", { exact: true })
    ).toBeVisible();
  });

  for (const query of [
    "task=signed-off",
    "task=Signed+off+version",
    "task=signed-off&filter=blocked&qa=never&owner=mine&group=owner&page=9",
  ]) {
    test(`delivery focus survives conflicting filters: ${query}`, async ({
      page,
    }) => {
      await page.goto(`/deliveries/review-demo?${query}`);
      await expect(
        page.getByText("Signed off on v1", { exact: true })
      ).toBeVisible();
      await expect(
        page.getByText(/linked task is shown even though/)
      ).toBeVisible();
      await page.reload();
      await expect(
        page.getByText("Signed off on v1", { exact: true })
      ).toBeVisible();
    });
  }

  test("review counts match filtered rows when trials carry newer review progress", async ({
    page,
  }) => {
    await page.goto("/experiments/review-demo?scenario=live-review");
    await page
      .getByRole("button", { name: "4 Review queued / running", exact: true })
      .click();
    for (const name of [
      "Unreviewed version",
      "New version with older review",
      "Queued review",
      "Running review",
    ])
      await expect(
        page.getByRole("button", { name, exact: true })
      ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Task A", exact: true })
    ).toHaveCount(0);
    await page
      .getByRole("button", { name: "0 No current review", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "Unreviewed version", exact: true })
    ).toHaveCount(0);
    await page.goBack();
    await expect(
      page.getByRole("button", { name: "Unreviewed version", exact: true })
    ).toBeVisible();
    await page.reload();
    await expect(
      page.getByRole("button", {
        name: "4 Review queued / running",
        exact: true,
      })
    ).toHaveAttribute("aria-pressed", "true");
  });

  test("an entirely unreviewed task set still has a matching review count", async ({
    page,
  }) => {
    await page.goto("/experiments/review-demo?scenario=unreviewed-only");
    await page
      .getByRole("button", { name: "2 No current review", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "Unreviewed version", exact: true })
    ).toBeVisible();
    await expect(
      page.getByRole("button", {
        name: "New version with older review",
        exact: true,
      })
    ).toBeVisible();
    await expect(
      page.getByRole("button", {
        name: "0 Review queued / running",
        exact: true,
      })
    ).toBeVisible();
  });

  test("opening an accepted task keeps drawer navigation inside accepted results", async ({
    page,
  }) => {
    await page.goto("/experiments/review-demo?verdict=accepted");
    await page
      .getByRole("button", { name: "Fair agent failure", exact: true })
      .click();
    await expect(page.getByLabel(/Task \d of 3/)).toBeVisible();
    const next = page.getByRole("button", { name: "Next task", exact: true });
    await next.click();
    await expect(page.getByLabel(/Task \d of 3/)).toBeVisible();
    await expect(page).toHaveURL(/verdict=accepted/);
    expect(["fair-failure", "awaiting-signoff", "signed-off"]).toContain(
      new URL(page.url()).searchParams.get("task")
    );
  });

  test("Back restores the complete finding address across task versions", async ({
    page,
  }) => {
    await page.goto(
      "/tasks/task-a?version=7&drawer=task&finding=empty-answer&taskPane=file&taskFile=tests%2Ftest.sh&taskLines=L7"
    );
    await expect(page.getByText("exit 0", { exact: true })).toBeVisible();
    // Add another version address without remounting the task page, then let
    // its normal popstate listeners load that version and restore its pane.
    await page.evaluate(() => {
      const url = new URL(window.location.href);
      url.searchParams.set("version", "task-a-v6");
      url.searchParams.delete("taskLines");
      window.history.pushState(null, "", url);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await expect(page).toHaveURL(/version=task-a-v6/);
    await expect(page).not.toHaveURL(/taskLines=/);
    await page.goBack();
    await expect(page.getByText("exit 0", { exact: true })).toBeVisible();
    await expect(page).toHaveURL(/version=7/);
    await expect(page).toHaveURL(/taskLines=L7/);
    await page.goForward();
    await expect(page).toHaveURL(/version=task-a-v6/);
    await expect(page).not.toHaveURL(/taskLines=/);
    await page.goBack();
    await page.reload();
    await expect(page.getByText("exit 0", { exact: true })).toBeVisible();
    await expect(page).toHaveURL(/taskLines=L7/);
    await page
      .locator("[data-column-number]")
      .filter({ hasText: /^3$/ })
      .click();
    await expect(page).toHaveURL(/taskLines=L3(?:&|$)/);
    await page
      .locator("[data-column-number]")
      .filter({ hasText: /^5$/ })
      .click({ modifiers: ["Shift"] });
    await expect(page).toHaveURL(/taskLines=L3-L5(?:&|$)/);
  });

  test("new version keeps older review available without claiming current coverage", async ({
    page,
  }) => {
    await page.goto(
      "/tasks/stale-review?version=8&drawer=task&taskPane=overview"
    );
    await expect(
      page.getByText("Review outdated", { exact: true }).first()
    ).toBeVisible();
    await page.getByRole("button", { name: "Close", exact: true }).click();
    await page
      .getByRole("button", { name: "v8 · default", exact: true })
      .click();
    await page.getByRole("menuitem", { name: /^v7/ }).click();
    await expect(page).toHaveURL(/version=(?:stale-review-v)?7/);
    await expect(
      page.getByText("No blocking defects found", { exact: true }).first()
    ).toBeVisible();
    await page.goBack();
    await expect(page).toHaveURL(/version=8/);
    await expect(
      page.getByText("Review outdated", { exact: true }).first()
    ).toBeVisible();
    await page.goForward();
    await expect(
      page.getByText("No blocking defects found", { exact: true }).first()
    ).toBeVisible();
  });

  test("favorable automated review remains awaiting explicit version sign-off", async ({
    page,
  }) => {
    let signed = false;
    const writes: unknown[] = [];
    await page.route("**/api/deliveries/review-demo", async (route) => {
      const next = structuredClone(board);
      if (signed) {
        const task = next.tasks.find(
          (task) => task.task_id === "awaiting-signoff"
        )!;
        task.ready = true;
        task.checks.find((check) => check.key === "signoff")!.status = "pass";
        task.checks.find((check) => check.key === "signoff")!.detail =
          "Signed off on v1";
      }
      await route.fulfill({ json: next });
    });
    await page.route("**/api/deliveries/review-demo/checks", async (route) => {
      writes.push(route.request().postDataJSON());
      signed = true;
      await route.fulfill({ json: {} });
    });
    await page.goto("/deliveries/review-demo");
    const row = page.getByRole("row").filter({
      has: page.getByRole("link", {
        name: "Completed review awaiting sign-off",
        exact: true,
      }),
    });
    await expect(
      row.getByText("No blocking defects found", { exact: true })
    ).toBeVisible();
    await row
      .getByRole("button", { name: "Awaiting sign-off", exact: true })
      .click();
    await expect(
      page.getByText("Awaiting sign-off on v1", { exact: true })
    ).toBeVisible();
    expect(writes).toEqual([]);
    await page
      .getByRole("checkbox", { name: "Signed off", exact: true })
      .click();
    await expect
      .poll(() => writes)
      .toEqual([
        {
          check_key: "signoff",
          delivery_task_id: "delivery-awaiting-signoff",
          checked: true,
          expected_version_id: "awaiting-signoff-v1",
        },
      ]);
    await expect(row).toBeVisible();
    await expect(
      page.getByText(/linked task is shown even though/)
    ).toBeVisible();
    await page.goto("/deliveries/review-demo?filter=all&task=awaiting-signoff");
    await expect(
      page.getByText("Signed off on v1", { exact: true })
    ).toBeVisible();
  });
});
