import { defineConfig, devices } from "@playwright/test";

/**
 * The alpha journey gate (alpha gap P0-6): the specs that prove the product's one
 * promise (find a problem, calibrate, review, apply, roll back) on an isolated server
 * with the shipped defaults: fake LLM, fake evaluator, real MLflow promoter, background
 * workers on.
 *
 *   - two-week-alpha-journey.spec.ts: a fixture-seeded flagged item through diagnosis,
 *     Apply and rollback.
 *   - user-prompt-journey.spec.ts: nothing seeded; a prompt created in the UI is
 *     calibrated, reviewed, applied and rolled back.
 *   - skill-journey.spec.ts: the same loop for a skill (alpha gap P1-20): render
 *     preview, calibrate, review the simulated evidence, apply, roll back.
 *   - tool-journey.spec.ts: register a tool, run it, calibrate its fixtures, pin a
 *     baseline, see it used by a workflow (alpha gap P1-21).
 *   - build-workflows.spec.ts and workflow-studio.spec.ts (alpha gap P1-22): the skill
 *     and tool builders, and the workflow studio from template to run, approval, and
 *     the wait-for-event and wait-until resumes, with the run queue on.
 *   - feature-flags.spec.ts: the four feature-flagged areas (Aria, MCP and OpenAPI
 *     integrations, cookbooks, release-candidate signoff) carry their shipped defaults —
 *     Aria on, the other three off, hidden and refused.
 *
 * The server starts with NO `CALIBER_FEATURE_*` variable: the whole point of this gate is that
 * the shipped defaults carry the journey, not whatever a developer's shell happens to export.
 * The command unsets them so an exported variable cannot silently change an area's state and
 * make the gate lie. The full suite (`playwright.config.ts`) and the cookbook journeys turn the
 * flags they need on.
 *
 * Run with `npm run test:e2e:alpha`. It gets its own port and state directory, so it
 * never reuses (or disturbs) the server a developer has running for `npm run test:e2e`.
 */
const PORT = Number(process.env["CALIBER_ALPHA_E2E_PORT"] ?? 5170);

export default defineConfig({
  testDir: "./e2e",
  testMatch: [
    "two-week-alpha-journey.spec.ts",
    "user-prompt-journey.spec.ts",
    "skill-journey.spec.ts",
    "tool-journey.spec.ts",
    "build-workflows.spec.ts",
    "workflow-studio.spec.ts",
    "feature-flags.spec.ts",
  ],
  // The object store needs MinIO, which this harness does not start; the stack smoke covers it.
  grepInvert: /object store/,
  fullyParallel: false,
  workers: 1,
  timeout: 240_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}/caliber`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 900 },
  },
  webServer: {
    command: `unset CALIBER_FEATURE_ARIA CALIBER_FEATURE_INTEGRATIONS CALIBER_FEATURE_COOKBOOKS CALIBER_FEATURE_RELEASE_CANDIDATES; MLFLOW_PORT=${PORT} MLFLOW_WORKERS=1 CALIBER_E2E_ENV_FILE=/dev/null CALIBER_DATABASE_URL= CALIBER_E2E_TMP_ROOT=$PWD/../.tmp/alpha-journey CALIBER_SKIP_KNOWLEDGE_WARMUP=1 bash ../scripts/run-playwright-server.sh`,
    url: `http://127.0.0.1:${PORT}/ajax-api/2.0/mlflow/caliber/health`,
    timeout: 360_000,
    reuseExistingServer: false,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
