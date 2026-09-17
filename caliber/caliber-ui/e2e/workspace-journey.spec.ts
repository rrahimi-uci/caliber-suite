import { expect, test } from "@playwright/test";

import { signIn, uniqueSlug } from "./helpers";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

test.describe("Workspace selection journey", () => {
  test("creates, scopes, and clears a workspace from the application shell", async ({ page }) => {
    await signIn(page);
    const workspaceName = uniqueSlug("playwright-workspace");
    let projectId: string | undefined;

    try {
      await page.getByRole("button", { name: "Create workspace" }).click();
      await page.getByLabel("Workspace name").fill(workspaceName);
      await page.getByRole("button", { name: "Create and select" }).click();

      const selector = page.getByLabel("Active workspace");
      await expect(selector.locator("option:checked")).toHaveText(workspaceName);
      projectId = await selector.inputValue();
      expect(projectId).toBeTruthy();

      const scopedRequest = page.waitForRequest(
        (request) =>
          request.method() === "GET" &&
          request.url().includes(`${API_BASE}/object-store/buckets`),
      );
      await page.goto("/caliber/object-store");
      await expect(page.getByRole("heading", { name: "Object Store" })).toBeVisible();
      expect((await scopedRequest).headers()["x-caliber-project"]).toBe(projectId);

      await selector.selectOption("");
      const unscopedRequest = page.waitForRequest(
        (request) =>
          request.method() === "GET" &&
          request.url().includes(`${API_BASE}/object-store/buckets`),
      );
      await page.reload();
      await expect(page.getByRole("heading", { name: "Object Store" })).toBeVisible();
      expect((await unscopedRequest).headers()["x-caliber-project"]).toBeUndefined();
    } finally {
      if (projectId) {
        const archived = await page.request.post(
          `${API_BASE}/projects/${encodeURIComponent(projectId)}/archive`,
        );
        expect([200, 204, 404]).toContain(archived.status());
      }
    }
  });
});
