import { expect, test } from "@playwright/test";

import {
  createObjectStoreBucket,
  openObjectStoreBucket,
  signIn,
  uniqueSlug,
  uploadObjectStoreFixtures,
} from "./helpers";

// Note: the standalone "Agent Fleet" page (manual agent register / search /
// pause / resume) was removed in favour of the asset-workspace model, so the
// former agent-fleet journey test no longer applies.
test.describe("Build Section Journeys", () => {
  test("skill builder creates a reusable skill and renders it in the playground", async ({
    page,
  }) => {
    await signIn(page);

    const skillName = uniqueSlug("playwright-skill");
    await page.goto("/caliber/skills");
    await expect(page.getByRole("heading", { name: "Skills" })).toBeVisible();

    await page.getByTestId("new-skill").click();
    await expect(page.getByRole("heading", { name: "Build a new skill" })).toBeVisible();

    await page.getByTestId("skill-wiz-name").fill(skillName);
    await page.getByTestId("skill-wiz-owner").fill("@playwright");
    await page.getByTestId("skill-wiz-category-workflow_automation").click();
    await page.getByTestId("skill-wiz-tag-input").fill("playwright");
    await page.getByTestId("skill-wiz-add-tag").click();
    await page.getByTestId("skill-wizard-next").click();

    await page.getByTestId("skill-wiz-summary").fill("Playwright coverage skill");
    await page
      .getByTestId("skill-wiz-content")
      .fill("# Instructions\n\nSummarize the operating plan for {{team_name}}.");
    await page.getByTestId("skill-wizard-next").click();

    await page.getByTestId("skill-wiz-allowed-tools").fill("Bash(python:*)");
    await page.getByTestId("skill-wizard-next").click();

    await page.getByTestId("skill-wiz-trigger-input").fill("summarize team plan");
    await page.getByTestId("skill-wiz-add-trigger").click();
    await page.getByTestId("skill-wizard-next").click();

    await page.getByTestId("skill-wizard-submit").click();
    await expect(page.getByRole("heading", { name: "Skills" })).toBeVisible();

    await page.getByLabel("Search skills").fill(skillName);
    await expect(page.getByText(skillName)).toBeVisible();

    // Open the skill's Workspace and render it from the Render Preview stage
    // (the standalone Playground was replaced by per-skill Workspace stages).
    await page.getByRole("button", { name: "Open" }).first().click();
    await expect(page.getByTestId("skill-workspace-header")).toContainText(skillName);
    await page.getByRole("button", { name: "Render Preview" }).click();
    await page.getByTestId("skill-playground-variables").fill('{"team_name":"Platform"}');
    await page.getByTestId("skill-playground-render").click();

    await expect(page.getByText("Rendered Output")).toBeVisible();
    await expect(
      page.locator("pre").filter({ hasText: "Summarize the operating plan for Platform." }),
    ).toBeVisible();
  });

  test("tool registry supports registration, validation, preview execution, and archival", async ({
    page,
  }) => {
    await signIn(page);

    const toolName = uniqueSlug("playwright-tool");

    await page.goto("/caliber/tools");
    await expect(page.getByRole("heading", { name: "Tools" })).toBeVisible();

    await page.getByRole("button", { name: "Register Tool" }).click();
    await expect(page.getByTestId("tool-wizard")).toBeVisible();
    await expect(page.getByTestId("wizard-next")).toBeDisabled();

    await page.getByTestId("wiz-name").fill(toolName);
    await page.getByTestId("wiz-description").fill("Playwright coverage tool for the Build registry.");
    await page.getByTestId("wiz-owner").fill("@playwright");
    await expect(page.getByTestId("wizard-next")).toBeEnabled();
    await page.getByTestId("wizard-next").click();

    await page.getByTestId("wiz-module").fill("caliber.workflows.demo_tools");
    await page.getByTestId("wiz-callable").fill("lookup_order");
    await page.getByTestId("wizard-next").click();

    await page.getByTestId("input-schema-add-prop").click();
    const property = page.locator('[data-testid^="input-schema-prop-"]').first();
    await property.locator('[data-testid^="input-schema-prop-name-"]').fill("order_id");
    await property.locator('[data-testid^="input-schema-prop-req-"]').check();
    await page.getByTestId("wizard-next").click();

    await expect(page.getByText("Playground available after registration.")).toBeVisible();
    await page.getByTestId("wizard-next").click();

    await page.getByTestId("wiz-allow-preview").check();
    await page.getByTestId("wizard-submit").click();

    await expect(page).toHaveURL(/\/caliber\/tools\/[^/]+$/);
    await expect(page.getByTestId("tool-detail")).toContainText(toolName);
    await expect(page.getByTestId("tool-module")).toHaveText("caliber.workflows.demo_tools");
    await expect(page.getByTestId("tool-callable")).toHaveText("lookup_order");

    await page.getByTestId("tool-run-input").fill("[]");
    await page.getByTestId("tool-run").click();
    await expect(page.getByTestId("tool-run-input-error")).toContainText("Tool input must be a JSON object.");

    await page.getByTestId("tool-edit-description").fill("Updated during Playwright validation.");
    await page.getByTestId("tool-save").click();
    await expect(page.getByTestId("tool-save-status")).toContainText("Saved");

    await page.getByTestId("tool-run-input").fill('{"order_id":"A-100"}');
    await page.getByTestId("tool-run").click();
    await expect(page.getByTestId("tool-run-result")).toContainText('"order_id": "A-100"');
    await expect(page.getByTestId("tool-run-result")).toContainText('"status": "delivered"');

    await page.getByTestId("tool-archive").click();
    await expect(page.getByTestId("tool-edit-status")).toHaveValue("archived");
    await expect(page.getByRole("heading", { level: 1 })).toContainText("archived");
  });

  test("object store supports upload, preview, bulk delete, and bucket cleanup", async ({
    page,
  }) => {
    await signIn(page);

    const bucket = uniqueSlug("pw-objects");
    await createObjectStoreBucket(page, bucket);
    await openObjectStoreBucket(page, bucket);
    await uploadObjectStoreFixtures(page, bucket, [
      {
        name: "alpha.md",
        mimeType: "text/markdown",
        body: "# Alpha\n\nCaliber keeps build artifacts organized.",
      },
      {
        name: "beta.json",
        mimeType: "application/json",
        body: JSON.stringify({ owner: "playwright", kind: "object-store-check" }, null, 2),
      },
    ]);
    await page.reload();
    await openObjectStoreBucket(page, bucket);

    const alphaRow = page.getByTestId("object-alpha.md");
    await alphaRow.getByTitle("View preview").click();
    await expect(page.getByTestId("object-preview-modal")).toBeVisible();
    await page.getByRole("button", { name: "Close file preview" }).click();
    await expect(page.getByTestId("object-preview-modal")).toHaveCount(0);

    page.once("dialog", (dialog) => void dialog.accept());
    await page.getByTestId("select-all").check();
    await page.getByTestId("bulk-delete").click();
    await expect(alphaRow).toHaveCount(0);
    await expect(
      page.getByText("This folder is empty. Drag files here or use Upload."),
    ).toBeVisible();

    const bucketRow = page
      .locator("li.group")
      .filter({ has: page.getByTestId(`bucket-${bucket}`) });
    await bucketRow.hover();
    page.once("dialog", (dialog) => void dialog.accept());
    await bucketRow.getByTitle("Delete bucket").click();
    await expect(page.getByTestId(`bucket-${bucket}`)).toHaveCount(0);
  });
});
