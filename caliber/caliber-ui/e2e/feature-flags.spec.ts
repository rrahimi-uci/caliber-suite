import { expect, test, type Page } from "@playwright/test";

import { createPromptViaApi, signIn, uniqueSlug } from "./helpers";

/**
 * The clean alpha: with the shipped defaults, the four areas the alpha does not stand behind
 * are off, hidden by the app, and refused by the API.
 *
 * `playwright.alpha.config.ts` starts this server with no `CALIBER_FEATURE_*` variable set, so
 * every assertion below is about the default posture. The other Playwright runs (the full suite
 * and the cookbook journeys) turn the relevant flags on and cover the areas themselves.
 *
 * What it proves:
 *  1. `GET /capabilities` reports every flag off, with the variable that turns each one on.
 *  2. The API answers a gated route with 404 `feature_disabled`, while the release board that
 *     the alpha does support (`/releases/live`) stays on.
 *  3. The navigation lists none of MCP Servers, OpenAPI Integrations, Cookbooks or Plans, and the
 *     top bar has no "Ask Aria".
 *  4. A deep link to a gated page says it is disabled, names the variable, and links home.
 *  5. The pages the alpha does support do not call a gated route (no 404 noise in the console
 *     of a default install), and hide the controls that would.
 */
const API = "/ajax-api/2.0/mlflow/caliber";

const FEATURES = [
  {
    id: "aria",
    env: "CALIBER_FEATURE_ARIA",
    label: "Aria assistant and plans",
    path: "/caliber/aria/plans",
  },
  {
    id: "integrations",
    env: "CALIBER_FEATURE_INTEGRATIONS",
    label: "MCP servers and OpenAPI integrations",
    path: "/caliber/mcp-servers",
  },
  {
    id: "integrations",
    env: "CALIBER_FEATURE_INTEGRATIONS",
    label: "MCP servers and OpenAPI integrations",
    path: "/caliber/openapi-integrations",
  },
  {
    id: "cookbooks",
    env: "CALIBER_FEATURE_COOKBOOKS",
    label: "Cookbooks",
    path: "/caliber/cookbooks",
  },
] as const;

const GATED_NAV_LABELS = [
  "MCP Servers",
  "OpenAPI Integrations",
  "Cookbooks",
  "Plans",
];

/** Server routes behind a flag (`caliber/features.py`). A default install must never call them. */
const GATED_API_PATH = new RegExp(
  `${API}/(?:assistant/|aria/|mcp-servers|openapi-integrations|cookbooks|releases/candidates|releases/report-jobs|projects/[^/]+/releases)`,
);

async function openAllNavGroups(page: Page): Promise<void> {
  const toggles = page.locator('[data-testid^="nav-group-toggle-"]');
  const count = await toggles.count();
  for (let i = 0; i < count; i += 1) {
    const toggle = toggles.nth(i);
    if ((await toggle.getAttribute("aria-expanded")) === "false") {
      await toggle.click();
    }
  }
}

