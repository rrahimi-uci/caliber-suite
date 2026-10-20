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
 * CALIBER_OPERATOR_USERS=admin,lena,sam  CALIBER_APPROVER_USERS=admin,lena). It runs on a
 * simulated stack (`up lab08 --flags cookbooks`) and on a real-provider one (`up lab08h --flags
 * cookbooks --real-llm`): the page's own "Simulated providers" banner picks the mode, and steps 11 to 13 differ:
 *   11. Evaluations: scoring a workflow version is refused without a real model (the page shows
 *       the server's reason), and runs over the test set with one.
 *   12. Workflow calibration (Calibrate, with an agent created on the Agents page): it ends
 *       `rejected` with no candidate on both; the run is marked "Structural score", and "Simulated
 *       evaluation" as well on a simulated stack.
 *   13. (real only) the optional model-based variant is imported and Preview shows the model's
 *       proposal beside the table-decided verdict.
 */

const PASSWORD = "cookbook08-lab-passphrase";
const HERE = dirname(fileURLToPath(import.meta.url));
const ASSETS = resolve(
  HERE,
  "../../../../../docs-site/cookbooks/08-incident-response-commander/assets",
);
const INCIDENT_FILE = resolve(ASSETS, "dataset/incident-cases.jsonl");
const INCIDENTS = readFileSync(INCIDENT_FILE, "utf8")
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
  // a missing control must fail the step in seconds, not wait out the whole-test timeout
  test.use({ actionTimeout: 30_000 });
  test("test set, import, validate, preview, publish, run, approve the rollback", async ({
    page: adminPage,
    browser,
  }, testInfo) => {
    test.setTimeout(1_800_000);
    await new CaliberUi(adminPage).signIn("admin", "admin");
    const simulated = await new CaliberUi(adminPage).isSimulatedStack();
    const baseURL = String(testInfo.project.use.baseURL);
    const suffix = uiOnlySlug("lab08").replace("lab08-", "");
    const workspace = `lab08-ui-${suffix}`;
    const workflowName = `lab08-ui-${suffix}-commander`;
    const datasetName = `lab08-ui-${suffix}-incidents`;
    const contexts: BrowserContext[] = [];
    let workflowId = "";
    let versionId = "";
    let v2Id = "";

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

      await test.step("3. Test Sets: create the test set and Import from file incident-cases.jsonl (41 rows)", async () => {
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
        await adminPage.getByTestId("import-toggle").click();
        await adminPage.getByTestId("import-file").setInputFiles(INCIDENT_FILE);
        await expect(adminPage.getByTestId("import-summary")).toContainText(
          `${INCIDENTS.length} rows ready to import`,
        );
        await adminPage.getByTestId("import-save").click();
        await expect(
          adminPage.getByTestId("eval-dataset-detail"),
        ).toContainText(`${INCIDENTS.length} active examples`, {
          timeout: 120_000,
        });
        await expect(adminPage.getByTestId("example-row")).toHaveCount(
          INCIDENTS.length,
        );
        const gateway = adminPage
          .getByTestId("example-row")
          .filter({ hasText: "gateway 5xx rate 18% for 10 minutes" });
        await expect(gateway).toHaveCount(1);
        await expect(gateway).toContainText("sev1 rollback");
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
        versionId = /editor\/(WFV-[^/?]+)/.exec(adminPage.url())?.[1] ?? "";
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
        await lena.page.getByTestId("run-resume").click();
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

      await test.step("9. Versions: restore v1 as a draft, loosen the rollback rule, save, Preview IN23, publish v2, compare v1 with v2", async () => {
        await adminPage.goto(`/caliber/workflows/${workflowId}?tab=versions`);
        await expect(
          adminPage.getByTestId("versions-table").locator("tbody tr"),
        ).toHaveCount(1);
        await adminPage.getByTestId(`restore-version-btn-${versionId}`).click();
        await expect(adminPage).toHaveURL(/\/editor\/WFV-[^/?]+$/, {
          timeout: 30_000,
        });
        v2Id = /editor\/(WFV-[^/?]+)/.exec(adminPage.url())?.[1] ?? "";
        expect(v2Id).not.toBe(versionId);
        // the rollback rule fires at a 5% error rate: a "tidy-up" moves it to 50%
        await adminPage.getByTestId("outline-action_table").dblclick();
        const config = adminPage.getByTestId("inspector-transform-config");
        await expect(config).toBeVisible();
        const rules = JSON.parse(await config.inputValue()) as {
          rules: { name: string; when: { path: string; value: number }[] }[];
        };
        const rule = rules.rules.find(
          (item) => item.name === "rollback-recent-high-risk-deploy-errors",
        )!;
        rule.when.find((c) => c.path.endsWith("error_rate"))!.value = 0.5;
        await config.fill(JSON.stringify(rules, null, 2));
        await config.blur();
        await adminPage.getByTestId("editor-save").click();
        await expect(adminPage.getByTestId("editor-save-status")).toContainText(
          /Saved|saved/,
        );
        await adminPage.getByTestId("editor-validate").click();
        await expect(adminPage.getByTestId("editor-message")).toContainText(
          "Valid.",
        );
        // IN23 (a 6% error rate after a high-risk deploy) was a rollback; now it is not
        await adminPage.getByTestId("editor-preview").click();
        await adminPage
          .getByTestId("preview-input")
          .fill(JSON.stringify(incident("IN23").input));
        await adminPage.getByTestId("preview-run").click();
        await expect(adminPage.getByTestId("preview-result")).toContainText(
          "completed",
          { timeout: 90_000 },
        );
        await adminPage.getByTestId("preview-step-summarize").click();
        await expect(adminPage.getByTestId("preview-result")).toContainText(
          "sev2 escalate_human",
        );
        await adminPage.getByTestId("preview-close").click();
        await adminPage.getByTestId("editor-publish").click();
        await adminPage.getByTestId("publish-run-validate").click();
        await adminPage.getByTestId("publish-next-1").click();
        await adminPage.getByTestId("publish-next-2").click();
        await adminPage.getByTestId("publish-confirm").click();
        await expect(adminPage.getByTestId("editor-message")).toContainText(
          "Published.",
        );
        await adminPage.goto(`/caliber/workflows/${workflowId}?tab=versions`);
        await adminPage
          .getByTestId("version-diff-base")
          .selectOption(versionId);
        await adminPage.getByTestId("version-diff-other").selectOption(v2Id);
        const diff = adminPage.getByTestId("version-diff-panel");
        await expect(diff.getByTestId("diff-modified-node")).toHaveCount(1);
        await expect(diff.getByTestId("diff-modified-node")).toContainText(
          "action_table",
        );
      });

      await test.step("10. Versions: restore v1 again, publish it as v3, Preview IN23: rollback again", async () => {
        await adminPage.goto(`/caliber/workflows/${workflowId}?tab=versions`);
        await adminPage.getByTestId(`restore-version-btn-${versionId}`).click();
        await expect(adminPage).toHaveURL(/\/editor\/WFV-[^/?]+$/, {
          timeout: 30_000,
        });
        await adminPage.getByTestId("editor-preview").click();
        await adminPage
          .getByTestId("preview-input")
          .fill(JSON.stringify(incident("IN23").input));
        await adminPage.getByTestId("preview-run").click();
        await expect(adminPage.getByTestId("preview-result")).toContainText(
          "completed",
          { timeout: 90_000 },
        );
        await adminPage.getByTestId("preview-step-summarize").click();
        await expect(adminPage.getByTestId("preview-result")).toContainText(
          "sev2 rollback",
        );
        await adminPage.getByTestId("preview-close").click();
        await adminPage.getByTestId("editor-publish").click();
        await adminPage.getByTestId("publish-run-validate").click();
        await adminPage.getByTestId("publish-next-1").click();
        await adminPage.getByTestId("publish-next-2").click();
        await adminPage.getByTestId("publish-confirm").click();
        await expect(adminPage.getByTestId("editor-message")).toContainText(
          "Published.",
        );
        await adminPage.goto(`/caliber/workflows/${workflowId}?tab=versions`);
        await expect(
          adminPage.getByTestId("versions-table").locator("tbody tr"),
        ).toHaveCount(3);
      });

      await test.step("11. Evaluations: score the workflow version over the test set", async () => {
        expect(versionId).toMatch(/^WFV-/);
        await adminPage.goto("/caliber/evaluations");
        await adminPage.getByRole("button", { name: "Run evaluation" }).click();
        await adminPage
          .getByLabel("Test set", { exact: true })
          .selectOption({ label: `${datasetName} (v${INCIDENTS.length + 1})` });
        await adminPage
          .getByLabel("Label", { exact: true })
          .fill(`lab08-${suffix}`);
        await adminPage.getByLabel("What to score").selectOption("workflow");
        await adminPage.getByLabel("Subject under test").fill(versionId);
        // The verdict text is the one deterministic check: Contains expected.
        await adminPage.getByText("Exact match", { exact: true }).click();
        await adminPage.getByText("Token F1", { exact: true }).click();
        await adminPage
          .getByRole("button", { name: "Run", exact: true })
          .click();
        if (simulated) {
          // No model behind the server: scoring fails closed with the server's own reason.
          await expect(
            adminPage.getByText(/evaluation requires a real LLM provider/),
          ).toBeVisible({ timeout: 60_000 });
        } else {
          const row = adminPage
            .getByTestId("eval-run-row")
            .filter({ hasText: `lab08-${suffix}` });
          await expect(row).toBeVisible({ timeout: 300_000 });
          await expect(row).toContainText("completed");
          // a workflow run scores at most 20 examples; the run's detail says how many it left out
          await expect(row).toContainText("20/20");
        }
      });

      await test.step("12. Agents and Calibrate: the run ends rejected with no candidate, and says what measured it", async () => {
        await adminPage.goto("/caliber/agents");
        await adminPage.getByTestId("new-agent").click();
        const form = adminPage.getByTestId("agent-create-form");
        await form.getByPlaceholder("support-agent").fill(`lab08-ui-${suffix}`);
        await form.getByPlaceholder("1234567890").fill(`exp-${suffix}`);
        await form
          .getByPlaceholder("Support Agent")
          .fill(`incident commander ${suffix}`);
        await form.getByRole("button", { name: "Register agent" }).click();
        await expect(
          adminPage.getByText(`incident commander ${suffix}`).first(),
        ).toBeVisible();

        await adminPage.goto(`/caliber/workflows/${workflowId}`);
        await adminPage.getByTestId("workflow-calibrate").click();
        await expect(
          adminPage.getByTestId("workflow-calibration-dataset"),
        ).toContainText(`${INCIDENTS.length} examples`);
        await adminPage
          .getByTestId("workflow-calibration-agent")
          .selectOption({ label: `incident commander ${suffix}` });
        await adminPage.getByTestId("workflow-calibration-start").click();
        const run = adminPage.getByTestId("workflow-calibration-run").first();
        await expect(run).toContainText("rejected", { timeout: 600_000 });
        await expect(
          run.getByTestId("workflow-calibration-structural"),
        ).toBeVisible();
        if (simulated) {
          await expect(
            run.getByTestId("workflow-calibration-simulated"),
          ).toBeVisible();
        } else {
          await expect(
            run.getByTestId("workflow-calibration-simulated"),
          ).toHaveCount(0);
        }
      });

      if (!simulated) {
        await test.step("13. The model variant: import it, Preview shows the model's proposal beside the verdict", async () => {
          await adminPage.goto("/caliber/workflows");
          await adminPage.getByTestId("import-workflow").click();
          await adminPage
            .getByTestId("workflow-import-name")
            .fill(`${workflowName}-model`);
          await adminPage
            .locator('input[type="file"]')
            .setInputFiles(
              resolve(
                ASSETS,
                "workflows/incident-commander-model.workflow.json",
              ),
            );
          await adminPage
            .getByRole("button", { name: "Validate dependencies" })
            .click();
          await expect(adminPage.getByText("Preflight passed")).toBeVisible();
          await adminPage
            .getByRole("button", { name: "Import as new" })
            .click();
          await expect(adminPage.getByTestId("workflow-editor")).toBeVisible();
          await expect(
            adminPage.getByTestId("outline-propose_triage"),
          ).toBeVisible();
          await adminPage.getByTestId("editor-preview").click();
          await adminPage
            .getByTestId("preview-input")
            .fill(JSON.stringify(incident("IN01").input));
          await adminPage.getByTestId("preview-run").click();
          await expect(adminPage.getByTestId("preview-result")).toContainText(
            "completed",
            { timeout: 120_000 },
          );
          await adminPage.getByTestId("preview-step-summarize").click();
          // the tables decide the verdict; the model's proposal is only shown beside it
          await expect(adminPage.getByTestId("preview-result")).toContainText(
            "sev1 rollback",
          );
          await expect(adminPage.getByTestId("preview-result")).toContainText(
            "model_proposal",
          );
          await adminPage.getByTestId("preview-close").click();
        });
      }
    } finally {
      for (const entry of contexts) await entry.close();
    }
  });
});
