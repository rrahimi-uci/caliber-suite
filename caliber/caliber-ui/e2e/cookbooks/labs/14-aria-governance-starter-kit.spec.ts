import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";

/**
 * Cookbook 14, "Run it through the UI": give Aria one sentence and get a judge, a review queue and
 * a test set, answering its questions as it asks. It needs a server with the Aria flag on
 * (`scripts/cookbook-env.sh up lab14 --flags cookbooks,aria`); nothing here needs a model.
 *
 * The planner is deterministic, so the plan is asserted exactly. Aria creates the test set empty;
 * three of the 30 rows are added through the row editor (the rest are the script's job).
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(here, "../../../../..");
const ASSETS = path.resolve(
  process.env["COOKBOOK_14_ASSETS"] ??
    path.join(
      REPO,
      "docs-site/cookbooks/14-aria-governance-starter-kit/assets",
    ),
);
const read = <T>(file: string): T =>
  JSON.parse(fs.readFileSync(path.join(ASSETS, file), "utf8")) as T;

interface Case {
  id: string;
  tags: string[];
  input: Record<string, string>;
  expected: Record<string, string>;
}
const CASES: Case[] = fs
  .readFileSync(
    path.join(ASSETS, "dataset/release-candidate-cases.jsonl"),
    "utf8",
  )
  .split("\n")
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line) as Case);
const JUDGE = read<{
  name: string;
  instructions: string;
  feedback_value_type: string;
  tags: string[];
}>("judges/answer-faithfulness.judge.json");
const QUEUE = read<{
  name: string;
  description: string;
  reviewers: string[];
  questions: { key: string; title: string }[];
}>("review-queues/governance-review.queue.json");
const DATASET = read<{ name: string; description: string; tags: string[] }>(
  "datasets/release-candidates.dataset.json",
);
const INTENT = fs
  .readFileSync(path.join(ASSETS, "intent.md"), "utf8")
  .split("\n")
  .find((line) => line.startsWith("> Stand up our governance"))!
  .replace(/^> /, "");

/** The label of a field in the "Aria needs information" form. */
function field(page: Page, label: RegExp) {
  return page
    .locator("label")
    .filter({ hasText: label })
    .locator("input, textarea, select");
}

async function openPlans(page: Page): Promise<void> {
  await page.goto("/caliber");
  const nav = page.getByLabel("CALIBER navigation");
  // Integrations only appears once the server's feature flags have loaded, and the sidebar
  // remembers which groups are open: open the group only if it is closed.
  const group = nav.getByRole("button", { name: /^Integrations/ });
  await expect(group).toBeVisible();
  if ((await group.getAttribute("aria-expanded")) !== "true") {
    await group.click();
  }
  await nav.getByRole("link", { name: "Plans" }).click();
  await expect(page.getByRole("heading", { name: "Aria Plans" })).toBeVisible();
}

async function fillJudge(page: Page, name: string): Promise<void> {
  await field(page, /^name/).fill(name);
  await field(page, /^instructions/).fill(JUDGE.instructions);
  await field(page, /^feedback value type/).selectOption(
    JUDGE.feedback_value_type,
  );
  await field(page, /^tags/).fill(JSON.stringify(JUDGE.tags));
  await page.getByRole("button", { name: "Continue plan" }).click();
}

async function fillQueue(page: Page, name: string): Promise<void> {
  await field(page, /^name/).fill(name);
  await field(page, /^description/).fill(QUEUE.description);
  await field(page, /^questions/).fill(JSON.stringify(QUEUE.questions));
  await page.getByRole("button", { name: "Continue plan" }).click();
}

async function fillDataset(page: Page, name: string): Promise<void> {
  await field(page, /^name/).fill(name);
  await field(page, /^description/).fill(DATASET.description);
  await field(page, /^tags/).fill(JSON.stringify(DATASET.tags));
  await page.getByRole("button", { name: "Continue plan" }).click();
}

