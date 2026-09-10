import { defineConfig, devices } from "@playwright/test";
export default defineConfig({
  testDir: "e2e",
  testMatch: ["review-meaning.spec.ts", "user-ui-layout.spec.ts"],
  workers: 1,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:3207",
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: {
    command:
      "cd e2e/review-app && node ../../node_modules/next/dist/bin/next build --webpack && node ../../node_modules/next/dist/bin/next start --hostname 127.0.0.1 --port 3207",
    url: "http://127.0.0.1:3207",
    reuseExistingServer: true,
    timeout: 120000,
  },
});
