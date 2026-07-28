import { expect, test } from "@playwright/test";

import { createPromptViaApi, expectRunStatus, signIn, uniqueSlug } from "./helpers";

test.describe("Build Section Advanced Journeys", () => {
  test("workflow editor run monitor enforces approval-first resume and supports reject-retry-resume recovery", async ({
    page,
  }) => {
    // This drives the hitl_review workflow to waiting_approval three times
    // (run → reject → retry → approve → resume), so it needs a larger budget
    // than the per-run timeouts sum to under a single-backend test run.
    test.setTimeout(360_000);

    await signIn(page);

    const workflowName = uniqueSlug("pw-hitl-monitor");

    await page.goto("/caliber/workflows");
    await expect(page.getByRole("heading", { name: "Workflows" })).toBeVisible();

    await page.getByTestId("new-workflow").click();
    await page.getByTestId("new-workflow-name").fill(workflowName);
    await page.getByTestId("template-hitl_review").click();

    await expect(page).toHaveURL(/\/caliber\/workflows\/[^/]+\/editor\/[^/]+$/);
    await expect(page.getByTestId("workflow-editor")).toBeVisible();

    await page.getByTestId("editor-run-monitor").click();
    await expect(page.getByTestId("run-monitor-panel")).toBeVisible();

    await page
      .getByTestId("run-input")
      .fill("Customer email is ada@example.com. Summarize the escalation and wait for human review.");
    await page.getByTestId("run-execute").click();

    await expectRunStatus(page, "waiting_approval", {
      timeout: 120_000,
      intervals: [1_000, 2_000, 5_000],
    });

    await expect(page.getByTestId("run-approval-actions")).toContainText("Approve to unlock Resume");
    await expect(page.getByTestId("run-resume")).toBeDisabled();
    await expect(page.getByTestId("workflow-run-recovery-panel")).toContainText("Awaiting approval");
    await expect(page.getByTestId("workflow-run-checkpoint-panel")).toBeVisible();
    await expect(page.getByTestId("workflow-run-debugger")).toBeVisible();

    await page.getByTestId("run-reject").click();

    await expectRunStatus(page, "failed", {
      timeout: 30_000,
      intervals: [500, 1_000, 2_000],
    });
    await expect(page.getByTestId("run-retry")).toBeEnabled();

    // The run-monitor's sticky header overlaps the run-control buttons' hit-test
    // point, so a real (or even forced) click lands on the header. Dispatch the
    // click straight to the enabled button instead.
    await page.getByTestId("run-retry").dispatchEvent("click");
    await expectRunStatus(page, "waiting_approval", {
      timeout: 120_000,
      intervals: [1_000, 2_000, 5_000],
    });

    await expect(page.getByTestId("workflow-run-lineage-panel")).toContainText("Attempt 2 of 2");
    await expect(page.getByTestId("run-resume")).toBeDisabled();

    await page.getByTestId("run-approve").dispatchEvent("click");
    await expect
      .poll(
        async () => page.getByTestId("run-resume").isEnabled(),
        { timeout: 30_000, intervals: [500, 1_000, 2_000] },
      )
      .toBe(true);

    await page.getByTestId("run-resume").dispatchEvent("click");

    await expectRunStatus(page, "completed", {
      timeout: 120_000,
      intervals: [1_000, 2_000, 5_000],
    });

    await expect(page.getByTestId("run-output")).not.toContainText("No output yet.");
    await expect(page.getByTestId("workflow-run-lineage-panel")).toContainText("Attempt 2 of 2");
  });

  test("prompt management persists edited versions and exposes prompt history", async ({
    page,
  }) => {
    await signIn(page);

    const initialSnippet = "Initial playbook guidance for customer escalations.";
    const updatedSnippet = "Revised playbook guidance for customer escalations.";

    // Seed a prompt via the API (agent registration no longer creates a prompt),
    // then drive the card's Edit → Save-as-new-version → version-history flow.
    const { name: promptName } = await createPromptViaApi(page, {
      template: `You are a calm support agent. ${initialSnippet}`,
    });

    await page.goto("/caliber/prompts");
    await expect(page.getByRole("heading", { name: "Prompts" })).toBeVisible();

    await page.getByLabel("Search prompts").fill(promptName);
    const promptCard = page
      .locator('[data-testid^="prompt-card-"]')
      .filter({ hasText: promptName })
      .first();
    await expect(promptCard).toBeVisible();

    await promptCard.getByRole("button", { name: "Edit" }).click();
    await expect(page.getByRole("heading", { name: /Edit Prompt:/ })).toBeVisible();

    const editTemplate = page.locator("form textarea").first();
    await editTemplate.fill(`You are a calm support agent. ${updatedSnippet}`);
    await page.getByPlaceholder("Updated prompt").fill("Playwright prompt revision");
    await page.getByRole("button", { name: "Save as New Version" }).click();

    await expect(page.getByRole("heading", { name: /Edit Prompt:/ })).toHaveCount(0);

    const updatedPromptCard = page
      .locator('[data-testid^="prompt-card-"]')
      .filter({ hasText: promptName })
      .first();
    await expect(updatedPromptCard).toBeVisible();

    await updatedPromptCard.getByRole("button", { name: "Versions" }).click();
    await expect(page.getByText(/Versions:/)).toBeVisible();
    await expect(page.getByText("Playwright prompt revision")).toBeVisible();
  });

  test("workflow list supports create, rename, search, and delete transitions", async ({
    page,
  }) => {
    await signIn(page);

    const workflowName = uniqueSlug("pw-advanced-workflow");
    const renamedWorkflowName = `${workflowName}-renamed`;

    await page.goto("/caliber/workflows");
    await expect(page.getByRole("heading", { name: "Workflows" })).toBeVisible();

    await page.getByTestId("new-workflow").click();
    await page.getByTestId("new-workflow-name").fill(workflowName);
    await page.getByTestId("template-blank").click();

    await expect(page).toHaveURL(/\/caliber\/workflows\/[^/]+\/editor\/[^/]+$/);
    await expect(page.getByTestId("workflow-editor")).toBeVisible();

    await page.goto("/caliber/workflows");
    await page.getByPlaceholder("Search workflows…").fill(workflowName);

    const workflowCard = page
      .locator('[data-testid^="workflow-card-"]')
      .filter({ hasText: workflowName })
      .first();
    await expect(workflowCard).toBeVisible();

    await workflowCard.getByTitle("Rename workflow").click();
    const renameInput = page.getByPlaceholder("Workflow name");
    await expect(renameInput).toHaveValue(workflowName);
    await renameInput.fill(renamedWorkflowName);
    await renameInput.press("Enter");

    await page.getByPlaceholder("Search workflows…").fill(renamedWorkflowName);
    const renamedCard = page
      .locator('[data-testid^="workflow-card-"]')
      .filter({ hasText: renamedWorkflowName })
      .first();
    await expect(renamedCard).toBeVisible();

    await renamedCard.getByTitle("Delete workflow").click();
    await expect(page.getByRole("dialog", { name: "Delete Workflow" })).toBeVisible();
    await page.getByTestId("confirm-delete").click();

    await expect(renamedCard).toHaveCount(0);
    await expect(page.getByTestId("workflows-empty")).toContainText(/No workflows (yet|match your filters)/);
  });

  test("mcp quick connect registers a server and surfaces playground failure states cleanly", async ({
    page,
  }) => {
    await signIn(page);

    const serverName = `Playwright GitHub MCP ${Date.now()}`;

    await page.goto("/caliber/mcp-servers");
    await expect(page.getByRole("heading", { name: "MCP Servers" })).toBeVisible();

    await page.getByTestId("catalog-github").click();
    await expect(page.getByTestId("add-server-dialog")).toBeVisible();
    await page.getByTestId("server-name-input").fill(serverName);
    await page.getByTestId("server-submit-btn").click();

    const serverRow = page
      .locator('[data-testid^="mcp-row-"]')
      .filter({ hasText: serverName })
      .first();
    await expect(serverRow).toBeVisible();
    await expect(serverRow).toContainText("create_issue");

    await serverRow.dblclick();
    await expect(page.getByTestId("mcp-detail-dialog")).toBeVisible();
    await expect(page.getByTestId("mcp-detail-dialog")).toContainText(serverName);
    await expect(page.getByText(/Discovered tools \(/)).toBeVisible();
    await page.getByRole("button", { name: "Close" }).click();
    await expect(page.getByTestId("mcp-detail-dialog")).toHaveCount(0);

    await serverRow.getByRole("button", { name: "Test" }).click();
    await expect(
      serverRow.getByRole("button", { name: "Test" }),
    ).toBeVisible({ timeout: 30000 });

    await page.getByRole("button", { name: "Playground" }).click();
    await page.getByLabel("Select MCP server").selectOption({ label: serverName });
    await expect(page.getByText("search_repositories")).toBeVisible();
    await page.getByRole("button").filter({ hasText: "search_repositories" }).click();
    await expect(page.getByText("Tool Policy")).toBeVisible();
    await page.getByRole("checkbox", { name: "Allow tool" }).check();
    await page.getByRole("button", { name: "Save Policy" }).click();
    await expect(page.getByText("Policy saved")).toBeVisible();
    await page.getByRole("button", { name: "Invoke Tool" }).click();
    await expect(
      page.getByRole("button", { name: "Invoke Tool" }),
    ).toBeVisible({ timeout: 20000 });
    await expect(page.getByText(/^Failed$/)).toBeVisible();
    await expect(
      page.getByText(
        /Invalid input|timed out|Request failed|CALIBER_MCP_STDIO_COMMAND_ALLOWLIST|server status is/,
      ),
    ).toBeVisible();
    await expect(page.getByText(/TaskGroup/)).toHaveCount(0);
  });
});