async function approve(page: Page, prompt: string): Promise<void> {
  await expect(page.getByText(prompt)).toBeVisible();
  await page.getByRole("button", { name: "Approve", exact: true }).click();
}

async function draw(page: Page, autonomy?: string): Promise<void> {
  await openPlans(page);
  await page.getByLabel("Goal").fill(INTENT);
  if (autonomy)
    await page.getByLabel("Autonomy").selectOption({ label: autonomy });
  await page.getByRole("button", { name: "Decompose goal" }).click();
  await expect(page.getByTitle("draft", { exact: true })).toBeVisible();
}

test.describe("Cookbook 14: a governance starter kit from one sentence", () => {
  test("states the intent, answers the questions and finds the judge, the queue and the test set", async ({
    page,
  }) => {
    test.setTimeout(300_000);
    const suffix = uiOnlySlug("c14");
    const judgeName = `${JUDGE.name}-${suffix}`;
    const queueName = `${QUEUE.name}-${suffix}`;
    const datasetName = `${DATASET.name}-${suffix}`;
    const ui = new CaliberUi(page);
    await ui.signIn();

    // ---- 1 + 2. Plans > Goal, and the drawn plan ----------------------------------------
    await draw(page, "Stop before each change");
    const steps = page.locator("ol > li");
    await expect(steps).toHaveCount(4);
    await expect(steps.nth(0)).toContainText("judge.create");
    await expect(steps.nth(1)).toContainText("review_queue.create");
    await expect(steps.nth(2)).toContainText("eval_dataset.create");
    await expect(steps.nth(3)).toContainText("review_queue.add_items");
    await expect(steps.nth(3)).toContainText("depends on 1 prior step(s)");

    // ---- 3. Approve ------------------------------------------------------------------------
    await page.getByRole("button", { name: "Approve plan" }).click();
    await expect(page.getByTitle("approved", { exact: true })).toBeVisible();

    // ---- 4. The judge: a form, then an approval ------------------------------------------
    await page.getByRole("button", { name: "Execute" }).click();
    await expect(
      page.getByText("Provide the required inputs for step 1: Create judge."),
    ).toBeVisible();
    await fillJudge(page, judgeName);
    await approve(page, "Approve step 1: Create judge (judge.create)?");

    // ---- 5. The review queue ---------------------------------------------------------------
    await expect(
      page.getByText(
        "Provide the required inputs for step 2: Create review queue.",
      ),
    ).toBeVisible();
    await fillQueue(page, queueName);
    await approve(
      page,
      "Approve step 2: Create review queue (review_queue.create)?",
    );

    // ---- 6. The test set ---------------------------------------------------------------------
    await expect(
      page.getByText(
        "Provide the required inputs for step 3: Create test set.",
      ),
    ).toBeVisible();
    await fillDataset(page, datasetName);
    await approve(
      page,
      "Approve step 3: Create test set (eval_dataset.create)?",
    );

    // ---- 7. The enqueue step: no traces yet, so skip it ------------------------------------
    await expect(
      page.getByText(
        "Provide the required inputs for step 4: Enqueue traces for review.",
      ),
    ).toBeVisible();
    await expect(field(page, /^queue id/)).toHaveCount(0);
    await page.getByRole("button", { name: "Skip step" }).click();
    await expect(page.getByTitle("completed", { exact: true })).toBeVisible();
    await expect(steps.nth(0)).toContainText("done");
    await expect(steps.nth(1)).toContainText("done");
    await expect(steps.nth(2)).toContainText("done");
    await expect(steps.nth(3)).toContainText("skipped");

    // ---- 8. Verify the judge and the queue -------------------------------------------------
    await page.goto("/caliber/judges");
    const judgeRow = page.getByRole("row").filter({ hasText: judgeName });
    await expect(judgeRow).toBeVisible();
    await expect(judgeRow).toContainText("bool");
    await page.goto("/caliber/review-queues");
    const queueRow = page.getByRole("row").filter({ hasText: queueName });
    await expect(queueRow).toBeVisible();
    await expect(queueRow).toContainText("no items");

    // ---- 9. Verify the test set and add rows ----------------------------------------------
    await page.goto("/caliber/eval-datasets");
    await page
      .getByPlaceholder("Search by name, owner, tag…")
      .fill(datasetName);
    const datasetRow = page.getByRole("row").filter({ hasText: datasetName });
    await expect(datasetRow).toBeVisible();
    await datasetRow.getByText(datasetName).click();
    await expect(page.getByText("v1").first()).toBeVisible();
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

  test("asking less: under 'Run everything it safely can' only the forms are asked", async ({
    page,
  }) => {
    const suffix = uiOnlySlug("c14b");
    const ui = new CaliberUi(page);
    await ui.signIn();

    // ---- 10. The same sentence, with the autonomy changed on the draft ----------------------
    await draw(page);
    await page
      .getByLabel("Autonomy")
      .selectOption({ label: "Run everything it safely can" });
    await expect(
      page.getByText("Autonomy: Run everything it safely can"),
    ).toBeVisible();
    await page.getByRole("button", { name: "Approve plan" }).click();
    await page.getByRole("button", { name: "Execute" }).click();
    await expect(
      page.getByText("Provide the required inputs for step 1: Create judge."),
    ).toBeVisible();
    await fillJudge(page, `${JUDGE.name}-${suffix}`);
    // No approval prompt: the next question is already the queue's form.
    await expect(
      page.getByText(
        "Provide the required inputs for step 2: Create review queue.",
      ),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Approve", exact: true }),
    ).toHaveCount(0);
    await fillQueue(page, `${QUEUE.name}-${suffix}`);
    await expect(
      page.getByText(
        "Provide the required inputs for step 3: Create test set.",
      ),
    ).toBeVisible();
    await fillDataset(page, `${DATASET.name}-${suffix}`);
    await expect(
      page.getByText(
        "Provide the required inputs for step 4: Enqueue traces for review.",
      ),
    ).toBeVisible();
    await page.getByRole("button", { name: "Skip step" }).click();
    await expect(page.getByTitle("completed", { exact: true })).toBeVisible();
  });

  test("denying the review queue skips the enqueue step with it", async ({
    page,
  }) => {
    const suffix = uiOnlySlug("c14c");
    const ui = new CaliberUi(page);
    await ui.signIn();

    // ---- 11. Deny the queue ------------------------------------------------------------------------
    await draw(page, "Stop before each change");
    await page.getByRole("button", { name: "Approve plan" }).click();
    await page.getByRole("button", { name: "Execute" }).click();
    await fillJudge(page, `${JUDGE.name}-${suffix}`);
    await approve(page, "Approve step 1: Create judge (judge.create)?");
    await fillQueue(page, `${QUEUE.name}-${suffix}`);
    await expect(
      page.getByText(
        "Approve step 2: Create review queue (review_queue.create)?",
      ),
    ).toBeVisible();
    await page.getByRole("button", { name: "Deny", exact: true }).click();
    // The enqueue step is never asked: the next question is the test set's form.
    await expect(
      page.getByText(
        "Provide the required inputs for step 3: Create test set.",
      ),
    ).toBeVisible();
    await fillDataset(page, `${DATASET.name}-${suffix}`);
    await approve(
      page,
      "Approve step 3: Create test set (eval_dataset.create)?",
    );
    await expect(page.getByTitle("completed", { exact: true })).toBeVisible();
    const steps = page.locator("ol > li");
    await expect(steps.nth(1)).toContainText("skipped");
    await expect(steps.nth(3)).toContainText("skipped");
    await expect(steps.nth(3)).toContainText(
      "skipped because step 2 (review_queue.create) was skipped",
    );
    await page.goto("/caliber/review-queues");
    await expect(
      page.getByRole("row").filter({ hasText: `${QUEUE.name}-${suffix}` }),
    ).toHaveCount(0);
  });
});
