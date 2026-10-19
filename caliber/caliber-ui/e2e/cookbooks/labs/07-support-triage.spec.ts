import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  expect,
  test,
  type Browser,
  type BrowserContext,
  type Page,
} from "@playwright/test";

import { expectRunStatus } from "../../helpers";
import { CaliberUi, uiOnlySlug } from "../ui-only-driver";

/**
 * Lab 07, "Run it through the UI": a support lead imports the triage workflow, validates it,
 * sees Preview refuse it, publishes it, runs tickets through the Run Monitor, and has a second
 * person answer the approval gate. The numbered steps in the lab README are the `test.step`s.
 *
 * Needs a server started as the README shows: CALIBER_FEATURE_COOKBOOKS=true, and
 *   CALIBER_OPERATOR_USERS=admin,lena,sam   CALIBER_APPROVER_USERS=admin,lena
 * Re-running is safe: accounts that already exist are left alone and every run makes a uniquely
 * named workspace and workflow. Nothing here calls the API directly.
 */

// Same variable the SDK script reads: on a stack shared with other labs `sam` may already exist
// (lab 18's roster has one) and the accounts keep that lab's password.
const PASSWORD =
  process.env["COOKBOOK_07_PASSWORD"] ?? "cookbook07-lab-passphrase";
const HERE = dirname(fileURLToPath(import.meta.url));
const ASSETS = resolve(
  HERE,
  "../../../../../docs-site/cookbooks/07-support-triage-resolution-loop/assets",
);
const MANIFEST = resolve(ASSETS, "workflows/support-triage.workflow.json");
const TICKETS = readFileSync(
  resolve(ASSETS, "dataset/ticket-cases.jsonl"),
  "utf8",
)
  .split("\n")
  .filter(Boolean)
  .map(
    (line) =>
      JSON.parse(line) as { id: string; input: Record<string, unknown> },
  );

function ticket(id: string): string {
  const row = TICKETS.find((item) => item.id === id);
  if (!row) throw new Error(`no ticket ${id} in ticket-cases.jsonl`);
  return JSON.stringify(row.input);
}

async function signedInAs(
  browser: Browser,
  baseURL: string,
  user: string,
  password = PASSWORD,
): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();
  await new CaliberUi(page).signIn(user, password);
  return { context, page };
}

async function selectWorkspace(page: Page, name: string): Promise<void> {
  const selector = page.getByLabel("Active workspace");
  await expect(selector.locator("option", { hasText: name })).toHaveCount(1, {
    timeout: 30_000,
  });
  await selector.selectOption({ label: name });
  await expect(selector.locator("option:checked")).toHaveText(name);
}

/** On a workflow's Runs tab, select the newest run that is awaiting approval (rows are newest first). */
async function openWaitingRun(page: Page): Promise<void> {
  const row = page
    .getByRole("row")
    .filter({ hasText: "Awaiting approval" })
    .first();
  await expect(row).toBeVisible({ timeout: 60_000 });
  await row.locator('[data-testid^="run-WR-"]').click();
  await expect(page.getByTestId("run-approve")).toBeVisible({
    timeout: 30_000,
  });
}

/** Type a ticket into the editor's Run Monitor and start a run. */
async function runTicket(page: Page, input: string): Promise<void> {
  await page.getByTestId("run-input").fill(input);
  const queued = page.waitForResponse(
    (response) =>
      response.url().includes("/workflow-runs") &&
      response.request().method() === "POST" &&
      response.status() === 202,
    { timeout: 90_000 },
  );
  await page.getByTestId("run-execute").click();
  await queued;
}

