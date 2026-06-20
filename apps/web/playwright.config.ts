import { defineConfig, devices } from "@playwright/test";

// Two e2e smokes (M8 §5): the agent loop and the thread loop. They run against a real
// running stack (control-plane + fake/real inference + Postgres) plus the Vite dev server.
// Bring the stack up yourself (see tests/e2e/README.md); set E2E_BASE_URL to the SPA origin.
const baseURL = process.env.E2E_BASE_URL ?? "http://localhost:5173";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: "list",
  use: {
    baseURL,
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  // When E2E_BASE_URL is not provided we boot the Vite dev server ourselves; the backend
  // stack must already be running and reachable from the SPA's configured URLs.
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: "pnpm dev",
        url: "http://localhost:5173",
        reuseExistingServer: !process.env.CI,
        timeout: 60_000,
      },
});
