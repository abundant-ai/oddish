import { defineConfig, devices } from "@playwright/test";
const port = process.env.DELIVERY_TEST_PORT ?? "3109";
export default defineConfig({
  testDir: "e2e",
  testMatch: "delivery-refresh.spec.ts",
  workers: 1,
  reporter: "list",
  use: {
    baseURL: `http://localhost:${port}`,
    ...devices["Desktop Chrome"],
    trace: "retain-on-failure",
  },
  webServer: {
    command: `node node_modules/next/dist/bin/next dev e2e/delivery-app --webpack -p ${port}`,
    url: `http://localhost:${port}`,
    reuseExistingServer: false,
    timeout: 120000,
  },
});
