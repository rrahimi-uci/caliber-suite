import { expect, test } from "@playwright/test";

test.describe("Docs Shell", () => {
  test("landing page exposes the shared search-first shell", async ({ page }) => {
    const consoleErrors: string[] = [];
    const pageErrors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text());
    });
    page.on("pageerror", (err) => pageErrors.push(String(err)));

    await page.goto("/caliber/docs/index.html");

    await expect(page.locator("#landingSearch")).toBeVisible();
    await expect(page.locator("#docsSidebar")).toBeHidden();
    await expect(page.locator("#menuToggle")).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Ask Aria" })).toHaveCount(0);
    await expect(page.locator("#reference .reference-group")).toHaveCount(8);
    await expect(
      page.locator(
        '#reference .ref-card[href="m-00-layered-architecture.html"]',
      ),
    ).toBeVisible();

    await page.locator("#landingSearch").fill("workflow");
    await expect(
      page.locator('#reference .ref-card[href="m-06-workflows.html"]'),
    ).toBeVisible();
    await expect(
      page.locator('#reference .ref-card[href="m-15-calibration.html"]'),
    ).toBeVisible();

    await expect(page.getByRole("heading", { name: "First success" })).toBeVisible();
    await expect(
      page.locator('#featured-references a[href="m-21-sdk-reference.html"]'),
    ).toBeVisible();

    expect(consoleErrors).toEqual([]);
    expect(pageErrors).toEqual([]);
  });

  test("layered architecture publishes the repository overview as a docs module", async ({
    page,
  }) => {
    const consoleErrors: string[] = [];
    const pageErrors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text());
    });
    page.on("pageerror", (err) => pageErrors.push(String(err)));

    await page.goto("/caliber/docs/m-00-layered-architecture.html");

    await expect(
      page.getByRole("heading", {
        name: "CALIBER — Layered Architecture",
        exact: true,
      }),
    ).toBeVisible();
    await expect(page.locator("#page-toc a")).toHaveCount(16);
    await expect(page.locator("pre.mermaid")).toHaveCount(8);
    await expect(
      page.locator(
        '.nav-link.active[href="m-00-layered-architecture.html"]',
      ),
    ).toHaveText("Layered architecture");
    await expect(page.locator(".doc-body")).not.toContainText(
      '<div align="center">',
    );

    expect(consoleErrors).toEqual([]);
    expect(pageErrors).toEqual([]);
  });

  test("prompts authoring page renders summary, toc, and copy-page action", async ({
    page,
  }) => {
    const consoleErrors: string[] = [];
    const pageErrors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text());
    });
    page.on("pageerror", (err) => pageErrors.push(String(err)));

    await page.goto("/caliber/docs/m-02-prompts.html");

    await expect(page.getByRole("heading", { name: "Prompts Architecture" })).toBeVisible();
    await expect(page.locator(".doc-summary")).toContainText(
      "MLflow Prompt Registry authoring",
    );
    await expect(page.locator(".topbar-links")).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Ask Aria" })).toHaveCount(0);
    await expect(page.locator(".doc-breadcrumb a")).toHaveCount(0);
    const tocLinks = page.locator("#page-toc a");
    await expect(tocLinks).toHaveCount(11);
    await expect(tocLinks.first()).toHaveText("At a glance");
    await expect(tocLinks.last()).toHaveText("9. Extension points and current constraints");

    const copyButton = page.locator(".doc-copy-button");
    await expect(copyButton).toHaveText("Copy page");
    await copyButton.click();
    await expect(copyButton).toHaveText("Copied");

    expect(consoleErrors).toEqual([]);
    expect(pageErrors).toEqual([]);
  });
});
