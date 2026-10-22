import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { expect, test, type Browser, type Page } from "@playwright/test";

import { CaliberUi } from "../ui-only-driver";
import { expectRunStatus } from "../../helpers";

/**
 * Cookbook 19, the UI path: import an OpenAPI spec, curate a read-only tool pack and a governed
 * write draft, publish both, and gate the write behind a second person in a workflow.
 *
 * Follows docs-site/cookbooks/19-openapi-integration-governance-lab/README.md ("Recipe: run it
 * through the UI"). Needs a stack started as that README describes
 * (scripts/cookbook-env.sh up lab19 --flags integrations, with CALIBER_EGRESS_ALLOWED_HOSTS naming
 * the upstream's loopback host, OPENAPI_TICKET_TOKEN set in both the upstream and CALIBER, and the
 * lab's accounts as platform scopes) and the lab's upstream (assets/server/ticket_api.py) already
 * running. The upstream writes its own request log that this spec reads from disk: TICKET_API_LEDGER,
 * else $CALIBER_STATE_DIR/lab19-ticket-ledger.jsonl, else the OS temp directory.
 *
 * This spec's own "reimport and read the diff" step calls the upstream's /admin/bump-spec, which
 * permanently switches that long-lived process to spec v2 (a new required body field, a deprecated
 * operation). A preview earlier in a later run would then fail schema validation against v2's
 * required 'priority' field. Restart the upstream between runs of this spec to get a clean v1 start.
 */
const UPSTREAM_PORT = Number(process.env["OPENAPI_TICKET_PORT"] ?? 8781);
const UPSTREAM_URL = `http://127.0.0.1:${UPSTREAM_PORT}`;
const REVIEWER = {
  user: "lab19-reviewer",
  password: "lab19-reviewer-password",
};
const VIEWER = { user: "lab19-viewer", password: "lab19-viewer-password" };
const OPERATOR = {
  user: "lab19-operator",
  password: "lab19-operator-password",
};
const LEDGER =
  process.env["TICKET_API_LEDGER"] ??
  path.join(
    process.env["CALIBER_STATE_DIR"] ?? os.tmpdir(),
    "lab19-ticket-ledger.jsonl",
  );

