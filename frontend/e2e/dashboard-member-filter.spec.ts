import { expect, test } from "@playwright/test";
import { clerk, setupClerkTestingToken } from "@clerk/testing/playwright";

const CLERK_EMAIL = process.env.E2E_CLERK_EMAIL;
const CLERK_SECRET = process.env.CLERK_SECRET_KEY;
const CLERK_PUBLISHABLE = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;
const hasClerkEnv = !!CLERK_EMAIL && !!CLERK_SECRET && !!CLERK_PUBLISHABLE;

test.describe("dashboard member filter", () => {
  test.skip(
    !hasClerkEnv,
    "needs E2E_CLERK_EMAIL + CLERK_SECRET_KEY + NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY"
  );

  test("selecting a person navigates with their stable user id", async ({
    page,
  }) => {
    await setupClerkTestingToken({ page });
    await page.goto("/");
    await clerk.signIn({ page, emailAddress: CLERK_EMAIL! });

    await page.route("**/api/people/search?*", async (route) => {
      const query = new URL(route.request().url()).searchParams.get("q");
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          items:
            query === "@kyl" || query === "user_kyle"
              ? [
                  {
                    id: "user_kyle",
                    display_name: "Kyle",
                    github_username: "kyle",
                  },
                ]
              : [],
        }),
      });
    });

    // The dashboard streams experiment rows behind Suspense, while this
    // control is part of the first shell. Let the locator below own readiness
    // instead of waiting for the unrelated stream to finish loading.
    await page.goto("/dashboard", { waitUntil: "commit" });
    await page
      .getByRole("combobox", { name: "Filter experiments by member" })
      .click();
    await page.getByPlaceholder("Search members…").fill("@kyl");
    await page.getByRole("option", { name: /Kyle/ }).click();

    await expect
      .poll(() => new URL(page.url()).searchParams.get("author"))
      .toBe("user_kyle");
  });
});
