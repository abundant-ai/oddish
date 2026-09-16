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

  for (const count of [25, 250]) {
    test(`${count} task headers follow page scrolling and horizontal columns`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: 800, height: 720 });
      await page.goto(`/experiments/review-demo?scenario=scroll-${count}`);
      await page.evaluate(() => {
        for (const [height, top] of [
          [28, 0],
          [56, 28],
        ]) {
          const bar = document.createElement("nav");
          bar.dataset.pageStickyHeader = "";
          Object.assign(bar.style, {
            position: "fixed",
            top: `${top}px`,
            height: `${height}px`,
            left: "0",
            right: "0",
            zIndex: "50",
            background: "white",
          });
          document.body.prepend(bar);
        }
      });
      const header = page.locator("thead");
      await expect(page.locator("tbody tr[data-index]").first()).toBeVisible();
      await page.evaluate(() => {
        const table = document.querySelector("table")!;
        window.scrollTo(
          0,
          table.getBoundingClientRect().top + window.scrollY + 500
        );
      });
      await expect
        .poll(() =>
          header.evaluate((element) => element.getBoundingClientRect().top)
        )
        .toBeCloseTo(84, 0);
      const agentHeader = header.locator("th").nth(1);
      const taskHeader = header.locator("th").first();
      const taskLeft = await taskHeader.evaluate(
        (element) => element.getBoundingClientRect().left
      );
      const before = await agentHeader.evaluate(
        (element) => element.getBoundingClientRect().left
      );
      const distance = await page.locator("table").evaluate((table) => {
        const wrapper = table.parentElement!;
        wrapper.scrollLeft = 120;
        return wrapper.scrollLeft;
      });
      expect(distance).toBeGreaterThan(0);
      await expect
        .poll(() =>
          agentHeader.evaluate(
            (element) => element.getBoundingClientRect().left
          )
        )
        .toBeCloseTo(before - distance, 0);
      await expect
        .poll(() =>
          taskHeader.evaluate((element) => element.getBoundingClientRect().left)
        )
        .toBeCloseTo(taskLeft, 0);
      await expect(
        header.getByRole("button", { name: "Toggle task sort" })
      ).toBeInViewport();
      await header.getByRole("button", { name: "Toggle task sort" }).click();
      await expect
        .poll(() =>
          header.evaluate((element) => element.getBoundingClientRect().top)
        )
        .toBeCloseTo(84, 0);
      await page.setViewportSize({ width: 1000, height: 720 });
      await expect
        .poll(() =>
          header.evaluate((element) => element.getBoundingClientRect().top)
        )
        .toBeCloseTo(84, 0);
      await page.evaluate(() => {
        const spacer = document.createElement("div");
        spacer.style.height = "1000px";
        document.body.append(spacer);
        const table = document.querySelector("table")!;
        window.scrollTo(
          0,
          table.getBoundingClientRect().bottom + window.scrollY - 30
        );
      });
      await expect
        .poll(() =>
          header.evaluate((element) => element.getBoundingClientRect().bottom)
        )
        .toBeCloseTo(30, 0);
      await page.evaluate(() => window.scrollTo(0, 0));
      await expect
        .poll(() =>
          header.evaluate((element) => element.getBoundingClientRect().top)
        )
        .toBeGreaterThan(0);
    });
  }

  test("crossing the row threshold keeps the table origin aligned while scrolled", async ({
    page,
  }) => {
    await page.goto("/experiments/review-demo?scenario=scroll-threshold");
    const first = page.getByRole("button", {
      name: "kafka-consumer-offset-recovery-after-broker-restart-001",
      exact: true,
    });
    await expect(page.locator("tbody tr[data-index]")).toHaveCount(199);
    await page.evaluate(() => {
      const body = document.querySelector("tbody")!;
      window.scrollTo(
        0,
        body.getBoundingClientRect().top + window.scrollY - 80
      );
    });
    await expect(first).toBeInViewport();
    const before = await first.evaluate(
      (element) => element.getBoundingClientRect().top
    );
    await expect
      .poll(async () => {
        await page.evaluate(() =>
          window.dispatchEvent(new Event("fixture-add-tasks"))
        );
        return page.locator("tbody tr[data-index]").count();
      })
      .toBeLessThan(199);
    await expect(first).toBeInViewport();
    expect(
      Math.abs(
        (await first.evaluate(
          (element) => element.getBoundingClientRect().top
        )) - before
      )
    ).toBeLessThan(3);
  });

  test("restoring a scrolled large table keeps its first row visible", async ({
    page,
  }) => {
    await page.goto("/experiments/review-demo?scenario=scroll-restored");
    const first = page.getByRole("button", {
      name: "kafka-consumer-offset-recovery-after-broker-restart-001",
      exact: true,
    });
    await expect(first).toBeAttached();
    await page.evaluate(() => {
      const body = document.querySelector("tbody")!;
      window.scrollTo(
        0,
        body.getBoundingClientRect().top + window.scrollY - 80
      );
    });
    await expect(first).toBeInViewport();
    const before = await page.evaluate(() => window.scrollY);
    await page.reload();
    await expect
      .poll(() => page.evaluate(() => window.scrollY))
      .toBeGreaterThan(before - 3);
    await expect(first).toBeInViewport();
  });

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
    await expect(badge).toHaveText("Rejected: 1 Must Fix");
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

  test("analysis completion includes runs with infrastructure errors without mixing experiment scope", async ({
    page,
    context,
  }) => {
    const original = tasks[0].trials![0];
    const foreign = {
      ...original,
      id: "foreign",
      experiment_id: "other-experiment",
      analysis: { classification: "HARNESS_ERROR" },
    };
    await page.route("**/api/tasks/task-a/trials?**", (route) =>
      route.fulfill({ json: [original, foreign] })
    );
    await page.route("**/api/trials/foreign", (route) =>
      route.fulfill({ json: foreign })
    );
    await page.goto("/experiments/review-demo?task=task-a");
    await expect(
      page.getByText("This experiment: 1/1 analyzed · v7", { exact: true })
    ).toBeVisible();
    await expect(page.getByText("1/1 analyzed", { exact: true })).toBeVisible();
    const otherExperiments = page.locator("section").filter({
      has: page.getByRole("heading", { name: "Other experiments", exact: true }),
    });
    await expect(
      otherExperiments.getByText("Other experiment · other-ex", { exact: true })
    ).toHaveAttribute("title", "other-experiment");
    await expect(otherExperiments.getByRole("link")).toHaveCount(0);
    await expect(
      page.getByText("1 good failure", { exact: true })
    ).toBeVisible();
    await expect(
      page.getByText("1 harness error", { exact: true })
    ).toHaveCount(0);
    await expect(
      page.getByText(
        /Missing access:|Inspects task instructions|fair agent failure does not/
      )
    ).toHaveCount(0);
    await otherExperiments.getByRole("button", { name: "View trial" }).click();
    await expect(page).toHaveURL(/\/experiments\/review-demo\?/);
    await expect(page).toHaveURL(/trial=foreign/);
    expect(context.pages()).toHaveLength(1);
  });

  test("verifier timeout retains completed trajectory analysis and its evidence", async ({
    page,
  }) => {
    const original = tasks[0].trials![0];
    await page.route("**/api/tasks/task-a/trials?**", (route) =>
      route.fulfill({
        json: [
          {
            ...original,
            status: "failed",
            reward: null,
            analysis_status: "success",
            analysis: {
              classification: "HARNESS_ERROR",
              subtype: "misgrade",
              root_cause:
                "The verifier timed out after 5400 seconds without producing a grade.",
              evidence:
                "The partial trajectory records the agent building and testing through step 1200.",
            },
          },
        ],
      })
    );
    await page.goto("/experiments/review-demo?task=task-a");
    await expect(
      page.getByText("This experiment: 1/1 analyzed · v7", { exact: true })
    ).toBeVisible();
    await expect(
      page.getByText("1 harness error", { exact: true })
    ).toHaveCount(1);
    await page.getByText("GRADING ERROR", { exact: true }).click();
    await expect(
      page.getByText(
        "The verifier timed out after 5400 seconds without producing a grade.",
        { exact: true }
      )
    ).toBeVisible();
    await expect(
      page.getByText(
        "The partial trajectory records the agent building and testing through step 1200.",
        { exact: true }
      )
    ).toBeVisible();
    await expect(
      page.getByText(
        /couldn’t be evaluated|COULD NOT EVALUATE RUN|ANALYSIS FAILED|^misgrade$/
      )
    ).toHaveCount(0);
  });

  test("analysis failure stays incomplete even when a previous classification exists", async ({
    page,
  }) => {
    await page.route("**/api/tasks/task-a/trials?**", (route) =>
      route.fulfill({
        json: [
          {
            ...tasks[0].trials![0],
            analysis_status: "failed",
            analysis_error:
              "Trajectory analysis worker stopped before saving its report.",
          },
        ],
      })
    );
    await page.goto("/experiments/review-demo?task=task-a");
    await expect(
      page.getByText("This experiment: 0/1 analyzed · 1 analysis failed · v7", {
        exact: true,
      })
    ).toBeVisible();
    await expect(
      page.getByText("ANALYSIS FAILED", { exact: true })
    ).toBeVisible();
    await expect(
      page.getByText(
        "Trajectory analysis worker stopped before saving its report.",
        { exact: true }
      )
    ).toBeVisible();
    await expect(page.getByText("1 good failure", { exact: true })).toHaveCount(
      0
    );
  });

  for (const sourceStatus of [null, "queued", "running", "failed", "success"]) {
    for (const origin of ["this experiment", "another experiment"]) {
      test(`source checks ${sourceStatus ?? "unchecked"} retain fixes from ${origin}`, async ({
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
        await page.route(
          /\/api\/tasks\/task-a\/panel(?:\?|$)/,
          async (route) => {
            const response = await route.fetch();
            const panel = await response.json();
            panel.version.pre_trial_status = sourceStatus;
            panel.version.pre_trial_findings = [];
            panel.version.retained_findings = [];
            await route.fulfill({ json: panel });
          }
        );
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
          .getByRole("heading", { name: "Findings", exact: true })
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
            page.getByText("Other experiment · another-", { exact: true })
          ).toBeVisible();
      });
    }
  }

  for (const includeMustFix of [false, true]) {
    test(`converted findings count as required fixes, additional must-fix ${includeMustFix}`, async ({
      page,
    }) => {
      const findings = [
        {
          ...records[0].finding!,
          id: "optional",
          tier: "optional",
          title: "Optional historical finding",
        },
        {
          ...records[0].finding!,
          id: "converted-fix",
          tier: "must_fix",
          title: "Converted historical finding",
        },
        {
          ...records[0].finding!,
          id: "unclassified",
          tier: undefined,
          title: "Unclassified historical finding",
        },
        ...(includeMustFix
          ? [
              {
                ...records[0].finding!,
                id: "required",
                tier: "must_fix",
                title: "Required fix",
              },
            ]
          : []),
      ];
      await page.route(/\/api\/tasks\/task-a\/panel(?:\?|$)/, async (route) => {
        const response = await route.fetch();
        const panel = await response.json();
        panel.version.pre_trial_status = "success";
        panel.version.pre_trial_findings = findings;
        panel.version.retained_findings = [];
        await route.fulfill({ json: panel });
      });
      await page.goto("/experiments/review-demo?task=task-a");
      const checks = page
        .getByRole("heading", { name: "Findings", exact: true })
        .locator("..");
      await expect(
        checks.getByText(includeMustFix ? "2 Must fix" : "1 Must fix", {
          exact: true,
        })
      ).toBeVisible();
      await expect(
        page.getByText("RECORDED OPTIONAL", { exact: true })
      ).toHaveCount(2);
      await expect(page.getByText("Must fix", { exact: true })).toHaveCount(
        includeMustFix ? 2 : 1
      );
      await expect(page.getByText(/RECORDED SHOULD FIX/)).toHaveCount(0);
      await expect(
        checks.getByText("No required fixes", { exact: true })
      ).toHaveCount(0);
    });
  }

  for (const count of [1, 2]) {
    test(`bad-success review count uses the correct plural for ${count}`, async ({
      page,
    }) => {
      const original = tasks[0].trials![0];
      await page.route("**/api/tasks/task-a/trials?**", (route) =>
        route.fulfill({
          json: Array.from({ length: count }, (_, index) => ({
            ...original,
            id: `invalid-${index}`,
            analysis: { classification: "BAD_SUCCESS" },
          })),
        })
      );
      await page.goto("/tasks/task-a?version=7&drawer=task&taskPane=overview");
      await expect(
        page.getByText(count === 1 ? "1 bad success" : "2 bad successes", {
          exact: true,
        })
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
    await page.goto("/deliveries/review-demo?filter=outstanding&task=task-a");
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
    ).toHaveText("Rejected: 1 Must Fix");
    await expect(
      rejectedRow.getByText("Rejected: 1 Must Fix", { exact: true })
    ).toHaveCount(1);
    await page
      .getByRole("button", { name: "2 QA verdict generation failed", exact: true })
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
      name: "Run pre-trial audit v7",
      exact: true,
    });
    await expect(source).toBeVisible();
    expect(writes).toEqual([]);
    await source.click();
    await expect.poll(() => writes).toEqual(["/api/tasks/task-a/qa/pre-trial"]);
    await page
      .getByRole("button", { name: "Generate QA verdict for v7", exact: true })
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
      page.getByRole("button", { name: "0 No current QA verdict", exact: true })
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
      .getByRole("button", { name: "2 No current QA verdict", exact: true })
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
      page.getByText("No QA verdict for this version", { exact: true }).first()
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
      page.getByText("No QA verdict for this version", { exact: true }).first()
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

test("task-page findings are counted and open independently", async ({
  page,
}) => {
  test.skip(process.env.E2E_REVIEW_FIXTURES !== "1");
  const first = {
    id: "first-fix",
    tier: "must_fix",
    source: "pre_trial",
    title: "Verifier accepts empty answers",
    detail: "First finding evidence",
    recommendation: "Reject empty answers",
  };
  const second = {
    ...first,
    id: "second-fix",
    title: "Missing required test coverage",
    detail: "Second finding evidence",
  };
  await page.route(
    /\/api\/tasks\/task-a\/(open|panel)(?:\?|$)/,
    async (route) => {
      const response = await route.fetch();
      const data = await response.json();
      if (data.selected_version) {
        data.selected_version.must_fix_count = 2;
        data.selected_version.pre_trial_must_fix_count = 2;
        expect(data.selected_version).not.toHaveProperty("pre_trial_findings");
        expect(data.selected_version).not.toHaveProperty("retained_findings");
      } else {
        data.version.pre_trial_findings = [first, second];
        data.version.retained_findings = [first];
      }
      await route.fulfill({ json: data });
    }
  );
  await page.goto("/tasks/task-a");
  await expect(
    page.getByText("Rejected · Pre-trial audit", { exact: true })
  ).toBeVisible();
  await expect(page.getByText("2 Must fix", { exact: true })).toBeVisible();
  await expect(page.getByText(/high confidence|The source audit/)).toHaveCount(
    0
  );
  await page
    .getByRole("button", { name: "View findings", exact: true })
    .click();
  const one = page.locator('details[data-finding="first-fix"]');
  const two = page.locator('details[data-finding="second-fix"]');
  await expect(one).not.toHaveAttribute("open", "");
  await expect(two).not.toHaveAttribute("open", "");
  await one.locator("summary").click();
  await expect(one.getByText(first.detail, { exact: true })).toBeVisible();
  await expect(two.getByText(second.detail, { exact: true })).not.toBeVisible();
  await two.locator("summary").click();
  await expect(two.getByText(second.detail, { exact: true })).toBeVisible();
  await one.locator("summary").click();
  await expect(one.getByText(first.detail, { exact: true })).not.toBeVisible();
  await expect(two.getByText(second.detail, { exact: true })).toBeVisible();
});

test("linked retained must-fix survives a historical optional audit finding", async ({
  page,
}) => {
  test.skip(process.env.E2E_REVIEW_FIXTURES !== "1");
  const retained = {
    id: "retained-fix",
    links_to: "audit-finding",
    source: "post_trial",
    tier: "must_fix",
    title: "Required verifier fix",
  };
  const audit = {
    id: "audit-finding",
    source: "pre_trial",
    tier: "optional",
    title: "Historical suggestion",
  };
  await page.route(
    /\/api\/tasks\/task-a\/(open|panel)(?:\?|$)/,
    async (route) => {
      const response = await route.fetch();
      const data = await response.json();
      if (data.selected_version) {
        data.selected_version.must_fix_count = 1;
        data.selected_version.pre_trial_must_fix_count = 0;
        expect(data.selected_version).not.toHaveProperty("pre_trial_findings");
        expect(data.selected_version).not.toHaveProperty("retained_findings");
      } else {
        data.version.pre_trial_findings = [audit];
        data.version.retained_findings = [retained];
      }
      await route.fulfill({ json: data });
    }
  );
  await page.goto("/tasks/task-a");
  await expect(page.getByText("1 Must fix", { exact: true })).toBeVisible();
  await page
    .getByRole("button", { name: "View findings", exact: true })
    .click();
  const findings = page
    .getByRole("heading", { name: "Findings", exact: true })
    .locator("..");
  await expect(findings.getByText("1 Must fix", { exact: true })).toBeVisible();
  await expect(
    page.locator('details[data-finding="retained-fix"]')
  ).toBeVisible();
  await expect(
    page.locator('details[data-finding="audit-finding"]')
  ).toBeVisible();
});

test("rejection without structured findings keeps its reason behind a disclosure", async ({
  page,
}) => {
  test.skip(process.env.E2E_REVIEW_FIXTURES !== "1");
  const reason = "The verifier cannot execute the required checks.";
  await page.route(
    /\/api\/tasks\/task-a\/(open|panel)(?:\?|$)/,
    async (route) => {
      const response = await route.fetch();
      const data = await response.json();
      if (data.selected_version) {
        data.selected_version.must_fix_count = 0;
        data.selected_version.pre_trial_must_fix_count = 0;
        expect(data.selected_version).not.toHaveProperty("pre_trial_findings");
        expect(data.selected_version).not.toHaveProperty("retained_findings");
      } else {
        data.version.pre_trial_findings = [];
        data.version.retained_findings = [];
      }
      data.task.verdict = {
        is_good: false,
        verdict: "reject",
        primary_issue: reason,
        confidence: null,
        recommendations: [],
      };
      await route.fulfill({ json: data });
    }
  );
  await page.goto("/tasks/task-a");
  await expect(page.getByText("Rejected", { exact: true })).toBeVisible();
  await expect(page.getByText(reason, { exact: true })).not.toBeVisible();
  await page
    .getByRole("button", { name: "View findings", exact: true })
    .click();
  const disclosure = page
    .locator("details")
    .filter({ has: page.getByText("Rejection reason", { exact: true }) });
  await expect(disclosure).not.toHaveAttribute("open", "");
  await expect(disclosure.getByText(reason, { exact: true })).not.toBeVisible();
  await disclosure.locator("summary").click();
  await expect(disclosure.getByText(reason, { exact: true })).toBeVisible();
});

test("a first run-review finding is counted without detailed findings in open", async ({
  page,
}) => {
  test.skip(process.env.E2E_REVIEW_FIXTURES !== "1");
  const finding = {
    id: "new-run-fix",
    tier: "must_fix",
    source: "post_trial",
    title: "Run exposed a verifier defect",
    detail: "Run evidence",
  };
  await page.route(
    /\/api\/tasks\/task-a\/(open|panel)(?:\?|$)/,
    async (route) => {
      const response = await route.fetch();
      const data = await response.json();
      if (data.selected_version) {
        expect(data.selected_version).not.toHaveProperty("pre_trial_findings");
        expect(data.selected_version).not.toHaveProperty("retained_findings");
        data.selected_version.must_fix_count = 1;
        data.selected_version.pre_trial_must_fix_count = 0;
      } else {
        data.version.pre_trial_findings = [];
        data.version.retained_findings = [];
      }
      await route.fulfill({ json: data });
    }
  );
  await page.route("**/api/tasks/task-a/trials?**", (route) =>
    route.fulfill({
      json: [
        {
          ...tasks[0].trials![0],
          analysis: {
            ...tasks[0].trials![0].analysis!,
            action_items: [finding],
          },
        },
      ],
    })
  );
  await page.goto("/tasks/task-a");
  await expect(
    page.getByText("Rejected · Run review", { exact: true })
  ).toBeVisible();
  await expect(page.getByText("1 Must fix", { exact: true })).toBeVisible();
  await page
    .getByRole("button", { name: "View findings", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: finding.title, exact: true })
  ).toBeVisible();
});