function ledgerCount(method: string, prefix: string): number {
  if (!fs.existsSync(LEDGER)) return 0;
  return fs
    .readFileSync(LEDGER, "utf-8")
    .split("\n")
    .filter((line) => line.trim())
    .map(
      (line) =>
        JSON.parse(line) as { method: string; path: string; ok: boolean },
    )
    .filter((e) => e.method === method && e.path.startsWith(prefix) && e.ok)
    .length;
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

test.describe("Cookbook 19: governed OpenAPI tool (UI)", () => {
  test("imports, curates, publishes and gates a write tool", async ({
    page,
    browser,
  }) => {
    test.setTimeout(900_000);
    const suffix = `_${Date.now().toString(36)}`;
    const workspace = `openapi-ops${suffix}`;
    const integrationName = `Ticketing${suffix}`;
    const ui = new CaliberUi(page);
    const api = "/ajax-api/2.0/mlflow/caliber";

    // ---- Setup: people and a workspace (same pattern as cookbook 05) --------
    await ui.signIn();
    await page.goto("/caliber/administration");
    await expect(
      page.getByRole("heading", { name: "Administration" }),
    ).toBeVisible();
    for (const person of [OPERATOR, REVIEWER, VIEWER]) {
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

    const projectsResponse = await page.request.get(`${api}/projects`);
    const projects = (
      (await projectsResponse.json()) as {
        data: { project_id: string; name: string }[];
      }
    ).data;
    const projectId =
      projects.find((p) => p.name === workspace)?.project_id ?? "";
    expect(projectId).toMatch(/^PRJ-/);
    const projectHeader = { "X-CALIBER-Project": projectId };

    await page.goto("/caliber/administration");
    await page
      .getByLabel("Project for access management")
      .selectOption({ label: `${workspace} (owner)` });
    for (const [person, role] of [
      [OPERATOR.user, "Editor"],
      [REVIEWER.user, "Reviewer"],
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

    // ---- 1. Create the integration -------------------------------------------
    await page.goto("/caliber/openapi-integrations");
    await expect(
      page.getByRole("heading", { name: "OpenAPI Integrations" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "+ New Integration" }).click();
    await page.getByPlaceholder("Ticketing API").fill(integrationName);
    await page
      .getByPlaceholder("External ticket API")
      .fill("The lab's stand-in Ticket API");
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: new RegExp(integrationName) }),
    ).toBeVisible();

    const integrationsResponse = await page.request.get(
      `${api}/openapi-integrations?status=all`,
      {
        headers: projectHeader,
      },
    );
    const integrations = (
      (await integrationsResponse.json()) as {
        data: { integration_id: string; name: string }[];
      }
    ).data;
    const integrationId =
      integrations.find((i) => i.name === integrationName)?.integration_id ??
      "";
    expect(integrationId).toMatch(/^OAI-/);

    // The UI's Publish button always uses the draft's own stored name with no rename field, and a
    // tool (name, version) pair is permanently unique once registered -- even an archived one still
    // blocks it. Rename each draft to something unique via the API (update_tool_draft supports it;
    // the UI just does not expose a field for it) right after generating it, before opening it in
    // the UI, so the Publish button this spec actually clicks works the same way every run.
    async function renameLatestDraft(
      autoName: string,
      uniqueName: string,
    ): Promise<void> {
      const draftsResponse = await page.request.get(
        `${api}/openapi-integrations/${integrationId}/tool-drafts`,
        { headers: projectHeader },
      );
      const drafts = (
        (await draftsResponse.json()) as {
          data: { draft_id: string; name: string }[];
        }
      ).data;
      const draft = drafts.find((d) => d.name === autoName);
      expect(
        draft,
        `expected a freshly generated draft named "${autoName}"`,
      ).toBeTruthy();
      await page.request.patch(
        `${api}/openapi-integrations/${integrationId}/tool-drafts/${draft!.draft_id}`,
        { headers: projectHeader, data: { name: uniqueName } },
      );
    }

    // ---- 2. Import the spec from the upstream's own URL ----------------------
    await page.getByRole("button", { name: "URL" }).click();
    await page
      .getByPlaceholder("https://example.com/openapi.json")
      .fill(`${UPSTREAM_URL}/openapi.yaml`);
    await page.getByRole("button", { name: "Check" }).click();
    await expect(page.getByText(/Reachable \(200\)/)).toBeVisible({
      timeout: 30_000,
    });
    await page.getByTestId("import-submit").click();
    await expect(page.getByText(/Imported OAIV-/)).toBeVisible({
      timeout: 30_000,
    });

    // ---- 3. Curate a read-only tool pack --------------------------------------
    await page.getByRole("button", { name: "Operations", exact: true }).click();
    await expect(page.getByText("GET /tickets/{ticket_id}")).toBeVisible();
    const checkboxes = page.getByRole("checkbox");
    await checkboxes.nth(0).check();
    await checkboxes.nth(1).check();
    await page.getByLabel("bind as one tool pack").check();
    await page.getByRole("button", { name: "Generate tool draft(s)" }).click();
    await expect(page.getByText(/Generated 1 tool draft/)).toBeVisible();
    const packDraftName = `tickets_pack${suffix}`;
    await renameLatestDraft("tickets_pack", packDraftName);

    // ---- 4. Review dependencies: the explicit link is auto-wired -------------
    await page
      .getByRole("button", { name: "Dependencies", exact: true })
      .click();
    await expect(page.getByText("Auto-wired").first()).toBeVisible();
    const reviewable = page.getByRole("button", { name: "Confirm" });
    if (await reviewable.count()) {
      await reviewable.first().click();
    }

    // ---- 5. Publish the pack ---------------------------------------------------
    await page
      .getByRole("button", { name: "Tool Drafts", exact: true })
      .click();
    const packDraftRow = page
      .getByText(packDraftName, { exact: false })
      .first();
    await packDraftRow.click();
    // The spec's declared server (https://tickets.example.com) is a documentation placeholder;
    // point the draft at the lab's real upstream before anything tries to call it.
    await page.getByLabel("Server URL").fill(UPSTREAM_URL);
    await page
      .getByRole("combobox", { name: "Auth kind" })
      .selectOption("bearer");
    await page
      .getByPlaceholder("env://MY_TOKEN or secret://name")
      .fill("env://OPENAPI_TICKET_TOKEN");
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByText("Saved.")).toBeVisible();
    await page.getByRole("button", { name: "Publish", exact: true }).click();
    await expect(page.getByTestId("openapi-publication-success")).toContainText(
      "Tool published successfully",
    );

    // ---- 6. Curate the write draft (no pack) -----------------------------------
    await page.getByRole("button", { name: "Operations", exact: true }).click();
    const writeCheckbox = page
      .getByRole("row")
      .filter({ hasText: "POST /tickets" })
      .getByRole("checkbox");
    await writeCheckbox.check();
    await page.getByRole("button", { name: "Generate tool draft(s)" }).click();
    await expect(page.getByText(/Generated 1 tool draft/)).toBeVisible();
    const writeDraftName = `create_ticket${suffix}`;
    await renameLatestDraft("create_ticket", writeDraftName);
    await page
      .getByRole("button", { name: "Tool Drafts", exact: true })
      .click();
    const writeDraftRow = page
      .getByText(writeDraftName, { exact: false })
      .first();
    await writeDraftRow.click();
    await page.getByLabel("Server URL").fill(UPSTREAM_URL);
    await page
      .getByRole("combobox", { name: "Auth kind" })
      .selectOption("bearer");
    await page
      .getByPlaceholder("env://MY_TOKEN or secret://name")
      .fill("env://OPENAPI_TICKET_TOKEN");
    await page.getByRole("button", { name: "Save" }).click();
    await expect(page.getByText("Saved.")).toBeVisible();

    // ---- 7. Meet the preview gate, then break-glass it once --------------------
    // Until allow_in_preview is set, the whole preview control is withheld, not merely disabled.
    await expect(page.getByRole("button", { name: "Run preview" })).toHaveCount(
      0,
    );
    const before = ledgerCount("POST", "/tickets");
    // The checkbox is controlled by server state (an async round trip), so click + a retrying
    // assertion is used instead of check()/uncheck(), which verify synchronously right after the click.
    const previewToggle = page.getByLabel(
      "allow live preview (fires a real request)",
    );
    await previewToggle.click();
    await expect(previewToggle).toBeChecked();
    const previewInput = page.locator("textarea");
    await previewInput.fill(
      JSON.stringify({ body: { title: "Filed from the UI" } }),
    );
    await page.getByRole("button", { name: "Run preview" }).click();
    await expect(page.getByText(/"ticket_id"/)).toBeVisible({
      timeout: 30_000,
    });
    expect(ledgerCount("POST", "/tickets")).toBe(before + 1);
    await previewToggle.click();
    await expect(previewToggle).not.toBeChecked();
    await expect(page.getByRole("button", { name: "Run preview" })).toHaveCount(
      0,
    );

    // ---- 8. Publish the write draft ---------------------------------------------
    await page.getByRole("button", { name: "Publish", exact: true }).click();
    const publicationSuccess = page.getByTestId("openapi-publication-success");
    await expect(publicationSuccess).toContainText(
      "Tool published successfully",
    );
    const writeToolHref =
      (await page
        .getByRole("link", { name: "Open in Tool Registry" })
        .getAttribute("href")) ?? "";
    const writeToolId = writeToolHref.split("/").pop() ?? "";
    expect(writeToolId).toMatch(/^TL-/);

    // ---- 9. Gate the write behind a second person in a workflow -----------------
    // The draft was renamed to a unique value before publish, and publish's name fallback is
    // `payload.name or draft.name` with no override sent -- so the registered tool's name is
    // exactly that renamed value.
    const writeToolName = writeDraftName;

    const workflowName = `file-ticket${suffix}`;
    const workflowYaml = `schema_version: 1
workflow_id: ${workflowName}
name: ${workflowName}
description: Cookbook 19 UI run.
runtime:
  sdk: openai-agents-python
  sdk_version_policy: runtime-pinned
  compiler_version: caliber-workflow-compiler-v1
  default_model_ref: CALIBER_WORKFLOW_DEFAULT_MODEL
  session:
    type: none
nodes:
  start:
    id: start
    type: start
    outputs:
      user_message:
        type: string
  call_tool:
    id: call_tool
    type: tool
    tool_name: ${writeToolName}
    inputs:
      input:
        type: string
      arguments:
        type: structured
    outputs:
      text:
        type: string
      result:
        type: structured
      metadata:
        type: structured
  done:
    id: done
    type: output
    inputs:
      response:
        type: string
edges:
  - id: e_start_call
    from: start
    to: call_tool
    map:
      user_message: input
  - id: e_call_done
    from: call_tool
    to: done
    map:
      text: response
tools:
  ${writeToolName}:
    registry_ref: tool.${writeToolName}.v1
    version_constraint: ">=1,<2"
`;
    await page.goto("/caliber/workflows");
    await page.getByTestId("import-workflow").click();
    await page.getByTestId("workflow-import-name").fill(workflowName);
    const manifestBox = page.getByTestId("workflow-import-manifest");
    await manifestBox.fill(workflowYaml);
    await page.getByTestId("workflow-import-validate").click();
    await expect(page.getByTestId("workflow-import-preview")).toContainText(
      "Preflight passed",
    );
    await page.getByTestId("workflow-import-submit").click();
    await expect(page).toHaveURL(
      /\/caliber\/workflows\/WF-[^/]+\/editor\/WFV-/,
    );
    const editorUrl = page.url();

    await page.getByTestId("editor-run-monitor").click();
    await expect(page.getByTestId("run-monitor-panel")).toBeVisible();
    await page.getByTestId("run-input").fill(
      JSON.stringify({
        body: { title: "Filed by a workflow, after approval" },
      }),
    );
    await page.getByTestId("run-execute").click();
    const link = page.getByTestId("run-active-open-link");
    await expect(link).toBeVisible({ timeout: 60_000 });
    const href = (await link.getAttribute("href")) ?? "";
    await expectRunStatus(page, "waiting_approval", { timeout: 120_000 });
    expect(ledgerCount("POST", "/tickets")).toBe(before + 1);

    await page.getByTestId("run-approve").click();
    await expect(page.getByText(/a second person must decide/)).toBeVisible();

    const runId = href.split("/").pop() as string;
    const reviewerPage = await signedIn(browser, REVIEWER, workspace);
    await reviewerPage.goto(editorUrl);
    await reviewerPage.getByTestId("editor-run-monitor").click();
    await reviewerPage.getByTestId(`run-history-select-${runId}`).click();
    await reviewerPage.getByTestId("run-approve").click();
    await expect(reviewerPage.getByTestId("run-resume")).toBeDisabled();
    await reviewerPage.close();

    await expect(page.getByTestId("run-resume")).toBeEnabled({
      timeout: 60_000,
    });
    await page.getByTestId("run-resume").click();
    await expectRunStatus(page, "completed", { timeout: 120_000 });
    expect(ledgerCount("POST", "/tickets")).toBe(before + 2);

    // ---- 10. Reimport after the upstream's spec changes and read the diff -------
    await page.goto("/caliber/openapi-integrations");
    await page.getByText(integrationName).click();
    const bumpResponse = await page.request.post(
      `${UPSTREAM_URL}/admin/bump-spec`,
    );
    expect(bumpResponse.ok()).toBeTruthy();
    await page.getByRole("button", { name: "Reimport & diff" }).click();
    await expect(page.getByText(/changed 2 \(1 breaking\)/)).toBeVisible({
      timeout: 30_000,
    });

    // As a viewer: the create action is unavailable.
    const viewerPage = await signedIn(browser, VIEWER, workspace);
    await viewerPage.goto("/caliber/openapi-integrations");
    await expect(
      viewerPage.getByRole("button", { name: "+ New Integration" }),
    ).toHaveCount(0);
    await viewerPage.close();
  });
});
