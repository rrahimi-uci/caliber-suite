import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
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
 * named workspace and workflow. Nothing here calls the API directly, but one read-only
 * `GET /readiness` that picks the mode: the spec runs on a simulated stack (`up lab07 --flags
 * cookbooks`) and on a real-provider one (`up lab07h --flags cookbooks --real-llm`).
 *
 * Mode differences: in step 5 the how-to reply is the simulated echo ("processed") or real text; and
 * on a real stack three more steps run (the README's "Run it with a real provider"):
 *   9.  Test Sets: four labelled tickets.
 *   10. Workflows > Import: the optional model-based variant (a model classifies, a guard decides),
 *       and Preview of a how-to and a bug ticket.
 *   11. Evaluations: the deterministic workflow cannot be scored (Preview refuses its Python Code
 *       node: the run ends failed, 0 of 4), the variant can; a range is asserted, never a score.
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
      JSON.parse(line) as {
        id: string;
        tags: string[];
        input: Record<string, unknown>;
        expected: { decision: string } & Record<string, unknown>;
      },
  );

/** `GET /readiness`: are the language model or the evaluator simulated on this stack? */
async function isSimulated(page: Page): Promise<boolean> {
  // The base URL ends in /caliber; the API lives beside it at the server root.
  const root = new URL(
    "/",
    String(process.env["CALIBER_LAB_BASE_URL"]),
  ).toString();
  const response = await page.request.get(
    `${root}ajax-api/2.0/mlflow/caliber/readiness`,
  );
  expect(response.ok()).toBeTruthy();
  const body = (await response.json()) as { data?: { simulated?: string[] } };
  const simulated = body.data?.simulated ?? [];
  return simulated.includes("llm") || simulated.includes("eval");
}

