import { defineConfig, devices } from "@playwright/test";
export default defineConfig({
  testDir: "e2e",
  testMatch: "delivery-refresh.spec.ts",
  workers: 1,
  reporter: "list",
  use: {
    baseURL: "http://localhost:3109",
    ...devices["Desktop Chrome"],
    trace: "retain-on-failure",
  },
  webServer: {
    command: "pnpm exec next dev e2e/delivery-app --webpack -p 3109",
    url: "http://localhost:3109",
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
  },
});
