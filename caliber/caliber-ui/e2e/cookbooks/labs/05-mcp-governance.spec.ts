import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Browser, type Page } from "@playwright/test";

import { CaliberUi } from "../ui-only-driver";
import { expectRunStatus } from "../../helpers";

/**
 * Cookbook 05, the UI path: register an MCP server, discover its tools, classify them, invoke the
 * allowed read tool, meet the refusals, calibrate, and gate a write behind a second person.
 *
 * Follows docs-site/cookbooks/05-mcp-connectivity-governance-lab/README.md ("Run it through the UI").
 * Needs a stack started as that README describes (scripts/cookbook-env.sh up lab05 --flags
 * integrations,cookbooks, with CALIBER_MCP_STDIO_PYTHON_SCRIPT_ALLOWLIST naming the lab's server
 * script and the lab's accounts as platform scopes). The MCP server writes a call ledger that the
 * spec reads from disk: LAB05_LEDGER, else $CALIBER_STATE_DIR/lab05-ui-ledger.jsonl, else the OS
 * temp directory. No API request context is used: every mutation goes through the pages.
 */
const here = path.dirname(fileURLToPath(import.meta.url));
const LAB = path.resolve(
  here,
  "../../../../../docs-site/cookbooks/05-mcp-connectivity-governance-lab",
);
const SERVER_SCRIPT = path.join(LAB, "assets/server/repo_tracker_mcp.py");
const OPERATOR = {
  user: "lab05-operator",
  password: "lab05-operator-password",
};
const APPROVER = {
  user: "lab05-approver",
  password: "lab05-approver-password",
};
const VIEWER = { user: "lab05-viewer", password: "lab05-viewer-password" };
const LEDGER =
  process.env["LAB05_LEDGER"] ??
  path.join(
    process.env["CALIBER_STATE_DIR"] ?? os.tmpdir(),
    "lab05-ui-ledger.jsonl",
  );

interface Classification {
  tool: string;
  side_effect_level: string;
  allowed: boolean;
  requires_approval: boolean;
  rate_limit_per_minute?: number;
}
interface CalibrationRow {
  id: string;
  tool_name: string;
  name: string;
  input: Record<string, unknown>;
  assertion: { type: string; value?: string };
}

function jsonl<T>(file: string): T[] {
  return fs
    .readFileSync(path.join(LAB, "assets/dataset", file), "utf-8")
    .split("\n")
    .filter((line) => line.trim())
    .map((line) => JSON.parse(line) as T);
}
const classification = jsonl<Classification>("tool-classification.jsonl");
const calibration = jsonl<CalibrationRow>("calibration-cases.jsonl");

function executed(tool: string): number {
  if (!fs.existsSync(LEDGER)) return 0;
  return fs
    .readFileSync(LEDGER, "utf-8")
    .split("\n")
    .filter((line) => line.trim())
    .map((line) => JSON.parse(line) as { tool: string })
    .filter((entry) => entry.tool === tool).length;
}

