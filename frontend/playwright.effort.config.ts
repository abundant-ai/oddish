import { defineConfig, devices } from "@playwright/test";
export default defineConfig({
  testDir: "e2e",
  testMatch: [
    "reasoning-effort.spec.ts",
    "experiment-agent-grouping.spec.ts",
    "ec2-rerun-command.spec.ts",
  ],
  // The fixture serves development bundles and lazy-loaded charts on CI.
  // Leave time for navigation and hydration before exercising the controls.
  timeout: 90_000,
  // The dev server compiles the lazy chart chunks on demand; on CI one such
  // compile stalled past the hydration wait (run 35189328446). A retry gets a
  // fresh page load, like the main config already allows.
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: "http://localhost:3117",
    ...devices["Desktop Chrome"],
    trace: "retain-on-failure",
    navigationTimeout: 60_000,
  },
  webServer: {
    command:
      "node node_modules/next/dist/bin/next dev e2e/delivery-app --webpack -p 3117",
    url: "http://localhost:3117/effort",
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
  },
});
