import { expect, test } from "@playwright/test";
import { clerk, setupClerkTestingToken } from "@clerk/testing/playwright";

const harness = process.env.E2E_ENDPOINT_HEALTH_HARNESS === "true";
const hasClerk = Boolean(
  process.env.E2E_CLERK_EMAIL &&
  process.env.CLERK_SECRET_KEY &&
  process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY
);
const healthy = {
  id: "healthy",
  name: "Healthy connection",
  model: "openai/test",
  credential_ref: "PLATFORM_KEY",
  alerts_enabled: true,
  last_outcome: "success",
  last_checked_at: new Date().toISOString(),
  last_success_at: new Date().toISOString(),
  error: null,
  incident_id: null,
  incident_opened_at: null,
  stale: false,
};
const failed = {
  ...healthy,
  id: "failed",
  name: "Failing connection",
  last_outcome: "failure",
  error: "Provider request timed out.",
  incident_id: "incident-1",
  incident_opened_at: new Date().toISOString(),
};

test.describe("Operator endpoint monitoring", () => {
  test.skip(
    !harness && !hasClerk,
    "Requires Clerk sign-in or the isolated local component harness"
  );
  test.beforeEach(async ({ page }) => {
    await page.route("**/api/admin/operator-access", (route) =>
      route.fulfill({ json: { allowed: true } })
    );
    // Keep unrelated queue data out of this component test.
    await page.route("**/api/admin/queue-health", (route) =>
      route.fulfill({ status: 503, json: { error: "Not part of this test" } })
    );
    if (!harness) {
      await setupClerkTestingToken({ page });
      await page.goto("/");
      await clerk.signIn({ page, emailAddress: process.env.E2E_CLERK_EMAIL! });
    }
  });

  test("healthy default performs reads only; history and recheck are explicit", async ({
    page,
  }) => {
    let posts = 0;
    let historyReads = 0;
    await page.route("**/api/admin/endpoint-health", (route) =>
      route.fulfill({
        json: {
          enabled: true,
          timestamp: new Date().toISOString(),
          monitors: [healthy],
        },
      })
    );
    await page.route("**/api/admin/endpoint-health/healthy/check", (route) => {
      posts += 1;
      return route.fulfill({ status: 202, json: { scheduled: true } });
    });
    await page.route("**/api/admin/endpoint-health/healthy/checks", (route) => {
      historyReads += 1;
      return route.fulfill({
        json: {
          checks: [
            {
              id: "check-1",
              checked_at: healthy.last_checked_at,
              outcome: "success",
              latency_ms: 42,
              error: null,
              status_code: null,
              request_id: "request-1",
            },
          ],
        },
      });
    });
    await page.goto("/admin");
    await expect(page.getByText(/No endpoint issues detected/)).toBeVisible();
    await expect(
      page.getByText("Healthy connection", { exact: true })
    ).toHaveCount(0);
    expect(posts).toBe(0);
    expect(historyReads).toBe(0);
    await page
      .getByRole("button", { name: /All connections and history/ })
      .click();
    await page.getByRole("button", { name: "Inspect checks" }).click();
    await expect(page).toHaveURL(/endpoint=healthy/);
    await expect(page.getByText("Provider request: request-1")).toBeVisible();
    await page.getByRole("button", { name: "Check again" }).click();
    await expect(
      page.getByText("Check requested; waiting for the monitor.")
    ).toBeVisible();
    expect(posts).toBe(1);
  });

  test("failure is first, direct links open evidence, and recovery leaves history", async ({
    page,
  }) => {
    let recovered = false;
    await page.route("**/api/admin/endpoint-health", (route) =>
      route.fulfill({
        json: {
          enabled: true,
          timestamp: new Date().toISOString(),
          monitors: [
            healthy,
            recovered
              ? { ...healthy, id: failed.id, name: failed.name }
              : failed,
          ],
        },
      })
    );
    await page.route("**/api/admin/endpoint-health/failed/checks", (route) =>
      route.fulfill({
        json: {
          checks: [
            {
              id: "failed-check",
              checked_at: failed.last_checked_at,
              outcome: "failure",
              latency_ms: 20000,
              error: failed.error,
              status_code: null,
              request_id: null,
            },
          ],
        },
      })
    );
    await page.goto("/admin?endpoint=failed");
    await expect(
      page.getByText("Repeated failures", { exact: true })
    ).toBeVisible();
    await expect(
      page.getByText("Healthy connection", { exact: true })
    ).toHaveCount(0);
    await expect(
      page.getByRole("region", { name: "Endpoint check history" })
    ).toContainText("Provider request timed out.");
    recovered = true;
    await page.reload();
    await expect(page.getByText(/No endpoint issues detected/)).toBeVisible();
    await expect(
      page.getByText("Repeated failures", { exact: true })
    ).toHaveCount(0);
    await expect(
      page.getByRole("region", { name: "Endpoint check history" })
    ).toContainText("Provider request timed out.");
  });

  for (const [name, enabled, monitors, expected] of [
    ["overdue success", true, [{ ...healthy, stale: true }], "Checks overdue"],
    [
      "checker defect",
      true,
      [
        {
          ...healthy,
          last_outcome: "monitor_error",
          error: "The monitor could not complete this check.",
        },
      ],
      "Check could not run",
    ],
    [
      "disabled",
      false,
      [],
      "Endpoint monitoring is disabled for this environment.",
    ],
    ["unconfigured", true, [], "No connections are being monitored."],
  ] as const) {
    test(`${name} does not imply healthy`, async ({ page }) => {
      await page.route("**/api/admin/endpoint-health", (route) =>
        route.fulfill({
          json: { enabled, timestamp: new Date().toISOString(), monitors },
        })
      );
      await page.goto("/admin");
      await expect(page.getByText(expected, { exact: false })).toBeVisible();
      await expect(page.getByText(/No endpoint issues detected/)).toHaveCount(
        0
      );
    });
  }

  test("failed status read shows recovery control", async ({ page }) => {
    await page.route("**/api/admin/endpoint-health", (route) =>
      route.fulfill({ status: 503, json: { error: "unavailable" } })
    );
    await page.goto("/admin");
    await expect(page.getByText("Endpoint health unavailable")).toBeVisible();
    await expect(page.getByText(/No endpoint issues detected/)).toHaveCount(0);
  });

  test("non-operator never requests global endpoint health", async ({
    page,
  }) => {
    let reads = 0;
    await page.route("**/api/admin/operator-access", (route) =>
      route.fulfill({ json: { allowed: false } })
    );
    await page.route("**/api/admin/endpoint-health", (route) => {
      reads += 1;
      return route.fulfill({ status: 403 });
    });
    await page.goto("/admin");
    await expect(
      page.getByRole("heading", { name: "Admin Dashboard" })
    ).toBeVisible();
    await expect(
      page.getByText("Model endpoint health", { exact: true })
    ).toHaveCount(0);
    expect(reads).toBe(0);
  });
});
