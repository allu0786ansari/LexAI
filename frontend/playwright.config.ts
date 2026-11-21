import { defineConfig, devices } from "@playwright/test";

// End-to-end tests against the built UI with the backend mocked (e2e/mock-api.ts):
// no model server, index or Python needed.
export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  // A loaded laptop (the live test generating with the 7B model) renders slower than CI.
  expect: { timeout: 15_000 },
  // With PW_LIVE, run one test at a time: the real answer must not compete with the browsers.
  workers: process.env.PW_LIVE ? 1 : undefined,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  // PW_BASE_URL runs the same tests against a deployed UI (CI: the nginx container, with its real
  // CSP and headers); otherwise against `vite preview`.
  use: { baseURL: process.env.PW_BASE_URL || "http://127.0.0.1:4173", trace: "retain-on-failure" },
  webServer: process.env.PW_BASE_URL
    ? undefined
    : {
        command: "npx vite preview --host 127.0.0.1 --port 4173 --strictPort",
        url: "http://127.0.0.1:4173",
        reuseExistingServer: !process.env.CI,
      },
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], channel: process.env.PW_CHANNEL || undefined },
    },
    {
      name: "mobile",
      use: { ...devices["Pixel 7"], channel: process.env.PW_CHANNEL || undefined },
    },
  ],
});
