import { expect, test } from "@playwright/test";

import { goToSidebarRoute, signIn } from "./helpers";

const EXPECTED_NAV_ORDER = [
  "Dashboard",
  "Workflows",
  "Plans",
  "Prompts",
  "Tools",
  "Skills",
  "MCP Servers",
  "Knowledge Base",
  "Object Store",
  "Test Sets",
  "Judges",
  "Evaluations",
  "Observability",
  "Review Queues",
  "Releases",
  "Audit Log",
  "LLM Gateway",
  "Settings",
  "Docs",
];

// SPA routes reachable from the sidebar, with the heading each page renders.
// "Dashboard" is the landing route and "Docs" links to the static documentation
// site, so both are exercised separately from this list.
const SIDEBAR_ROUTES = [
  { label: "Workflows", path: "/caliber/workflows", heading: "Workflows" },
  { label: "Plans", path: "/caliber/aria/plans", heading: "Aria Plans" },
  { label: "Prompts", path: "/caliber/prompts", heading: "Prompts" },
  { label: "Tools", path: "/caliber/tools", heading: "Tools" },
  { label: "Skills", path: "/caliber/skills", heading: "Skills" },
  { label: "MCP Servers", path: "/caliber/mcp-servers", heading: "MCP Servers" },
  { label: "Knowledge Base", path: "/caliber/knowledge-bases", heading: "Knowledge Bases" },
  { label: "Object Store", path: "/caliber/object-store", heading: "Object Store" },
  { label: "Test Sets", path: "/caliber/eval-datasets", heading: "Test Sets" },
  { label: "Judges", path: "/caliber/judges", heading: "Judges" },
  { label: "Evaluations", path: "/caliber/evaluations", heading: "Evaluations" },
  { label: "Observability", path: "/caliber/observability", heading: "Observability" },
  { label: "Review Queues", path: "/caliber/review-queues", heading: "Review Queues" },
  { label: "Releases", path: "/caliber/releases", heading: "Releases" },
  { label: "Audit Log", path: "/caliber/audit-log", heading: "Audit Log" },
  { label: "LLM Gateway", path: "/caliber/gateway", heading: "LLM Gateway" },
  { label: "Settings", path: "/caliber/settings", heading: "Settings" },
] as const;

test.describe("Toolbar Navigation", () => {
  test("keeps sidebar order aligned with toolbar information architecture", async ({
    page,
  }) => {
    await signIn(page);
    const labels = (await page
      .locator('aside[aria-label="CALIBER navigation"] .nav-item span.flex-1')
      .allInnerTexts())
      .map((text) => text.trim())
      .filter(Boolean);
    expect(labels).toEqual(EXPECTED_NAV_ORDER);
  });

  test("opens each main sidebar route without falling through to Not found", async ({
    page,
  }) => {
    await signIn(page);
    await expect(page.getByRole("heading", { name: "Dashboard" }).first()).toBeVisible();

    for (const route of SIDEBAR_ROUTES) {
      await goToSidebarRoute(page, route.label, route.path);
      await expect(
        page.getByRole("heading", { name: route.heading }).first(),
      ).toBeVisible();
    }

    await expect(page.getByRole("heading", { name: "Not found" })).toHaveCount(0);
  });

  test("preserves the active theme across every sidebar route", async ({ page }) => {
    await signIn(page);

    const html = page.locator("html");
    const initiallyDark = (await html.getAttribute("class"))?.includes("dark") ?? false;
    if (!initiallyDark) {
      await page.getByRole("button", { name: /Switch to dark mode/ }).click();
    }

    await expect.poll(async () => {
      const cls = await html.getAttribute("class");
      return (cls ?? "").includes("dark");
    }).toBe(true);

    for (const route of SIDEBAR_ROUTES) {
      await goToSidebarRoute(page, route.label, route.path);
      await expect(
        page.getByRole("heading", { name: route.heading }).first(),
      ).toBeVisible();
      await expect.poll(async () => {
        const cls = await html.getAttribute("class");
        return (cls ?? "").includes("dark");
      }).toBe(true);
    }
  });
});
