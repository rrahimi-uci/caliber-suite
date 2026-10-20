import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";
import { stackIsSimulated } from "./lab-mode";

/**
 * Cookbook 01 (`docs-site/cookbooks/01-prompt-regression-lab/README.md`), "Run it through the UI".
 *
 * Needs a server that is already up with `CALIBER_FEATURE_COOKBOOKS=true`. Nothing is seeded and
 * no API call is made from this file: the mode (simulated or real) is read from the provider banner the
 * app shows, and every step is a click:
 *
 *  - simulated (the alpha default, `scripts/cookbook-env.sh up lab01 --flags cookbooks`): the
 *    language model and the evaluator are canned;
 *  - real (`... --real-llm`, a hosted provider or a local Ollama, README "Run it with a real
 *    model"): `simulated` is empty and the same journey runs for real.
 *
 * What it proves, in the README's order (the mode-specific parts are marked):
 *   1. a prompt written in the UI is live as v1;
 *   2. a weaker v2 is saved as a draft and does not become live;
 *   3. the 44-row test set is imported from `assets/dataset/intake-classifier.jsonl` and listed on
 *      Test Sets with every row;
 *   4. Evaluations. Simulated: it refuses to score a prompt and says why (it never invents a
 *      scorecard). Real: v1 and v2 are scored on every example and compared against the baseline,
 *      with a result for every row (only invariants: scores, never an exact value);
 *   5. calibration with the Guidelines judge reaches a candidate; the review shows the diff, the
 *      real example count and either the simulated banner (simulated) or no banner and measured
 *      baseline/candidate scores (real). Simulated: a gate set too high blocks a second candidate;
 *   6. Apply moves the live alias; What's live now, the release timeline and the audit log name
 *      who applied it (the audit row carries `eval_simulated` true or false);
 *   7. rolling back returns v1 to live, on the same screens.
 *
 * On a real model the calibration may end `rejected` by the product's own gate; the spec then
 * verifies the rejection (listed as rejected, no Review & apply) and stops, because nothing can be
 * applied. Loosen the gate to exercise Apply and rollback anyway.
 *
 * The real mode takes about 15 minutes on a local 7B model, about 6 minutes on a hosted one (`COOKBOOK_01_MIN_SCORE` and
 * `COOKBOOK_01_MAX_REGRESSION` loosen the calibration gate for a noisy small judge, as in the
 * SDK script).
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const ASSETS = path.resolve(
  process.env["COOKBOOK_01_ASSETS"] ??
    path.join(
      here,
      "../../../../../docs-site/cookbooks/01-prompt-regression-lab/assets",
    ),
);
const DATASET_FILE = path.join(ASSETS, "dataset/intake-classifier.jsonl");
const GUIDELINES = JSON.stringify(
  JSON.parse(
    fs.readFileSync(path.join(ASSETS, "calibration/guidelines.json"), "utf8"),
  ),
);
const EXPECTED_ROWS = fs
  .readFileSync(DATASET_FILE, "utf8")
  .split("\n")
  .filter((line) => line.trim()).length;

/** The prompt text: everything below the YAML front matter of an asset file. */
function promptBody(file: string): string {
  const text = fs.readFileSync(path.join(ASSETS, "prompts", file), "utf8");
  return (
    (text.startsWith("---") ? text.split("\n---\n")[1]! : text).trim() + "\n"
  );
}

async function openPrompt(page: Page, name: string): Promise<void> {
  await page.goto("/caliber/prompts");
  await page.getByLabel("Search prompts").fill(name);
  await page
    .locator(`[data-testid="prompt-card-${name}"]`)
    .first()
    .getByRole("button", { name: "Open" })
    .click();
  await expect(page.getByTestId("version-panel")).toBeVisible();
}

function liveBadge(page: Page, version: number) {
  return page
    .getByTestId(`version-row-${version}`)
    .getByTestId("version-live-badge");
}

