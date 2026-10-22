import { expect, test, type Page } from "@playwright/test";

import { createPromptViaApi, signIn, uniqueSlug } from "./helpers";

/**
 * The clean alpha: with the shipped defaults, three of the four areas the alpha does not stand
 * behind are off, hidden by the app, and refused by the API. Aria is the exception and ships on.
 *
 * `playwright.alpha.config.ts` starts this server with no `CALIBER_FEATURE_*` variable set, so
 * every assertion below is about the default posture. The other Playwright runs (the full suite
 * and the cookbook journeys) turn the remaining flags on and cover those areas themselves.
 *
 * What it proves:
 *  1. `GET /capabilities` reports aria on and the other three off, each with the variable that
 *     flips it.
 *  2. The API answers a gated (off-by-default) route with 404 `feature_disabled`, serves an
 *     Aria route normally, and leaves the release board that the alpha does support
 *     (`/releases/live`) on.
 *  3. The navigation lists Plans (Aria) but none of MCP Servers, OpenAPI Integrations or
 *     Cookbooks, and the top bar has "Ask Aria".
 *  4. A deep link to a route gated by one of the three off-by-default flags says it is disabled,
 *     names the variable, and links home.
 *  5. The pages the alpha does support call Aria routes (it's on) but never a route gated by one
 *     of the three off-by-default flags, and hide only those three areas' controls.
 */
const API = "/ajax-api/2.0/mlflow/caliber";

/** The three flags that still ship off by default; Aria ships on and is covered separately. */
const OFF_BY_DEFAULT_FEATURES = [
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

const GATED_NAV_LABELS = ["MCP Servers", "OpenAPI Integrations", "Cookbooks"];

/**
 * Server routes behind one of the three off-by-default flags (`caliber/features.py`). A default
 * install must never call them. Aria's routes (`/assistant/`, `/aria/`) are deliberately excluded:
 * they're on by default, so the supported pages below call them as part of normal behavior.
 */
const GATED_API_PATH = new RegExp(
  `${API}/(?:mcp-servers|openapi-integrations|cookbooks|releases/candidates|releases/report-jobs|projects/[^/]+/releases)`,
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
  test("the API reports Aria on, the other three off, and refuses a gated route", async ({
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
    expect(features["aria"]?.enabled).toBe(true);
    for (const id of ["integrations", "cookbooks", "release_candidates"]) {
      expect(features[id]?.enabled, `${id} should be off by default`).toBe(
        false,
      );
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

    // Aria ships on, so its routes answer normally instead of 404.
    const ariaPlans = await page.request.get(`${API}/aria/plans`);
    expect(ariaPlans.ok(), await ariaPlans.text()).toBeTruthy();
  });

  test("the navigation and top bar show Plans but none of the other gated areas", async ({
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
    // Aria ships on, so its nav entry is present alongside the core journey.
    expect(labels, "Plans must be in the navigation").toContain("Plans");
    // The group keeps the LLM Gateway under the same neutral title.
    expect(labels).toContain("LLM Gateway");
    await expect(
      nav.getByRole("button", { name: "Integrations", exact: true }),
    ).toBeVisible();
    await expect(nav.getByTestId("nav-preview-pill")).toHaveCount(0);

    // Aria ships on, so "Ask Aria" is available; the panel starts closed.
    await expect(page.getByRole("button", { name: "Ask Aria" })).toBeVisible();
    await expect(page.locator("#main-content")).toHaveAttribute(
      "data-assistant-open",
      "false",
    );
  });

  for (const feature of OFF_BY_DEFAULT_FEATURES) {
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

  test("/caliber/aria/plans renders instead of the disabled panel", async ({
    page,
  }) => {
    await signIn(page);
    await page.goto("/caliber/aria/plans");

    await expect(page.getByTestId("feature-disabled-panel")).toHaveCount(0);
    await expect(page.getByTestId("route-not-found")).toHaveCount(0);
    await expect(
      page.getByRole("heading", { name: "Aria Plans", exact: true }),
    ).toBeVisible();
  });

  test("supported pages offer the Aria-backed controls and never call a route gated by one of the other three flags", async ({
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

    // Dashboard: Aria's reliability tiles are present now that it's on.
    await expect(
      page.getByRole("heading", { name: "Dashboard" }).first(),
    ).toBeVisible();
    await expect(page.getByText("Execution success")).toBeVisible();

    // Prompts: the create fork offers "Describe it" (Aria drafts it).
    await page.goto("/caliber/prompts");
    await page.getByRole("button", { name: "New prompt" }).click();
    await expect(
      page.getByRole("button", { name: /Write \/ paste/ }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: /Describe it/ }),
    ).toBeVisible();

    // A prompt's workspace: the Playground, Test Sets and Calibration stages offer their
    // Aria-backed controls instead of the disabled notice.
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
    await expect(page.getByTestId("feature-disabled-notice")).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Start Chat Session" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Test Sets", exact: true }).click();
    await expect(page.getByTestId("feature-disabled-notice")).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Generate Test Cases" }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Calibration", exact: true })
      .click();
    await expect(
      page.getByLabel("Calibration dataset", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Assistant-Guided Calibration")).toBeVisible();

    // Settings: Providers and the Aria tab are both present.
    await page.goto("/caliber/settings");
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Providers" })).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Aria", exact: true }),
    ).toBeVisible();

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
