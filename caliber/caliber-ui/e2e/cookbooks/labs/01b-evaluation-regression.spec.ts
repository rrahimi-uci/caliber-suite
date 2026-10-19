import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";

/**
 * Cookbook 01, the evaluation half of "Run it through the UI": score prompt v1 and a weaker v2 on
 * the imported test set, and see the regression against the baseline.
 *
 * This needs a model that answers, so it runs against a different server than
 * `01-prompt-release-loop`: one started with a stand-in model (README, "Run the evaluation with a
 * stand-in model"):
 *
 *     python docs-site/cookbooks/01-prompt-regression-lab/assets/stand-in-model/server.py --port 8765
 *     CALIBER_LLM_PROVIDER=openai OPENAI_API_KEY=stand-in OPENAI_BASE_URL=http://127.0.0.1:8765/v1 \
 *       CALIBER_EVAL_PROVIDER=mlflow scripts/cookbook-env.sh up lab01eval --flags cookbooks --real-llm
 *
 * The stand-in answers with a bare JSON record when the prompt says "JSON ONLY" and with a chatty
 * sentence plus a code fence when it does not. So the only difference between the two scored runs
 * is the prompt text, which is what the comparison has to surface. It proves the wiring, not how a
 * real model scores.
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
const ROWS = fs
  .readFileSync(DATASET_FILE, "utf8")
  .split("\n")
  .filter((line) => line.trim()).length;

function promptBody(file: string): string {
  const text = fs.readFileSync(path.join(ASSETS, "prompts", file), "utf8");
  return (
    (text.startsWith("---") ? text.split("\n---\n")[1]! : text).trim() + "\n"
  );
}

test.describe("Cookbook 01: evaluation and regression (stand-in model)", () => {
  test("scores v1 and a weaker v2 and shows the regression against the baseline", async ({
    page,
  }) => {
    test.setTimeout(420_000);
    const suffix = uiOnlySlug("c01b");
    const promptName = `cookbook-01-eval-${suffix}`;
    const datasetName = `cookbook-01-eval-golden-${suffix}`;
    const baselineLabel = `baseline v1 ${suffix}`;
    const weakLabel = `weaker v2 ${suffix}`;
    await new CaliberUi(page).signIn();

    // Prompt v1 (strict JSON) is live; v2 (JSON-only rules dropped) is a draft.
    await page.goto("/caliber/prompts");
    await page.getByRole("button", { name: "New prompt" }).click();
    await page.getByRole("button", { name: /Write \/ paste/ }).click();
    await page
      .getByLabel("Prompt text")
      .fill(promptBody("intake-classifier.md"));
    await page.getByLabel("Prompt name").fill(promptName);
    await page.getByRole("button", { name: "Create and Open" }).click();
    await expect(page.getByTestId("version-panel")).toBeVisible();
    await page
      .getByLabel("Prompt template")
      .fill(promptBody("intake-classifier-weak.md"));
    await page.getByRole("button", { name: "Save draft" }).click();
    await expect(page.getByTestId("version-row-2")).toBeVisible();

    // Import the test set from the file.
    await page
      .getByRole("button", { name: "Calibration", exact: true })
      .click();
    await page
      .getByLabel("Upload calibration dataset")
      .setInputFiles(DATASET_FILE);
    await page.getByPlaceholder("Dataset name").fill(datasetName);
    await page.getByRole("button", { name: "Upload Dataset" }).click();
    await expect(
      page.getByText(`Uploaded ${ROWS} examples to ${datasetName}.`),
    ).toBeVisible({
      timeout: 120_000,
    });

    // Evaluations: score each prompt version with the Token F1 grader only.
    for (const [version, label] of [
      [1, baselineLabel],
      [2, weakLabel],
    ] as const) {
      await page.goto("/caliber/evaluations");
      await page.getByRole("button", { name: "Run evaluation" }).click();
      await page
        .getByLabel("Test set", { exact: true })
        .selectOption({ label: `${datasetName} (v${ROWS + 1})` });
      await page.getByLabel("Label", { exact: true }).fill(label);
      await page.getByLabel("What to score").selectOption("prompt");
      await page
        .getByLabel("Subject under test")
        .fill(`${promptName}@${version}`);
      await page.getByText("Exact match", { exact: true }).click();
      await page.getByText("Contains expected", { exact: true }).click();
      await page.getByRole("button", { name: "Run", exact: true }).click();
      const row = page.getByTestId("eval-run-row").filter({ hasText: label });
      await expect(row).toBeVisible({ timeout: 240_000 });
      await expect(row).toContainText("completed");
      await expect(row).toContainText(`/${ROWS}`);
    }

    // The baseline's own scorecard: prompt target, subject, and bare JSON predictions.
    await page
      .getByTestId("eval-run-row")
      .filter({ hasText: baselineLabel })
      .getByRole("link")
      .click();
    await expect(page.getByText(`prompt: ${promptName}@1`)).toBeVisible();
    const baselinePass = await page.getByTestId("eval-aggregate").textContent();
    expect(baselinePass).toContain("Token F1");
    await expect(page.getByTestId("eval-result-row")).toHaveCount(ROWS);
    await expect(page.getByTestId("eval-result-row").first()).toContainText(
      '"intent": "billing"',
    );

    // The weaker v2, compared to the baseline: scores fall and the predictions are chatty prose.
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
    await expect(page.getByTestId("baseline-comparison-context")).toContainText(
      `${promptName}@1`,
    );
    const aggregate = page.getByTestId("eval-aggregate");
    await expect(aggregate.getByTitle("vs. baseline run")).toHaveCount(2);
    await expect(aggregate.getByTitle("vs. baseline run").first()).toHaveClass(
      /text-red-600/,
    );
    await expect(page.getByTestId("eval-result-row").first()).toContainText(
      "Happy to help!",
    );
    await expect(page.getByTestId("eval-result-row").first()).toContainText(
      "Fail",
    );
  });
});