for (const address of ["retained-fix", "historical-audit"]) {
  test(`finding link ${address} opens and highlights the retained required fix`, async ({
    page,
  }) => {
    test.skip(process.env.E2E_REVIEW_FIXTURES !== "1");
    await page.route(/\/api\/tasks\/task-a\/panel(?:\?|$)/, async (route) => {
      const response = await route.fetch();
      const data = await response.json();
      data.version.pre_trial_findings = [
        {
          id: "historical-audit",
          tier: "optional",
          title: "Historical audit finding",
        },
      ];
      data.version.retained_findings = [
        {
          id: "retained-fix",
          links_to: "historical-audit",
          tier: "must_fix",
          source: "post_trial",
          title: "Required retained fix",
          detail: "Required fix evidence",
        },
      ];
      await route.fulfill({ json: data });
    });
    await page.goto(
      `/tasks/task-a?version=7&drawer=task&taskPane=overview&finding=${address}`
    );
    await expect(
      page.locator('details[data-finding="retained-fix"]')
    ).toHaveAttribute("open", "");
    await expect(
      page.locator('details[data-finding="retained-fix"]')
    ).toHaveClass(/ring-amber-500\/40/);
    await expect(
      page.getByText("Required fix evidence", { exact: true })
    ).toBeVisible();
  });
}

