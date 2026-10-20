import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

import { expectRunStatus } from "../../helpers";
import { CaliberUi, uiOnlySlug } from "../ui-only-driver";

/**
 * Cookbook 16, "Run it through the UI": a workflow is already running in production; read its
 * traces in Observability, find the failing node, capture failures into a test set, triage them in
 * a review queue whose answers are written back onto the traces, then fix the workflow (a new
 * version), re-run the tickets that mattered and compare the old and new traces.
 *
 * Follows docs-site/cookbooks/16-observability-triage/README.md. Needs a stack with the lab's
 * stand-in account service (port 8780) and `CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1`:
 *
 *   python3 docs-site/cookbooks/09-workflow-debugger-self-healing-lab/assets/upstream/account_service.py 8780 &
 *   CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1 scripts/cookbook-env.sh up lab16 --flags cookbooks
 *
 * Every step is a click or a file upload from the lab's `assets/`: the workflow is imported in the
 * Workflows page and the 40 production tickets are run one by one in its Run Monitor (the way
 * traffic would arrive), so the traces are the product's own. No API request context is used.
 * Each run of this spec makes 40 + 16 more traces on the stack it runs against.
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(here, "../../../../..");
const LAB = path.join(REPO, "docs-site/cookbooks/16-observability-triage");
const ASSETS = path.join(LAB, "assets");
const WORKFLOW_V1 = path.join(
  REPO,
  "docs-site/cookbooks/09-workflow-debugger-self-healing-lab/assets/workflow/ticket-intake.v1.workflow.yaml",
);

interface Row {
  id: string;
  tags: string[];
  input: { payload: string };
  expected: Record<string, unknown>;
}
const ROWS = new Map<string, Row>(
  fs
    .readFileSync(
      path.join(ASSETS, "dataset/prod-regression.sample.jsonl"),
      "utf8",
    )
    .split("\n")
    .filter((line) => line.trim())
    .map((line) => JSON.parse(line) as Row)
    .map((row) => [row.id, row] as [string, Row]),
);
const QUESTIONS = (
  JSON.parse(
    fs.readFileSync(
      path.join(ASSETS, "review-queues/triage-review.review-questions.json"),
      "utf8",
    ),
  ) as {
    questions: {
      key: string;
      title: string;
      type: string;
      options?: string[];
      target: string;
    }[];
  }
).questions;

/** The ticket number inside a row's payload (`T-5038`), which is what Observability shows. */
const ticketOf = (id: string): string =>
  /T-\d+/.exec(ROWS.get(id)!.input.payload)![0];

/** Editor > Publish: validate, review, publish. */
async function publishFromEditor(page: Page): Promise<void> {
  await page.getByTestId("editor-publish").click();
  await page.getByTestId("publish-run-validate").click();
  const ack = page.getByTestId("publish-ack");
  if (await ack.isVisible()) await ack.check();
  await page.getByTestId("publish-next-1").click();
  await page.getByTestId("publish-next-2").click();
  await page.getByTestId("publish-confirm").click();
  await expect(page.getByTestId("editor-message")).toContainText("Published.");
}

/** Paste a ticket into the Run Monitor, run it, wait for the run to stop, return its output. */
async function runTicket(
  page: Page,
  id: string,
  expectedStatus: "completed" | "failed",
): Promise<string> {
  const panel = page.getByTestId("run-monitor-panel");
  if (!(await panel.isVisible())) {
    await page.getByTestId("editor-run-monitor").click();
  }
  await expect(panel).toBeVisible();
  const link = page.getByTestId("run-active-open-link");
  const before = (await link.isVisible())
    ? await link.getAttribute("href")
    : "";
  await page.getByTestId("run-input").fill(ROWS.get(id)!.input.payload);
  await page.getByTestId("run-execute").click();
  await expect
    .poll(
      async () =>
        (await link.isVisible()) ? await link.getAttribute("href") : "",
      { timeout: 60_000 },
    )
    .not.toBe(before);
  await expectRunStatus(page, expectedStatus, { timeout: 120_000 });
  return expectedStatus === "completed"
    ? ((await page.getByTestId("run-output").textContent()) ?? "")
    : "";
}

