import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";

/**
 * Cookbook 16, "Run it through the UI": read production traces in Observability, find the failing
 * node, capture failures into a test set, and triage them in a review queue whose answers are
 * written back onto the traces.
 *
 * Follows docs-site/cookbooks/16-observability-triage/README.md. Needs a stack with the lab's
 * stand-in account service (port 8780) and `CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1`, and a personal
 * access token in `CALIBER_TOKEN` (for the seed script only; every other step is a click):
 *
 *   python3 docs-site/cookbooks/09-workflow-debugger-self-healing-lab/assets/upstream/account_service.py 8780 &
 *   CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1 scripts/cookbook-env.sh up lab16 --flags cookbooks
 *
 * The 40 traces come from `assets/seed_traces.py`, which runs 40 tickets through a real workflow
 * (`CALIBER_LAB_PYTHON`, default the repo's dev venv). Each run of this spec seeds 40 more.
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(here, "../../../../..");
const LAB = path.join(REPO, "docs-site/cookbooks/16-observability-triage");
const ASSETS = path.join(LAB, "assets");

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

function seedTraces(): Record<string, { trace_id: string; status: string }> {
  const token = process.env["CALIBER_TOKEN"];
  if (!token)
    throw new Error(
      "CALIBER_TOKEN (a personal access token) is required to seed traces",
    );
  const base = (process.env["CALIBER_LAB_BASE_URL"] ?? "").replace(
    /\/caliber\/?$/,
    "",
  );
  const python =
    process.env["CALIBER_LAB_PYTHON"] ??
    path.join(REPO, "caliber/.venv/bin/python");
  const out = execFileSync(python, [path.join(ASSETS, "seed_traces.py")], {
    env: {
      ...process.env,
      CALIBER_BASE_URL: base,
      CALIBER_TOKEN: token,
      PYTHONPATH: path.join(REPO, "sdk/caliber-sdk"),
    },
    encoding: "utf8",
    timeout: 600_000,
  });
  return JSON.parse(out) as Record<
    string,
    { trace_id: string; status: string }
  >;
}

test("Cookbook 16: observe, capture and triage production failures in the UI", async ({
  page,
}) => {
  test.setTimeout(900_000);
  const ui = new CaliberUi(page);
  const slug = uiOnlySlug("lab16");
  const testSet = `prod-regression-${slug}`;
  const queueName = `prod-triage-${slug}`;
  const seeded = seedTraces();
  const traceOf = (id: string): string => seeded[id]!.trace_id;
  expect(Object.keys(seeded)).toHaveLength(40);
  expect(
    Object.values(seeded).filter((s) => s.status === "failed"),
  ).toHaveLength(12);

  await ui.signIn();

  // ---- 1. Observability: filter by status ---------------------------------------------------
  await page.goto("/caliber/observability");
  const rows = page.getByTestId("observability-trace-row");
  await expect(rows.first()).toBeVisible();
  await page.getByLabel("Filter by status").selectOption({ label: "Error" });
  await expect(rows.first()).toBeVisible();
  const errorRows = await rows.count();
  expect(errorRows).toBeGreaterThanOrEqual(12);
  for (const text of await rows.allInnerTexts())
    expect(text).toContain("ERROR");
  await page.getByLabel("Filter by status").selectOption({ label: "OK" });
  await expect(rows.first()).toContainText("OK");
  expect(await rows.count()).toBeGreaterThanOrEqual(28);

  // ---- 2. A trace: the request and the node that failed -------------------------------------
  await page
    .getByLabel("Filter by status")
    .selectOption({ label: "All statuses" });
  await page.goto(`/caliber/observability?trace=${traceOf("P35")}`);
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
  await page.goto(`/caliber/observability?trace=${traceOf("P38")}`);
  await expect(
    page.getByTestId("trace-span-row").filter({ hasText: "node.decide" }),
  ).toContainText("ERROR");
  await expect(
    page.getByTestId("trace-span-row").filter({ hasText: "node.lookup" }),
  ).not.toContainText("ERROR");

  // ---- 3. Failure paths: nothing matches, and a trace that does not exist -------------------
  await page.goto("/caliber/observability");
  await page.getByLabel("Search traces").fill("no-such-ticket-zzz");
  await expect(page.getByTestId("observability-empty")).toBeVisible();
  await page.goto("/caliber/observability?trace=tr-doesnotexist");
  await expect(
    page.getByText("trace 'tr-doesnotexist' not found"),
  ).toBeVisible();

  // ---- 4. A test set from traces -------------------------------------------------------------
  await page.goto("/caliber/eval-datasets");
  await page.getByRole("button", { name: "+ New Test Set" }).click();
  await page.getByPlaceholder("factual-checks").fill(testSet);
  await page.getByPlaceholder("@sarah").fill("@admin");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.getByText(testSet, { exact: true }).first()).toBeVisible();

  // From Observability: open the trace, pick the test set, Add example.
  await page.goto(`/caliber/observability?trace=${traceOf("P38")}`);
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
  await page.getByTestId("from-trace-id").fill(traceOf("P35"));
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

  // ---- 5. The triage queue -------------------------------------------------------------------
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
  const toReview = ["P38", "P25"];
  await page.getByLabel("Trace ids").fill(toReview.map(traceOf).join(" "));
  await page.getByRole("button", { name: "Enqueue" }).click();
  await expect(page.getByText(`Items (${toReview.length})`)).toBeVisible();

  const reviewing = page
    .getByText("Reviewing trace")
    .locator("xpath=following-sibling::div");
  const answers: Record<string, { mode: string; severity: string }> = {
    [traceOf("P38")]: { mode: "tool", severity: "high" },
    [traceOf("P25")]: { mode: "data", severity: "low" },
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

  // ---- 6. The answers are on the traces -----------------------------------------------------
  await page.goto(`/caliber/observability?trace=${traceOf("P38")}`);
  const assessments = page.getByTestId("trace-assessments");
  await expect(assessments).toContainText("failure_mode");
  await expect(assessments).toContainText("tool");
  await expect(assessments).toContainText("severity");
  await expect(assessments).toContainText("high");
});
