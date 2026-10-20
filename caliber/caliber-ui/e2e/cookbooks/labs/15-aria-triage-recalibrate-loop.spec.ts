import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";
import { askAria, closeAria, isRealProvider } from "./aria-real";

/**
 * Cookbook 15, "Run it through the UI": a support lead builds the thing Aria will remediate (two
 * tools, a 30-case test set, a published workflow, an agent), gives Aria one sentence about weak
 * runs, answers its questions, watches the plan park on a calibration job and settle by itself,
 * triages a flagged run, then changes the workflow (a new version) and calibrates it again. It needs
 * a server with the Aria flag on and its background workers running
 * (`scripts/cookbook-env.sh up NAME --flags cookbooks,aria`); nothing here needs a model.
 *
 * Everything is created by clicks and file uploads from `assets/`, except the flagged traces: they
 * stand for what your own agent logs, so `assets/seed_traces.py` writes them to MLflow (run with
 * `CALIBER_LAB_PYTHON`, default the repo's dev venv, against `MLFLOW_TRACKING_URI` from
 * `.tmp/cookbook-env/NAME/env.sh`).
 *
 * Two modes, told apart by the provider banner on screen. On a simulated deployment the calibration
 * is rejected (the fake executor gives a candidate the same outputs as the baseline), so the plan
 * ends `failed` with the job's reason. On a real one (`scripts/cookbook-env.sh up NAME --flags
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
  question: string;
  reply: string;
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

const GATE_FILE = path.join(ASSETS, "dataset/support-gate-cases.jsonl");
const GATE_ROWS = fs
  .readFileSync(GATE_FILE, "utf8")
  .split("\n")
  .filter((line) => line.trim()).length;
const MANIFEST_FILE = path.join(
  ASSETS,
  "workflows/support-policy-agent.workflow.json",
);
const TOOL_FILES = ["lookup-policy.tool.json", "get-order.tool.json"];

interface Subject {
  workflowId: string;
  workflowName: string;
  agentId: string;
  datasetName: string;
}

/**
 * Build what Aria will remediate, the way a person does: register the two tools, create the
 * 30-case test set and import its rows from the file, import the workflow (its deploy gate points
 * at that test set by name) and publish it, and register an agent. A tool that is already
 * registered (this server has seen the lab before) is kept.
 */
async function buildSubject(page: Page, suffix: string): Promise<Subject> {
  // ---- a. Agent resources > Tools > Register Tool --------------------------------------
  for (const file of TOOL_FILES) {
    const asset = JSON.parse(
      fs.readFileSync(path.join(ASSETS, "tools", file), "utf8"),
    ) as Record<string, unknown>;
    await page.goto("/caliber/tools");
    await page.getByRole("button", { name: "Register Tool" }).click();
    await expect(page.getByTestId("tool-wizard")).toBeVisible();
    await page.getByTestId("wiz-name").fill(String(asset["name"]));
    await page.getByTestId("wiz-version").fill(String(asset["version"]));
    await page
      .getByTestId("wiz-description")
      .fill(String(asset["description"]));
    await page.getByTestId("wiz-owner").fill("admin");
    await page.getByTestId("wizard-next").click();
    await page.getByTestId("wiz-module").fill(String(asset["module_path"]));
    await page.getByTestId("wiz-callable").fill(String(asset["callable_name"]));
    await page.getByTestId("wizard-next").click();
    await page.getByTestId("input-schema-toggle-raw").click();
    await page
      .getByTestId("input-schema-raw")
      .fill(JSON.stringify(asset["input_schema"], null, 2));
    await page.getByTestId("wizard-next").click();
    await page.getByTestId("wizard-next").click();
    await page.getByTestId("wiz-allow-preview").check();
    await page.getByTestId("wizard-submit").click();
    // Registered (the tool's page opens), or this server has seen the lab before and says so.
    await expect(
      page
        .getByTestId("wizard-error")
        .filter({ hasText: "already registered" })
        .or(page.getByTestId("tool-workspace-header")),
    ).toBeVisible();
  }

  // ---- b. Prompt lifecycle > Test Sets > New Test Set, Import from file ------------------
  const datasetName = `cookbook-15-support-policy-gate-${suffix}`;
  await page.goto("/caliber/eval-datasets");
  await page.getByRole("button", { name: "+ New Test Set" }).click();
  await page.getByPlaceholder("factual-checks").fill(datasetName);
  await page.getByPlaceholder("@sarah").fill("admin");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await page.getByText(datasetName, { exact: true }).first().click();
  await expect(page.getByTestId("eval-dataset-detail")).toBeVisible();
  await page.getByTestId("import-toggle").click();
  await page.getByTestId("import-file").setInputFiles(GATE_FILE);
  await expect(page.getByTestId("import-summary")).toContainText(
    `${GATE_ROWS} rows ready to import`,
  );
  await page.getByTestId("import-save").click();
  await expect(page.getByTestId("eval-dataset-version")).toHaveText(
    `v${GATE_ROWS + 1}`,
    { timeout: 120_000 },
  );

  // ---- c. Agent resources > Workflows > Import: the manifest, with the test set's name -----
  // The manifest's deploy gate names its test set; a person edits that one line to the name they
  // gave theirs (here, an in-memory copy of the file with the unique name).
  const manifest = JSON.parse(fs.readFileSync(MANIFEST_FILE, "utf8")) as {
    artifacts: { eval_datasets: { support_eval: { dataset_name: string } } };
  };
  manifest.artifacts.eval_datasets.support_eval.dataset_name = datasetName;
  const workflowName = `cookbook-15-support-policy-agent-${suffix}`;
  await page.goto("/caliber/workflows");
  await page.getByTestId("import-workflow").click();
  await page.getByTestId("workflow-import-name").fill(workflowName);
  await page.locator('input[type="file"]').setInputFiles({
    name: "support-policy-agent.workflow.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(manifest, null, 2)),
  });
  await page.getByRole("button", { name: "Validate dependencies" }).click();
  await expect(page.getByText("Preflight passed")).toBeVisible();
  await page.getByRole("button", { name: "Import as new" }).click();
  await expect(page).toHaveURL(
    /\/caliber\/workflows\/WF-[^/]+\/editor\/WFV-[^/?]+$/,
  );
  const workflowId = /workflows\/(WF-[^/]+)\//.exec(page.url())![1]!;
  await expect(page.getByTestId("workflow-editor")).toBeVisible();

  // ---- d. Editor > Publish ----------------------------------------------------------------------
  await publishFromEditor(page);

  // ---- e. Agent resources > Agents > New agent configuration ------------------------------
  const agentId = `cookbook-15-agent-${suffix}`;
  await page.goto("/caliber/agents");
  await page.getByTestId("new-agent").click();
  const form = page.getByTestId("agent-create-form");
  await form.getByLabel("Agent ID").fill(agentId);
  await form.getByLabel("MLflow experiment ID").fill(`exp-${agentId}`);
  await form.getByLabel("Display name").fill("support policy agent");
  await form.getByRole("button", { name: "Register agent" }).click();
  // Registering opens the new agent.
  await expect(page).toHaveURL(new RegExp(`/caliber/agents/${agentId}$`));
  return { workflowId, workflowName, agentId, datasetName };
}

