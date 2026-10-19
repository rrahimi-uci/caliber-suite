import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";
import { askAria, closeAria, isRealProvider } from "./aria-real";

/**
 * Cookbook 15, "Run it through the UI": give Aria one sentence about weak runs, answer its
 * questions, watch the plan park on a calibration job and settle by itself, and triage a flagged
 * run. It needs a server with the Aria flag on and its background workers running
 * (`scripts/cookbook-env.sh up lab15 --flags cookbooks,aria`); nothing here needs a model.
 *
 * The things Aria remediates (a workflow, its test set, an agent) and the flagged traces come from
 * two scripts in `assets/` (`setup_subject.py`, `seed_traces.py`), run with `CALIBER_LAB_PYTHON`
 * (default: the repo's dev venv) against `CALIBER_BASE_URL` and `MLFLOW_TRACKING_URI` (both from
 * `.tmp/cookbook-env/NAME/env.sh`). In a real deployment those already exist.
 *
 * Two modes, told apart by one read-only `GET /readiness`. On a simulated deployment the calibration
 * is rejected (the fake executor gives a candidate the same outputs as the baseline), so the plan
 * ends `failed` with the job's reason. On a real one (`scripts/cookbook-env.sh up lab15h --flags
 * cookbooks,aria --real-llm`, README "Run it with a real provider") the job replays the test set
 * through the model, which takes minutes, and ends `candidate_ready` (the plan `completed`) or
 * `rejected` (the plan `failed`, with the gate's reasons): the spec accepts either and asserts that
 * the page says which, then checks the Ask Aria chat on both sides of its permission boundary.
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(here, "../../../../..");
const ASSETS = path.resolve(
  process.env["COOKBOOK_15_ASSETS"] ??
    path.join(
      REPO,
      "docs-site/cookbooks/15-aria-triage-recalibrate-loop/assets",
    ),
);
const PYTHON =
  process.env["CALIBER_LAB_PYTHON"] ??
  path.join(REPO, "caliber/.venv/bin/python");

interface Run {
  id: string;
  reference: string;
  weak: boolean;
  reason: string;
}
const RUNS: Run[] = fs
  .readFileSync(path.join(ASSETS, "dataset/support-runs.jsonl"), "utf8")
  .split("\n")
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line) as Run);
const WEAK = RUNS.filter((run) => run.weak);
const QUEUE = JSON.parse(
  fs.readFileSync(
    path.join(ASSETS, "review-queues/triage-review.queue.json"),
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
  .find((line) => line.startsWith("> Our workflow's recent runs"))!
  .replace(/^> /, "");

function runScript(
  script: string,
  args: string[] = [],
): Record<string, string> {
  const base = process.env["CALIBER_BASE_URL"] ?? process.env["CALIBER_URL"];
  if (!base) {
    throw new Error(
      "source .tmp/cookbook-env/NAME/env.sh first (CALIBER_BASE_URL is needed)",
    );
  }
  const out = execFileSync(PYTHON, [path.join(ASSETS, script), ...args], {
    env: {
      ...process.env,
      CALIBER_BASE_URL: base,
      PYTHONPATH: path.join(REPO, "sdk/caliber-sdk"),
    },
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
    timeout: 180_000,
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

async function approve(page: Page, prompt: string): Promise<void> {
  await expect(page.getByText(prompt)).toBeVisible();
  await page.getByRole("button", { name: "Approve", exact: true }).click();
}

test.describe("Cookbook 15: triage weak runs and recalibrate", () => {
  test("queues the weak runs, parks on the calibration job and settles by itself", async ({
    page,
  }) => {
    test.setTimeout(2_100_000);
    const suffix = uiOnlySlug("c15");
    const subject = runScript("setup_subject.py", ["--suffix", suffix]);
    const traces = runScript("seed_traces.py");
    expect(Object.keys(traces)).toHaveLength(WEAK.length);
    const queueName = `${QUEUE.name}-${suffix}`;
    const ui = new CaliberUi(page);
    await ui.signIn();
    const real = await isRealProvider(page);

    // ---- 1. Plans > Goal -----------------------------------------------------------------
    await openPlans(page);
    await page.getByLabel("Goal").fill(INTENT);
    await page
      .getByLabel("Autonomy")
      .selectOption({ label: "Stop before each change" });
    await page.getByRole("button", { name: "Decompose goal" }).click();

    // ---- 2. The drawn plan --------------------------------------------------------------------
    const steps = page.locator("ol > li");
    await expect(steps).toHaveCount(3);
    await expect(steps.nth(0)).toContainText("review_queue.create");
    await expect(steps.nth(1)).toContainText("review_queue.add_items");
    await expect(steps.nth(1)).toContainText("depends on 1 prior step(s)");
    await expect(steps.nth(2)).toContainText("workflow.calibrate");
    await expect(steps.nth(2)).not.toContainText("depends on");

    // ---- 3. Approve ------------------------------------------------------------------------------
    await page.getByRole("button", { name: "Approve plan" }).click();
    await expect(page.getByTitle("approved", { exact: true })).toBeVisible();

    // ---- 4. The queue --------------------------------------------------------------------------------
    await page.getByRole("button", { name: "Execute" }).click();
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
    await approve(
      page,
      "Approve step 1: Create review queue (review_queue.create)?",
    );

    // ---- 5. The flagged traces ---------------------------------------------------------------------
    await expect(
      page.getByText(
        "Provide the required inputs for step 2: Enqueue traces for review.",
      ),
    ).toBeVisible();
    await expect(field(page, /^queue id/)).toHaveCount(0);
    await field(page, /^trace ids/).fill(
      JSON.stringify(WEAK.map((run) => traces[run.id])),
    );
    await field(page, /^assigned to/).fill("admin");
    await page.getByRole("button", { name: "Continue plan" }).click();
    await approve(
      page,
      "Approve step 2: Enqueue traces for review (review_queue.add_items)?",
    );

    // ---- 6. The calibration: the plan parks ----------------------------------------------------------
    await expect(
      page.getByText(
        "Provide the required inputs for step 3: Calibrate workflow.",
      ),
    ).toBeVisible();
    await field(page, /^workflow id/).fill(subject["workflow_id"]!);
    await field(page, /^agent id/).fill(subject["agent_id"]!);
    await page.getByRole("button", { name: "Continue plan" }).click();
    await approve(
      page,
      "Approve step 3: Calibrate workflow (workflow.calibrate)?",
    );
    await expect(steps.nth(2)).toContainText("waiting job");
    await expect(page.getByTitle("running", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Execute" })).toBeVisible();

    // ---- 7. The plan settles by itself: the page re-reads it every few seconds -----------------------
    await expect(
      page
        .getByTitle("failed", { exact: true })
        .or(page.getByTitle("completed", { exact: true }))
        .first(),
    ).toBeVisible({ timeout: real ? 1_500_000 : 240_000 });
    await expect(steps.nth(0)).toContainText("done");
    await expect(steps.nth(1)).toContainText("done");
    if (await page.getByTitle("failed", { exact: true }).first().isVisible()) {
      await expect(steps.nth(2)).toContainText("failed");
      await expect(steps.nth(2)).toContainText(
        "rejected: workflow calibration rejected: no candidate met calibration gates",
      );
      if (!real) {
        await expect(steps.nth(2)).toContainText(
          "quality delta +0.000 < epsilon 0.02",
        );
      }
    } else {
      await expect(steps.nth(2)).toContainText("done");
    }

    // ---- 8. Review Queues: the queue and its twelve flagged traces -----------------------------------
    await page.goto("/caliber/review-queues");
    const row = page.getByRole("row").filter({ hasText: queueName });
    await expect(row).toBeVisible();
    await expect(row).toContainText("0/12 reviewed");

    // ---- 9. Triage one flagged run -------------------------------------------------------------------
    await row.getByRole("button", { name: queueName }).click();
    await expect(page.getByText("Items (12)")).toBeVisible();
    await page.getByRole("button", { name: traces["S19"]! }).click();
    const ask = (title: string) =>
      page.locator("label", { hasText: title }).locator("xpath=..");
    await ask("Did the workflow reach the correct resolution?")
      .getByRole("button", { name: "Fail", exact: true })
      .click();
    await page
      .getByLabel("Primary failure mode")
      .selectOption("missing_evidence");
    await page.getByLabel("Severity (1-5)").fill("3");
    await page
      .getByLabel("What should the answer have been? (becomes the expectation)")
      .fill(RUNS.find((run) => run.id === "S19")!.reference);
    await page.getByRole("button", { name: "Submit review" }).click();
    await expect(
      page.getByRole("button", { name: traces["S19"]! }),
    ).toContainText("completed");
    await page.goto("/caliber/review-queues");
    await expect(
      page.getByRole("row").filter({ hasText: queueName }),
    ).toContainText("1/12 reviewed");
  });

  test("an unknown workflow fails the step at once and a denied calibration queues nothing", async ({
    page,
  }) => {
    test.setTimeout(240_000);
    const suffix = uiOnlySlug("c15b");
    const subject = runScript("setup_subject.py", ["--suffix", suffix]);
    const ui = new CaliberUi(page);
    await ui.signIn();

    // ---- 10. A workflow id that does not exist -----------------------------------------------------
    await openPlans(page);
    await page.getByLabel("Goal").fill("Calibrate the support workflow.");
    await page.getByRole("button", { name: "Decompose goal" }).click();
    await page.getByRole("button", { name: "Approve plan" }).click();
    await page.getByRole("button", { name: "Execute" }).click();
    await field(page, /^workflow id/).fill("WF-does-not-exist");
    await field(page, /^agent id/).fill(subject["agent_id"]!);
    await page.getByRole("button", { name: "Continue plan" }).click();
    await expect(
      page.getByTitle("failed", { exact: true }).first(),
    ).toBeVisible();
    await expect(page.locator("ol > li").first()).toContainText(
      "workflow 'WF-does-not-exist' not found (HTTP 404)",
    );
    await expect(page.locator("ol > li").first()).not.toContainText(
      "waiting job",
    );

    // ---- 11. Deny the calibration ---------------------------------------------------------------------
    await openPlans(page);
    await page.getByLabel("Goal").fill("Calibrate the support workflow.");
    await page
      .getByLabel("Autonomy")
      .selectOption({ label: "Stop before each change" });
    await page.getByRole("button", { name: "Decompose goal" }).click();
    await page.getByRole("button", { name: "Approve plan" }).click();
    await page.getByRole("button", { name: "Execute" }).click();
    await field(page, /^workflow id/).fill(subject["workflow_id"]!);
    await field(page, /^agent id/).fill(subject["agent_id"]!);
    await page.getByRole("button", { name: "Continue plan" }).click();
    await expect(
      page.getByText(
        "Approve step 1: Calibrate workflow (workflow.calibrate)?",
      ),
    ).toBeVisible();
    await page.getByRole("button", { name: "Deny", exact: true }).click();
    await expect(page.getByTitle("completed", { exact: true })).toBeVisible();
    await expect(page.locator("ol > li").first()).toContainText("skipped");
    await expect(page.locator("ol > li").first()).not.toContainText(
      "waiting job",
    );
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
    const suffix = uiOnlySlug("c15chat");
    const acting = `c15-chat-queue-${suffix}`;
    const reading = `c15-chat-manual-${suffix}`;
    const ask = (name: string) =>
      `Create a review queue named ${name} with one pass_fail question: key resolved, title ` +
      "'Did the agent resolve the request?'. Then tell me its id.";
    await page.goto("/caliber/review-queues");

    // Default approval ("Ask first"): the model is offered Aria's read tools only.
    await askAria(page, ask(reading));
    await closeAria(page);
    // "Legacy auto": the mutating tools are offered too, and the model creates the queue itself.
    const acted = await askAria(page, ask(acting), { approval: "Legacy auto" });
    expect(acted).toMatch(/\d+ actions?/);
    await closeAria(page);

    await page.goto("/caliber/review-queues");
    await expect(
      page.getByRole("row").filter({ hasText: acting }),
    ).toBeVisible();
    await expect(
      page.getByRole("row").filter({ hasText: reading }),
    ).toHaveCount(0);
  });
});
