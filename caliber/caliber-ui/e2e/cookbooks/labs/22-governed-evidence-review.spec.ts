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
 *   5. replay of that captured evidence reports `matches_original` without a second mutation;
 *   6. an injected unknown effect is recovered through the public reconciliation API.
 *
 * The API example owns the Harborlight downstream assertions and project-isolation matrix. This
 * browser test owns the installed-artifact/public-UI journey and the evidence/replay controls;
 * the recovery test uses the public operator API only for the reconciliation action that has no
 * browser control, and does not bypass browser actions for installation, approval or execution.
 */
const LAB22_ID = "22";
const LAB22_TITLE = "Governed Evidence Review";
const PROJECT_ID = "PRJ-cookbook-22";
const WORKSPACE = "cookbook-22-governed-review";
const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const HARBORLIGHT_URL = "http://127.0.0.1:8810";
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

async function resetHarborlight(page: Page): Promise<void> {
  const response = await page.request.post(
    `${HARBORLIGHT_URL}/_test/reset?preserve_request_ids=true`,
  );
  expect(response.ok()).toBeTruthy();
}

async function installPublishedWorkflow(
  page: Page,
  workflowName: string,
): Promise<{ editorUrl: string; workflowId: string }> {
  await page.getByLabel("Active workspace").selectOption(PROJECT_ID);
  await expect(page.getByLabel("Active workspace")).toContainText(WORKSPACE);
  await expect(page.getByLabel("Active workspace")).toHaveValue(PROJECT_ID);

  await page.goto("/caliber/cookbooks");
  await expect(page.getByRole("heading", { name: "Cookbooks" })).toBeVisible();
  const card = page.getByTestId(`cookbook-card-${LAB22_ID}`);
  await expect(card).toBeVisible();
  await expect(card).toContainText(LAB22_TITLE);
  await page.getByTestId(`install-cookbook-${LAB22_ID}`).click();
  await page.getByTestId("cookbook-install-name").fill(workflowName);
  const acknowledgement = page.getByTestId("cookbook-prerequisites-ack");
  if (await acknowledgement.isVisible()) await acknowledgement.check();
  await page.getByTestId("confirm-cookbook-install").click();

  await expect(page).toHaveURL(/\/caliber\/workflows\/WF-[^/]+\/editor\/WFV-/);
  const editorUrl = page.url();
  await page.getByTestId("editor-validate").click();
  await expect(page.getByTestId("editor-message")).toContainText("Valid.");
  await page.getByTestId("editor-publish").click();
  await page.getByTestId("publish-run-validate").click();
  await page.getByTestId("publish-next-1").click();
  await page.getByTestId("publish-next-2").click();
  await page.getByTestId("publish-confirm").click();

  const workflowId = editorUrl.match(/\/workflows\/(WF-[^/]+)/)?.[1];
  expect(workflowId).toMatch(/^WF-/);
  await page.goto(`/caliber/workflows/${workflowId}`);
  await expect(page.getByTestId("workflow-detail")).toBeVisible();
  await page.getByTestId("workflow-pause-toggle").click();
  await expect(page.getByTestId("workflow-pause-toggle")).toHaveText("Pause");
  await page.goto(editorUrl);
  return { editorUrl, workflowId: workflowId as string };
}

async function submitRun(
  page: Page,
): Promise<{ runHref: string; runId: string }> {
  await page.getByTestId("editor-run-monitor").click();
  await page
    .getByTestId("run-input")
    .fill("request pay_statement for MORT-2026-000184 at version 1");
  await page.getByTestId("run-execute").click();
  const runLink = page.getByTestId("run-active-open-link");
  await expect(runLink).toBeVisible({ timeout: 120_000 });
  const runHref = (await runLink.getAttribute("href")) ?? "";
  const runId = runHref.split("/").pop() ?? "";
  expect(runId).toMatch(/^WR-/);
  await expectRunStatus(page, "waiting_approval", { timeout: 180_000 });
  return { runHref, runId };
}

async function expectPublicRunStatus(
  page: Page,
  runId: string,
  status: string,
): Promise<void> {
  await expect
    .poll(
      async () => {
        const response = await page.request.get(
          `${API_BASE}/workflow-runs/${encodeURIComponent(runId)}`,
        );
        expect(response.ok()).toBeTruthy();
        const body = (await response.json()) as {
          data?: { status?: string };
        };
        return body.data?.status;
      },
      { timeout: 180_000 },
    )
    .toBe(status);
}

