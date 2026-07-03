import { defineConfig, devices } from "@playwright/test";

// No `webServer` block on purpose: Playwright would boot it before it knows the
// suite is going to skip, so a credential-less run (the CI/local skip path)
// would still try to spin up `pnpm dev`. The authenticated spec instead assumes
// an already-running dev stack (see docs/e2e-test-plan.md P4 for the env it
// needs). Point it at a different origin with E2E_BASE_URL.
export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: "list",
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
