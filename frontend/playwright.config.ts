/**
 * Browser tests: the built app, driven in Chromium, against its own backend.
 *
 * Playwright is a devDependency: nothing under src/ imports it, so the
 * production bundle in dist/ never contains it. Run with `npm run test:e2e`.
 *
 * Isolated from the dev stack on purpose. Playwright starts its own API on
 * :8001 against the `evidwell_test` database (never `evidwell`) and serves the
 * production build on :4174, proxying /api to that API. `reuseExistingServer`
 * is off so a test can never land on a server holding real data. Before every
 * run, global-setup.ts reseeds the test database (see
 * backend/tests/e2e/browser_seed.py).
 *
 * One worker, in order: the tests share one seeded database, and the approve
 * and sign-up flows change it. Each test touches different seeded rows, so the
 * order does not matter — but two of them at once would.
 */
import { defineConfig, devices } from "@playwright/test";

const python = process.env.E2E_PYTHON ?? "../backend/.venv/bin/python";
const testDatabase =
  process.env.E2E_DATABASE_URL ??
  "postgresql+asyncpg://evidwell:evidwell@localhost:5433/evidwell_test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env.CI,
  reporter: [["list"], ["html", { open: "never" }]],
  globalSetup: "./e2e/global-setup.ts",
  use: {
    baseURL: "http://localhost:4174",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] }, grepInvert: /@phone/ },
    { name: "phone", use: { ...devices["Pixel 7"] }, grep: /@phone/ },
  ],
  webServer: [
    {
      command: `${python} -m uvicorn app.main:app --port 8001`,
      cwd: "../backend",
      url: "http://localhost:8001/api/healthz",
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        DATABASE_URL: testDatabase,
        DISCOVERY_SCAN_ENABLED: "false",
        IMAGE_PROVIDER: "none",
      },
    },
    {
      command: "npm run build && npx vite preview --port 4174 --strictPort",
      url: "http://localhost:4174",
      reuseExistingServer: false,
      timeout: 120_000,
      env: { VITE_API_PROXY_TARGET: "http://localhost:8001" },
    },
  ],
});
