import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PORT = Number(process.env["MLFLOW_PORT"] ?? 5150);
const CONFIG_DIR = path.dirname(fileURLToPath(import.meta.url));
const BASE_URL = process.env["CALIBER_E2E_BASE_URL"] ?? `http://127.0.0.1:${PORT}/caliber`;
const USE_EXISTING_SERVER = process.env["CALIBER_E2E_USE_EXISTING_SERVER"] === "1";
// The E2E specs share one CALIBER backend; running too many heavy
// workflow/knowledge-build runs concurrently starves them into false timeouts
// (they pass in isolation). Keep parallelism modest. Override with
// PLAYWRIGHT_WORKERS when the backend can take more.
const DEFAULT_WORKERS = 4;
const WORKERS = Number.parseInt(
  process.env["PLAYWRIGHT_WORKERS"] ?? String(DEFAULT_WORKERS),
  10,
);

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: Number.isFinite(WORKERS) && WORKERS > 0 ? WORKERS : DEFAULT_WORKERS,
  timeout: 90_000,
  globalTeardown: "./playwright.global-teardown.ts",
  expect: {
    timeout: 12_000,
  },
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report", open: "never" }],
    // Emit Allure results (auto-attaches screenshots/video/trace on failure).
    // Shares the allure-results/ dir with the vitest reporter so a single
    // report can cover both unit + e2e runs.
    ["allure-playwright", { resultsDir: "allure-results", detail: true }],
  ],
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 900 },
  },
  webServer: USE_EXISTING_SERVER
    ? undefined
    : {
        command: "bash ../scripts/run-playwright-server.sh",
        cwd: CONFIG_DIR,
        url: `http://127.0.0.1:${PORT}/ajax-api/2.0/mlflow/caliber/health`,
        timeout: 360_000,
        reuseExistingServer: true,
      },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
      },
    },
  ],
});