test.describe("Feature flags (alpha defaults)", () => {
  test("the API reports every flag off and refuses a gated route", async ({
    page,
  }) => {
    await signIn(page);

    const capabilities = await page.request.get(`${API}/capabilities`);
    expect(capabilities.ok(), await capabilities.text()).toBeTruthy();
    const features = (await capabilities.json()).data.features as Record<
      string,
      { enabled: boolean; env_var: string; label: string; stability: string }
    >;
    expect(Object.keys(features).sort()).toEqual([
      "aria",
      "cookbooks",
      "integrations",
      "release_candidates",
    ]);
    for (const feature of Object.values(features)) {
      expect(feature.enabled).toBe(false);
    }
    expect(features["aria"]?.env_var).toBe("CALIBER_FEATURE_ARIA");
    expect(features["release_candidates"]?.stability).toBe("record_only");

    const refused = await page.request.get(`${API}/cookbooks`);
    expect(refused.status()).toBe(404);
    expect(await refused.json()).toMatchObject({
      status_code: 404,
      reason_code: "feature_disabled",
      detail:
        "Cookbooks is disabled in this deployment. Set CALIBER_FEATURE_COOKBOOKS=true to enable it.",
    });

    // The release board the alpha does support is not behind a flag.
    const live = await page.request.get(`${API}/releases/live`);
    expect(live.ok(), await live.text()).toBeTruthy();
  });

  test("the navigation and top bar show none of the gated areas", async ({
    page,
  }) => {
    await signIn(page);
    await openAllNavGroups(page);

    const nav = page.locator('aside[aria-label="CALIBER navigation"]');
    const labels = (
      await nav.locator(".nav-item span.flex-1").allInnerTexts()
    ).map((text) => text.trim());
    for (const label of GATED_NAV_LABELS) {
      expect(labels, `${label} must not be in the navigation`).not.toContain(
        label,
      );
    }
    // The group keeps the LLM Gateway under the same neutral title.
    expect(labels).toContain("LLM Gateway");
    await expect(
      nav.getByRole("button", { name: "Integrations", exact: true }),
    ).toBeVisible();
    await expect(nav.getByTestId("nav-preview-pill")).toHaveCount(0);

    await expect(page.getByRole("button", { name: "Ask Aria" })).toHaveCount(0);
    await expect(page.locator("#main-content")).toHaveAttribute(
      "data-assistant-open",
      "false",
    );
  });

  for (const feature of FEATURES) {
    test(`${feature.path} says it is disabled and links back to the dashboard`, async ({
      page,
    }) => {
      await signIn(page);
      await page.goto(feature.path);

      const panel = page.getByTestId("feature-disabled-panel");
      await expect(panel).toBeVisible();
      await expect(
        panel.getByRole("heading", {
          name: "This isn't available on this deployment",
        }),
      ).toBeVisible();
      await expect(panel).toContainText(feature.label);
      await expect(panel).toContainText(`${feature.env}=true`);
      // Not a blank page, not "Not found", and the page behind it never rendered.
      await expect(page.getByTestId("route-not-found")).toHaveCount(0);

      await panel.getByRole("link", { name: "Back to the dashboard" }).click();
      await expect(page).toHaveURL(/\/caliber\/?$/);
      await expect(
        page.getByRole("heading", { name: "Dashboard" }).first(),
      ).toBeVisible();
    });
  }

  test("supported pages hide the gated controls and never call a gated route", async ({
    page,
  }) => {
    const gatedCalls: string[] = [];
    page.on("request", (request) => {
      if (GATED_API_PATH.test(new URL(request.url()).pathname)) {
        gatedCalls.push(`${request.method()} ${request.url()}`);
      }
    });

    await signIn(page);
    const { name: promptName } = await createPromptViaApi(page);

    // Dashboard: no Aria reliability tiles.
    await expect(
      page.getByRole("heading", { name: "Dashboard" }).first(),
    ).toBeVisible();
    await expect(page.getByText("Execution success")).toHaveCount(0);

    // Prompts: the create fork has no "Describe it" (Aria drafts it).
    await page.goto("/caliber/prompts");
    await page.getByRole("button", { name: "New prompt" }).click();
    await expect(
      page.getByRole("button", { name: /Write \/ paste/ }),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: /Describe it/ })).toHaveCount(
      0,
    );

    // A prompt's workspace: the Playground stage is listed and says why it cannot chat, and
    // Test Sets points at the manual route.
    await page.goto("/caliber/prompts");
    await page.getByLabel("Search prompts").fill(promptName);
    await page
      .locator('[data-testid^="prompt-card-"]')
      .filter({ hasText: promptName })
      .first()
      .getByRole("button", { name: "Open" })
      .click();
    for (const stage of [
      "Author",
      "Playground",
      "Test Sets",
      "Runs",
      "Calibration",
      "Bind",
    ]) {
      await expect(
        page.getByRole("button", { name: stage, exact: true }),
      ).toBeVisible();
    }
    await page.getByRole("button", { name: "Playground", exact: true }).click();
    await expect(page.getByTestId("feature-disabled-notice")).toContainText(
      "CALIBER_FEATURE_ARIA=true",
    );
    await expect(
      page.getByRole("button", { name: "Start Chat Session" }),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Test Sets", exact: true }).click();
    await expect(page.getByTestId("feature-disabled-notice")).toContainText(
      "CALIBER_FEATURE_ARIA=true",
    );
    await page
      .getByRole("button", { name: "Calibration", exact: true })
      .click();
    await expect(
      page.getByLabel("Calibration dataset", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Assistant-Guided Calibration")).toHaveCount(0);

    // Settings: Providers first, no Aria tab.
    await page.goto("/caliber/settings");
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Providers" })).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Aria", exact: true }),
    ).toHaveCount(0);

    // Releases: the board and timeline stay; the record-only signoff section is gone.
    await page.goto("/caliber/releases");
    await expect(page.getByTestId("releases-live-board")).toBeVisible();
    await expect(page.getByTestId("release-signoff-factory")).toHaveCount(0);

    // The workflow editor does not ask for MCP servers.
    await page.goto("/caliber/workflows");
    await expect(
      page.getByRole("heading", { name: "Workflows" }),
    ).toBeVisible();
    await page.getByTestId("new-workflow").click();
    await page
      .getByTestId("new-workflow-name")
      .fill(uniqueSlug("playwright-flags"));
    await page.getByTestId("template-single_agent").click();
    await expect(page.getByTestId("workflow-editor")).toBeVisible();

    expect(
      gatedCalls,
      "a default install called a feature-flagged route",
    ).toEqual([]);
  });
});
