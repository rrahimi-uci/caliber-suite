import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

import { expectRunStatus } from "../../helpers";
import { CaliberUi, uiOnlySlug } from "../ui-only-driver";

/**
 * Cookbook 09, "Run it through the UI": import the `refund-intake` workflow, reproduce three kinds of
 * failure in the Run Monitor, read the debugger, retry, patch the three faulty nodes in the editor,
 * publish version 2, prove the fix on the same inputs, compare the versions, and see the live alias
 * refuse a version that has no deploy gate.
 *
 * Follows docs-site/cookbooks/09-workflow-debugger-self-healing-lab/README.md. Needs a stack with the
 * Cookbooks flag, the lab's stand-in account service on port 8780 and
 * `CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1`:
 *
 *   python3 docs-site/cookbooks/09-workflow-debugger-self-healing-lab/assets/upstream/account_service.py 8780 &
 *   CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1 scripts/cookbook-env.sh up lab09 --flags cookbooks
 *
 * No API request context is used: every mutation goes through the pages.
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const LAB = path.resolve(
  here,
  "../../../../../docs-site/cookbooks/09-workflow-debugger-self-healing-lab",
);
const ASSETS = path.join(LAB, "assets");

interface Row {
  id: string;
  input: { payload: string };
  expected: Record<string, unknown>;
}
const ROWS = new Map<string, Row>(
  fs
    .readFileSync(path.join(ASSETS, "dataset/failing-inputs.jsonl"), "utf8")
    .split("\n")
    .filter((line) => line.trim())
    .map((line) => JSON.parse(line) as Row)
    .map((row) => [row.id, row] as [string, Row]),
);
const ticket = (id: string): string => ROWS.get(id)!.input.payload;

/** Editor > Publish: validate, review, publish. */
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

/** Paste a ticket into the Run Monitor, run it, and wait for the run to stop. */
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
  await page.getByTestId("run-input").fill(ticket(id));
  // The run monitor's sticky header overlaps the run controls; dispatch the click to the button.
  await page.getByTestId("run-execute").click();
  await expect
    .poll(
      async () =>
        (await link.isVisible()) ? await link.getAttribute("href") : "",
      {
        timeout: 60_000,
      },
    )
    .not.toBe(before);
  await expectRunStatus(page, expectedStatus, { timeout: 120_000 });
  return (await link.getAttribute("href")) ?? "";
}

