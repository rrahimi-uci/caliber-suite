import { expect, test } from "@playwright/test";

import { signIn } from "./helpers";

test.describe("Auth And Shell", () => {
  test("redirects unauthenticated navigation to /login", async ({ page }) => {
    await page.goto("/caliber/tools");
    await expect(page).toHaveURL(/\/caliber\/login$/);
    await expect(
      page.getByRole("heading", { name: "Build Trusted Agentic Workflows with Verification and Calibration" }),
    ).toBeVisible();
  });

  test("supports theme toggle, assistant panel, sidebar collapse persistence, and logout", async ({
    page,
  }) => {
    await signIn(page);

    const html = page.locator("html");
    const wasDark = (await html.getAttribute("class"))?.includes("dark") ?? false;
    await page.getByRole("button", { name: /Switch to (light|dark) mode/ }).click();
    await expect.poll(async () => {
      const cls = await html.getAttribute("class");
      return (cls ?? "").includes("dark");
    }).toBe(!wasDark);
    const main = page.locator("main#main-content");
    // The assistant panel is opened from the top-bar "Ask Aria" toggle (the
    // sidebar "Assistant" nav item was removed).
    const assistantToggle = page.getByRole("button", { name: "Ask Aria" });
    await assistantToggle.click();
    await expect(main).toHaveAttribute("data-assistant-open", "true");
    await assistantToggle.click();
    await expect(main).toHaveAttribute("data-assistant-open", "false");

    await page.getByRole("button", { name: "Collapse sidebar" }).click();
    await expect(page.getByRole("button", { name: "Expand sidebar" })).toBeVisible();
    await expect
      .poll(() => page.evaluate(() => localStorage.getItem("caliber.sidebar.collapsed")))
      .toBe("true");

    await page.reload();
    await expect(page.getByRole("button", { name: "Expand sidebar" })).toBeVisible();

    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page).toHaveURL(/\/caliber\/login$/);
  });
});
