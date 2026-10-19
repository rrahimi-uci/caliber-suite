import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";

/**
 * Cookbook 12, "Run it through the UI": give Aria one sentence, answer its questions, and find the
 * judge and the test set it made. It needs a server with the Aria and Cookbooks flags on
 * (`scripts/cookbook-env.sh up lab12 --flags cookbooks,aria`); nothing here needs a model.
 *
 * The planner is deterministic, so the plan is asserted exactly. Aria creates the test set empty;
 * three of the 30 rows are added through the row editor (the rest are the script's job).
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(here, "../../../../..");
const ASSETS = path.resolve(
  process.env["COOKBOOK_12_ASSETS"] ??
    path.join(REPO, "docs-site/cookbooks/12-aria-evaluation-harness/assets"),
);

interface Case {
  id: string;
  tags: string[];
  input: Record<string, string>;
  expected: Record<string, string>;
}
const CASES: Case[] = fs
  .readFileSync(path.join(ASSETS, "dataset/faithfulness-cases.jsonl"), "utf8")
  .split("\n")
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line) as Case);
const JUDGE = JSON.parse(
  fs.readFileSync(
    path.join(ASSETS, "judges/answer-faithfulness.judge.json"),
    "utf8",
  ),
) as { name: string; instructions: string; feedback_value_type: string };
const DATASET = JSON.parse(
  fs.readFileSync(
    path.join(ASSETS, "datasets/support-faithfulness.dataset.json"),
    "utf8",
  ),
) as { name: string; description: string; tags: string[] };
const INTENT = fs
  .readFileSync(path.join(ASSETS, "intent.md"), "utf8")
  .split("\n")
  .find((line) => line.startsWith("> Create a judge"))!
  .replace(/^> /, "");

/** The label of a field in the "Aria needs information" form. */
function field(page: Page, label: RegExp) {
  return page
    .locator("label")
    .filter({ hasText: label })
    .locator("input, textarea, select");
}

