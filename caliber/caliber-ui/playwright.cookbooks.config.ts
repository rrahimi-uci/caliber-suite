import { defineConfig, devices } from "@playwright/test";

const PORT = Number(process.env["CALIBER_COOKBOOK_E2E_PORT"] ?? 5160);

export default defineConfig({
  testDir: "./e2e/cookbooks",
  fullyParallel: false,
  workers: 1,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}/caliber`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 900 },
  },
  webServer: {
    command: `MLFLOW_PORT=${PORT} MLFLOW_WORKERS=1 CALIBER_E2E_ENV_FILE=/dev/null CALIBER_DATABASE_URL= CALIBER_E2E_TMP_ROOT=$PWD/../.tmp/cookbooks-ui-only CALIBER_SKIP_KNOWLEDGE_WARMUP=1 CALIBER_BACKGROUND_TASKS_ENABLED=false CALIBER_ASSISTANT_ENGINE=fake bash ../scripts/run-playwright-server.sh`,
    url: `http://127.0.0.1:${PORT}/ajax-api/2.0/mlflow/caliber/health`,
    timeout: 360_000,
    reuseExistingServer: false,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
