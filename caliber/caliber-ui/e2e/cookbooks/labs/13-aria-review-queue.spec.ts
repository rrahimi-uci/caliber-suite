import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";

/**
 * Cookbook 13, "Run it through the UI": give Aria one sentence, answer its questions, and review
 * one of the traces it queued. It needs a server with the Aria flag on
 * (`scripts/cookbook-env.sh up lab13 --flags cookbooks,aria`); nothing here needs a model.
 *
 * The traces come from `assets/seed_traces.py` (it writes them to MLflow, not through the CALIBER
 * API), run with `CALIBER_LAB_PYTHON` (default: the repo's dev venv) against `MLFLOW_TRACKING_URI`
 * (from `.tmp/cookbook-env/NAME/env.sh`).
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(here, "../../../../..");
const ASSETS = path.resolve(
  process.env["COOKBOOK_13_ASSETS"] ??
    path.join(
      REPO,
      "docs-site/cookbooks/13-aria-review-governance-queue/assets",
    ),
);

interface Reply {
  id: string;
  review: Record<string, string | boolean>;
}
const REPLIES: Reply[] = fs
  .readFileSync(path.join(ASSETS, "dataset/agent-replies.jsonl"), "utf8")
  .split("\n")
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line) as Reply);
const QUEUE = JSON.parse(
  fs.readFileSync(
    path.join(ASSETS, "review-queues/agent-reply-review.queue.json"),
    "utf8",
  ),
) as {
  name: string;
  description: string;
  reviewers: string[];
  questions: { key: string; title: string }[];
};
const INTENT = fs
  .readFileSync(path.join(ASSETS, "intent.md"), "utf8")
  .split("\n")
  .find((line) => line.startsWith("> Set up a review queue"))!
  .replace(/^> /, "");

function seedTraces(): Record<string, string> {
  const python =
    process.env["CALIBER_LAB_PYTHON"] ??
    path.join(REPO, "caliber/.venv/bin/python");
  const out = execFileSync(python, [path.join(ASSETS, "seed_traces.py")], {
    env: process.env,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
    timeout: 120_000,
  });
  return JSON.parse(out) as Record<string, string>;
}

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

test.describe("Cookbook 13: a human-review queue from one sentence", () => {
  test("states the intent, answers the questions and reviews a queued trace", async ({
    page,
  }) => {
    test.setTimeout(300_000);
    const suffix = uiOnlySlug("c13");
    const queueName = `${QUEUE.name}-${suffix}`;
    const traces = seedTraces();
    expect(Object.keys(traces)).toHaveLength(30);
    const queued = REPLIES.slice(0, 5).map((row) => traces[row.id]!);
    const ui = new CaliberUi(page);
    await ui.signIn();

    // ---- 1. Plans > Goal -----------------------------------------------------------------
    await openPlans(page);
    await page.getByLabel("Goal").fill(INTENT);
    await page
      .getByLabel("Autonomy")
      .selectOption({ label: "Stop before each change" });
    await page.getByRole("button", { name: "Decompose goal" }).click();

    // ---- 2. The drawn plan: create the queue, then enqueue (wired to it) -----------------
    const steps = page.locator("ol > li");
    await expect(steps).toHaveCount(2);
    await expect(steps.nth(0)).toContainText("review_queue.create");
    await expect(steps.nth(1)).toContainText("review_queue.add_items");
    await expect(steps.nth(1)).toContainText("depends on 1 prior step(s)");
    await expect(page.getByTitle("draft", { exact: true })).toBeVisible();

    // ---- 3. Approve ------------------------------------------------------------------------
    await page.getByRole("button", { name: "Approve plan" }).click();
    await expect(page.getByTitle("approved", { exact: true })).toBeVisible();

    // ---- 4. Execute: the queue's form, then an approval ---------------------------------
    await page.getByRole("button", { name: "Execute" }).click();
    await expect(page.getByText("Aria needs information")).toBeVisible();
    await expect(
      page.getByText(
        "Provide the required inputs for step 1: Create review queue.",
      ),
    ).toBeVisible();
    await field(page, /^name/).fill(queueName);
    await field(page, /^description/).fill(QUEUE.description);
    await field(page, /^questions/).fill(JSON.stringify(QUEUE.questions));
    await field(page, /^reviewers/).fill(JSON.stringify(QUEUE.reviewers));
    await page.getByRole("button", { name: "Continue plan" }).click();
    await expect(
      page.getByText(
        "Approve step 1: Create review queue (review_queue.create)?",
      ),
    ).toBeVisible();
    await page.getByRole("button", { name: "Approve", exact: true }).click();

    // ---- 5. The enqueue form asks for the trace ids only ----------------------------------
    await expect(
      page.getByText(
        "Provide the required inputs for step 2: Enqueue traces for review.",
      ),
    ).toBeVisible();
    await expect(field(page, /^queue id/)).toHaveCount(0);
    await field(page, /^trace ids/).fill(JSON.stringify(queued));
    await field(page, /^assigned to/).fill("admin");
    await page.getByRole("button", { name: "Continue plan" }).click();
    await expect(
      page.getByText(
        "Approve step 2: Enqueue traces for review (review_queue.add_items)?",
      ),
    ).toBeVisible();
    await page.getByRole("button", { name: "Approve", exact: true }).click();
    await expect(page.getByTitle("completed", { exact: true })).toBeVisible();
    await expect(steps.nth(0)).toContainText("done");
    await expect(steps.nth(1)).toContainText("done");

    // ---- 6. Review Queues: the queue and its five traces -----------------------------------
    await page.goto("/caliber/review-queues");
    const row = page.getByRole("row").filter({ hasText: queueName });
    await expect(row).toBeVisible();
    await expect(row).toContainText("0/5 reviewed");
    await expect(row).toContainText(QUEUE.reviewers.join(", "));

    // ---- 7. Review one trace ---------------------------------------------------------------
    await row.getByRole("button", { name: queueName }).click();
    await expect(page.getByText("Items (5)")).toBeVisible();
    await page.getByRole("button", { name: queued[0]! }).click();
    const first = REPLIES[0]!.review;
    const ask = (title: string) =>
      page.locator("label", { hasText: title }).locator("xpath=..");
    for (const question of QUEUE.questions) {
      const answer = first[question.key];
      if (typeof answer === "boolean") {
        await ask(question.title)
          .getByRole("button", { name: answer ? "Pass" : "Fail", exact: true })
          .click();
      }
    }
    await page.getByRole("button", { name: "Submit review" }).click();
    // The reviewed trace now reads "completed" in the item list, and the form moves to the next.
    await expect(page.getByRole("button", { name: queued[0]! })).toContainText(
      "completed",
    );
    await page.goto("/caliber/review-queues");
    await expect(
      page.getByRole("row").filter({ hasText: queueName }),
    ).toContainText("1/5 reviewed");
  });

  test("a review that does not fit the questions is refused and stays open", async ({
    page,
  }) => {
    test.setTimeout(300_000);
    const suffix = uiOnlySlug("c13r");
    const queueName = `${QUEUE.name}-${suffix}`;
    const traces = seedTraces();
    const ui = new CaliberUi(page);
    await ui.signIn();
    await openPlans(page);
    await page.getByLabel("Goal").fill(INTENT);
    await page.getByRole("button", { name: "Decompose goal" }).click();
    await page.getByRole("button", { name: "Approve plan" }).click();
    await page.getByRole("button", { name: "Execute" }).click();
    await field(page, /^name/).fill(queueName);
    await field(page, /^questions/).fill(JSON.stringify(QUEUE.questions));
    await page.getByRole("button", { name: "Continue plan" }).click();
    await field(page, /^trace ids/).fill(JSON.stringify([traces["R02"]]));
    await page.getByRole("button", { name: "Continue plan" }).click();
    await expect(page.getByTitle("completed", { exact: true })).toBeVisible();

    await page.goto("/caliber/review-queues");
    await page
      .getByRole("row")
      .filter({ hasText: queueName })
      .getByRole("button", { name: queueName })
      .click();
    await page.getByRole("button", { name: traces["R02"]! }).click();
    // Only one of the three required questions is answered.
    await page
      .locator("label", { hasText: QUEUE.questions[0]!.title })
      .locator("xpath=..")
      .getByRole("button", { name: "Pass", exact: true })
      .click();
    await page.getByRole("button", { name: "Submit review" }).click();
    await expect(
      page.getByText(/missing required answer 'citation_ok'/),
    ).toBeVisible();
  });

  test("skipping the queue skips the enqueue step with it", async ({
    page,
  }) => {
    const ui = new CaliberUi(page);
    await ui.signIn();
    await openPlans(page);
    await page.getByLabel("Goal").fill(INTENT);
    await page.getByRole("button", { name: "Decompose goal" }).click();
    await page.getByRole("button", { name: "Approve plan" }).click();
    await page.getByRole("button", { name: "Execute" }).click();
    await page.getByRole("button", { name: "Skip step" }).click();
    await expect(page.getByTitle("completed", { exact: true })).toBeVisible();
    const steps = page.locator("ol > li");
    await expect(steps.nth(0)).toContainText("skipped");
    await expect(steps.nth(1)).toContainText("skipped");
    await expect(steps.nth(1)).toContainText(
      "skipped because step 1 (review_queue.create) was skipped",
    );
  });

  test("a categorical question with no options fails the plan and says which", async ({
    page,
  }) => {
    const suffix = uiOnlySlug("c13c");
    const ui = new CaliberUi(page);
    await ui.signIn();
    await openPlans(page);
    await page.getByLabel("Goal").fill(INTENT);
    await page.getByRole("button", { name: "Decompose goal" }).click();
    await page.getByRole("button", { name: "Approve plan" }).click();
    await page.getByRole("button", { name: "Execute" }).click();
    await field(page, /^name/).fill(`no-options-${suffix}`);
    await field(page, /^questions/).fill(
      JSON.stringify([{ key: "tone", title: "Tone?", type: "categorical" }]),
    );
    await page.getByRole("button", { name: "Continue plan" }).click();
    await expect(
      page.getByTitle("failed", { exact: true }).first(),
    ).toBeVisible();
    await expect(page.locator("ol > li").first()).toContainText(
      "invalid input: questions.0: categorical questions require a non-empty 'options' list",
    );
  });
});
