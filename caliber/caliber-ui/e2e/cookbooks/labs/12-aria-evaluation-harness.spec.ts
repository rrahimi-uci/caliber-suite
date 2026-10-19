import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";
import { askAria, closeAria, isRealProvider } from "./aria-real";

/**
 * Cookbook 12, "Run it through the UI": give Aria one sentence, answer its questions, and find the
 * judge and the test set it made. It needs a server with the Aria and Cookbooks flags on
 * (`scripts/cookbook-env.sh up lab12 --flags cookbooks,aria`); nothing here needs a model.
 *
 * The planner is deterministic, so the plan is asserted exactly. Aria creates the test set empty;
 * three of the 30 rows are added through the row editor (the rest are the script's job).
 *
 * Two modes, told apart by one read-only `GET /readiness`. Simulated (the default stack): running
 * the judge in Evaluations is refused with the reason. Real (`scripts/cookbook-env.sh up lab12h
 * --flags cookbooks,aria --real-llm`, README "Run it with a real provider"): a prompt that sees the
 * policy text is written in Prompts, the three rows are scored by the judge in Evaluations
 * (structure and ranges asserted, never an exact score), and the Ask Aria chat is checked on both
 * sides of its permission boundary.
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
const GROUNDED_PROMPT = fs
  .readFileSync(path.join(ASSETS, "prompts/grounded-answer.prompt.txt"), "utf8")
  .trim();
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
    // Real mode scores three rows with a model (a minute or so); the default 4 minutes is for the
    // simulated stack.
    test.setTimeout(900_000);
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

    // ---- 7. Evaluations: refused without a real model, scored by the judge with one -------
    const real = await isRealProvider(page);
    const promptName = `cookbook-12-grounded-${suffix}`;
    if (real) {
      // The answerer under test: the question is the user's turn, the row's evidence fills
      // {{ evidence }} in the prompt (a test set row's other fields never reach the model).
      await page.goto("/caliber/prompts");
      await page.getByRole("button", { name: "New prompt" }).click();
      await page.getByRole("button", { name: /Write \/ paste/ }).click();
      await page.getByLabel("Prompt text").fill(GROUNDED_PROMPT);
      await page.getByLabel("Prompt name").fill(promptName);
      await page.getByRole("button", { name: "Create and Open" }).click();
      await expect(page.getByTestId("version-panel")).toBeVisible();
    }
    const label = `faithfulness ${suffix}`;
    await page.goto("/caliber/evaluations");
    await page.getByRole("button", { name: "Run evaluation" }).click();
    await page
      .getByLabel("Test set", { exact: true })
      .selectOption({ label: `${datasetName} (v4)` });
    await page.getByLabel("Label", { exact: true }).fill(label);
    if (real) {
      await page.getByLabel("What to score").selectOption("prompt");
      await page.getByLabel("Subject under test").fill(`${promptName}@1`);
    }
    // The judge, with Non-empty; not the three string-match graders that are ticked by default.
    await page.getByText("Exact match", { exact: true }).click();
    await page.getByText("Token F1", { exact: true }).click();
    await page.getByText("Contains expected", { exact: true }).click();
    await page.getByText("Non-empty", { exact: true }).click();
    await page.getByTestId("judge-scorer-options").getByText(judgeName).click();
    await page.getByRole("button", { name: "Run", exact: true }).click();
    if (!real) {
      await expect(
        page.getByText(/evaluation requires a real LLM provider/),
      ).toBeVisible({ timeout: 60_000 });
    } else {
      const run = page.getByTestId("eval-run-row").filter({ hasText: label });
      await expect(run).toBeVisible({ timeout: 300_000 });
      await expect(run).toContainText("completed");
      await expect(run).toContainText("3/3");
      await run.getByRole("link").click();
      await expect(page.getByText(`prompt: ${promptName}@1`)).toBeVisible();
      await expect(page.getByTestId("eval-result-row")).toHaveCount(3);
      await expect(page.getByTestId("eval-aggregate")).toBeVisible();
    }
  });

  test("real provider: Ask Aria acts in a Legacy auto chat and only reads in the default one", async ({
    page,
  }) => {
    test.setTimeout(600_000);
    const ui = new CaliberUi(page);
    await ui.signIn();
    test.skip(
      !(await isRealProvider(page)),
      "Ask Aria needs a real engine (a simulated stack answers with a canned heuristic)",
    );
    const suffix = uiOnlySlug("c12chat");
    const acting = `c12-chat-judge-${suffix}`;
    const reading = `c12-chat-manual-${suffix}`;
    const ask = (name: string) =>
      `Create a judge named ${name} with a boolean feedback value type and exactly these ` +
      `instructions: Is {{ outputs }} faithful to the evidence in {{ inputs }}? Answer true or ` +
      "false. Then tell me its id.";
    await page.goto("/caliber/judges");

    // Default approval ("Ask first"): the model is offered Aria's read tools only.
    const readOnly = await askAria(page, ask(reading));
    expect(readOnly).not.toMatch(/\b2 actions\b/);
    await closeAria(page);

    // "Legacy auto": the mutating tools are offered too, and the model creates the judge itself.
    const acted = await askAria(page, ask(acting), { approval: "Legacy auto" });
    expect(acted).toMatch(/\d+ actions?/);
    await closeAria(page);

    await page.goto("/caliber/judges");
    await expect(
      page.getByRole("row").filter({ hasText: acting }),
    ).toBeVisible();
    await expect(
      page.getByRole("row").filter({ hasText: acting }),
    ).toContainText("bool");
    await expect(
      page.getByRole("row").filter({ hasText: reading }),
    ).toHaveCount(0);
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