/** The tickets the real-provider steps label, and the label the model variant prints for each. */
const EVAL_TICKETS = ["TK01", "TK10", "TK29", "TK40"];
const LABEL: Record<string, string> = {
  reply: "REPLY",
  clarify: "CLARIFY",
  escalate_support: "ESCALATE_SUPPORT",
  escalate_bug: "ESCALATE_BUG",
};

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
  // a missing control must fail the step in seconds, not wait out the whole-test timeout
  test.use({ actionTimeout: 30_000 });
  test("import, validate, publish, run tickets, answer the approval gate", async ({
    page: adminPage,
    browser,
  }, testInfo) => {
    test.setTimeout(1_500_000);
    const simulated = await isSimulated(adminPage);
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
        await expectRunStatus(adminPage, "completed", { timeout: 120_000 });
        if (simulated) {
          await expect(adminPage.getByTestId("run-output")).toContainText(
            "processed",
          );
        } else {
          // a real model wrote the reply: text, not the simulated echo
          await expect(adminPage.getByTestId("run-output")).toHaveText(/\S/);
          await expect(adminPage.getByTestId("run-output")).not.toContainText(
            "[support-reply]",
          );
        }
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

      if (!simulated) {
        const datasetName = `lab07-ui-${suffix}-ticket-eval`;
        let variantVersionId = "";
        let deterministicVersionId = "";
        await test.step("9. Test Sets: four labelled tickets (answer = the label the variant prints)", async () => {
          deterministicVersionId =
            /editor\/(WFV-[^/?]+)/.exec(workflowUrl)?.[1] ?? "";
          expect(deterministicVersionId).toMatch(/^WFV-/);
          await adminPage.goto("/caliber/eval-datasets");
          await adminPage
            .getByRole("button", { name: "+ New Test Set" })
            .click();
          await adminPage.getByPlaceholder("factual-checks").fill(datasetName);
          await adminPage.getByPlaceholder("@sarah").fill("admin");
          await adminPage
            .getByRole("button", { name: "Create", exact: true })
            .click();
          await adminPage
            .getByText(datasetName, { exact: true })
            .first()
            .click();
          await expect(
            adminPage.getByTestId("eval-dataset-detail"),
          ).toBeVisible();
          for (const id of EVAL_TICKETS) {
            const row = TICKETS.find((item) => item.id === id)!;
            await adminPage.getByTestId("example-add-toggle").click();
            await adminPage
              .getByTestId("example-input")
              .fill(JSON.stringify(row.input));
            await adminPage.getByTestId("example-expected").fill(
              JSON.stringify({
                decision: row.expected.decision,
                answer: LABEL[row.expected.decision],
              }),
            );
            await adminPage
              .getByTestId("example-tags")
              .fill(row.tags.join(", "));
            await adminPage.getByTestId("example-save").click();
          }
          await expect(adminPage.getByTestId("example-row")).toHaveCount(
            EVAL_TICKETS.length,
          );
        });

        await test.step("10. Workflows > Import the model variant; Preview a how-to and a bug ticket", async () => {
          // The variant names its gate's test set; point it at the one created above.
          const variant = JSON.parse(
            readFileSync(
              resolve(ASSETS, "workflows/support-triage-model.workflow.json"),
              "utf8",
            ),
          );
          variant.artifacts.eval_datasets["ticket-eval"].dataset_name =
            datasetName;
          const variantFile = join(
            mkdtempSync(join(tmpdir(), "lab07-")),
            "support-triage-model.workflow.json",
          );
          writeFileSync(variantFile, JSON.stringify(variant, null, 2));
          await adminPage.goto("/caliber/workflows");
          await adminPage.getByTestId("import-workflow").click();
          await adminPage
            .getByTestId("workflow-import-name")
            .fill(`${workflowName}-model`);
          await adminPage
            .locator('input[type="file"]')
            .setInputFiles(variantFile);
          await adminPage
            .getByRole("button", { name: "Validate dependencies" })
            .click();
          await expect(adminPage.getByText("Preflight passed")).toBeVisible();
          await adminPage
            .getByRole("button", { name: "Import as new" })
            .click();
          await expect(adminPage.getByTestId("workflow-editor")).toBeVisible();
          variantVersionId =
            /editor\/(WFV-[^/?]+)/.exec(adminPage.url())?.[1] ?? "";
          for (const node of ["classify_model", "guard", "review_bug"]) {
            await expect(
              adminPage.getByTestId(`outline-${node}`),
            ).toBeVisible();
          }
          await adminPage.getByTestId("editor-validate").click();
          await expect(adminPage.getByTestId("editor-message")).toContainText(
            "Valid.",
          );
          // Unlike the deterministic workflow, Preview runs it (no Python Code node).
          for (const [id, label] of [
            ["TK01", "REPLY"],
            ["TK29", "ESCALATE_BUG"],
          ]) {
            await adminPage.getByTestId("editor-preview").click();
            await adminPage.getByTestId("preview-input").fill(ticket(id));
            await adminPage.getByTestId("preview-run").click();
            await expect(adminPage.getByTestId("preview-result")).toContainText(
              "completed",
              { timeout: 120_000 },
            );
            await expect(adminPage.getByTestId("preview-result")).toContainText(
              label,
            );
            await adminPage.getByTestId("preview-close").click();
          }
        });

        await test.step("11. Evaluations: the deterministic workflow fails (0 of 4), the model variant scores in range", async () => {
          async function evaluate(
            label: string,
            versionId: string,
            status: "completed" | "failed",
          ) {
            await adminPage.goto("/caliber/evaluations");
            await adminPage
              .getByRole("button", { name: "Run evaluation" })
              .click();
            await adminPage
              .getByLabel("Test set", { exact: true })
              .selectOption({
                label: `${datasetName} (v${EVAL_TICKETS.length + 1})`,
              });
            await adminPage.getByLabel("Label", { exact: true }).fill(label);
            await adminPage
              .getByLabel("What to score")
              .selectOption("workflow");
            await adminPage.getByLabel("Subject under test").fill(versionId);
            await adminPage.getByText("Exact match", { exact: true }).click();
            await adminPage.getByText("Token F1", { exact: true }).click();
            await adminPage
              .getByRole("button", { name: "Run", exact: true })
              .click();
            const row = adminPage
              .getByTestId("eval-run-row")
              .filter({ hasText: label });
            await expect(row).toBeVisible({ timeout: 600_000 });
            await expect(row).toContainText(status);
            return row;
          }
          const refused = await evaluate(
            `lab07-${suffix}-rules`,
            deterministicVersionId,
            "failed",
          );
          // Preview cannot run the Python Code node: every row errors, none passes, and the run
          // ends failed with that reason
          await expect(refused).toContainText(`0/${EVAL_TICKETS.length}`);
          const model = await evaluate(
            `lab07-${suffix}-model`,
            variantVersionId,
            "completed",
          );
          const text = await model.innerText();
          const passed = Number(
            new RegExp(`(\\d+)/${EVAL_TICKETS.length}`).exec(text)?.[1] ?? "-1",
          );
          // model-dependent: at least half of four labelled tickets are routed as labelled
          expect(passed).toBeGreaterThanOrEqual(EVAL_TICKETS.length / 2);
          expect(passed).toBeLessThanOrEqual(EVAL_TICKETS.length);
        });
      }
    } finally {
      for (const entry of contexts) await entry.close();
    }
  });
});
