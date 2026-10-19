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
 * Lab 08, "Run it through the UI": an on-call lead creates the test set, imports the incident
 * workflow (refused until the test set exists), validates and previews it, publishes it, runs
 * incidents through the Run Monitor, and has a second person approve the rollback.
 *
 * The alpha web app is in single-environment mode (no Deployments or Promotions tab), so aliases,
 * the deploy gate and rollback of an alias are API-only; the README says so.
 *
 * Needs a server started as the README shows (CALIBER_FEATURE_COOKBOOKS=true and
 * CALIBER_OPERATOR_USERS=admin,lena,sam  CALIBER_APPROVER_USERS=admin,lena).
 */

const PASSWORD = "cookbook08-lab-passphrase";
const HERE = dirname(fileURLToPath(import.meta.url));
const ASSETS = resolve(
  HERE,
  "../../../../../docs-site/cookbooks/08-incident-response-commander/assets",
);
const INCIDENTS = readFileSync(
  resolve(ASSETS, "dataset/incident-cases.jsonl"),
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
        expected: Record<string, unknown>;
      },
  );

function incident(id: string) {
  const row = INCIDENTS.find((item) => item.id === id);
  if (!row) throw new Error(`no incident ${id} in incident-cases.jsonl`);
  return row;
}

async function signedInAs(
  browser: Browser,
  baseURL: string,
  user: string,
): Promise<{ context: BrowserContext; page: Page }> {
  const context = await browser.newContext({
    baseURL,
    viewport: { width: 1440, height: 900 },
  });
  const page = await context.newPage();
  await new CaliberUi(page).signIn(user, PASSWORD);
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

async function runIncident(page: Page, id: string): Promise<void> {
  await page.getByTestId("run-input").fill(JSON.stringify(incident(id).input));
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

test.describe("Lab 08: incident response through the UI", () => {
  test("test set, import, validate, preview, publish, run, approve the rollback", async ({
    page: adminPage,
    browser,
  }, testInfo) => {
    test.setTimeout(480_000);
    const baseURL = String(testInfo.project.use.baseURL);
    const suffix = uiOnlySlug("lab08").replace("lab08-", "");
    const workspace = `lab08-ui-${suffix}`;
    const workflowName = `lab08-ui-${suffix}-commander`;
    const datasetName = `lab08-ui-${suffix}-incidents`;
    const contexts: BrowserContext[] = [];
    let workflowId = "";

    // The manifest names its gate's test set; point it at the one this run creates.
    const manifest = JSON.parse(
      readFileSync(
        resolve(ASSETS, "workflows/incident-commander.workflow.json"),
        "utf8",
      ),
    );
    manifest.artifacts.eval_datasets["incident-cases"].dataset_name =
      datasetName;
    const manifestFile = join(
      mkdtempSync(join(tmpdir(), "lab08-")),
      "incident-commander.workflow.json",
    );
    writeFileSync(manifestFile, JSON.stringify(manifest, null, 2));

    try {
      await test.step("1. Administration: accounts lena and sam, a workspace, both as Reviewers", async () => {
        await new CaliberUi(adminPage).signIn("admin", "admin");
        await adminPage.goto("/caliber/administration");
        const accounts = adminPage.locator("#accounts-heading").locator("..");
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

      await test.step("2. Workflows > Import before the test set exists: preflight needs attention", async () => {
        await adminPage.goto("/caliber/workflows");
        await adminPage.getByTestId("import-workflow").click();
        await adminPage.getByTestId("workflow-import-name").fill(workflowName);
        await adminPage
          .locator('input[type="file"]')
          .setInputFiles(manifestFile);
        await adminPage
          .getByRole("button", { name: "Validate dependencies" })
          .click();
        await expect(
          adminPage.getByText("Preflight needs attention"),
        ).toBeVisible();
        await expect(
          adminPage.getByTestId("workflow-import-preview"),
        ).toContainText("is not visible in this workspace");
        await adminPage.getByRole("button", { name: "Cancel" }).click();
      });

      await test.step("3. Test Sets: create the test set and add three incidents", async () => {
        await adminPage.goto("/caliber/eval-datasets");
        await adminPage.getByRole("button", { name: "+ New Test Set" }).click();
        await adminPage.getByPlaceholder("factual-checks").fill(datasetName);
        await adminPage.getByPlaceholder("@sarah").fill("@lab08");
        await adminPage
          .getByRole("button", { name: "Create", exact: true })
          .click();
        await adminPage.getByText(datasetName, { exact: true }).first().click();
        await expect(
          adminPage.getByTestId("eval-dataset-detail"),
        ).toBeVisible();
        for (const id of ["IN01", "IN03", "IN06"]) {
          const row = incident(id);
          await adminPage.getByTestId("example-add-toggle").click();
          await adminPage
            .getByTestId("example-input")
            .fill(JSON.stringify(row.input, null, 2));
          await adminPage
            .getByTestId("example-expected")
            .fill(JSON.stringify(row.expected, null, 2));
          await adminPage.getByTestId("example-tags").fill(row.tags.join(", "));
          await adminPage.getByTestId("example-save").click();
        }
        await expect(adminPage.getByTestId("example-row")).toHaveCount(3);
      });

      await test.step("4. Workflows > Import again: preflight passes, import", async () => {
        await adminPage.goto("/caliber/workflows");
        await adminPage.getByTestId("import-workflow").click();
        await adminPage.getByTestId("workflow-import-name").fill(workflowName);
        await adminPage
          .locator('input[type="file"]')
          .setInputFiles(manifestFile);
        await adminPage
          .getByRole("button", { name: "Validate dependencies" })
          .click();
        await expect(adminPage.getByText("Preflight passed")).toBeVisible();
        await adminPage.getByRole("button", { name: "Import as new" }).click();
        await expect(adminPage).toHaveURL(
          /\/caliber\/workflows\/WF-[^/]+\/editor\/WFV-[^/?]+$/,
        );
        workflowId = /workflows\/(WF-[^/]+)\//.exec(adminPage.url())?.[1] ?? "";
        await expect(adminPage.getByTestId("workflow-editor")).toBeVisible();
        for (const node of [
          "severity_table",
          "action_table",
          "join",
          "triage_router",
          "review",
        ]) {
          await expect(adminPage.getByTestId(`outline-${node}`)).toBeVisible();
        }
      });

      await test.step("5. Editor: Validate, then Preview a gateway incident (IN01)", async () => {
        await adminPage.getByTestId("editor-validate").click();
        await expect(adminPage.getByTestId("editor-message")).toContainText(
          "Valid.",
        );
        await adminPage.getByTestId("editor-preview").click();
        await adminPage
          .getByTestId("preview-input")
          .fill(JSON.stringify(incident("IN01").input));
        await adminPage.getByTestId("preview-run").click();
        await expect(adminPage.getByTestId("preview-result")).toContainText(
          "completed",
          {
            timeout: 90_000,
          },
        );
        await adminPage.getByTestId("preview-step-summarize").click();
        await expect(adminPage.getByTestId("preview-result")).toContainText(
          "sev1 rollback",
        );
        await adminPage.getByTestId("preview-close").click();
      });

      await test.step("6. Editor > Publish", async () => {
        await adminPage.getByTestId("editor-publish").click();
        await adminPage.getByTestId("publish-run-validate").click();
        await adminPage.getByTestId("publish-next-1").click();
        await adminPage.getByTestId("publish-next-2").click();
        await adminPage.getByTestId("publish-confirm").click();
        await expect(adminPage.getByTestId("editor-message")).toContainText(
          "Published.",
        );
      });

      await test.step("7. Run Monitor: IN06 recommends monitoring; IN01 waits for the rollback approval", async () => {
        await adminPage.getByTestId("editor-run-monitor").click();
        await expect(adminPage.getByTestId("run-monitor-panel")).toBeVisible();
        await runIncident(adminPage, "IN06");
        await expectRunStatus(adminPage, "completed");
        await expect(adminPage.getByTestId("run-output")).toContainText(
          "RECOMMENDATION",
        );
        await expect(adminPage.getByTestId("run-output")).toContainText(
          "sev4 monitor",
        );
        await runIncident(adminPage, "IN01");
        await expectRunStatus(adminPage, "waiting_approval");
        await adminPage.getByTestId("run-approve").click();
        await expect(
          adminPage.getByText(/a second person must decide/),
        ).toBeVisible({
          timeout: 30_000,
        });
      });

      const lena = await signedInAs(browser, baseURL, "lena");
      contexts.push(lena.context);
      await test.step("8. lena: Workflows > commander > Runs: Approve, Resume, rollback released", async () => {
        await selectWorkspace(lena.page, workspace);
        await lena.page.goto(`/caliber/workflows/${workflowId}?tab=runs`);
        const waiting = lena.page
          .getByRole("row")
          .filter({ hasText: "Awaiting approval" })
          .first();
        await expect(waiting).toBeVisible({ timeout: 60_000 });
        await waiting.locator('[data-testid^="run-WR-"]').click();
        await expect(lena.page.getByTestId("run-approve")).toBeVisible({
          timeout: 30_000,
        });
        await lena.page.getByTestId("run-approve").click();
        await expect(lena.page.getByTestId("run-resume")).toBeEnabled({
          timeout: 30_000,
        });
        await lena.page.getByTestId("run-resume").dispatchEvent("click");
        // The resumed run completes (Recovery Diagnostics names the terminal state), and the
        // Execution Debugger, which follows the newest step until you pick one, shows the output that
        // carries the release. (The text also sits, collapsed, in the event timeline, so assert on
        // the visible step detail rather than on the first text match anywhere in the page.)
        await expect(
          lena.page.getByText("This run ended in completed."),
        ).toBeVisible({
          timeout: 90_000,
        });
        await expect(
          lena.page.getByTestId("workflow-run-step-detail"),
        ).toContainText("ROLLBACK_APPROVED", { timeout: 30_000 });
      });
    } finally {
      for (const entry of contexts) await entry.close();
    }
  });
});