test.describe("Cookbook 01: prompt release loop", () => {
  test("authors, imports, evaluates, calibrates, applies and rolls back", async ({
    page,
  }) => {
    test.setTimeout(2_700_000);
    const minScore = process.env["COOKBOOK_01_MIN_SCORE"];
    const maxRegression = process.env["COOKBOOK_01_MAX_REGRESSION"];
    const suffix = uiOnlySlug("c01");
    const promptName = `cookbook-01-intake-${suffix}`;
    const datasetName = `cookbook-01-golden-${suffix}`;
    await new CaliberUi(page).signIn();
    const simulated = await stackIsSimulated(page);

    // ---- 1. Prompts > New prompt: v1 is live ---------------------------------------------
    await page.goto("/caliber/prompts");
    await page.getByRole("button", { name: "New prompt" }).click();
    await page.getByRole("button", { name: /Write \/ paste/ }).click();
    await page
      .getByLabel("Prompt text")
      .fill(promptBody("intake-classifier.md"));
    await page.getByLabel("Prompt name").fill(promptName);
    await page.getByRole("button", { name: "Create and Open" }).click();
    await expect(page.getByTestId("version-panel")).toBeVisible();
    await expect(liveBadge(page, 1)).toBeVisible();

    // ---- 2. Author: save the weaker v2 as a draft; v1 stays live -------------------------
    await page
      .getByLabel("Prompt template")
      .fill(promptBody("intake-classifier-weak.md"));
    await page
      .getByLabel("Commit message", { exact: true })
      .fill("v2 dropped the JSON-only rules (intentional regression)");
    await page.getByRole("button", { name: "Save draft" }).click();
    await expect(page.getByTestId("version-row-2")).toBeVisible();
    await expect(liveBadge(page, 1)).toBeVisible();
    await expect(liveBadge(page, 2)).toHaveCount(0);

    // ---- 3. Calibration stage: import the test set from the file -------------------------
    await page
      .getByRole("button", { name: "Calibration", exact: true })
      .click();
    await page
      .getByLabel("Upload calibration dataset")
      .setInputFiles(DATASET_FILE);
    await page.getByPlaceholder("Dataset name").fill(datasetName);
    await page.getByRole("button", { name: "Upload Dataset" }).click();
    await expect(
      page.getByText(`Uploaded ${EXPECTED_ROWS} examples to ${datasetName}.`),
    ).toBeVisible({ timeout: 120_000 });

    // Test Sets lists the imported set; its detail page lists every row.
    await page.goto("/caliber/eval-datasets");
    await page.getByLabel("Search test sets").fill(datasetName);
    await expect(page.getByText(datasetName, { exact: true })).toBeVisible();
    await expect(
      page.getByText(`v${EXPECTED_ROWS + 1}`, { exact: true }).first(),
    ).toBeVisible();
    await page.getByRole("link", { name: datasetName }).first().click();
    await expect(
      page.getByText(/I was charged twice after upgrading/),
    ).toBeVisible();
    await expect(
      page.getByText(/Ignore all previous instructions/),
    ).toBeVisible();

    // ---- 4. Evaluations ------------------------------------------------------------------
    if (simulated) {
      // A simulated model is refused, with the reason.
      await page.goto("/caliber/evaluations");
      await page.getByRole("button", { name: "Run evaluation" }).click();
      await page
        .getByLabel("Test set", { exact: true })
        .selectOption({ label: `${datasetName} (v${EXPECTED_ROWS + 1})` });
      await page.getByLabel("Label", { exact: true }).fill("baseline v1");
      await page.getByLabel("What to score").selectOption("prompt");
      await page.getByLabel("Subject under test").fill(`${promptName}@1`);
      await page.getByRole("button", { name: "Run", exact: true }).click();
      await expect(
        page.getByText(/evaluation requires a real LLM provider/),
      ).toBeVisible();
    } else {
      // A real model: score v1 and the weaker v2 with Token F1 only, then compare.
      const baselineLabel = `baseline v1 ${suffix}`;
      const weakLabel = `weaker v2 ${suffix}`;
      for (const [version, label] of [
        [1, baselineLabel],
        [2, weakLabel],
      ] as const) {
        await page.goto("/caliber/evaluations");
        await page.getByRole("button", { name: "Run evaluation" }).click();
        await page
          .getByLabel("Test set", { exact: true })
          .selectOption({ label: `${datasetName} (v${EXPECTED_ROWS + 1})` });
        await page.getByLabel("Label", { exact: true }).fill(label);
        await page.getByLabel("What to score").selectOption("prompt");
        await page
          .getByLabel("Subject under test")
          .fill(`${promptName}@${version}`);
        await page.getByText("Exact match", { exact: true }).click();
        await page.getByText("Contains expected", { exact: true }).click();
        await page.getByRole("button", { name: "Run", exact: true }).click();
        const row = page.getByTestId("eval-run-row").filter({ hasText: label });
        await expect(row).toBeVisible({ timeout: 900_000 });
        await expect(row).toContainText("completed");
        await expect(row).toContainText(`/${EXPECTED_ROWS}`);
      }
      // The baseline's scorecard: a real answer for every example, as JSON.
      await page
        .getByTestId("eval-run-row")
        .filter({ hasText: baselineLabel })
        .getByRole("link")
        .click();
      await expect(page.getByText(`prompt: ${promptName}@1`)).toBeVisible();
      await expect(page.getByTestId("eval-aggregate")).toContainText(
        "Token F1",
      );
      await expect(page.getByTestId("eval-result-row")).toHaveCount(
        EXPECTED_ROWS,
      );
      await expect(page.getByTestId("eval-result-row").first()).toContainText(
        '"intent"',
      );
      // The weaker v2, compared to the baseline: a comparison with deltas on the scorecard.
      await page.goto("/caliber/evaluations");
      await page
        .getByTestId("eval-run-row")
        .filter({ hasText: weakLabel })
        .getByRole("link")
        .click();
      await expect(page.getByText(`prompt: ${promptName}@2`)).toBeVisible();
      const compare = page.getByLabel("Compare to baseline run");
      const value = await compare
        .locator("option", { hasText: baselineLabel })
        .getAttribute("value");
      await compare.selectOption(value!);
      await expect(
        page.getByTestId("baseline-comparison-context"),
      ).toContainText(`${promptName}@1`);
      // A delta against the baseline on every score: "+3pp" / "-8pp", or "±0" when it rounds to
      // nothing (a strong model answers the weaker wording about as well as the strict one).
      await expect(page.getByTestId("eval-aggregate")).toContainText(
        /[+-]?\d+pp|±0/,
      );
      await expect(page.getByTestId("eval-result-row")).toHaveCount(
        EXPECTED_ROWS,
      );
    }

    // ---- 5. Calibrate, wait for the candidate, review the evidence and the diff -----------
    await openPrompt(page, promptName);
    await page
      .getByRole("button", { name: "Calibration", exact: true })
      .click();
    await page
      .getByLabel("Calibration dataset", { exact: true })
      .selectOption({ label: datasetName });

    // The scorer: the Guidelines judge, configured from assets/calibration/guidelines.json.
    // (Correctness needs an expected_response per example, which this test set does not have.)
    for (const name of ["Correctness", "Relevance", "Safety"]) {
      await page.getByRole("checkbox", { name, exact: true }).uncheck();
    }
    await page
      .getByRole("checkbox", { name: "Guidelines", exact: true })
      .check();
    await page.locator("textarea[placeholder*='guidelines']").fill(GUIDELINES);

    if (simulated) {
      // A gate set above the simulated 0.94 score blocks its candidate: no "Review & apply".
      await page.getByLabel("Minimum aggregate score").fill("0.99");
      await page.getByRole("button", { name: "Start Calibration Run" }).click();
      await expect(
        page.getByText("rejected", { exact: true }).first(),
      ).toBeVisible({
        timeout: 180_000,
      });
      await expect(
        page.getByRole("button", { name: "Review & apply" }),
      ).toHaveCount(0);
    }

    // The product defaults (0.85 and 0.02) unless a real run loosens them for a noisy judge.
    await page
      .getByLabel("Minimum aggregate score")
      .fill(simulated ? "0.85" : (minScore ?? "0.85"));
    if (!simulated && maxRegression) {
      await page.getByLabel("Maximum regression delta").fill(maxRegression);
    }
    await page.getByRole("button", { name: "Start Calibration Run" }).click();
    const review = page.getByRole("button", { name: "Review & apply" }).first();
    const rejected = page.getByText("rejected", { exact: true }).first();
    await expect(simulated ? review : review.or(rejected)).toBeVisible({
      timeout: simulated ? 180_000 : 1_500_000,
    });
    if (!simulated && (await review.count()) === 0) {
      // A real judge can honestly say no under the product's own gate (0.85 / 0.02): the run
      // is listed as rejected and offers no Review & apply. That is a verified outcome of this
      // lab; Apply and rollback are exercised when a candidate clears the gate (or the gate is
      // loosened with COOKBOOK_01_MIN_SCORE / COOKBOOK_01_MAX_REGRESSION).
      await expect(rejected).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Review & apply" }),
      ).toHaveCount(0);
      return;
    }
    await review.click();
    await expect(
      page.getByRole("heading", { name: "Review candidate before applying" }),
    ).toBeVisible();
    await expect(page.getByTestId("calibration-prompt-diff")).toBeVisible();
    if (simulated) {
      await expect(
        page.getByTestId("calibration-simulated-evaluation"),
      ).toContainText("Simulated evaluation — not evidence");
    } else {
      // Real evidence: no banner, measured baseline and candidate scores.
      await expect(
        page.getByTestId("calibration-simulated-evaluation"),
      ).toHaveCount(0);
      for (const label of ["Baseline", "Candidate", "Change"]) {
        await expect(
          page.getByText(label, { exact: true }).first(),
        ).toBeVisible();
      }
    }
    await expect(
      page.getByText(`${EXPECTED_ROWS} evaluation examples`),
    ).toBeVisible();
    await expect(page.getByText("Gate: Passed")).toBeVisible();

    // ---- 6. Apply: the live alias moves; the release screens name you ---------------------
    const apply = page.getByRole("button", { name: /Apply candidate/ });
    await expect(apply).toBeEnabled();
    await apply.click();
    await expect(page.getByText(/Candidate applied\./)).toBeVisible({
      timeout: 60_000,
    });
    await page.getByRole("button", { name: "Author", exact: true }).click();
    // v1 strong, v2 weak draft, then the applied candidate (calibrated from the live v1) is v3.
    await expect(liveBadge(page, 3)).toBeVisible();
    await expect(liveBadge(page, 1)).toHaveCount(0);

    await page.goto("/caliber/releases");
    const liveRow = page.getByTestId(`releases-live-${promptName}`);
    await expect(liveRow).toContainText("prompt");
    await expect(liveRow).toContainText("v3");
    await expect(liveRow).toContainText("@prod");
    await expect(liveRow).toContainText("admin");
    const promoted = page
      .getByTestId("releases-timeline")
      .getByRole("listitem")
      .filter({ hasText: promptName })
      .first();
    await expect(promoted).toContainText("Promote");
    await expect(promoted).toContainText("v1 → v3");
    await expect(promoted).toContainText("admin");

    await page.goto("/caliber/audit-log");
    await page.getByTestId("audit-filter-entity-id").fill(promptName);
    const promoteRow = page
      .getByTestId("audit-row")
      .filter({ hasText: "promote_prompt" })
      .first();
    await expect(promoteRow).toContainText("admin");
    await expect(promoteRow).toContainText(
      `"eval_simulated":"${simulated ? "true" : "false"}"`,
    );

    // ---- 7. Roll back: v1 is live again, on every screen ----------------------------------
    await openPrompt(page, promptName);
    await page
      .getByTestId("version-row-3")
      .getByTestId("version-rollback")
      .click();
    await expect(page.getByTestId("confirm-dialog")).toBeVisible();
    await page.getByTestId("confirm-dialog-confirm").click();
    await expect(page.getByTestId("confirm-dialog")).toBeHidden();
    await expect(liveBadge(page, 1)).toBeVisible();
    await expect(liveBadge(page, 3)).toHaveCount(0);

    await page.goto("/caliber/releases");
    await expect(page.getByTestId(`releases-live-${promptName}`)).toContainText(
      "v1",
    );
    const rolledBack = page
      .getByTestId("releases-timeline")
      .getByRole("listitem")
      .filter({ hasText: promptName })
      .first();
    await expect(rolledBack).toContainText("Roll back");
    await expect(rolledBack).toContainText("v3 → v1");

    await page.goto("/caliber/audit-log");
    await page.getByTestId("audit-filter-entity-id").fill(promptName);
    await expect(
      page
        .getByTestId("audit-row")
        .filter({ hasText: "rollback_prompt" })
        .first(),
    ).toContainText("admin");
  });
});