for (const origin of ["this experiment", "another experiment"]) {
  test(`finding trial attribution opens ${origin} in the current drawer`, async ({
    page,
    context,
  }) => {
    test.skip(process.env.E2E_REVIEW_FIXTURES !== "1");
    const original = tasks[0].trials![0];
    const trial = {
      ...original,
      id: origin === "this experiment" ? original.id : "external-source-trial",
      name: "attributed-trial",
      experiment_id:
        origin === "this experiment" ? "review-demo" : "another-experiment",
      analysis: {
        ...original.analysis,
        root_cause: "Attribution drawer evidence",
        action_items: [
          {
            ...records[0].finding!,
            id: "attributed-finding",
            tier: "must_fix",
          },
        ],
      },
    };
    await page.route("**/api/tasks/task-a/trials?**", (route) =>
      route.fulfill({ json: [trial] })
    );
    await page.route(`**/api/trials/${trial.id}`, (route) =>
      route.fulfill({ json: trial })
    );
    await page.goto("/experiments/review-demo?task=task-a&taskPane=overview");
    const finding = page.locator('details[data-finding="attributed-finding"]');
    await expect(finding).toBeVisible();
    await finding.locator("summary").click();
    await finding
      .locator('button[title^="Open trial attributed-trial"]')
      .click();
    await expect(page).toHaveURL(new RegExp(`trial=${trial.id}`));
    await expect(page).toHaveURL(/\/experiments\/review-demo\?/);
    expect(context.pages()).toHaveLength(1);
  });
}

test("finding source opens the file and line inside the experiment pane", async ({
  page,
  context,
}) => {
  test.skip(process.env.E2E_REVIEW_FIXTURES !== "1");
  await page.goto("/experiments/review-demo?task=task-a&taskPane=overview");
  const finding = page.locator('details[data-finding="empty-answer"]');
  await expect(finding).toBeVisible();
  await finding.locator("summary").click();
  await finding
    .getByRole("link", { name: "Open tests/test.sh:7", exact: true })
    .click();
  await expect(page).toHaveURL(/\/experiments\/review-demo\?/);
  await expect(page).toHaveURL(/taskFile=tests%2Ftest.sh/);
  await expect(page).toHaveURL(/taskLines=L?7/);
  await expect(page.getByText("exit 0", { exact: true })).toBeVisible();
  expect(context.pages()).toHaveLength(1);
});
