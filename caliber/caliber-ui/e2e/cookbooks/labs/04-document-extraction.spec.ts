import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

import { expectRunStatus as expectStatus } from "../../helpers";
import { CaliberUi, uiOnlySlug } from "../ui-only-driver";

/**
 * Cookbook 04 (`docs-site/cookbooks/04-document-extraction-structuring-lab/README.md`), "Recipe: run it
 * through the UI".
 *
 * Needs a server started with `CALIBER_APPROVER_USERS=admin,reviewer CALIBER_OPERATOR_USERS=admin,reviewer
 * scripts/cookbook-env.sh up NAME --flags cookbooks --s3` (the two variables give the second account its
 * platform scopes). Nothing is seeded and no API call is made while the steps run: the reviewer account,
 * the workspace, the buckets, the uploads, the install, the runs, the approvals, the version 2 edit and
 * the comparison are all clicks in the web app. The one exception is the documented cleanup at the
 * start, which empties the two buckets so the run can be repeated (the output bucket refuses to
 * overwrite an invoice, by design).
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const ASSETS = path.resolve(
  process.env["COOKBOOK_04_ASSETS"] ??
    path.join(
      here,
      "../../../../../docs-site/cookbooks/04-document-extraction-structuring-lab/assets",
    ),
);
const SOURCES = path.join(ASSETS, "dataset/sources");

interface Case {
  id: string;
  inputs: { key: string };
  expectations: { outcome: string; errors?: string[] };
}
const CASES: Case[] = fs
  .readFileSync(path.join(ASSETS, "dataset/extraction-cases.jsonl"), "utf8")
  .split("\n")
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line) as Case);
const fileOf = (c: Case): string => c.inputs.key.split("/")[1]!;
const byFile = (name: string): Case => CASES.find((c) => fileOf(c) === name)!;

const INPUT_BUCKET = "doc-intake";
const OUTPUT_BUCKET = "doc-extracted";
const REVIEWER = { user: "reviewer", password: "reviewer-pass-123" };

/** The documented cleanup: empty and remove both buckets so the walk-through starts from nothing. */
async function resetBuckets(page: Page): Promise<void> {
  const api = "/ajax-api/2.0/mlflow/caliber/object-store";
  for (const bucket of [INPUT_BUCKET, OUTPUT_BUCKET]) {
    const listed = await page.request.get(
      `${api}/buckets/${bucket}/objects?recursive=true`,
    );
    if (!listed.ok()) continue;
    const body = (await listed.json()) as {
      data: { objects: { key: string }[] };
    };
    const keys = body.data.objects.map((o) => o.key);
    if (keys.length)
      await page.request.post(`${api}/buckets/${bucket}/objects/delete`, {
        data: { keys },
      });
    await page.request.delete(`${api}/buckets/${bucket}`);
  }
}

async function createBucket(page: Page, name: string): Promise<void> {
  await page.getByTestId("new-bucket-input").fill(name);
  await page.getByRole("button", { name: "Create bucket" }).click();
  await expect(page.getByTestId(`bucket-${name}`)).toBeVisible();
  await expect(page.getByTestId("new-bucket-input")).toHaveValue("");
}

/** Queued runs can take a while on a busy machine, so wait longer than the helper's default. */
const expectRunStatus = (page: Page, status: string): Promise<void> =>
  expectStatus(page, status, { timeout: 180_000 });

/** Start one run from the editor's run monitor (the run input is the document's object key). */
async function runDocument(page: Page, key: string): Promise<string> {
  await page.getByTestId("run-input").fill(key);
  const started = page.waitForResponse(
    (r) =>
      r.url().endsWith("/workflow-runs") &&
      r.request().method() === "POST" &&
      r.status() === 202,
  );
  // The run monitor's sticky header overlaps its own buttons' hit-test point, so the click is
  // dispatched straight to the button (the same pattern the Workflow Studio specs use).
  await page.getByTestId("run-execute").click();
  const body = (await (await started).json()) as {
    data: { workflow_run_id: string };
  };
  return body.data.workflow_run_id;
}