test.describe("Cookbook 12: an evaluation harness from one sentence", () => {
  test("states the intent, answers the questions and finds the judge and the test set", async ({
    page,
  }) => {
    const suffix = uiOnlySlug("c12");
    const judgeName = `${JUDGE.name}-${suffix}`;
    const datasetName = `${DATASET.name}-${suffix}`;
    const ui = new CaliberUi(page);
    await ui.signIn();

    // ---- 1. Plans > Goal ---------------------------------------------------------------
    const nav = page.getByLabel("CALIBER navigation");
    await nav.getByRole("button", { name: /^Integrations/ }).click();
    await nav.getByRole("link", { name: "Plans" }).click();
    await expect(page).toHaveURL(/\/caliber\/aria\/plans$/);
    await expect(
      page.getByRole("heading", { name: "Aria Plans" }),
    ).toBeVisible();
    await page.getByLabel("Goal").fill(INTENT);
    await page
      .getByLabel("Autonomy")
      .selectOption({ label: "Stop before each change" });
    await page.getByRole("button", { name: "Decompose goal" }).click();

    // ---- 2. The drawn plan: two steps, nothing run -----------------------------------
    await expect(page.getByText("Plan steps")).toBeVisible();
    await expect(page.getByText("2 steps")).toBeVisible();
    const steps = page.locator("ol > li");
    await expect(steps).toHaveCount(2);
    await expect(steps.nth(0)).toContainText("judge.create");
    await expect(steps.nth(1)).toContainText("eval_dataset.create");
    await expect(steps.nth(0)).toContainText("pending");
    await expect(page.getByTitle("draft", { exact: true })).toBeVisible();

    // ---- 3. Approve ----------------------------------------------------------------------
    await page.getByRole("button", { name: "Approve plan" }).click();
    await expect(page.getByTitle("approved", { exact: true })).toBeVisible();

    // ---- 4. Execute: the judge form, then an approval, then the test set's ----------
    await page.getByRole("button", { name: "Execute" }).click();
    await expect(page.getByText("Aria needs information")).toBeVisible();
    await expect(
      page.getByText("Provide the required inputs for step 1: Create judge."),
    ).toBeVisible();
    await expect(steps.nth(0)).toContainText("waiting input");
    await field(page, /^name/).fill(judgeName);
    await field(page, /^instructions/).fill(JUDGE.instructions);
    await field(page, /^feedback value type/).selectOption(
      JUDGE.feedback_value_type,
    );
    await page.getByRole("button", { name: "Continue plan" }).click();
    await expect(page.getByText("Aria needs your approval")).toBeVisible();
    await expect(
      page.getByText("Approve step 1: Create judge (judge.create)?"),
    ).toBeVisible();
    await page.getByRole("button", { name: "Approve", exact: true }).click();

    await expect(
      page.getByText(
        "Provide the required inputs for step 2: Create test set.",
      ),
    ).toBeVisible();
    await expect(steps.nth(0)).toContainText("done");
    await field(page, /^name/).fill(datasetName);
    await field(page, /^description/).fill(DATASET.description);
    await field(page, /^tags/).fill(JSON.stringify(DATASET.tags));
    await page.getByRole("button", { name: "Continue plan" }).click();
    await expect(
      page.getByText("Approve step 2: Create test set (eval_dataset.create)?"),
    ).toBeVisible();
    await page.getByRole("button", { name: "Approve", exact: true }).click();

    await expect(page.getByTitle("completed", { exact: true })).toBeVisible();
    await expect(steps.nth(0)).toContainText("done");
    await expect(steps.nth(1)).toContainText("done");

    // ---- 5. Verify: Judges and Test Sets ---------------------------------------------
    await page.goto("/caliber/judges");
    const judgeRow = page.getByRole("row").filter({ hasText: judgeName });
    await expect(judgeRow).toBeVisible();
    await expect(judgeRow).toContainText("bool");
    await page.goto("/caliber/eval-datasets");
    await page
      .getByPlaceholder("Search by name, owner, tag…")
      .fill(datasetName);
    const datasetRow = page.getByRole("row").filter({ hasText: datasetName });
    await expect(datasetRow).toBeVisible();
    await datasetRow.getByText(datasetName).click();
    await expect(page.getByText("v1").first()).toBeVisible();

    // ---- 6. Follow-up: add rows in the row editor -----------------------------------
    for (const row of CASES.slice(0, 3)) {
      await page.getByRole("button", { name: "+ Add example" }).click();
      await page
        .getByLabel("Input (JSON object)")
        .fill(JSON.stringify(row.input));
      await page
        .getByLabel("Expected (JSON object)")
        .fill(JSON.stringify(row.expected));
      await page.getByLabel("Tags").fill(row.tags.join(", "));
      await page.getByTestId("example-save").click();
      await expect(page.getByText(row.input["question"]!)).toBeVisible();
    }
    await expect(page.getByText("v4").first()).toBeVisible();
  });

  test("a sentence with no domain word makes a plan that cannot be approved", async ({
    page,
  }) => {
    const ui = new CaliberUi(page);
    await ui.signIn();
    await page.goto("/caliber/aria/plans");
    await page
      .getByLabel("Goal")
      .fill("Write me a poem about customer support.");
    await page.getByRole("button", { name: "Decompose goal" }).click();
    await expect(page.getByTestId("aria-plan-warning")).toContainText(
      "No platform steps matched this goal",
    );
    // The goal stays in the box so it can be reworded; nothing opened, so nothing to approve.
    await expect(page.getByLabel("Goal")).toHaveValue(
      "Write me a poem about customer support.",
    );
    await expect(
      page.getByRole("button", { name: "Approve plan" }),
    ).toHaveCount(0);
  });
  test("Skip step leaves the step skipped and the plan completes", async ({
    page,
  }) => {
    const ui = new CaliberUi(page);
    await ui.signIn();
    await page.goto("/caliber/aria/plans");
    await page.getByLabel("Goal").fill("Create a judge for tone.");
    await page.getByRole("button", { name: "Decompose goal" }).click();
    await page.getByRole("button", { name: "Approve plan" }).click();
    await page.getByRole("button", { name: "Execute" }).click();
    await expect(page.getByText("Aria needs information")).toBeVisible();
    await page.getByRole("button", { name: "Skip step" }).click();
    await expect(page.getByTitle("completed", { exact: true })).toBeVisible();
    await expect(page.locator("ol > li").first()).toContainText("skipped");
  });

  test("a judge with no evaluation variable fails the plan and names the field", async ({
    page,
  }) => {
    const suffix = uiOnlySlug("c12bad");
    const ui = new CaliberUi(page);
    await ui.signIn();
    await page.goto("/caliber/aria/plans");
    await page.getByLabel("Goal").fill("Create a judge for tone.");
    await page.getByRole("button", { name: "Decompose goal" }).click();
    await page.getByRole("button", { name: "Approve plan" }).click();
    await page.getByRole("button", { name: "Execute" }).click();
    await field(page, /^name/).fill(`no-variable-${suffix}`);
    await field(page, /^instructions/).fill("Is {{ evidence }} polite?");
    await page.getByRole("button", { name: "Continue plan" }).click();
    await expect(
      page.getByTitle("failed", { exact: true }).first(),
    ).toBeVisible();
    await expect(page.locator("ol > li").first()).toContainText(
      "invalid input: instructions: instructions must reference at least one evaluation variable",
    );
    await page.goto("/caliber/judges");
    await expect(
      page.getByRole("row").filter({ hasText: `no-variable-${suffix}` }),
    ).toHaveCount(0);
  });
});
