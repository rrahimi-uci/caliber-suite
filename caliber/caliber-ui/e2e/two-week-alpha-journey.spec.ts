import { expect, test } from "@playwright/test";

import { signIn } from "./helpers";

/**
 * Day 9 of `docs/two-week-alpha-plan.md` (Week 2): "One end-to-end regression
 * test (a single Playwright journey, or one API-level integration test) that
 * runs the seed fixture through to Apply and asserts the outcome." Acceptance:
 * "Test passes on a clean checkout; it is the one piece of 'proof this still
 * works' going forward."
 *
 * This drives the real browser through the full journey Days 1-8 built:
 * a seeded flagged item on the Diagnosis tab -> its linked, already
 * `candidate_ready` refinement job (real DSPy candidate content, real eval
 * evidence) -> the Calibration tab's Apply flow -> a genuine live-version
 * change -> rollback via the Author tab's version history, restoring the
 * prior live version. See `docs/reports/two-week-alpha-day9-decision.md` for
 * the full design rationale, including why this is a real Playwright journey
 * rather than an API-level fallback, and how the fixture (a flagged
 * verification item + a `candidate_ready` `CaliberRefinementJob`, both
 * produced by real backend code, not stubs) gets into the database the
 * Playwright dev server points at.
 *
 * The fixture prompt is seeded once, before any spec runs, by
 * `scripts/run-playwright-server.sh` invoking `scripts/two_week_alpha_e2e_seed.py`
 * against a freshly-created DB -- this spec does not seed anything itself,
 * it only drives the browser against what is already there. `PROMPT_NAME`
 * must match `tests/fixtures/day1_seed_fixture.py::DEFAULT_AGENT_ID` (the
 * fixture's `agent_id`, which is also the prompt's name -- see
 * `caliber/prompt_targets.py::ensure_prompt_target`) and
 * `scripts/two_week_alpha_e2e_seed.py::PROMPT_NAME`.
 *
 * Backend note: run this against a Postgres-backed server for a reliable
 * result --
 * `CALIBER_DATABASE_URL=postgresql+psycopg://user@host/db npx playwright
 * test e2e/two-week-alpha-journey.spec.ts`. Against this harness's SQLite
 * default, the real Apply step (a genuine `mlflow.genai.register_prompt`
 * write racing this server's own periodic background workers on one
 * single-writer file) can starve past even a 30s busy-timeout -- a verified,
 * pre-existing SQLite concurrency limitation, not a bug in this journey or
 * in the Days 1-8 code it drives. See
 * `docs/reports/two-week-alpha-day9-decision.md` for the full investigation
 * and the Postgres-backed confirmation runs.
 */
const PROMPT_NAME = "intake-classifier-day1-seed";

