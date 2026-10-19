import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Browser, type Page } from "@playwright/test";

import { CaliberUi, uiOnlySlug } from "../ui-only-driver";
import { expectRunStatus } from "../../helpers";

/**
 * Cookbook 03, the UI path: a deterministic refund-policy tool, hardened, then used behind an
 * approval gate that a second person decides.
 *
 * Follows docs-site/cookbooks/03-tool-hardening-contract-lab/README.md ("Run it through the UI").
 * Needs a stack started with `scripts/cookbook-env.sh up lab03 --flags cookbooks` plus
 * CALIBER_APPROVER_USERS=admin,approver1 and CALIBER_REGISTERED_TOOL_SANDBOX_TIMEOUT_SECONDS=5.
 * No API request context is used: every mutation goes through the pages.
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const LAB = path.resolve(
  here,
  "../../../../../docs-site/cookbooks/03-tool-hardening-contract-lab",
);
const APPROVER = { user: "approver1", password: "lab03-approver1-password" };
const VIEWER = { user: "viewer1", password: "lab03-viewer1-password" };

interface Row {
  id: string;
  name: string;
  inputs: Record<string, unknown>;
  expectations: Record<string, unknown>;
}

const rows: Row[] = fs
  .readFileSync(path.join(LAB, "assets/dataset/refund-fixtures.jsonl"), "utf-8")
  .split("\n")
  .filter((line) => line.trim())
  .map((line) => JSON.parse(line) as Row);
const byId = new Map(rows.map((row) => [row.id, row]));

function sortedJson(value: unknown): string {
  // Matches Python's json.dumps(sort_keys=True): ", " and ": " separators.
  const sort = (v: unknown): unknown =>
    Array.isArray(v)
      ? v.map(sort)
      : v && typeof v === "object"
        ? Object.fromEntries(
            Object.entries(v as Record<string, unknown>)
              .sort(([a], [b]) => (a < b ? -1 : 1))
              .map(([k, x]) => [k, sort(x)]),
          )
        : v;
  return JSON.stringify(sort(value))
    .replace(/":/g, '": ')
    .replace(/,"/g, ', "');
}

function toolAsset(file: string): Record<string, unknown> {
  return JSON.parse(
    fs.readFileSync(path.join(LAB, "assets/tools", file), "utf-8"),
  ) as Record<string, unknown>;
}

async function signedIn(
  browser: Browser,
  who: { user: string; password: string },
  workspaceName: string,
): Promise<Page> {
  const context = await browser.newContext();
  const page = await context.newPage();
  await new CaliberUi(page).signIn(who.user, who.password);
  await selectWorkspace(page, workspaceName);
  return page;
}

async function selectWorkspace(page: Page, name: string): Promise<void> {
  await page.getByLabel("Active workspace").selectOption({ label: name });
}

test.describe("Cookbook 03: policy-safe decision tool (UI)", () => {
  test("registers, hardens and gates the refund tool", async ({
    page,
    browser,
  }) => {
    test.setTimeout(1_200_000);
    const suffix = `_${Date.now().toString(36)}`;
    const workspace = `refund-ops${suffix}`;
    const decideName = `decide_refund${suffix}`;
    const refundName = `initiate_refund${suffix}`;
    const ui = new CaliberUi(page);

    // ---- Setup: people and a workspace --------------------------------------
    await ui.signIn();
    await page.goto("/caliber/administration");
    await expect(
      page.getByRole("heading", { name: "Administration" }),
    ).toBeVisible();
    for (const person of [APPROVER, VIEWER]) {
      const accounts = page.getByLabel("Accounts");
      const exists = await accounts
        .getByRole("cell", { name: person.user, exact: true })
        .count();
      if (exists === 0) {
        await page.getByLabel("User ID").first().fill(person.user);
        await page.getByLabel("Password").first().fill(person.password);
        await page.getByRole("button", { name: "Create account" }).click();
        await expect(
          accounts.getByRole("cell", { name: person.user, exact: true }),
        ).toBeVisible();
      }
    }

    await page.getByRole("button", { name: "Create workspace" }).click();
    await page.getByPlaceholder("Document automation").fill(workspace);
    await page.getByRole("button", { name: "Create and select" }).click();
    await expect(page.getByLabel("Active workspace")).toContainText(workspace);

    await page.goto("/caliber/administration");
    await page
      .getByLabel("Project for access management")
      .selectOption({ label: `${workspace} (owner)` });
    for (const [person, role] of [
      [APPROVER.user, "Reviewer"],
      [VIEWER.user, "Viewer"],
    ]) {
      await page.getByPlaceholder("@developer").fill(person);
      await page
        .getByRole("combobox")
        .filter({ hasText: "Viewer" })
        .first()
        .selectOption({ label: role });
      await page.getByRole("button", { name: "Add member" }).click();
      await expect(
        page
          .getByLabel("Project access")
          .getByRole("cell", { name: person, exact: true }),
      ).toBeVisible();
    }

    // ---- 1. Register the tools ----------------------------------------------
    for (const [file, name] of [
      ["decide-refund.tool.json", decideName],
      ["initiate-refund.tool.json", refundName],
    ] as const) {
      const asset = toolAsset(file);
      await page.goto("/caliber/tools");
      await page.getByRole("button", { name: "Register Tool" }).click();
      await expect(page.getByTestId("tool-wizard")).toBeVisible();
      await page.getByTestId("wiz-name").fill(name);
      await page.getByTestId("wiz-version").fill(String(asset.version));
      await page.getByTestId("wiz-description").fill(String(asset.description));
      await page.getByTestId("wiz-owner").fill("@refund-ops");
      await page.getByTestId("wizard-next").click();
      await page.getByTestId("wiz-module").fill(String(asset.module_path));
      await page.getByTestId("wiz-callable").fill(String(asset.callable_name));
      await page.getByTestId("wizard-next").click();
      await page.getByTestId("input-schema-toggle-raw").click();
      await page
        .getByTestId("input-schema-raw")
        .fill(JSON.stringify(asset.input_schema, null, 2));
      await page.getByTestId("output-schema-toggle-raw").click();
      await page
        .getByTestId("output-schema-raw")
        .fill(JSON.stringify(asset.output_schema, null, 2));
      await page.getByTestId("wizard-next").click();
      await page.getByTestId("wizard-next").click();
      if (asset.side_effect_level === "write") {
        await page.getByTestId("wiz-side-effect-write").click();
        await expect(page.getByTestId("wiz-requires-approval")).toBeChecked();
        await expect(page.getByTestId("wiz-requires-approval")).toBeDisabled();
      } else {
        await page.getByTestId("wiz-allow-preview").check();
      }
      await page.getByTestId("wizard-submit").click();
      await expect(page).toHaveURL(/\/caliber\/tools\/[^/]+$/);
    }

    // ---- 2. Sandbox ---------------------------------------------------------
    const openTool = async (name: string): Promise<void> => {
      await page.goto("/caliber/tools");
      await page.getByTestId(`tool-open-${name}`).click();
      await expect(page.getByTestId("tool-workspace-header")).toContainText(
        name,
      );
    };
    await openTool(decideName);
    await expect(page.getByTestId(`tool-row-${decideName}`)).toHaveCount(0);
    await page.getByRole("button", { name: "Sandbox", exact: true }).click();
    await page
      .getByPlaceholder('{"key": "value"}')
      .fill(JSON.stringify(byId.get("R01")!.inputs));
    await page.getByRole("button", { name: "Test Run", exact: true }).click();
    await expect(page.getByText("Success", { exact: true })).toBeVisible();
    await expect(
      page.locator("pre").filter({ hasText: '"decision": "approve"' }),
    ).toBeVisible();

    await openTool(refundName);
    await page.getByRole("button", { name: "Sandbox", exact: true }).click();
    await page
      .getByPlaceholder('{"key": "value"}')
      .fill('{"order_id": "ord_1007", "amount": 49.0}');
    await page.getByRole("button", { name: "Test Run", exact: true }).click();
    await expect(page.getByText("Success (mocked)")).toBeVisible();

    // ---- 3. Fixtures: the editor takes one case at a time --------------------
    await openTool(decideName);
    await page.getByRole("button", { name: "Fixtures", exact: true }).click();
    const fixtures = page.getByTestId("tool-fixtures-calibration");
    for (const [index, row] of rows.entries()) {
      if (index > 0)
        await page.getByTestId("tool-fixtures-calibration-add").click();
      const block = fixtures
        .getByTestId("tool-fixtures-calibration-case")
        .nth(index);
      await block
        .getByTestId("tool-fixtures-calibration-case-name")
        .fill(`${row.id} ${row.name}`);
      await block
        .getByTestId("tool-fixtures-calibration-case-input")
        .fill(JSON.stringify(row.inputs));
      await block
        .getByTestId("tool-fixtures-calibration-case-assertion")
        .selectOption("equals");
      await block
        .getByTestId("tool-fixtures-calibration-case-value")
        .fill(sortedJson(row.expectations));
    }
    await page.getByTestId("tool-fixtures-calibration-save").click();
    await expect(
      page.getByTestId("tool-fixtures-calibration-saved"),
    ).toBeVisible();
    await expect(
      fixtures.getByTestId("tool-fixtures-calibration-case"),
    ).toHaveCount(rows.length);

    // ---- 4. Hardening: run the suite -----------------------------------------
    await page.getByRole("button", { name: "Hardening", exact: true }).click();
    await page.getByTestId("tool-hardening-calibrate-btn").click();
    await expect(
      page.getByTestId("tool-hardening-calibration-result"),
    ).toContainText(`Pass rate 100% — ${rows.length}/${rows.length} passed`, {
      timeout: 360_000,
    });

    // ---- 5. Pin the run as the baseline ----------------------------------------
    await page.getByRole("button", { name: "Test Runs", exact: true }).click();
    await page.getByRole("button", { name: "Set as baseline" }).click();
    await expect(page.getByTestId("tool-run-baseline-marker")).toBeVisible();

    // ---- 5b. Regression drill: wrong expectation on one case ---------------------
    await page.getByRole("button", { name: "Hardening", exact: true }).click();
    const drillIndex = rows.findIndex((row) => row.id === "R09");
    const drillCase = page
      .getByTestId("tool-hardening-calibration-case")
      .nth(drillIndex);
    await drillCase.getByTestId("tool-hardening-calibration-case-value").fill(
      sortedJson({
        decision: "manual_review",
        reason_code: "AMOUNT_OVER_THRESHOLD",
        requires_approval: true,
      }),
    );
    await page.getByTestId("tool-hardening-calibrate-btn").click();
    await expect(
      page.getByTestId("tool-hardening-calibration-result"),
    ).toContainText(
      `Pass rate 98% — ${rows.length - 1}/${rows.length} passed`,
      { timeout: 360_000 },
    );
    await expect(
      drillCase.getByTestId("tool-hardening-calibration-case-verdict"),
    ).toHaveText("fail");
    await page.getByRole("button", { name: "Test Runs", exact: true }).click();
    await expect(page.getByTestId("tool-run-regressions")).toContainText(
      "1 regression",
    );
    const comparison = page.getByTestId("tool-workspace-run-comparison");
    await expect(
      comparison.getByText(`${byId.get("R09")!.id} ${byId.get("R09")!.name}`),
    ).toBeVisible();
    await expect(
      comparison.getByText("regression", { exact: true }),
    ).toHaveCount(1);
    await expect(page.getByTestId("tool-run-score-delta")).toContainText(
      "Net score -2%",
    );

    // Put the expectation back and confirm the suite is green again.
    await page.getByRole("button", { name: "Hardening", exact: true }).click();
    await page
      .getByTestId("tool-hardening-calibration-case")
      .nth(drillIndex)
      .getByTestId("tool-hardening-calibration-case-value")
      .fill(sortedJson(byId.get("R09")!.expectations));
    await page.getByTestId("tool-hardening-calibrate-btn").click();
    await expect(
      page.getByTestId("tool-hardening-calibration-result"),
    ).toContainText(`Pass rate 100% — ${rows.length}/${rows.length} passed`, {
      timeout: 360_000,
    });

    // ---- 6. Import the workflow -----------------------------------------------
    const manifest = fs
      .readFileSync(
        path.join(LAB, "assets/workflow/refund-decision.workflow.yaml"),
        "utf-8",
      )
      .replaceAll("decide_refund", decideName)
      .replaceAll("initiate_refund", refundName);
    const workflowName = `refund-decision${suffix}`;
    await page.goto("/caliber/workflows");
    await page.getByTestId("import-workflow").click();
    await page.getByTestId("workflow-import-name").fill(workflowName);
    await page.locator('input[type="file"]').setInputFiles({
      name: "refund-decision.workflow.yaml",
      mimeType: "text/yaml",
      buffer: Buffer.from(manifest),
    });
    await expect(page.getByTestId("workflow-import-manifest")).toHaveValue(
      /tool_name:/,
    );
    await page.getByTestId("workflow-import-validate").click();
    const preview = page.getByTestId("workflow-import-preview");
    await expect(preview).toContainText("Preflight passed");
    await expect(preview).toContainText(`tool.${decideName}.v1`);
    await expect(preview).toContainText(`tool.${refundName}.v1`);
    await page.getByTestId("workflow-import-submit").click();
    await expect(page).toHaveURL(
      /\/caliber\/workflows\/WF-[^/]+\/editor\/WFV-/,
    );
    const editorUrl = page.url();

    // ---- 7. Run it: the refund node pauses for a second person ----------------
    let lastHref = "";
    const startRun = async (row: Row): Promise<string> => {
      const panel = page.getByTestId("run-monitor-panel");
      if (!(await panel.isVisible())) {
        await page.getByTestId("editor-run-monitor").click();
      }
      await expect(panel).toBeVisible();
      await page.getByTestId("run-input").fill(JSON.stringify(row.inputs));
      // The run monitor's sticky header overlaps the run controls; dispatch the click to the button.
      await page.getByTestId("run-execute").dispatchEvent("click");
      const link = page.getByTestId("run-active-open-link");
      await expect(link).toBeVisible({ timeout: 60_000 });
      await expect
        .poll(async () => (await link.getAttribute("href")) ?? "", {
          timeout: 60_000,
        })
        .not.toBe(lastHref);
      lastHref = (await link.getAttribute("href")) ?? "";
      return lastHref;
    };
    const href = await startRun(byId.get("R01")!);
    await expectRunStatus(page, "waiting_approval", { timeout: 120_000 });
    await expect(page.getByTestId("run-approval-actions")).toBeVisible();

    // The person who started the run cannot approve it.
    await page.getByTestId("run-approve").dispatchEvent("click");
    await expect(page.getByText(/a second person must decide/)).toBeVisible();
    await expectRunStatus(page, "waiting_approval");

    // A second person (approver1, a reviewer in this workspace) approves.
    const runId = href.split("/").pop() as string;
    const approverPage = await signedIn(browser, APPROVER, workspace);
    await approverPage.goto(editorUrl);
    await approverPage.getByTestId("editor-run-monitor").click();
    await approverPage.getByTestId(`run-history-select-${runId}`).click();
    await expect(
      approverPage.getByTestId("run-approval-actions"),
    ).toBeVisible();
    await approverPage.getByTestId("run-approve").dispatchEvent("click");
    await expect(approverPage.getByTestId("run-resume")).toBeDisabled();

    // Approve records the decision; Resume (the initiator, who holds operator) advances the run.
    await expect(page.getByTestId("run-resume")).toBeEnabled({
      timeout: 60_000,
    });
    await page.getByTestId("run-resume").dispatchEvent("click");
    await expectRunStatus(page, "completed", { timeout: 120_000 });
    await expect(page.getByTestId("run-output")).toContainText(
      '"refund_status": "initiated"',
    );
    await expect(page.getByTestId("run-output")).toContainText(
      '"amount_usd": 49.0',
    );

    // ---- 8. Reject: the refund never runs --------------------------------------
    const rejectedHref = await startRun(byId.get("R01")!);
    await expectRunStatus(page, "waiting_approval", { timeout: 120_000 });
    const rejectedId = rejectedHref.split("/").pop() as string;
    await approverPage.goto(editorUrl);
    await approverPage.getByTestId("editor-run-monitor").click();
    await approverPage.getByTestId(`run-history-select-${rejectedId}`).click();
    await approverPage.getByTestId("run-reject").dispatchEvent("click");
    await expectRunStatus(page, "failed", { timeout: 60_000 });
    await expect(page.getByTestId("run-output")).not.toContainText(
      "refund_status",
    );

    // ---- 9. Deny: finishes by itself, nothing to approve -------------------------
    await startRun(byId.get("R23")!);
    await expectRunStatus(page, "completed", { timeout: 120_000 });
    await expect(page.getByTestId("run-output")).toContainText(
      '"decision": "deny"',
    );
    await expect(page.getByTestId("run-approval-actions")).toHaveCount(0);

    // ---- 10. The audit log names who did what ------------------------------------
    await page.goto("/caliber/audit-log");
    await page.getByPlaceholder("@alice").fill("approver1");
    await expect(
      page.getByText("approve_workflow_run_runtime_approval").first(),
    ).toBeVisible();
    await expect(
      page.getByText("reject_workflow_run_runtime_approval").first(),
    ).toBeVisible();
    await page.getByPlaceholder("@alice").fill("");
    await page.getByPlaceholder("approve").fill("resume_workflow_run");
    await expect(page.getByText("resume_workflow_run").first()).toBeVisible();
    await expect(page.getByText("admin").first()).toBeVisible();

    // ---- Failure: a viewer cannot run a tool --------------------------------------
    const viewerPage = await signedIn(browser, VIEWER, workspace);
    await viewerPage.goto("/caliber/tools");
    await viewerPage.getByTestId(`tool-open-${decideName}`).click();
    await viewerPage
      .getByRole("button", { name: "Sandbox", exact: true })
      .click();
    await viewerPage
      .getByPlaceholder('{"key": "value"}')
      .fill(JSON.stringify(byId.get("R01")!.inputs));
    await viewerPage
      .getByRole("button", { name: "Test Run", exact: true })
      .click();
    await expect(
      viewerPage.getByText(/caliber\.operator/).first(),
    ).toBeVisible();

    // ---- The gallery starter ------------------------------------------------------
    await ui.installCookbookDraft("03", `Cookbook 03 starter${suffix}`);
  });
});