async function signedIn(
  browser: Browser,
  who: { user: string; password: string },
  workspaceName: string,
): Promise<Page> {
  const context = await browser.newContext();
  const page = await context.newPage();
  await new CaliberUi(page).signIn(who.user, who.password);
  await page
    .getByLabel("Active workspace")
    .selectOption({ label: workspaceName });
  return page;
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

async function openPlayground(page: Page, serverName: string): Promise<void> {
  await page.goto("/caliber/mcp-servers");
  await page.getByRole("button", { name: "Playground" }).click();
  await page
    .getByLabel("Select MCP server")
    .selectOption({ label: serverName });
}

async function pickTool(page: Page, tool: string): Promise<void> {
  await page.getByRole("button").filter({ hasText: tool }).first().click();
  await expect(page.getByRole("heading", { name: tool })).toBeVisible();
}

test.describe("Cookbook 05: governed MCP connectivity (UI)", () => {
  test("registers, classifies, invokes, refuses, calibrates and gates", async ({
    page,
    browser,
  }) => {
    test.setTimeout(900_000);
    const suffix = `_${Date.now().toString(36)}`;
    const workspace = `mcp-ops${suffix}`;
    const serverName = `repo-tracker${suffix}`;
    const ui = new CaliberUi(page);
    fs.mkdirSync(path.dirname(LEDGER), { recursive: true });
    fs.writeFileSync(LEDGER, "");

    // ---- Setup: people and a workspace --------------------------------------
    await ui.signIn();
    await page.goto("/caliber/administration");
    await expect(
      page.getByRole("heading", { name: "Administration" }),
    ).toBeVisible();
    for (const person of [OPERATOR, APPROVER, VIEWER]) {
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
      [OPERATOR.user, "Editor"],
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

    // ---- 1. Register the MCP server -----------------------------------------
    await page.goto("/caliber/mcp-servers");
    await expect(
      page.getByRole("heading", { name: "MCP Servers" }),
    ).toBeVisible();
    await page.getByTestId("add-server-btn").click();
    await expect(page.getByTestId("add-server-dialog")).toBeVisible();
    await page.getByTestId("server-name-input").fill(serverName);
    await page.getByTestId("server-command-input").fill("${PYTHON}");
    await page
      .getByPlaceholder("e.g. -y @modelcontextprotocol/server-filesystem /data")
      .fill(`${SERVER_SCRIPT} --ledger ${LEDGER}`);
    await page.getByTestId("server-submit-btn").click();
    const row = page
      .locator('[data-testid^="mcp-row-"]')
      .filter({ hasText: serverName })
      .first();
    await expect(row).toBeVisible();
    await expect(row).toContainText("Runtime ready");
    await expect(row).toContainText("local containment");

    // ---- 2. Test the connection and discover the tools ----------------------
    await row.getByRole("button", { name: "Test" }).click();
    await expect(row).toContainText("Connected · 6 tools", { timeout: 60_000 });

    // ---- 3. The Playground: nothing is callable until it is classified --------
    await openPlayground(page, serverName);
    await page.getByRole("button", { name: "Test Connection" }).click();
    await expect(page.getByText("Connection Successful")).toBeVisible({
      timeout: 60_000,
    });
    await expect(page.getByText("6 tools discovered")).toBeVisible();
    for (const row of classification) {
      await expect(
        page.getByRole("button").filter({ hasText: row.tool }).first(),
      ).toContainText("Unclassified");
    }
    await pickTool(page, "search_repositories");
    await expect(
      page.getByRole("button", { name: "Classify Tool Before Invoking" }),
    ).toBeDisabled();

    // ---- 4. Classify every tool ---------------------------------------------
    for (const row of classification) {
      await pickTool(page, row.tool);
      const allow = page.getByRole("checkbox", { name: "Allow tool" });
      const approval = page.getByRole("checkbox", {
        name: "Requires approval",
      });
      await allow.setChecked(row.allowed);
      await page
        .getByLabel("Side effect level")
        .selectOption(row.side_effect_level);
      if (row.rate_limit_per_minute) {
        await page
          .getByLabel("Rate limit / minute")
          .fill(String(row.rate_limit_per_minute));
      }
      if (row.tool === "issue_write") {
        // An allowed write tool without approval is refused: the page shows the server's reason.
        await page.getByRole("button", { name: "Save Policy" }).click();
        await expect(
          page.getByText(/set requires_approval=true/),
        ).toBeVisible();
      }
      await approval.setChecked(row.requires_approval);
      await page.getByRole("button", { name: "Save Policy" }).click();
      await expect(page.getByText("Policy saved.")).toBeVisible();
    }
    for (const row of classification) {
      await expect(
        page.getByRole("button").filter({ hasText: row.tool }).first(),
      ).not.toContainText("Unclassified");
    }
    await expect(
      page.getByRole("button").filter({ hasText: "create_release" }).first(),
    ).toContainText("Blocked");
    await expect(
      page.getByRole("button").filter({ hasText: "issue_write" }).first(),
    ).toContainText("Approval");

    // ---- 5. Invoke the allowed read tool ------------------------------------
    await pickTool(page, "search_repositories");
    await page
      .getByPlaceholder(/Free text plus qualifiers/)
      .fill("language:python topic:mcp");
    await page.getByRole("button", { name: "Invoke Tool" }).click();
    await expect(page.getByText("Success", { exact: true })).toBeVisible({
      timeout: 60_000,
    });
    await expect(
      page.locator("pre").filter({ hasText: "acme/mcp-gateway" }),
    ).toBeVisible();
    await expect(page.getByTestId("invocation-history-row-0")).toContainText(
      "search_repositories",
    );
    expect(executed("search_repositories")).toBe(1);

    // The server's own error is a failed call, not a crash.
    await page
      .getByPlaceholder(/Free text plus qualifiers/)
      .fill("stars:>>not-a-number");
    await page.getByRole("button", { name: "Invoke Tool" }).click();
    await expect(page.getByText("Failed", { exact: true })).toBeVisible({
      timeout: 60_000,
    });
    await expect(page.getByText(/stars: expects a number/)).toBeVisible();

    // ---- 6. A blocked tool cannot be invoked --------------------------------
    await pickTool(page, "create_release");
    await expect(
      page.getByRole("button", { name: "Tool Blocked by Policy" }),
    ).toBeDisabled();
    await expect(
      page.getByText("This tool is blocked by its saved policy."),
    ).toBeVisible();
    expect(executed("create_release")).toBe(0);

    // The approval-class write tool still runs from the admin Playground (break-glass).
    await pickTool(page, "issue_write");
    await page
      .getByPlaceholder("Repository as owner/name")
      .fill("acme/release-notes-bot");
    await page
      .getByPlaceholder("title")
      .fill("Release notes bot skips pre-release tags");
    await page.getByRole("button", { name: "Invoke Tool" }).click();
    await expect(page.getByText("Success", { exact: true })).toBeVisible({
      timeout: 60_000,
    });
    await expect(
      page.locator("pre").filter({ hasText: '"created": true' }),
    ).toBeVisible();
    expect(executed("issue_write")).toBe(1);

    // ---- 7. Calibrate the read tool -----------------------------------------
    await pickTool(page, "search_repositories");
    const cases = calibration
      .filter((c) => c.tool_name === "search_repositories")
      .slice(0, 6);
    const panel = page.getByTestId("mcp-calibration");
    for (let i = 0; i < cases.length; i += 1) {
      if (i > 0) await panel.getByTestId("mcp-calibration-add").click();
      const draft = panel.getByTestId("mcp-calibration-case").nth(i);
      await draft.getByTestId("mcp-calibration-case-name").fill(cases[i]!.name);
      await draft
        .getByTestId("mcp-calibration-case-input")
        .fill(JSON.stringify(cases[i]!.input));
      await draft
        .getByTestId("mcp-calibration-case-assertion")
        .selectOption(cases[i]!.assertion.type);
      await draft
        .getByTestId("mcp-calibration-case-value")
        .fill(String(cases[i]!.assertion.value));
    }
    await panel.getByTestId("mcp-calibration-save").click();
    await expect(panel.getByTestId("mcp-calibration-saved")).toBeVisible();
    await page.getByTestId("mcp-calibrate-btn").click();
    await expect(panel.getByTestId("mcp-calibration-result")).toContainText(
      `Pass rate 100% — ${cases.length}/${cases.length} passed`,
      { timeout: 120_000 },
    );
    // A wrong expectation is one red case.
    const first = panel.getByTestId("mcp-calibration-case").first();
    await first
      .getByTestId("mcp-calibration-case-value")
      .fill('"total_count": 9');
    await page.getByTestId("mcp-calibrate-btn").click();
    await expect(panel.getByTestId("mcp-calibration-result")).toContainText(
      `Pass rate 83% — ${cases.length - 1}/${cases.length} passed`,
      { timeout: 120_000 },
    );
    await expect(first.getByTestId("mcp-calibration-case-verdict")).toHaveText(
      "fail",
    );
    await first
      .getByTestId("mcp-calibration-case-value")
      .fill(String(cases[0]!.assertion.value));
    await page.getByTestId("mcp-calibrate-btn").click();
    await expect(panel.getByTestId("mcp-calibration-result")).toContainText(
      "Pass rate 100%",
      { timeout: 120_000 },
    );

    // ---- 8. The write waits for a second person, in a workflow -----------------
    await page.goto("/caliber/mcp-servers");
    const serverId = (
      (await page
        .locator('[data-testid^="mcp-row-"]')
        .filter({ hasText: serverName })
        .first()
        .getAttribute("data-testid")) ?? ""
    ).replace("mcp-row-", "");
    expect(serverId).toMatch(/^MCP-/);
    const workflowName = `mcp-file-issue${suffix}`;
    await page.goto("/caliber/workflows");
    await page.getByTestId("import-workflow").click();
    await page.getByTestId("workflow-import-name").fill(workflowName);
    // The asset is uploaded unchanged; the four placeholders are filled in the dialog's manifest box.
    await page
      .locator('input[type="file"]')
      .setInputFiles(
        path.join(LAB, "assets/workflow/file-issue.workflow.yaml"),
      );
    const manifestBox = page.getByTestId("workflow-import-manifest");
    await expect(manifestBox).toHaveValue(/__SERVER_ID__/);
    await manifestBox.fill(
      (await manifestBox.inputValue())
        .replaceAll("__SERVER_ID__", serverId)
        .replaceAll("__TOOL__", "issue_write")
        .replaceAll("__LEVEL__", "write")
        .replaceAll("__APPROVAL__", "true"),
    );
    await expect(manifestBox).toHaveValue(/mcp_tool/);
    await page.getByTestId("workflow-import-validate").click();
    const preview = page.getByTestId("workflow-import-preview");
    await expect(preview).toContainText("Preflight passed");
    await expect(preview).toContainText(`${serverId}/issue_write`);
    await page.getByTestId("workflow-import-submit").click();
    await expect(page).toHaveURL(
      /\/caliber\/workflows\/WF-[^/]+\/editor\/WFV-/,
    );
    const editorUrl = page.url();

    await page.getByTestId("editor-run-monitor").click();
    await expect(page.getByTestId("run-monitor-panel")).toBeVisible();
    await page.getByTestId("run-input").fill(
      JSON.stringify({
        repo: "acme/release-notes-bot",
        title: "Release notes bot posts twice on re-run",
        body: "Filed by a workflow, after a second person approved it.",
        labels: ["bug"],
      }),
    );
    await page.getByTestId("run-execute").click();
    const link = page.getByTestId("run-active-open-link");
    await expect(link).toBeVisible({ timeout: 60_000 });
    const href = (await link.getAttribute("href")) ?? "";
    await expectRunStatus(page, "waiting_approval", { timeout: 120_000 });
    expect(executed("issue_write")).toBe(1);

    await page.getByTestId("run-approve").click();
    await expect(page.getByText(/a second person must decide/)).toBeVisible();

    const runId = href.split("/").pop() as string;
    const approverPage = await signedIn(browser, APPROVER, workspace);
    await approverPage.goto(editorUrl);
    await approverPage.getByTestId("editor-run-monitor").click();
    await approverPage.getByTestId(`run-history-select-${runId}`).click();
    await approverPage.getByTestId("run-approve").click();
    await expect(approverPage.getByTestId("run-resume")).toBeDisabled();
    expect(executed("issue_write")).toBe(1);

    await expect(page.getByTestId("run-resume")).toBeEnabled({
      timeout: 60_000,
    });
    await page.getByTestId("run-resume").click();
    await expectRunStatus(page, "completed", { timeout: 120_000 });
    await expect(page.getByTestId("run-output")).toContainText("created");
    expect(executed("issue_write")).toBe(2);

    // ---- 8b. Version the workflow: publish v1, loosen the binding in v2, meet the policy gate ---
    await publishFromEditor(page);
    await expect(page.getByText("v1 · published")).toBeVisible();
    await page.getByTestId("editor-restore-as-draft").click();
    await expect(page.getByText("v2 · draft")).toBeVisible();
    // The status chips of the version we left are gone, and no toolbar button was ever pushed
    // out of the 1280px window by them (the bar wraps).
    await expect(page.getByTestId("editor-message")).toHaveCount(0);
    await expect(page.getByTestId("editor-run-monitor")).toBeInViewport();
    await expect(page.getByTestId("editor-publish")).toBeInViewport();
    // The run monitor covers the canvas while it is open: close it from its own header.
    await page.getByTestId("run-monitor-close").click();
    await expect(page.getByTestId("run-monitor-panel")).toHaveCount(0);
    await page.getByTestId("outline-call_tool").click();
    // The node points at where the binding's approval switch is; follow it: an empty spot on the
    // canvas opens the workflow settings, and Tool bindings is in them.
    await expect(page.getByTestId("inspector-tool-binding-hint")).toBeVisible();
    await page.locator(".react-flow__pane").click({ position: { x: 30, y: 30 } });
    const bindingApproval = page.getByTestId(
      "workflow-tool-binding-approval-issue-write",
    );
    await expect(bindingApproval).toBeChecked();
    await bindingApproval.uncheck();
    await page.getByTestId("editor-save").click();
    await expect(page.getByTestId("editor-save-status")).toContainText("Saved");
    await page.getByTestId("editor-run-monitor").click();
    await page.getByTestId("run-input").fill(
      JSON.stringify({
        repo: "acme/release-notes-bot",
        title: "This must never be filed",
      }),
    );
    await page.getByTestId("run-execute").click();
    await expect(page.getByTestId("editor-run-message")).toContainText(
      "tool policy requires approval but the workflow binding does not",
      { timeout: 60_000 },
    );
    expect(executed("issue_write")).toBe(2);

    // ---- 9. Servers CALIBER will not run ------------------------------------
    await page.goto("/caliber/mcp-servers");
    await page.getByTestId("add-server-btn").click();
    const rogue = `rogue-shell${suffix}`;
    await page.getByTestId("server-name-input").fill(rogue);
    await page.getByTestId("server-command-input").fill("/bin/sh");
    await page
      .getByPlaceholder("e.g. -y @modelcontextprotocol/server-filesystem /data")
      .fill("-c echo pwned");
    await page.getByTestId("server-submit-btn").click();
    const rogueRow = page
      .locator('[data-testid^="mcp-row-"]')
      .filter({ hasText: rogue })
      .first();
    await expect(rogueRow).toContainText("Runtime blocked");
    await rogueRow.getByRole("button", { name: "Test" }).click();
    await expect(rogueRow).toContainText(
      "CALIBER_MCP_STDIO_COMMAND_ALLOWLIST",
      {
        timeout: 30_000,
      },
    );

    // ---- 10. The audit log ----------------------------------------------------
    await page.goto("/caliber/audit-log");
    await page.getByPlaceholder("approve").fill("update_mcp_tool_policy");
    await expect(
      page.getByText("update_mcp_tool_policy").first(),
    ).toBeVisible();
    await page.getByPlaceholder("approve").fill("calibrate_mcp_tool");
    await expect(page.getByText("calibrate_mcp_tool").first()).toBeVisible();

    // The traces: one per call that passed policy, none for the blocked tool.
    await page.goto("/caliber/observability");
    await page.getByPlaceholder("Search traces…").fill(`mcp.${serverName}.`);
    await expect(
      page
        .getByTestId("observability-trace-row")
        .filter({ hasText: `mcp.${serverName}.search_repositories` })
        .first(),
    ).toBeVisible();
    await expect(
      page
        .getByTestId("observability-trace-row")
        .filter({ hasText: `mcp.${serverName}.issue_write` })
        .first(),
    ).toBeVisible();
    await expect(
      page
        .getByTestId("observability-trace-row")
        .filter({ hasText: `mcp.${serverName}.create_release` }),
    ).toHaveCount(0);

    // ---- Failure: a viewer cannot test, classify or invoke ----------------------
    const viewerPage = await signedIn(browser, VIEWER, workspace);
    await openPlayground(viewerPage, serverName);
    await viewerPage.getByRole("button", { name: "Test Connection" }).click();
    await expect(viewerPage.getByText(/Connection Failed/)).toBeVisible({
      timeout: 30_000,
    });
    await expect(
      viewerPage.getByText(/caliber\.admin|scope|forbidden/i).first(),
    ).toBeVisible();
  });
});