test.describe("Lab 07: support triage through the UI", () => {
  test("import, validate, publish, run tickets, answer the approval gate", async ({
    page: adminPage,
    browser,
  }, testInfo) => {
    test.setTimeout(480_000);
    const baseURL = String(testInfo.project.use.baseURL);
    const suffix = uiOnlySlug("lab07").replace("lab07-", "");
    const workspace = `lab07-ui-${suffix}`;
    const workflowName = `lab07-ui-${suffix}-triage`;
    const contexts: BrowserContext[] = [];
    let workflowUrl = "";
    let workflowId = "";

    try {
      // ---- 1. Administration: the two accounts, a workspace, the two members ---------------
      await test.step("1. Administration: accounts lena and sam, a workspace, both as Reviewers", async () => {
        await new CaliberUi(adminPage).signIn("admin", "admin");
        await adminPage.goto("/caliber/administration");
        const accounts = adminPage.locator("#accounts-heading").locator("..");
        // The accounts table loads after the page: wait for the admin row before looking for others.
        await expect(
          accounts.getByRole("cell", { name: "admin", exact: true }),
        ).toBeVisible();
        for (const user of ["lena", "sam"]) {
          const row = accounts.getByRole("row").filter({
            has: adminPage.getByRole("cell", { name: user, exact: true }),
          });
          if ((await row.count()) > 0) {
            if ((await row.getByText("Disabled").count()) > 0) {
              await row.getByRole("button", { name: "Enable" }).click();
              await expect(row.getByText("Active")).toBeVisible();
            }
            continue;
          }
          await accounts.getByLabel("User ID").fill(user);
          await accounts.getByLabel("Password", { exact: true }).fill(PASSWORD);
          await accounts
            .getByRole("button", { name: "Create account" })
            .click();
          await expect(adminPage.getByText(`Created ${user}.`)).toBeVisible();
        }
        await adminPage
          .getByRole("button", { name: "Create workspace" })
          .click();
        await adminPage.getByLabel("Workspace name").fill(workspace);
        await adminPage
          .getByRole("button", { name: "Create and select" })
          .click();
        const selector = adminPage.getByLabel("Active workspace");
        await expect(selector.locator("option:checked")).toHaveText(workspace);
        const projectId = await selector.inputValue();
        await adminPage.goto("/caliber/administration");
        const section = adminPage
          .locator("#project-access-heading")
          .locator("..");
        await section
          .getByLabel("Project for access management")
          .selectOption(projectId);
        for (const user of ["lena", "sam"]) {
          const form = section.locator("form");
          await form.getByRole("textbox").fill(user);
          await form.getByRole("combobox").selectOption({ label: "Reviewer" });
          await form.getByRole("button", { name: "Add member" }).click();
          await expect(
            section.getByText(`Added ${user} to ${workspace}.`),
          ).toBeVisible();
        }
      });

      // ---- 2. Workflows > Import: upload the manifest, preflight, import --------------------
      await test.step("2. Workflows > Import: choose support-triage.workflow.json", async () => {
        await adminPage.goto("/caliber/workflows");
        await adminPage.getByTestId("import-workflow").click();
        await adminPage.getByTestId("workflow-import-name").fill(workflowName);
        await adminPage.locator('input[type="file"]').setInputFiles(MANIFEST);
        await adminPage
          .getByRole("button", { name: "Validate dependencies" })
          .click();
        await expect(adminPage.getByText("Preflight passed")).toBeVisible();
        await adminPage.getByRole("button", { name: "Import as new" }).click();
        await expect(adminPage).toHaveURL(
          /\/caliber\/workflows\/WF-[^/]+\/editor\/WFV-[^/?]+$/,
        );
        workflowUrl = adminPage.url();
        workflowId = /workflows\/(WF-[^/]+)\//.exec(workflowUrl)?.[1] ?? "";
        await expect(adminPage.getByTestId("workflow-editor")).toBeVisible();
        for (const node of [
          "classify",
          "triage_router",
          "draft_reply",
          "review",
          "action_request",
        ]) {
          await expect(adminPage.getByTestId(`outline-${node}`)).toBeVisible();
        }
      });

      // ---- 3. Validate; Preview refuses a Python Code node ----------------------------------
      await test.step("3. Editor: Validate says Valid; Preview is refused", async () => {
        await adminPage.getByTestId("editor-validate").click();
        await expect(adminPage.getByTestId("editor-message")).toContainText(
          "Valid.",
        );
        await adminPage.getByTestId("editor-preview").click();
        await adminPage
          .getByTestId("preview-input")
          .fill("How do I enable SSO for my workspace?");
        await adminPage.getByTestId("preview-run").click();
        await expect(adminPage.getByTestId("preview-result")).toContainText(
          "error",
          {
            timeout: 60_000,
          },
        );
        // The only step is the refused node; its detail names why.
        await adminPage.getByTestId("preview-step-classify").click();
        await expect(adminPage.getByTestId("preview-result")).toContainText(
          "cannot isolate",
        );
        await adminPage.getByTestId("preview-close").click();
      });

      // ---- 4. Publish ------------------------------------------------------------------------
      await test.step("4. Editor > Publish: validate, review, publish", async () => {
        await adminPage.getByTestId("editor-publish").click();
        await adminPage.getByTestId("publish-run-validate").click();
        await adminPage.getByTestId("publish-next-1").click();
        await adminPage.getByTestId("publish-next-2").click();
        await adminPage.getByTestId("publish-confirm").click();
        await expect(adminPage.getByTestId("editor-message")).toContainText(
          "Published.",
        );
      });

      // ---- 5. Run a how-to ticket and a bug ticket ------------------------------------------
      await test.step("5. Run Monitor: TK01 (how-to) completes, TK29 (bug) waits for approval", async () => {
        await adminPage.getByTestId("editor-run-monitor").click();
        await expect(adminPage.getByTestId("run-monitor-panel")).toBeVisible();
        await runTicket(adminPage, ticket("TK01"));
        await expectRunStatus(adminPage, "completed");
        await expect(adminPage.getByTestId("run-output")).toContainText(
          "processed",
        );
        await runTicket(adminPage, ticket("TK29"));
        await expectRunStatus(adminPage, "waiting_approval");
        await expect(
          adminPage.getByTestId("run-approval-actions"),
        ).toBeVisible();
      });

      // ---- 6. The person who ran it cannot approve it -----------------------------------------
      await test.step("6. Approve as the same account: refused", async () => {
        await adminPage.getByTestId("run-approve").click();
        await expect(
          adminPage.getByText(/a second person must decide/),
        ).toBeVisible({
          timeout: 30_000,
        });
      });

      // ---- 7. Lena approves and resumes ---------------------------------------------------------
      const lena = await signedInAs(browser, baseURL, "lena");
      contexts.push(lena.context);
      await test.step("7. lena: Workflows > triage > Runs: Approve, then Resume", async () => {
        await selectWorkspace(lena.page, workspace);
        await lena.page.goto(`/caliber/workflows/${workflowId}?tab=runs`);
        await openWaitingRun(lena.page);
        await expect(lena.page.getByTestId("run-approve")).toBeVisible();
        await lena.page.getByTestId("run-approve").click();
        await expect(lena.page.getByTestId("run-resume")).toBeEnabled({
          timeout: 30_000,
        });
        await lena.page.getByTestId("run-resume").dispatchEvent("click");
        await expect(lena.page.getByText("Completed").first()).toBeVisible({
          timeout: 90_000,
        });
      });

      // ---- 8. A rejection ends the run without releasing the action ---------------------------
      await test.step("8. Run TK40 (billing refund) and reject it as lena: the run ends failed", async () => {
        await adminPage.getByTestId("run-input").fill(ticket("TK40"));
        const queued = adminPage.waitForResponse(
          (response) =>
            response.url().includes("/workflow-runs") &&
            response.request().method() === "POST" &&
            response.status() === 202,
          { timeout: 90_000 },
        );
        await adminPage.getByTestId("run-execute").click();
        await queued;
        await expectRunStatus(adminPage, "waiting_approval");
        await lena.page.goto(`/caliber/workflows/${workflowId}?tab=runs`);
        await openWaitingRun(lena.page);
        await lena.page.getByTestId("run-reject").click();
        await expect(lena.page.getByText("Failed").first()).toBeVisible({
          timeout: 60_000,
        });
        await expectRunStatus(adminPage, "failed", { timeout: 90_000 });
      });
    } finally {
      for (const entry of contexts) await entry.close();
    }
  });
});
