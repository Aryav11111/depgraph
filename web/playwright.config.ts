import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests against the real stack: API + Postgres (docker compose) with
 * the frozen OSV advisory fixture loaded, and this app's production build.
 *
 * Browsers aren't downloaded: tests drive an installed one via `channel`
 * (Edge locally on Windows, the Chrome preinstalled on GitHub's runners in CI).
 */
const channel = process.env.PW_CHANNEL ?? "msedge";
const baseURL = process.env.E2E_BASE_URL ?? "http://localhost:3000";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false, // tests share one API + database
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], channel, viewport: { width: 1280, height: 900 } },
      testIgnore: /mobile\.spec\.ts/,
    },
    {
      name: "mobile",
      use: { ...devices["Pixel 7"], channel },
      testMatch: /mobile\.spec\.ts/,
    },
  ],
  webServer: {
    command: "npm run start",
    url: baseURL,
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
