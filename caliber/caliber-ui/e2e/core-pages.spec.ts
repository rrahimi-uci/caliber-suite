import { expect, test } from "@playwright/test";

import { createPromptViaApi, signIn, uniqueSlug } from "./helpers";

test.describe("Core UI Workflows", () => {
  test("prompts surface supports tabbed workflows", async ({ page }) => {
    await signIn(page);
    // Seed a prompt via the API, then exercise its Workspace stages (the Prompts
    // page is now a Library landing → per-prompt Workspace with stage tabs).
    const { name: promptName } = await createPromptViaApi(page, {
      template: "You are a calm support agent. Resolve the escalation and cite context.",
    });

    await page.goto("/caliber/prompts");
    await expect(page.getByRole("heading", { name: "Prompts" })).toBeVisible();

    await page.getByLabel("Search prompts").fill(promptName);
    const card = page
      .locator('[data-testid^="prompt-card-"]')
      .filter({ hasText: promptName })
      .first();
    await expect(card).toBeVisible();
    await card.getByRole("button", { name: "Open" }).click();

    // The per-prompt Workspace exposes its full set of stage tabs, and each
    // stays reachable — this is the "tabbed workflows" the surface provides.
    for (const stage of ["Author", "Playground", "Test Sets", "Runs", "Calibration", "Bind"]) {
      await expect(page.getByRole("button", { name: stage, exact: true })).toBeVisible();
    }
    await page.getByRole("button", { name: "Calibration", exact: true }).click();
    await page.getByRole("button", { name: "Author", exact: true }).click();
  });

  test("tool workspace opens with stage tabs and a runnable sandbox", async ({ page }) => {
    await signIn(page);
    await page.goto("/caliber/tools");
    await expect(
      page.getByRole("heading", { name: "Tools" }),
    ).toBeVisible();

    // Open the first tool into its Workspace (Library landing → per-tool
    // Workspace). End-to-end run-with-result is covered by build-workflows; here
    // we assert the workspace opens with its six stages and a runnable Sandbox.
    await page.getByRole("button", { name: "Open" }).first().click();
    await expect(page.getByTestId("tool-workspace-header")).toBeVisible();
    for (const stage of ["Spec", "Sandbox", "Fixtures", "Test Runs", "Hardening", "Publish"]) {
      await expect(page.getByRole("button", { name: stage, exact: true })).toBeVisible();
    }

    await page.getByRole("button", { name: "Sandbox" }).click();
    await expect(page.getByRole("heading", { name: "Input Signature" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Test Run", exact: true })).toBeVisible();
  });

  test("workflow creation from template reaches editor and run monitor", async ({
    page,
  }) => {
    await signIn(page);
    await page.goto("/caliber/workflows");
    await expect(
      page.getByRole("heading", { name: "Workflows" }),
    ).toBeVisible();

    const workflowName = uniqueSlug("playwright-workflow");
    await page.getByTestId("new-workflow").click();
    await page.getByTestId("new-workflow-name").fill(workflowName);
    await page.getByTestId("template-single_agent").click();

    await expect(page).toHaveURL(/\/caliber\/workflows\/[^/]+\/editor\/[^/]+$/);
    await expect(page.getByTestId("workflow-editor")).toBeVisible();
    await expect(page.getByTestId("outline-search")).toBeVisible();
    await page.getByTestId("outline-search").fill("agent");
    await expect(page.getByTestId("outline-agent")).toBeVisible();
    await page.getByTestId("editor-run-monitor").click();
    await expect(page.getByTestId("run-monitor-panel")).toBeVisible();
    await expect(page.getByTestId("run-history-list")).toContainText(
      "Recent Runs",
    );
    await page.getByTestId("editor-auto-layout").click();
    await expect(page.getByTestId("editor-message")).toContainText(
      "Canvas layout reset",
    );

  });
});
