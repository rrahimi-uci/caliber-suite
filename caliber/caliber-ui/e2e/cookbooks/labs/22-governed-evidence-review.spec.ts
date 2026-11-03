import { expect, test, type Browser, type Page } from "@playwright/test";

import { expectRunStatus } from "../../helpers";
import { CaliberUi, uiOnlySlug } from "../ui-only-driver";

/**
 * Cookbook 22, "Run it through the UI": the governed evidence-review journey.
 *
 * The API cookbook path runs first on the same reference stack and installs the deterministic
 * Harborlight tool binding. This browser path uses the normal operator UI to install, publish and
 * run the same catalog recipe, then uses a second browser identity for the exact approval.
 *
 * Required journey, from the Lab 22 README and issue #869:
 *   1. editor submits the Harborlight evidence-review run and sees the exact proposal diff;
 *   2. the editor cannot approve it; a distinct approver approves the proposal;
 *   3. the operator resumes the run and it reaches `accepted`;
 *   4. evidence export downloads the scope-bound archive;
 *   5. replay of that captured evidence reports `matches_original` without a second mutation.
 *
 * The API example owns the Harborlight downstream assertions and project-isolation matrix. This
 * browser test owns the installed-artifact/public-UI journey and the evidence/replay controls;
 * it does not create a second downstream route or bypass a browser action with raw API calls.
 */
const LAB22_ID = "22";
const LAB22_TITLE = "Governed Evidence Review";
const PROJECT_ID = "PRJ-cookbook-22";
const WORKSPACE = "cookbook-22-governed-review";
const ADMIN_PASSWORD = process.env.CALIBER_LAB22_ADMIN_PASSWORD ?? "admin";
const APPROVER = {
  user: "lab22-approver",
  password: "cookbook22-approver-passphrase",
};

async function signedInAs(
  browser: Browser,
  baseURL: string,
  workspace: string,
): Promise<Page> {
  const context = await browser.newContext({ baseURL });
  const page = await context.newPage();
  await new CaliberUi(page).signIn(APPROVER.user, APPROVER.password);
  await page.getByLabel("Active workspace").selectOption({ label: workspace });
  return page;
}

async function ensureApproverAccount(page: Page): Promise<void> {
  await page.goto("/caliber/administration");
  const accounts = page.getByLabel("Accounts");
  const existing = accounts.getByRole("cell", {
    name: APPROVER.user,
    exact: true,
  });
  if ((await existing.count()) === 0) {
    await page.getByLabel("User ID").first().fill(APPROVER.user);
    await page.getByLabel("Password").first().fill(APPROVER.password);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(existing).toBeVisible();
  }
}

test.describe(`Cookbook ${LAB22_ID}: governed evidence review`, () => {
  test("editor submits, approver reviews and approves, then evidence replays as matching", async ({
    page: editorPage,
    browser,
  }, testInfo) => {
    test.setTimeout(900_000);
    const workflowName = `cookbook-22-${uiOnlySlug("harborlight")}`;
    const ui = new CaliberUi(editorPage);

    await ui.signIn("admin", ADMIN_PASSWORD);
    await ensureApproverAccount(editorPage);

    await expect(editorPage.getByLabel("Active workspace")).toContainText(
      WORKSPACE,
    );
    await expect(editorPage.getByLabel("Active workspace")).toHaveValue(
      PROJECT_ID,
    );

    await editorPage.goto("/caliber/cookbooks");
    await expect(
      editorPage.getByRole("heading", { name: "Cookbooks" }),
    ).toBeVisible();
    const card = editorPage.getByTestId(`cookbook-card-${LAB22_ID}`);
    await expect(card).toBeVisible();
    await expect(card).toContainText(LAB22_TITLE);
    await editorPage.getByTestId(`install-cookbook-${LAB22_ID}`).click();
    await editorPage.getByTestId("cookbook-install-name").fill(workflowName);
    const acknowledgement = editorPage.getByTestId(
      "cookbook-prerequisites-ack",
    );
    if (await acknowledgement.isVisible()) await acknowledgement.check();
    await editorPage.getByTestId("confirm-cookbook-install").click();

    await expect(editorPage).toHaveURL(
      /\/caliber\/workflows\/WF-[^/]+\/editor\/WFV-/,
    );
    const editorUrl = editorPage.url();

    await editorPage.getByTestId("editor-validate").click();
    await expect(editorPage.getByTestId("editor-message")).toContainText(
      "Valid.",
    );
    await editorPage.getByTestId("editor-publish").click();
    await editorPage.getByTestId("publish-run-validate").click();
    await editorPage.getByTestId("publish-next-1").click();
    await editorPage.getByTestId("publish-next-2").click();
    await editorPage.getByTestId("publish-confirm").click();

    await editorPage.getByTestId("editor-run-monitor").click();
    await editorPage
      .getByTestId("run-input")
      .fill(`request pay_statement for MORT-2026-000184 at version 1`);
    await editorPage.getByTestId("run-execute").click();
    const runLink = editorPage.getByTestId("run-active-open-link");
    await expect(runLink).toBeVisible({ timeout: 120_000 });
    const runHref = (await runLink.getAttribute("href")) ?? "";
    const runId = runHref.split("/").pop() ?? "";
    expect(runId).toMatch(/^WR-/);
    await expectRunStatus(editorPage, "waiting_approval", { timeout: 180_000 });

    // The initiator may see the proposal but cannot authorize their own protected effect.
    await expect(
      editorPage.getByTestId("run-bound-approval-note"),
    ).toBeVisible();
    await expect(editorPage.getByTestId("run-approve")).toHaveCount(0);

    const approverPage = await signedInAs(
      browser,
      String(testInfo.project.use.baseURL),
      WORKSPACE,
    );
    try {
      await approverPage.goto(editorUrl);
      await approverPage.getByTestId("editor-run-monitor").click();
      await approverPage.getByTestId(`run-history-select-${runId}`).click();
      const approvalPanel = approverPage.getByTestId("approval-decision-panel");
      await expect(approvalPanel).toBeVisible({ timeout: 120_000 });
      await expect(approvalPanel).toContainText(
        "cases/MORT-2026-000184/document-requests",
      );
      await approvalPanel.getByTestId("approval-approve").click();
      await expect(approvalPanel).toContainText(/approved|recorded/i);
    } finally {
      await approverPage.context().close();
    }

    await expect(editorPage.getByTestId("run-resume")).toBeEnabled({
      timeout: 120_000,
    });
    await editorPage.getByTestId("run-resume").click();
    await expectRunStatus(editorPage, "completed", { timeout: 180_000 });

    await editorPage.goto(runHref);
    await expect(
      editorPage.getByTestId("selected-run-acceptance"),
    ).toContainText("Acceptance: accepted", { timeout: 120_000 });

    const evidencePanel = editorPage.getByTestId("workflow-run-evidence-panel");
    await expect(evidencePanel).toBeVisible();
    const download = editorPage.waitForEvent("download");
    await evidencePanel.getByTestId("workflow-run-evidence-download").click();
    const archive = await download;
    expect(archive.suggestedFilename()).toBe(`caliber-evidence-${runId}.zip`);
    await expect(
      evidencePanel.getByTestId("workflow-run-evidence-status"),
    ).toContainText("Evidence archive downloaded.");

    await evidencePanel.getByTestId("workflow-run-historical-replay").click();
    await expect(
      editorPage.getByTestId("workflow-run-replay-result"),
    ).toContainText("matches_original: true", { timeout: 120_000 });
  });
});