async function approveRun(
  editorPage: Page,
  browser: Browser,
  baseURL: string,
  editorUrl: string,
  runId: string,
): Promise<void> {
  await expect(editorPage.getByTestId("run-bound-approval-note")).toBeVisible();
  await expect(editorPage.getByTestId("run-approve")).toHaveCount(0);
  const approverPage = await signedInAs(browser, baseURL, WORKSPACE);
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
}

test.describe(`Cookbook ${LAB22_ID}: governed evidence review`, () => {
  test("editor submits, approver reviews and approves, then evidence replays as matching", async ({
    page: editorPage,
    browser,
  }, testInfo) => {
    test.setTimeout(900_000);
    await resetHarborlight(editorPage);
    const workflowName = `cookbook-22-${uiOnlySlug("harborlight")}`;
    const ui = new CaliberUi(editorPage);

    await ui.signIn("admin", ADMIN_PASSWORD);
    await ensureApproverAccount(editorPage);
    const { editorUrl } = await installPublishedWorkflow(
      editorPage,
      workflowName,
    );
    const { runHref, runId } = await submitRun(editorPage);
    await approveRun(
      editorPage,
      browser,
      String(testInfo.project.use.baseURL),
      editorUrl,
      runId,
    );

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

  test("recovers an unknown effect through the public reconciliation API", async ({
    page: editorPage,
    browser,
  }, testInfo) => {
    test.setTimeout(900_000);
    await resetHarborlight(editorPage);
    const ui = new CaliberUi(editorPage);
    await ui.signIn("admin", ADMIN_PASSWORD);
    await ensureApproverAccount(editorPage);
    const { editorUrl } = await installPublishedWorkflow(
      editorPage,
      `cookbook-22-${uiOnlySlug("unknown")}`,
    );
    const { runHref, runId } = await submitRun(editorPage);
    await approveRun(
      editorPage,
      browser,
      String(testInfo.project.use.baseURL),
      editorUrl,
      runId,
    );

    const armed = await editorPage.request.post(
      `${HARBORLIGHT_URL}/_test/faults`,
      {
        data: { fault: "drop_response_after_commit" },
      },
    );
    expect(armed.ok()).toBeTruthy();
    await expect(editorPage.getByTestId("run-resume")).toBeEnabled({
      timeout: 120_000,
    });
    await editorPage.getByTestId("run-resume").click();
    await expectPublicRunStatus(editorPage, runId, "waiting_effect");

    const listed = await editorPage.request.get(
      `${API_BASE}/system/effects?source=proposal&proposal_state=unknown&workflow_run_id=${encodeURIComponent(runId)}`,
    );
    expect(listed.ok()).toBeTruthy();
    const listedBody = (await listed.json()) as {
      data?: { proposals?: Array<{ proposal_id?: string }> };
    };
    const proposals = listedBody.data?.proposals ?? [];
    expect(proposals).toHaveLength(1);
    const proposalId = proposals[0]?.proposal_id;
    expect(proposalId).toMatch(/^prop-/);

    const csrf = await editorPage.request.get(`${API_BASE}/csrf`);
    expect(csrf.ok()).toBeTruthy();
    const csrfBody = (await csrf.json()) as {
      data?: { enabled?: boolean; token?: string | null };
    };
    expect(csrfBody.data?.enabled).toBe(true);
    expect(csrfBody.data?.token).toEqual(expect.any(String));
    const reconciled = await editorPage.request.post(
      `${API_BASE}/system/effects/proposals/${encodeURIComponent(proposalId as string)}/reconcile`,
      {
        data: { reason: "Beta journey verifies unknown-effect recovery" },
        headers: { "X-CALIBER-CSRF": csrfBody.data?.token as string },
      },
    );
    expect(reconciled.ok(), await reconciled.text()).toBeTruthy();
    const reconciledBody = (await reconciled.json()) as {
      data?: { outcome?: string; state?: string; settled?: boolean };
    };
    expect(reconciledBody.data).toMatchObject({
      outcome: "committed",
      state: "committed",
      settled: true,
    });
    await expectPublicRunStatus(editorPage, runId, "completed");

    await editorPage.goto(runHref);
    await expect(
      editorPage.getByTestId("selected-run-acceptance"),
    ).toContainText("Acceptance: accepted", { timeout: 120_000 });
    await expect
      .poll(async () => {
        const mutations = await editorPage.request.get(
          `${HARBORLIGHT_URL}/_test/mutations`,
        );
        expect(mutations.ok()).toBeTruthy();
        return ((await mutations.json()) as { count: number }).count;
      })
      .toBe(1);
  });
});
