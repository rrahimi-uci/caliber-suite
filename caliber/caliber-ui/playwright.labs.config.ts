import { defineConfig, devices } from "@playwright/test";

/**
 * The cookbook labs' browser paths, run against a server that is already up.
 *
 * Each lab README has a "Run it through the UI" section and a spec in
 * `e2e/cookbooks/labs/` that performs those same steps with real clicks. They need a server with
 * the lab's feature flags on, which `scripts/cookbook-env.sh up NAME --flags ...` starts:
 *
 *     scripts/cookbook-env.sh up labs --flags all
 *     source .tmp/cookbook-env/labs/env.sh
 *     cd caliber/caliber-ui
 *     CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" \
 *       npx playwright test --config=playwright.labs.config.ts
 *
 * No server is started here, so a run can never reuse (or disturb) another one.
 */
const BASE_URL = process.env["CALIBER_LAB_BASE_URL"];
if (!BASE_URL) {
  throw new Error(
    "CALIBER_LAB_BASE_URL is required, for example http://127.0.0.1:5001/caliber (see scripts/cookbook-env.sh)",
  );
}

export default defineConfig({
  testDir: "./e2e/cookbooks/labs",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 240_000,
  expect: { timeout: 20_000 },
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 900 },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
