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
        "Good failure"
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

  for (const count of [25, 250]) {
    test(`${count} tasks use page scrolling without reloading results`, async ({
      page,
    }) => {
      await page.goto(`/experiments/review-demo?scenario=scroll-${count}`);
      const first = page.getByRole("button", {
        name: "kafka-consumer-offset-recovery-after-broker-restart-001",
        exact: true,
      });
      await expect(first).toBeVisible();
      const requests: string[] = [];
      page.on("request", (request) => {
        if (/\/(results|tasks)(?:[/?]|$)/.test(request.url()))
          requests.push(request.url());
      });
      expect(
        await page.evaluate(
          () =>
            Array.from(document.querySelectorAll("main div")).filter(
              (element) =>
                element.scrollHeight > element.clientHeight + 2 &&
                ["auto", "scroll"].includes(getComputedStyle(element).overflowY)
            ).length
        )
      ).toBe(0);
      if (count === 25)
        await expect(page.locator("tbody tr[data-index]")).toHaveCount(25);
      await page.evaluate(() =>
        window.scrollTo(0, document.documentElement.scrollHeight)
      );
      const last = page.getByRole("button", {
        name: `kafka-consumer-offset-recovery-after-broker-restart-${String(count).padStart(3, "0")}`,
        exact: true,
      });
      await expect(last).toBeVisible();
      expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(1000);
      await page.evaluate(() => window.scrollTo(0, 0));
      await expect(first).toBeVisible();
      expect(requests).toEqual([]);
    });
  }

  test("task names and result badges do not repeat themselves on hover", async ({
    page,
  }) => {
    await page.goto("/experiments/review-demo");
    const name = page.getByRole("button", { name: "Task A", exact: true });
    await name.hover();
    await expect(page.getByRole("tooltip")).toHaveCount(0);
    await page
      .getByRole("button", { name: "Open findings for Task A", exact: true })
      .hover();
    await expect(page.getByRole("tooltip")).toHaveCount(0);
    await page.getByRole("button", { name: "Pass", exact: true }).hover();
    await expect(page.getByRole("tooltip")).toHaveCount(0);
  });

  test("rejected rows show only the must-fix count, with the issue in details", async ({
    page,
  }) => {
    await page.goto("/experiments/review-demo?verdict=rejected");
    const row = page.getByRole("row").filter({
      has: page.getByRole("button", { name: "Task A", exact: true }),
    });
    const badge = row.getByRole("button", {
      name: "Open findings for Task A",
      exact: true,
    });
    await expect(badge).toHaveText("1 Must fix");
    await expect(row).not.toContainText(
      "The verifier accepts an empty answer."
    );
    await expect(row.getByText(/Task checks:|Run reviews:/)).toHaveCount(0);
    await badge.click();
    await expect(
      page.getByRole("heading", {
        name: "The verifier accepts an empty answer.",
        exact: true,
      })
    ).toBeVisible();
  });

  test("background updates retain scores, selection, and scroll position", async ({
    page,
  }) => {
    await page.goto("/experiments/review-demo?scenario=refresh");
    await expect(page.getByText("40.0%", { exact: true })).toBeVisible();
    const row = page.getByRole("row").filter({
      has: page.getByRole("button", { name: "Task A", exact: true }),
    });
    await row.getByRole("checkbox").check();
    await page
      .getByRole("button", { name: "Toggle background refresh" })
      .click();
    await expect(page.getByText("40.0%", { exact: true })).toBeVisible();
    await expect(row.getByRole("checkbox")).toBeChecked();
    await expect(
      page.getByText(
        /All results loaded|Downloading results|Refreshing results|Post-trial/
      )
    ).toHaveCount(0);
  });

  test("review coverage separates unusable evaluations and runs from other experiments", async ({
    page,
  }) => {
    const original = tasks[0].trials![0];
    await page.route("**/api/tasks/task-a/trials?**", (route) =>
      route.fulfill({
        json: [
          original,
          {
            ...original,
            id: "foreign",
            experiment_id: "other-experiment",
            analysis: { classification: "HARNESS_ERROR" },
          },
        ],
      })
    );
    await page.goto("/experiments/review-demo?task=task-a");
    await expect(
      page.getByText("This experiment: 1/1 evaluated · v7", { exact: true })
    ).toBeVisible();
    await expect(
      page.getByText("0/1 evaluated · 1 couldn’t be evaluated", { exact: true })
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: "Experiment other-ex" })
    ).toHaveAttribute("href", "/experiments/other-experiment");
    await expect(
      page.getByText("1 good failure", { exact: true })
    ).toBeVisible();
    await expect(
      page.getByText("1 run couldn’t be evaluated", { exact: true })
    ).toHaveCount(0);
    await expect(
      page.getByText(
        /Missing access:|Inspects task instructions|fair agent failure does not/
      )
    ).toHaveCount(0);
  });

  for (const origin of ["this experiment", "another experiment"]) {
    test(`clean source checks retain fixes from ${origin}`, async ({
      page,
    }) => {
      const original = tasks[0].trials![0];
      const finding = {
        ...records[0].finding!,
        id: "run-finding",
        source: "post_trial",
        title: "A failed verifier process still awards credit.",
      };
      const reviewed = {
        ...original,
        analysis: { ...original.analysis!, action_items: [finding] },
      };
      await page.route(/\/api\/tasks\/task-a\/panel(?:\?|$)/, async (route) => {
        const response = await route.fetch();
        const panel = await response.json();
        panel.version.pre_trial_status = "success";
        panel.version.pre_trial_findings = [];
        panel.version.retained_findings = [];
        await route.fulfill({ json: panel });
      });
      await page.route("**/api/tasks/task-a/trials?**", (route) =>
        route.fulfill({
          json:
            origin === "this experiment"
              ? [reviewed]
              : [
                  original,
                  {
                    ...reviewed,
                    id: "foreign-finding",
                    experiment_id: "another-experiment",
                  },
                ],
        })
      );
      await page.goto("/experiments/review-demo?task=task-a");
      await expect(
        page.getByRole("heading", { name: finding.title, exact: true })
      ).toBeVisible();
      const checks = page
        .getByRole("heading", { name: "Task checks", exact: true })
        .locator("..");
      await expect(
        checks.getByText("1 Must fix", { exact: true })
      ).toBeVisible();
      await expect(
        checks.getByText("No required fixes", { exact: true })
      ).toHaveCount(0);
      await expect(
        page.getByText("1 good failure", { exact: true })
      ).toBeVisible();
      if (origin === "another experiment")
        await expect(
          page.getByRole("link", { name: "Experiment another-", exact: true })
        ).toBeVisible();
    });
  }

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
      name: "The verifier accepts an empty answer.",
      exact: true,
    });
    await expect(blocker).toBeVisible();
    await expect(
      page.getByRole("link", { name: "Signed off version", exact: true })
    ).toHaveCount(0);
    await blocker.click();
    await expect(page.locator('[data-finding="empty-answer"]')).toBeVisible();
    await expect(page.getByText("GOOD FAILURE", { exact: true })).toBeVisible();
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
    const rejectedRow = page
      .getByRole("row")
      .filter({ has: page.getByText("Task A", { exact: true }) });
    await expect(
      rejectedRow.getByText(/Task checks:|Run reviews:/)
    ).toHaveCount(0);

    await expect(
      rejectedRow.getByRole("button", {
        name: "Open findings for Task A",
        exact: true,
      })
    ).toHaveText("1 Must fix");
    await expect(
      rejectedRow.getByText("1 Must fix", { exact: true })
    ).toHaveCount(1);
    await page
      .getByRole("button", { name: "2 Review error", exact: true })
      .click();
    await expect(page).toHaveURL(/verdict=failed/);
    await expect(
      page.getByRole("button", { name: "Task A", exact: true })
    ).toHaveCount(0);
    await expect(
      page.getByText("Execution could not run", { exact: true })
    ).toBeVisible();
    await page.getByRole("button", { name: "1 Rejected", exact: true }).click();
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
      name: "Check task v7",
      exact: true,
    });
    await expect(source).toBeVisible();
    expect(writes).toEqual([]);
    await source.click();
    await expect.poll(() => writes).toEqual(["/api/tasks/task-a/qa/pre-trial"]);
    await page
      .getByRole("button", { name: "Review runs for v7", exact: true })
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
      .getByRole("button", { name: "4 In progress", exact: true })
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
    await expect(
      page.getByRole("button", { name: "0 No current result", exact: true })
    ).toHaveCount(0);
    await page.getByRole("button", { name: "3 Accepted", exact: true }).click();
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
        name: "4 In progress",
        exact: true,
      })
    ).toHaveAttribute("aria-pressed", "true");
  });

  test("an entirely unreviewed task set still has a matching review count", async ({
    page,
  }) => {
    await page.goto("/experiments/review-demo?scenario=unreviewed-only");
    await page
      .getByRole("button", { name: "2 No current result", exact: true })
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
        name: "0 In progress",
        exact: true,
      })
    ).toHaveCount(0);
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
      page.getByText("No result for this version", { exact: true }).first()
    ).toBeVisible();
    await page.getByRole("button", { name: "Close", exact: true }).click();
    await page
      .getByRole("button", { name: "v8 · default", exact: true })
      .click();
    await page.getByRole("menuitem", { name: /^v7/ }).click();
    await expect(page).toHaveURL(/version=(?:stale-review-v)?7/);
    await expect(
      page.getByText("Accepted", { exact: true }).first()
    ).toBeVisible();
    await page.goBack();
    await expect(page).toHaveURL(/version=8/);
    await expect(
      page.getByText("No result for this version", { exact: true }).first()
    ).toBeVisible();
    await page.goForward();
    await expect(
      page.getByText("Accepted", { exact: true }).first()
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
    await expect(row.getByText("Accepted", { exact: true })).toBeVisible();
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