test.describe("Two-week alpha journey (Day 9)", () => {
  test("runs the seeded flagged item through diagnosis, apply, and rollback", async ({
    page,
  }) => {
    // This journey drives real backend work end to end (a real Apply
    // through MLflowPromoter/release_operations, then a real rollback),
    // not just page loads -- give it more than the config's default 90s.
    test.setTimeout(180_000);
    await signIn(page);

    // ---- Open the seeded prompt's Workspace -------------------------------
    await page.goto("/caliber/prompts");
    await expect(page.getByRole("heading", { name: "Prompts" })).toBeVisible();
    await page.getByLabel("Search prompts").fill(PROMPT_NAME);
    const card = page
      .locator(`[data-testid="prompt-card-${PROMPT_NAME}"]`)
      .first();
    await expect(card).toBeVisible();
    await card.getByRole("button", { name: "Open" }).click();

    // ---- Diagnosis tab: open the flagged item, see why it was flagged -----
    await page.getByRole("button", { name: "Diagnosis", exact: true }).click();
    const itemRow = page
      .locator('[data-testid^="diagnosis-item-row-"]')
      .first();
    await expect(itemRow).toBeVisible();
    await itemRow.click();

    const detail = page.getByTestId("diagnosis-item-detail");
    await expect(detail).toBeVisible();
    await expect(detail).toContainText("classification_accuracy");
    await expect(detail).toContainText(
      "reports both a billing dispute and a page crash",
    );

    // The seeded job is already candidate_ready (real DSPy candidate + real
    // eval evidence, produced by tests/fixtures/day2_day3_pipeline.py) --
    // confirm the linked job and its eval gate are visible before handing off.
    const linkedJob = page.getByTestId("diagnosis-linked-job");
    await expect(linkedJob).toBeVisible();
    await expect(linkedJob).toContainText("Gate passed");

    // ---- Hand off to Calibration ------------------------------------------
    await page.getByTestId("diagnosis-open-calibration-btn").click();
    await expect(
      page.getByRole("button", { name: "Calibration", exact: true }),
    ).toHaveAttribute("aria-selected", "true");

    // Two elements share `job-apply-btn` (the Active Run panel and the
    // Recent Prompt Runs table row for the same job) -- the Active Run panel
    // renders first in the DOM, so `.first()` targets it deterministically.
    const applyButton = page.getByTestId("job-apply-btn").first();
    await expect(applyButton).toBeVisible();
    await applyButton.click();

    // ---- Review candidate/eval evidence and Apply --------------------------
    await expect(page.getByText("Review candidate before applying")).toBeVisible();
    await expect(page.getByTestId("calibration-prompt-diff")).toBeVisible();
    await expect(page.getByText("Evaluation score")).toBeVisible();
    await expect(page.getByText("Gate: Passed")).toBeVisible();

    const confirmApply = page.getByRole("button", { name: /Apply candidate/ });
    await expect(confirmApply).toBeEnabled();
    await confirmApply.click();

    // ---- See the applied outcome -------------------------------------------
    // `page.getByRole("status")` would also match the unrelated
    // `data-testid="provider-banner"` simulated-providers notice (also
    // `role="status"`). Apply drives a real chain (MLflowPromoter -> real
    // mlflow.genai register/set-alias calls -> the real
    // caliber.release_operations state machine, several DB commits) against
    // the real local backend store, so it genuinely takes longer than the
    // default 12s expect timeout under this DSPy-warmed, single-worker E2E
    // server -- give it a generous, explicit budget rather than assume it's
    // stuck the moment the default timeout would otherwise fire.
    await expect(page.getByText(/Candidate applied\./)).toBeVisible({
      timeout: 45_000,
    });
    await expect(page.getByText("applied", { exact: true }).first()).toBeVisible();

    // ---- Rollback via the Author tab's version history ----------------------
    await page.getByRole("button", { name: "Author", exact: true }).click();
    const versionPanel = page.getByTestId("version-panel");
    await expect(versionPanel).toBeVisible();

    // Apply registered and promoted a new (second) version -- it is now the
    // one live version, and its own row is what exposes rollback.
    const liveRow = page
      .getByTestId(/^version-row-/)
      .filter({ has: page.getByTestId("version-live-badge") });
    await expect(liveRow).toBeVisible();
    // Capture which version Apply just promoted (e.g. "version-row-2") before
    // rolling back, so the post-rollback assertion below proves the *live*
    // version actually changed, not just that some row still has a live
    // badge (which would be true even if rollback silently no-op'd).
    const appliedVersionTestId = await liveRow.getAttribute("data-testid");
    expect(appliedVersionTestId).toBeTruthy();
    const rollbackButton = liveRow.getByTestId("version-rollback");
    await expect(rollbackButton).toBeVisible();
    await rollbackButton.click();

    await expect(page.getByTestId("confirm-dialog")).toBeVisible();
    await expect(page.getByText("Roll back the live version")).toBeVisible();
    await page.getByTestId("confirm-dialog-confirm").click();
    await expect(page.getByTestId("confirm-dialog")).toBeHidden();

    // ---- See the prior state restored ---------------------------------------
    // Rollback restores the seed fixture's own v1 template as the live
    // version again -- the version list reloads (VersionPanel's own
    // post-action `reload()`) and the live badge moves off the version Apply
    // just promoted, onto the prior version.
    await expect(
      versionPanel.getByTestId("version-panel-action-error"),
    ).toHaveCount(0);
    const liveRowAfterRollback = page
      .getByTestId(/^version-row-/)
      .filter({ has: page.getByTestId("version-live-badge") });
    await expect(liveRowAfterRollback).toBeVisible();
    await expect(liveRowAfterRollback).not.toHaveAttribute(
      "data-testid",
      appliedVersionTestId as string,
    );
  });
});
