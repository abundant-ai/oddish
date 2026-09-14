import { defineConfig, devices } from "@playwright/test";
export default defineConfig({
  testDir: "e2e",
  testMatch: [
    "reasoning-effort.spec.ts",
    "experiment-agent-grouping.spec.ts",
    "ec2-rerun-command.spec.ts",
  ],
  workers: 1,
  reporter: "list",
  use: {
    baseURL: "http://localhost:3117",
    ...devices["Desktop Chrome"],
    trace: "retain-on-failure",
  },
  webServer: {
    command:
      "node node_modules/next/dist/bin/next dev e2e/delivery-app --webpack -p 3117",
    url: "http://localhost:3117/effort",
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
  },
});