async function publishFromEditor(page: Page): Promise<void> {
  await page.getByTestId("editor-publish").click();
  await page.getByTestId("publish-run-validate").click();
  await page.getByTestId("publish-next-1").click();
  await page.getByTestId("publish-next-2").click();
  await page.getByTestId("publish-confirm").click();
  await expect(page.getByTestId("editor-message")).toContainText("Published.");
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
  // Plans sits in Operate. The sidebar remembers which groups are open: open the group only
  // if it is closed (the Plans link itself appears once the server's feature flags have loaded).
  const group = nav.getByRole("button", { name: /^Operate/ });
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

/** The subject the first test built, so the second one need not build it again. */
let built: Subject | undefined;

test.describe("Cookbook 15: triage weak runs and recalibrate", () => {
  test("queues the weak runs, parks on the calibration job and settles by itself", async ({
    page,
  }) => {
    test.setTimeout(2_100_000);
    const suffix = uiOnlySlug("c15");
    const traces = runScript("seed_traces.py");
    expect(Object.keys(traces)).toHaveLength(WEAK.length);
    const queueName = `${QUEUE.name}-${suffix}`;
    const ui = new CaliberUi(page);
    await ui.signIn();
    const real = await isRealProvider(page);

    // ---- 0. The thing Aria will remediate: tools, test set, workflow, agent -------------------
    const subject = await buildSubject(page, suffix);
    built = subject;

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
    await field(page, /^workflow id/).fill(subject.workflowId);
    await field(page, /^agent id/).fill(subject.agentId);
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

    // ---- 9. Triage the twelve flagged runs, reading each reply first ------------------------
    await row.getByRole("button", { name: queueName }).click();
    await expect(page.getByText("Items (12)")).toBeVisible();
    const ask = (title: string) =>
      page.locator("label", { hasText: title }).locator("xpath=..");
    for (const run of WEAK) {
      await page.getByRole("button", { name: traces[run.id]! }).click();
      // The reviewer is shown what the customer asked and what the workflow answered.
      const content = page.getByTestId("review-trace-content");
      await expect(content).toContainText(run.question);
      await expect(content).toContainText(run.reply.slice(0, 25));
      const mode = run.reason.split(":", 1)[0]!;
      await ask("Did the workflow reach the correct resolution?")
        .getByRole("button", { name: "Fail", exact: true })
        .click();
      await page.getByLabel("Primary failure mode").selectOption(mode);
      await page
        .getByLabel("Severity (1-5)")
        .fill(mode === "hallucinated_claim" ? "4" : "3");
      await page
        .getByLabel(
          "What should the answer have been? (becomes the expectation)",
        )
        .fill(run.reference);
      await page.getByRole("button", { name: "Submit review" }).click();
      await expect(
        page.getByRole("button", { name: traces[run.id]! }),
      ).toContainText("completed");
    }
    await page.goto("/caliber/review-queues");
    await expect(
      page.getByRole("row").filter({ hasText: queueName }),
    ).toContainText("12/12 reviewed");

    // ---- 10. The calibration run is on the workflow's page ---------------------------------------
    await page.goto(`/caliber/workflows/${subject.workflowId}`);
    await page.getByTestId("workflow-calibrate").click();
    await expect(page.getByTestId("workflow-calibration-run")).toHaveCount(1);
    await expect(
      page.getByTestId("workflow-calibration-run").first(),
    ).toContainText(real ? /rejected|candidate ready/ : "rejected");
    if (!real) {
      await expect(
        page.getByTestId("workflow-calibration-simulated"),
      ).toBeVisible();
    }

    // ---- 11. Modify -> new version: also require a lookup before a shipping claim --------------
    await page.goto(`/caliber/workflows/${subject.workflowId}?tab=versions`);
    const table = page.getByTestId("versions-table");
    await expect(table.locator("tbody tr")).toHaveCount(1);
    const v1Id = /(WFV-[0-9a-f]+)/.exec(await table.innerText())![1]!;
    await page.getByTestId(`restore-version-btn-${v1Id}`).click();
    await expect(page).toHaveURL(/\/editor\/WFV-[^/?]+$/);
    const v2Id = /editor\/(WFV-[^/?]+)/.exec(page.url())![1]!;
    expect(v2Id).not.toBe(v1Id);
    await page.getByTestId("outline-policy_guardrail").dblclick();
    const categories = page.getByTestId("check-0-param-categories");
    await expect(categories).toBeVisible();
    await categories.fill("refund_policy, warranty_policy, shipping_policy");
    await page.getByTestId("editor-save").click();
    await expect(page.getByTestId("editor-save-status")).toContainText("Saved");
    await page.getByTestId("editor-validate").click();
    await expect(page.getByTestId("editor-message")).toContainText("Valid.");
    await publishFromEditor(page);

    // ---- 12. Compare the two versions: one node changed -----------------------------------------
    await page.goto(`/caliber/workflows/${subject.workflowId}?tab=versions`);
    await expect(table.locator("tbody tr")).toHaveCount(2);
    await page.getByTestId("version-diff-base").selectOption(v1Id);
    await page.getByTestId("version-diff-other").selectOption(v2Id);
    const diff = page.getByTestId("version-diff-panel");
    await expect(diff.getByTestId("diff-modified-node")).toHaveCount(1);
    await expect(diff.getByTestId("diff-modified-node")).toContainText(
      "policy_guardrail",
    );
    await expect(diff.getByTestId("diff-added-node")).toHaveCount(0);

    // ---- 13. Re-run: calibrate version 2 through a second plan --------------------------------
    await openPlans(page);
    await page.getByLabel("Goal").fill("Calibrate the support workflow.");
    await page
      .getByLabel("Autonomy")
      .selectOption({ label: "Stop before each change" });
    await page.getByRole("button", { name: "Decompose goal" }).click();
    await page.getByRole("button", { name: "Approve plan" }).click();
    await page.getByRole("button", { name: "Execute" }).click();
    await field(page, /^workflow id/).fill(subject.workflowId);
    await field(page, /^agent id/).fill(subject.agentId);
    await page.getByRole("button", { name: "Continue plan" }).click();
    await approve(
      page,
      "Approve step 1: Calibrate workflow (workflow.calibrate)?",
    );
    await expect(page.locator("ol > li").first()).toContainText("waiting job");
    await expect(
      page
        .getByTitle("failed", { exact: true })
        .or(page.getByTitle("completed", { exact: true }))
        .first(),
    ).toBeVisible({ timeout: real ? 1_500_000 : 240_000 });

    // ---- 14. Compare the two calibration runs on the workflow's page -----------------------------
    await page.goto(`/caliber/workflows/${subject.workflowId}`);
    await page.getByTestId("workflow-calibrate").click();
    await expect(page.getByTestId("workflow-calibration-run")).toHaveCount(2);
    for (const run of await page
      .getByTestId("workflow-calibration-run")
      .all()) {
      await expect(run).toContainText(
        real ? /rejected|candidate ready/ : "rejected",
      );
    }
  });

  test("an unknown workflow fails the step at once and a denied calibration queues nothing", async ({
    page,
  }) => {
    test.setTimeout(600_000);
    const suffix = uiOnlySlug("c15b");
    const ui = new CaliberUi(page);
    await ui.signIn();
    // The first test's subject when it ran; otherwise build one the same way.
    const subject = built ?? (await buildSubject(page, suffix));

    // ---- 10. A workflow id that does not exist -----------------------------------------------------
    await openPlans(page);
    await page.getByLabel("Goal").fill("Calibrate the support workflow.");
    await page.getByRole("button", { name: "Decompose goal" }).click();
    await page.getByRole("button", { name: "Approve plan" }).click();
    await page.getByRole("button", { name: "Execute" }).click();
    await field(page, /^workflow id/).fill("WF-does-not-exist");
    await field(page, /^agent id/).fill(subject.agentId);
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
    await field(page, /^workflow id/).fill(subject.workflowId);
    await field(page, /^agent id/).fill(subject.agentId);
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
