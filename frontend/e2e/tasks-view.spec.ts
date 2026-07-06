import { expect, test } from "@playwright/test";
import { clerk, setupClerkTestingToken } from "@clerk/testing/playwright";

const CLERK_EMAIL = process.env.E2E_CLERK_EMAIL;
const CLERK_SECRET = process.env.CLERK_SECRET_KEY;
const CLERK_PUBLISHABLE = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;
const TASK_ID = process.env.E2E_TASK_ID;

const hasClerkEnv = !!CLERK_EMAIL && !!CLERK_SECRET && !!CLERK_PUBLISHABLE;

test.describe("authenticated task view", () => {
  test.skip(
    !hasClerkEnv,
    "needs E2E_CLERK_EMAIL + CLERK_SECRET_KEY + NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY",
  );

  test("dashboard task detail renders for a signed-in user", async ({
    page,
  }) => {
    // Testing Token first: it bypasses Clerk bot detection so the programmatic
    // sign-in below is accepted. Then load an unprotected page that boots Clerk.
    await setupClerkTestingToken({ page });
    await page.goto("/");

    await clerk.signIn({
      page,
      // Non-null: the describe-level skip guarantees CLERK_EMAIL is set.
      emailAddress: CLERK_EMAIL!,
    });

    if (TASK_ID) {
      await page.goto(`/tasks/${TASK_ID}`);
    } else {
      await page.goto("/tasks");
      await expect(
        page.getByRole("heading", { name: "Recent Tasks" }),
      ).toBeVisible();
      await page.locator('a[href^="/tasks/"]').first().click();
    }

    // The task-detail client always renders an "Agents" section once the detail
    // payload resolves (frontend/src/app/(app)/tasks/[task_id]/
    // task-detail-client.tsx), so it's a stable, non-brittle readiness anchor.
    await expect(
      page.getByRole("heading", { name: "Agents" }),
    ).toBeVisible({ timeout: 15_000 });
  });
});