test("Cookbook 09: debug a failing workflow, patch it, and prove the patch in the UI", async ({
  page,
}) => {
  test.setTimeout(900_000);
  const ui = new CaliberUi(page);
  const slug = uiOnlySlug("lab09");
  const workflowName = `refund-intake-${slug}`;

  // ---- 1. Import version 1 and publish it ------------------------------------------------
  await ui.signIn();
  await page.goto("/caliber/workflows");
  await page.getByTestId("import-workflow").click();
  await page.getByTestId("workflow-import-name").fill(workflowName);
  await page
    .locator('input[type="file"]')
    .setInputFiles(
      path.join(ASSETS, "workflow/ticket-intake.v1.workflow.yaml"),
    );
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
  const editorV1 = page.url();

  // ---- 2. A healthy ticket completes ------------------------------------------------------
  await runTicket(page, "F01", "completed");
  await expect(page.getByTestId("run-output")).toContainText(
    '"decision": "approve"',
  );

  // ---- 3. A schema violation fails at `validate` ------------------------------------------
  await runTicket(page, "F12", "failed");
  const recovery = page.getByTestId("run-recovery-section");
  await expect(recovery).toContainText("This run ended in failed.");
  await expect(recovery).toContainText("validate");
  await expect(recovery).toContainText(
    "ValueError: amount must be a number, got '48.00'",
  );
  await expect(page.getByTestId("run-checkpoints-section")).toContainText(
    "No persisted checkpoints exist for this run yet",
  );
  await expect(page.getByTestId("run-debugger-section")).toContainText(
    "1 recorded step",
  );

  // ---- 4. The poison record fails at `check_reference` ------------------------------------
  await runTicket(page, "F29", "failed");
  await expect(page.getByTestId("run-recovery-section")).toContainText(
    "python_code node 'check_reference' timed out after 2.0s",
  );
  await expect(page.getByTestId("run-debugger-section")).toContainText(
    "2 recorded step",
  );

  // ---- 5. The upstream failure surfaces two nodes later, in `decide` ---------------------
  await runTicket(page, "F32", "failed");
  await expect(page.getByTestId("run-recovery-section")).toContainText(
    "python_code node 'decide' failed: KeyError: 'tier'",
  );
  await expect(page.getByTestId("run-debugger-section")).toContainText(
    "4 recorded step",
  );
  await page
    .getByTestId("run-debugger-section")
    .getByRole("button", { name: /lookup/ })
    .first()
    .click();
  await expect(page.getByTestId("run-debugger-section")).toContainText("503");

  // ---- 6. Retry: the same code fails the same way ----------------------------------------
  await page.getByTestId("run-retry").click();
  await expect(page.getByTestId("run-lineage-section")).toContainText(
    "Attempt 2 of 2",
    {
      timeout: 60_000,
    },
  );
  await expectRunStatus(page, "failed", { timeout: 120_000 });
  await expect(page.getByTestId("run-recovery-section")).toContainText(
    "python_code node 'decide' failed: KeyError: 'tier'",
  );

  // ---- 7. CALIBER Patches: nothing is proposed from the UI -------------------------------
  await page.goto(`${workflowUrl}?tab=patches`);
  await expect(page.getByText("CALIBER Patches").first()).toBeVisible();
  await expect(page.getByTestId("patches-list")).toContainText(
    "No CALIBER patches yet",
  );

  // ---- 8. Patch: restore version 1 as a draft, replace three node bodies, publish -------
  await page.goto(`${workflowUrl}?tab=versions`);
  await page.getByRole("button", { name: "Restore as draft" }).first().click();
  await expect(page).toHaveURL(/\/editor\/WFV-/);
  for (const node of ["validate", "check_reference", "decide"]) {
    await page.getByTestId(`rf__node-${node}`).click();
    await page
      .getByTestId("inspector-python-code")
      .fill(
        fs.readFileSync(path.join(ASSETS, `workflow/patch/${node}.py`), "utf8"),
      );
  }
  await page.getByTestId("editor-save").click();
  await expect(page.getByTestId("editor-save-status")).toContainText("Saved");
  await publishFromEditor(page);
  const editorV2 = page.url();
  expect(editorV2).not.toBe(editorV1);

  // ---- 9. The same three tickets, now on version 2 ---------------------------------------
  await runTicket(page, "F12", "completed");
  await expect(page.getByTestId("run-output")).toContainText(
    '"reason_code": "ELIGIBLE_AUTO_APPROVE"',
  );
  await runTicket(page, "F29", "completed");
  await expect(page.getByTestId("run-output")).toContainText(
    '"reason_code": "ORDER_REF_UNVERIFIED"',
  );
  await runTicket(page, "F32", "completed");
  await expect(page.getByTestId("run-output")).toContainText(
    '"reason_code": "ACCOUNT_LOOKUP_FAILED"',
  );
  await expect(page.getByTestId("run-output")).toContainText(
    '"upstream_status": 503',
  );
  await runTicket(page, "F18", "completed");
  await expect(page.getByTestId("run-output")).toContainText(
    '"decision": "rejected_input"',
  );

  // ---- 10. Compare the versions -----------------------------------------------------------
  await page.goto(`${workflowUrl}?tab=versions`);
  await page
    .getByTestId("version-diff-base")
    .selectOption({ label: "v1 · published" });
  await page
    .getByTestId("version-diff-other")
    .selectOption({ label: "v2 · published" });
  await expect(page.getByTestId("version-diff-panel")).toContainText(
    "validate",
  );

  // ---- 11. The live alias refuses a version with no deploy gate ---------------------------
  await page.getByRole("button", { name: "Promote" }).first().click();
  await page.getByRole("button", { name: "Promote" }).last().click();
  await expect(page.getByText(/requires a deploy gate/)).toBeVisible();
});