/** Editor > Publish: validate, review, publish (the drawer's three steps). */
async function publishFromEditor(page: Page): Promise<void> {
  await page.getByTestId("editor-publish").click();
  await page.getByTestId("publish-run-validate").click();
  // The report (problems, warnings or "No problems.") must be on screen before the box is looked for.
  await expect(
    page.getByTestId("publish-validate").locator("li").first(),
  ).toBeVisible();
  const ack = page.getByTestId("publish-ack");
  if (await ack.isVisible()) await ack.check();
  await page.getByTestId("publish-next-1").click();
  await page.getByTestId("publish-next-2").click();
  await page.getByTestId("publish-confirm").click();
  await expect(page.getByTestId("editor-message")).toContainText("Published.");
}

test.use({ actionTimeout: 30_000 });

test.describe("Cookbook 04: document-to-JSON pipeline", () => {
  test("buckets, upload, install, run valid and invalid documents, approve and reject", async ({
    page,
  }) => {
    test.setTimeout(900_000);
    const ui = new CaliberUi(page);
    const workspace = uiOnlySlug("invoice-intake");

    await ui.signIn();
    await resetBuckets(page);

    // 1. A workspace for the lab, and the reviewer in it.
    await page.getByRole("button", { name: "Create workspace" }).click();
    await page.getByPlaceholder("Document automation").fill(workspace);
    await page.getByRole("button", { name: "Create and select" }).click();
    await expect(page.getByLabel("Active workspace")).toContainText(workspace);

    await page.goto("/caliber/administration");
    // The second person: an account made on the Administration page (skipped when a previous run of
    // this spec already created it: accounts are not deleted in the alpha).
    const accounts = page.getByRole("table", {
      name: "Accounts in this deployment",
    });
    await expect(
      page.getByRole("button", { name: "Create account" }),
    ).toBeVisible();
    if (!(await accounts.getByText(REVIEWER.user, { exact: true }).count())) {
      await page.getByPlaceholder("@alice").fill(REVIEWER.user);
      await page
        .getByLabel("Password", { exact: true })
        .fill(REVIEWER.password);
      await page.getByRole("button", { name: "Create account" }).click();
      await expect(
        accounts.getByText(REVIEWER.user, { exact: true }),
      ).toBeVisible();
    }
    await page
      .getByLabel("Project for access management")
      .selectOption({ label: `${workspace} (owner)` });
    await page.getByPlaceholder("@developer").fill(REVIEWER.user);
    await page.locator("form select").selectOption("reviewer");
    await page.getByRole("button", { name: "Add member" }).click();
    await expect(
      page.getByRole("table", { name: "Project members" }),
    ).toContainText(REVIEWER.user);

    // 2. Both buckets, then the intake folder and the thirty documents.
    await page.goto("/caliber/object-store");
    await expect(page.getByTestId("object-store-page")).toBeVisible();
    await expect(page.getByTestId("object-store-offline")).toHaveCount(0);
    await createBucket(page, INPUT_BUCKET);
    await createBucket(page, OUTPUT_BUCKET);
    await page.getByTestId(`bucket-${INPUT_BUCKET}`).click();
    await page.getByRole("button", { name: "New folder" }).click();
    await page.getByTestId("new-folder-input").fill("incoming");
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await page.getByTitle("Open folder").first().click();
    await page
      .getByTestId("upload-input")
      .setInputFiles(CASES.map((c) => path.join(SOURCES, fileOf(c))));
    for (const c of CASES) {
      await expect(page.getByTestId(`object-${c.inputs.key}`)).toBeVisible({
        timeout: 60_000,
      });
    }

    // 3. Preview one stored document.
    await page
      .getByTestId(`object-${byFile("inv-2026-0101-acme.txt").inputs.key}`)
      .getByTitle("View preview")
      .click();
    const preview = page.getByTestId("object-preview-modal");
    await expect(preview).toContainText("Invoice No: INV-2026-0101");
    await expect(preview).toContainText("Hydraulic hose assembly");
    await page.getByLabel("Close file preview").click();

    // 4. Install the cookbook: one paused workflow and one draft version.
    const workflowName = uiOnlySlug("doc-intake-to-json");
    await page.goto("/caliber/cookbooks");
    await page.getByTestId("install-cookbook-04").click();
    await page.getByTestId("cookbook-install-name").fill(workflowName);
    await page.getByTestId("cookbook-prerequisites-ack").check();
    await page.getByTestId("confirm-cookbook-install").click();
    await expect(page).toHaveURL(
      /\/caliber\/workflows\/[^/]+\/editor\/[^/?]+$/,
    );
    for (const node of [
      "read_document",
      "extract_fields",
      "validate_schema",
      "package_result",
      "route",
      "review_gate",
      "write_extracted",
      "write_reviewed",
    ]) {
      await expect(page.getByTestId(`outline-${node}`)).toBeVisible();
    }
    await page.getByTestId("editor-validate").click();
    await expect(page.getByTestId("editor-message")).toContainText("Valid.");
    const editorUrl = page.url();
    const workflowId = /workflows\/([^/]+)\/editor/.exec(editorUrl)![1]!;

    // 5. The installed workflow is paused: resume it, then open the editor again.
    await page.goto(`/caliber/workflows/${workflowId}`);
    await page.getByTestId("workflow-pause-toggle").click();
    await expect(page.getByTestId("workflow-pause-toggle")).toHaveText("Pause");
    await page.goto(editorUrl);
    await page.getByTestId("editor-run-monitor").click();
    await expect(page.getByTestId("run-monitor-panel")).toBeVisible();

    // 6. A valid invoice runs to the end and the JSON lands in the output bucket.
    const validKey = byFile("inv-2026-0101-acme.txt").inputs.key;
    await runDocument(page, validKey);
    await expectRunStatus(page, "completed");
    await expect(page.getByTestId("run-debugger-section")).toContainText(
      "write_extracted",
    );

    // A yen invoice is valid under version 1 too: it is written, which is what version 2 will change.
    const yenKey = byFile("inv-2026-0107-tokyo.txt").inputs.key;
    await runDocument(page, yenKey);
    await expectRunStatus(page, "completed");
    const v1YenHref = await page
      .getByTestId("run-active-open-link")
      .getAttribute("href");

    // 7. The same invoice again is refused by the output bucket (it never overwrites).
    await runDocument(page, byFile("inv-2026-0101-acme-resend.txt").inputs.key);
    await expectRunStatus(page, "failed");
    await expect(page.getByTestId("run-monitor-panel")).toContainText(
      "already exists",
    );

    // 8. An invoice with no total fails validation and stops at the approval gate.
    const holdKey = byFile("inv-2026-0115-no-total.txt").inputs.key;
    const heldRun = await runDocument(page, holdKey);
    await expectRunStatus(page, "waiting_approval");
    await expect(page.getByTestId("run-debugger-section")).toContainText(
      "review_gate",
    );
    await expect(page.getByTestId("run-approval-actions")).toBeVisible();
    // The account that started the run is not the one who may approve it.
    await page.getByTestId("run-approve").click();
    await expect(
      page.getByText(/cannot approve it; a second person must decide/),
    ).toBeVisible();
    await expectRunStatus(page, "waiting_approval");

    // 9. A second invalid invoice (no vendor) is held as well; it will be rejected.
    const rejectRun = await runDocument(
      page,
      byFile("inv-2026-0117-no-vendor.txt").inputs.key,
    );
    await expectRunStatus(page, "waiting_approval");

    // 10. The reviewer (a second person) approves the first and rejects the second.
    await page.context().clearCookies();
    await ui.signIn(REVIEWER.user, REVIEWER.password);
    await page
      .getByLabel("Active workspace")
      .selectOption({ label: workspace });
    await page.goto(editorUrl);
    await page.getByTestId("editor-run-monitor").click();
    await page.getByTestId(`run-history-select-${heldRun}`).click();
    await expectRunStatus(page, "waiting_approval");
    await page.getByTestId("run-approve").click();
    await expect(page.getByTestId("run-resume")).toBeEnabled({
      timeout: 30_000,
    });
    await page.getByTestId("run-resume").click();
    await expectRunStatus(page, "completed");
    await expect(page.getByTestId("run-debugger-section")).toContainText(
      "write_reviewed",
    );

    await page.getByTestId(`run-history-select-${rejectRun}`).click();
    await expectRunStatus(page, "waiting_approval");
    await page.getByTestId("run-reject").click();
    await expectRunStatus(page, "failed");

    // 11. What reached the output bucket: the valid invoice and the approved one, nothing else.
    await page.goto("/caliber/object-store");
    await page.getByTestId(`bucket-${OUTPUT_BUCKET}`).click();
    await page
      .getByTitle("Open folder")
      .filter({ hasText: "invoices" })
      .click();
    await expect(
      page.getByTestId("object-invoices/INV-2026-0101.json"),
    ).toBeVisible();
    await page
      .getByTestId("object-invoices/INV-2026-0101.json")
      .getByTitle("View preview")
      .click();
    await expect(page.getByTestId("object-preview-modal")).toContainText(
      '"invoice_number": "INV-2026-0101"',
    );
    await expect(page.getByTestId("object-preview-modal")).toContainText(
      '"total": 646.92',
    );
    await page.getByLabel("Close file preview").click();
    // The yen invoice (valid under version 1) was written as well.
    await expect(
      page.getByTestId("object-invoices/TP-20260317-02.json"),
    ).toBeVisible();
    await page.goto("/caliber/object-store?bucket=" + OUTPUT_BUCKET);
    await page
      .getByTitle("Open folder")
      .filter({ hasText: "reviewed" })
      .click();
    await expect(
      page.getByTestId("object-reviewed/inv-2026-0115-no-total.json"),
    ).toBeVisible();
    await expect(
      page.getByTestId("object-reviewed/inv-2026-0117-no-vendor.json"),
    ).toHaveCount(0);

    // 12. Version 1 is final: sign back in as admin and publish it from the editor.
    await page.context().clearCookies();
    await ui.signIn();
    await page
      .getByLabel("Active workspace")
      .selectOption({ label: workspace });
    await page.goto(editorUrl);
    await expect(page.getByText("v1 · draft")).toBeVisible();
    await publishFromEditor(page);
    await expect(page.getByText("v1 · published")).toBeVisible();

    // 13. Finance now wants yen invoices checked by a person: restore v1 as a draft (version 2),
    //     remove JPY from the schema's currency list in the validate_schema node, save and publish.
    await page.getByTestId("editor-restore-as-draft").click();
    await expect(page.getByText("v2 · draft")).toBeVisible();
    await page.getByTestId("rf__node-validate_schema").click();
    const config = page.getByTestId("inspector-transform-config");
    const cfg = JSON.parse(await config.inputValue()) as {
      schema: { properties: { currency: { enum: string[] } } };
    };
    expect(cfg.schema.properties.currency.enum).toContain("JPY");
    cfg.schema.properties.currency.enum =
      cfg.schema.properties.currency.enum.filter((code) => code !== "JPY");
    await config.fill(JSON.stringify(cfg, null, 2));
    await page.getByTestId("editor-save").click();
    await expect(page.getByTestId("editor-save-status")).toContainText("Saved");
    await publishFromEditor(page);
    await expect(page.getByText("v2 · published")).toBeVisible();

    // 14. The same yen invoice on version 2 is held for review instead of being written.
    await page.getByTestId("editor-run-monitor").click();
    await runDocument(page, yenKey);
    await expectRunStatus(page, "waiting_approval");
    await expect(page.getByTestId("run-debugger-section")).toContainText(
      "review_gate",
    );
    const v2YenHref = await page
      .getByTestId("run-active-open-link")
      .getAttribute("href");
    expect(v2YenHref).not.toBe(v1YenHref);

    // 15. Compare: the Versions tab diff names the node that changed, and the Runs tab lists both
    //     runs of the same document, one per version.
    await page.goto(`/caliber/workflows/${workflowId}?tab=versions`);
    await page
      .getByTestId("version-diff-base")
      .selectOption({ label: "v1 · published" });
    await page
      .getByTestId("version-diff-other")
      .selectOption({ label: "v2 · published" });
    await expect(page.getByTestId("diff-modified-node")).toContainText(
      "validate_schema",
    );
    await page.goto(`/caliber/workflows/${workflowId}?tab=runs`);
    await expect(page.getByTestId("runs-table")).toContainText("Completed");
    await expect(page.getByTestId("runs-table")).toContainText(
      "Awaiting approval",
    );
  });
});