/** Observability: search for a ticket and open its newest trace; returns the trace id. */
async function openTraceOf(page: Page, id: string, nth = 0): Promise<string> {
  await page.goto("/caliber/observability");
  await page.getByLabel("Search traces").fill(ticketOf(id));
  const row = page.getByTestId("observability-trace-row").nth(nth);
  await row.click();
  await expect(page).toHaveURL(/[?&]trace=tr-/);
  return new URL(page.url()).searchParams.get("trace")!;
}

test("Cookbook 16: observe, capture, triage, fix and compare production failures in the UI", async ({
  page,
}) => {
  test.setTimeout(1_500_000);
  const ui = new CaliberUi(page);
  const slug = uiOnlySlug("lab16");
  const workflowName = `prod-refund-intake-${slug}`;
  const testSet = `prod-regression-${slug}`;
  const queueName = `prod-triage-${slug}`;
  const mattered = [...ROWS.values()].filter(
    (row) => !row.tags.includes("correct"),
  );
  expect(mattered).toHaveLength(16);

  await ui.signIn();

  // ---- 0. The production workflow: import version 1 and publish it -------------------------
  await page.goto("/caliber/workflows");
  await page.getByTestId("import-workflow").click();
  await page.getByTestId("workflow-import-name").fill(workflowName);
  await page.locator('input[type="file"]').setInputFiles(WORKFLOW_V1);
  await expect(page.getByTestId("workflow-import-manifest")).toHaveValue(
    /refund-intake/,
  );
  await page.getByTestId("workflow-import-validate").click();
  await expect(page.getByTestId("workflow-import-preview")).toContainText(
    "Preflight passed",
  );
  await page.getByTestId("workflow-import-submit").click();
  await expect(page).toHaveURL(/\/caliber\/workflows\/WF-[^/]+\/editor\/WFV-/);
  const workflowUrl = page.url().replace(/\/editor\/.*$/, "");
  await publishFromEditor(page);

  // ---- 1. Traffic: the 40 tickets go through the Run Monitor -------------------------------
  for (const row of ROWS.values()) {
    await runTicket(
      page,
      row.id,
      row.tags.includes("error") ? "failed" : "completed",
    );
  }

  // ---- 2. Observability: filter by status --------------------------------------------------
  await page.goto("/caliber/observability");
  const rows = page.getByTestId("observability-trace-row");
  await expect(rows.first()).toBeVisible();
  await page.getByLabel("Filter by status").selectOption({ label: "Error" });
  await expect(rows.first()).toBeVisible();
  // The list re-queries on a filter change: wait until it shows only what was asked for, then count.
  await expect(rows.filter({ hasNotText: "ERROR" })).toHaveCount(0);
  await expect.poll(() => rows.count()).toBeGreaterThanOrEqual(12);
  await page.getByLabel("Filter by status").selectOption({ label: "OK" });
  await expect(rows.first()).toContainText("OK");
  await expect(rows.filter({ hasNotText: "OK" })).toHaveCount(0);
  await expect.poll(() => rows.count()).toBeGreaterThanOrEqual(28);
  await page
    .getByLabel("Filter by status")
    .selectOption({ label: "All statuses" });

  // ---- 3. A trace: the request and the node that failed ------------------------------------
  const poisonTrace = await openTraceOf(page, "P35");
  const spans = page.getByTestId("trace-span-row");
  await expect(spans.filter({ hasText: "workflow.run" })).toContainText(
    "ERROR",
  );
  await expect(spans.filter({ hasText: "node.check_reference" })).toContainText(
    "ERROR",
  );
  await expect(spans.filter({ hasText: "node.validate" })).not.toContainText(
    "ERROR",
  );
  const upstreamTrace = await openTraceOf(page, "P38");
  await expect(
    page.getByTestId("trace-span-row").filter({ hasText: "node.decide" }),
  ).toContainText("ERROR");
  await expect(
    page.getByTestId("trace-span-row").filter({ hasText: "node.lookup" }),
  ).not.toContainText("ERROR");
  const silentTrace = await openTraceOf(page, "P25");
  await expect(page.getByTestId("trace-identity")).toBeVisible();
  await expect(
    page.getByTestId("trace-span-row").filter({ hasText: "workflow.run" }),
  ).not.toContainText("ERROR");

  // ---- 4. Failure paths: nothing matches, and a trace that does not exist ------------------
  await page.goto("/caliber/observability");
  await page.getByLabel("Search traces").fill("no-such-ticket-zzz");
  await expect(page.getByTestId("observability-empty")).toBeVisible();
  await page.goto("/caliber/observability?trace=tr-doesnotexist");
  await expect(
    page.getByText("trace 'tr-doesnotexist' not found"),
  ).toBeVisible();

  // ---- 5. A test set from traces -----------------------------------------------------------
  await page.goto("/caliber/eval-datasets");
  await page.getByRole("button", { name: "+ New Test Set" }).click();
  await page.getByPlaceholder("factual-checks").fill(testSet);
  await page.getByPlaceholder("@sarah").fill("@admin");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.getByText(testSet, { exact: true }).first()).toBeVisible();

  // From Observability: open the trace, pick the test set, Add example.
  await page.goto(`/caliber/observability?trace=${upstreamTrace}`);
  const picker = page.getByTestId("trace-add-to-dataset");
  const option = picker.locator("option", { hasText: testSet });
  await picker
    .getByLabel("Choose test set")
    .selectOption((await option.getAttribute("value"))!);
  await picker.getByRole("button", { name: "Add example" }).click();
  // From the test set: capture another trace by id.
  await page.goto("/caliber/eval-datasets");
  await page.getByText(testSet, { exact: true }).first().click();
  await expect(page.getByTestId("eval-dataset-detail")).toBeVisible();
  await page.getByTestId("from-trace-toggle").click();
  await page.getByTestId("from-trace-id").fill(poisonTrace);
  await page.getByTestId("from-trace-save").click();
  const examples = page.getByTestId("example-row");
  await expect(examples).toHaveCount(2);
  const upstream = examples.filter({ hasText: "T-5038" });
  await expect(upstream).toHaveCount(1);
  await expect(examples.filter({ hasText: "T-5035" })).toHaveCount(1);
  // The capture stores the failing output as the expected answer: put the gold outcome there.
  await expect(upstream).toContainText("KeyError: 'tier'");
  await upstream.getByTestId("example-edit").click();
  await page
    .getByTestId("example-expected")
    .fill(JSON.stringify(ROWS.get("P38")!.expected, null, 2));
  await page.getByTestId("example-save").click();
  await expect(examples.filter({ hasText: "T-5038" })).toContainText(
    "ACCOUNT_LOOKUP_FAILED",
  );

  // ---- 6. The triage queue -----------------------------------------------------------------
  await page.goto("/caliber/review-queues");
  await page.getByRole("button", { name: "New queue" }).click();
  await page.getByPlaceholder("answer-quality").fill(queueName);
  for (const [i, q] of QUESTIONS.entries()) {
    if (i > 0)
      await page.getByRole("button", { name: "+ Add question" }).click();
    const n = i + 1;
    await page.getByLabel(`Question ${n} key`).fill(q.key);
    await page.getByLabel(`Question ${n} title`).fill(q.title);
    await page.getByLabel(`Question ${n} type`).selectOption(q.type);
    await page.getByLabel(`Question ${n} target`).selectOption(q.target);
    if (q.options)
      await page.getByLabel(`Question ${n} options`).fill(q.options.join(", "));
  }
  await page.getByRole("button", { name: "Create queue" }).click();
  await page.getByRole("button", { name: queueName }).click();
  const toReview = [upstreamTrace, silentTrace];
  await page.getByLabel("Trace ids").fill(toReview.join(" "));
  await page.getByRole("button", { name: "Enqueue" }).click();
  await expect(page.getByText(`Items (${toReview.length})`)).toBeVisible();

  const reviewing = page
    .getByText("Reviewing trace")
    .locator("xpath=following-sibling::div");
  const answers: Record<string, { mode: string; severity: string }> = {
    [upstreamTrace]: { mode: "tool", severity: "high" },
    [silentTrace]: { mode: "data", severity: "low" },
  };
  let previous = "";
  for (let i = 0; i < toReview.length; i++) {
    await expect(reviewing).toBeVisible();
    if (previous) await expect(reviewing).not.toHaveText(previous);
    const traceId = (await reviewing.textContent())!.trim();
    const answer = answers[traceId]!;
    await page.getByRole("button", { name: "Pass", exact: true }).click();
    await page.getByLabel("Where did it fail?").selectOption(answer.mode);
    await page.getByLabel("Operational severity").selectOption(answer.severity);
    await page
      .getByLabel("What should the run have produced?")
      .fill("manual_review");
    await page.getByLabel("Triage notes").fill(`lab 16 review ${slug}`);
    await page.getByRole("button", { name: "Submit review" }).click();
    previous = traceId;
  }
  await expect(page.getByText("All items reviewed.")).toBeVisible();

  // ---- 7. The answers are on the traces ----------------------------------------------------
  await page.goto(`/caliber/observability?trace=${upstreamTrace}`);
  const assessments = page.getByTestId("trace-assessments");
  await expect(assessments).toContainText("failure_mode");
  await expect(assessments).toContainText("tool");
  await expect(assessments).toContainText("severity");
  await expect(assessments).toContainText("high");

  // ---- 8. Fix it: a new version of the workflow ---------------------------------------------
  await page.goto(`${workflowUrl}?tab=versions`);
  await page.getByRole("button", { name: "Restore as draft" }).first().click();
  await expect(page).toHaveURL(/\/editor\/WFV-/);
  for (const node of ["validate", "check_reference", "decide"]) {
    await page.getByTestId(`rf__node-${node}`).click();
    await page
      .getByTestId("inspector-python-code")
      .fill(fs.readFileSync(path.join(ASSETS, `fix/${node}.py`), "utf8"));
  }
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-save-status")).toContainText("Saved");
  await publishFromEditor(page);

  // ---- 9. Re-run the 16 tickets that mattered: each now gives its gold outcome --------------
  for (const row of mattered) {
    const output = await runTicket(page, row.id, "completed");
    const out = JSON.parse(output.slice(output.indexOf("{"))) as Record<string, unknown>;
    expect(out["decision"], row.id).toBe(row.expected["decision"]);
    expect(out["reason_code"], row.id).toBe(row.expected["reason_code"]);
    if (row.expected["upstream_status"] !== undefined)
      expect(out["upstream_status"], row.id).toBe(
        row.expected["upstream_status"],
      );
  }

  // ---- 10. Compare: the same ticket before and after ---------------------------------------
  await page.goto("/caliber/observability");
  await page.getByLabel("Search traces").fill(ticketOf("P38"));
  // Newest first: the version 2 trace, then the version 1 trace of the same ticket (a stack
  // that an earlier run of this spec used has older pairs below).
  await expect(rows.nth(1)).toBeVisible();
  await expect(rows.nth(0)).toContainText("OK");
  await expect(rows.nth(1)).toContainText("ERROR");
  await page.getByRole("button", { name: "Compare", exact: true }).click();
  await rows.nth(0).click();
  await rows.nth(1).click();
  const compare = page.getByTestId("trace-compare");
  await expect(compare).toBeVisible();
  await expect(compare).toContainText("ERROR");
  await expect(compare).toContainText("OK");
  // The fixed ticket is OK, and so are all 16: the Error filter no longer finds new failures.
  await page.getByRole("button", { name: "Compare", exact: true }).click();
  await page.getByLabel("Filter by status").selectOption({ label: "OK" });
  await page.getByLabel("Search traces").fill("");
  await expect(rows.first()).toContainText("OK");
  await expect(rows.filter({ hasNotText: "OK" })).toHaveCount(0);
  await expect.poll(() => rows.count()).toBeGreaterThanOrEqual(44);

  // ---- 11. Compare the two versions of the workflow ----------------------------------------
  await page.goto(`${workflowUrl}?tab=versions`);
  await page
    .getByTestId("version-diff-base")
    .selectOption({ label: "v1 · published" });
  await page
    .getByTestId("version-diff-other")
    .selectOption({ label: "v2 · published" });
  await expect(page.getByTestId("version-diff-panel")).toContainText(
    "check_reference",
  );
});
