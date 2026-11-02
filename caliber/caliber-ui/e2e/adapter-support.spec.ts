import { expect, test, type Page } from "@playwright/test";

import { signIn } from "./helpers";

/**
 * Beta support labels in the navigation (B15-Support.3, #771), in a real browser against the
 * built app and its API.
 *
 * What this proves, and how:
 *  - The *app* is real (the built SPA, the real session, the real Releases page and the real
 *    API behind it). Only `GET /capabilities` is rewritten in the browser, from the real
 *    response, to put the deployment in a state the disposable SQLite server cannot be in: a
 *    valid reference deployment (PostgreSQL, a real evaluation provider) is not available to
 *    this harness, so `supported` cannot be reached for real here. The default install's real,
 *    unrewritten answer is covered by `feature-flags.spec.ts` (alpha-defaults run).
 *  - The server's three statuses and its exact reason text reach the screen; nothing the server
 *    did not report as supported is shown as supported; an unavailable adapter is a disabled item
 *    that cannot be followed with the mouse or the keyboard; the labels are words, not colour
 *    alone, and meet WCAG AA contrast.
 *
 * The full suite's server has the integrations and release-candidate flags on, and the rewrite
 * turns them on regardless, so this spec does not depend on the server's own state.
 */
const API = "/ajax-api/2.0/mlflow/caliber";

interface Adapter {
  status: "supported" | "preview" | "unavailable";
  kind: string;
  label: string;
  reason_code: string | null;
  disabled_by: string | null;
  message: string;
  evidence: string[];
}

const SUPPORTED_RELEASE: Adapter = {
  status: "supported",
  kind: "release_evaluation",
  label: "Workspace release evaluation",
  reason_code: null,
  disabled_by: null,
  message:
    "Workspace release evaluation is supported: the evaluator and its evidence shape match the scenario evidence.",
  evidence: [
    "tests/egi_scenarios/test_v13_release_evaluation.py::test_evaluator_crash_recovers",
    "tests/egi_scenarios/test_v13_release_evaluation.py::test_worker_records_provider_run_before_completion_crash",
    "tests/egi_scenarios/test_v13_release_evaluation.py::test_recovery_before_provider_result_keeps_attempt_history",
  ],
};

const SUPPORTED_CONNECTOR: Adapter = {
  status: "supported",
  kind: "connector",
  label: "OpenAPI document-request connector",
  reason_code: null,
  disabled_by: null,
  message:
    "OpenAPI document-request connector is supported: the live transport matches its admission record.",
  evidence: ["tests/egi_scenarios/test_connector_capabilities.py::test_stable_key_mutates_once"],
};

const PREVIEW_MCP: Adapter = {
  status: "preview",
  kind: "mcp_server",
  label: "MCP servers",
  reason_code: "no_support_evidence",
  disabled_by: "profile",
  message:
    "MCP servers are preview: no scenario evidence supports them in the reference profile, and no setting changes that.",
  evidence: [],
};

const PREVIEW_OPENAPI: Adapter = {
  status: "preview",
  kind: "openapi_integration",
  label: "OpenAPI integrations",
  reason_code: "no_support_evidence",
  disabled_by: "profile",
  message:
    "OpenAPI integrations are preview: no scenario evidence supports them in the reference profile, and no setting changes that.",
  evidence: [],
};

const UNAVAILABLE_MCP: Adapter = {
  status: "unavailable",
  kind: "mcp_server",
  label: "MCP servers",
  reason_code: "mcp_not_in_reference_profile",
  disabled_by: "profile",
  message:
    "MCP servers are unavailable under the reference profile: it lists no MCP command or host, so no server is reachable.",
  evidence: [],
};

/** Serve the real `/capabilities`, with the flags on and `adapters` replaced. */
async function serveAdapters(
  page: Page,
  adapters: Record<string, Adapter> | null,
): Promise<void> {
  await page.route(`**${API}/capabilities`, async (route) => {
    const response = await route.fetch();
    const body = (await response.json()) as {
      data: {
        features: Record<string, { enabled: boolean }>;
        adapters?: Record<string, Adapter>;
      };
    };
    for (const id of ["integrations", "release_candidates"]) {
      const feature = body.data.features[id];
      if (feature) feature.enabled = true;
    }
    if (adapters === null) delete body.data.adapters;
    else body.data.adapters = adapters;
    await route.fulfill({ response, json: body });
  });
}

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

function navItem(page: Page, label: string) {
  return page
    .locator('aside[aria-label="CALIBER navigation"] .nav-item')
    .filter({ has: page.locator("span.flex-1", { hasText: new RegExp(`^${label}$`) }) });
}

/** WCAG contrast ratio of a chip's text on its own background, from computed styles. */
async function contrastOf(page: Page, selector: string): Promise<number> {
  return page.locator(selector).first().evaluate((element) => {
    const parse = (value: string): [number, number, number] => {
      const match = value.match(/rgba?\(([^)]+)\)/);
      if (!match?.[1]) throw new Error(`unparseable colour: ${value}`);
      const [r = 0, g = 0, b = 0] = match[1].split(/[ ,/]+/).map(Number);
      return [r, g, b];
    };
    const luminance = ([r, g, b]: [number, number, number]): number => {
      const channel = (c: number): number => {
        const s = c / 255;
        return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
    };
    const style = getComputedStyle(element);
    const a = luminance(parse(style.color));
    const b = luminance(parse(style.backgroundColor));
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  });
}

test.describe("Beta support labels in the navigation", () => {
  test("a supported reference path is discoverable, with the server's text, and neighbours stay preview", async ({
    page,
  }) => {
    await serveAdapters(page, {
      document_request_connector: SUPPORTED_CONNECTOR,
      release_evaluation: SUPPORTED_RELEASE,
      mcp_servers: PREVIEW_MCP,
      openapi_integrations: PREVIEW_OPENAPI,
    });
    await signIn(page);
    await openAllNavGroups(page);

    const candidates = page.getByRole("link", { name: "Release candidates" });
    await expect(candidates).toBeVisible();
    await expect(candidates.getByTestId("adapter-support-chip")).toHaveText("Supported");
    await expect(candidates).toHaveAccessibleDescription(
      `Supported. ${SUPPORTED_RELEASE.message}`,
    );
    // MCP servers and OpenAPI integrations are not promoted by the flag being on.
    for (const label of ["MCP Servers", "OpenAPI Integrations"]) {
      await expect(
        navItem(page, label).getByTestId("adapter-support-chip"),
        label,
      ).toHaveText("Preview");
    }
    await expect(navItem(page, "MCP Servers")).toHaveAccessibleDescription(
      `Preview. ${PREVIEW_MCP.message}`,
    );

    // Following it lands on the signoff section of the Releases page.
    await candidates.click();
    await expect(page).toHaveURL(/\/caliber\/releases#release-signoff$/);
    await expect(page.getByTestId("release-signoff-factory")).toBeVisible();
    await expect(candidates).toHaveAttribute("aria-current", "location");
    await expect(page.getByRole("link", { name: "Releases", exact: true })).toHaveAttribute(
      "aria-current",
      "page",
    );

    // The panel names the connector, which has no page of its own, with its evidence.
    await page.getByTestId("nav-support-toggle").click();
    const connector = page.getByTestId("adapter-support-document_request_connector");
    await expect(connector).toContainText("Supported");
    await expect(connector).toContainText(SUPPORTED_CONNECTOR.message);
    await expect(connector).toContainText("Backed by 1 scenario test.");
  });

  test("an unavailable adapter is a disabled item with the server's refusal, and neither click nor keyboard follows it", async ({
    page,
  }) => {
    await serveAdapters(page, {
      document_request_connector: SUPPORTED_CONNECTOR,
      release_evaluation: SUPPORTED_RELEASE,
      mcp_servers: UNAVAILABLE_MCP,
      openapi_integrations: PREVIEW_OPENAPI,
    });
    await signIn(page);
    await openAllNavGroups(page);

    const disabled = page.getByTestId("nav-disabled-mcp_servers");
    await expect(disabled).toBeVisible();
    await expect(disabled).toHaveAttribute("aria-disabled", "true");
    await expect(disabled).not.toHaveAttribute("href", /.*/);
    await expect(disabled.getByTestId("adapter-support-chip")).toHaveText("Unavailable");
    await expect(disabled).toHaveAccessibleName("MCP Servers");
    await expect(disabled).toHaveAccessibleDescription(`Unavailable. ${UNAVAILABLE_MCP.message}`);

    const before = page.url();
    // Playwright refuses to click an `aria-disabled` element (it is not "enabled", which is the
    // point); a person's pointer can still land on it, so force the click to prove nothing follows.
    await expect(disabled).toBeDisabled();
    await disabled.click({ force: true });
    await expect(page).toHaveURL(before);
    // Reachable by keyboard, so its reason is announced, and Enter does not follow it.
    await disabled.focus();
    await expect(disabled).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(before);
    await expect(page.getByTestId("route-not-found")).toHaveCount(0);

    // A preview neighbour is still a real link.
    await navItem(page, "OpenAPI Integrations").click();
    await expect(page).toHaveURL(/\/caliber\/openapi-integrations$/);
  });

  test("a server that reports no adapters shows nothing as supported", async ({ page }) => {
    await serveAdapters(page, null);
    await signIn(page);
    await openAllNavGroups(page);

    await expect(navItem(page, "MCP Servers").getByTestId("adapter-support-chip")).toHaveText(
      "Preview",
    );
    await expect(navItem(page, "Release candidates").getByTestId("adapter-support-chip")).toHaveText(
      "Preview",
    );
    await page.getByTestId("nav-support-toggle").click();
    const list = page.getByTestId("adapter-support-list");
    await expect(list.getByTestId("adapter-support-chip")).toHaveText([
      "Preview",
      "Preview",
      "Preview",
      "Preview",
    ]);
    await expect(list).not.toContainText("Supported");
    await expect(list).toContainText("The server did not report why.");
  });

  test("the support panel is a keyboard-operable disclosure and each status is a word, not a colour alone, at AA contrast", async ({
    page,
  }) => {
    await serveAdapters(page, {
      document_request_connector: SUPPORTED_CONNECTOR,
      release_evaluation: SUPPORTED_RELEASE,
      mcp_servers: UNAVAILABLE_MCP,
      openapi_integrations: PREVIEW_OPENAPI,
    });
    await signIn(page);

    const toggle = page.getByTestId("nav-support-toggle");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await toggle.focus();
    await page.keyboard.press("Enter");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    const list = page.getByRole("list", { name: "Support status by adapter" });
    await expect(list.getByRole("listitem")).toHaveCount(4);
    await expect(list.getByTestId("adapter-support-chip")).toHaveText([
      "Supported",
      "Supported",
      "Unavailable",
      "Preview",
    ]);

    for (const status of ["supported", "preview", "unavailable"]) {
      const ratio = await contrastOf(
        page,
        `[data-testid="adapter-support-list"] [data-testid="adapter-support-chip"][data-status="${status}"]`,
      );
      expect(ratio, `${status} chip contrast`).toBeGreaterThanOrEqual(4.5);
    }

    await page.keyboard.press("Space");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByTestId("adapter-support-list")).toHaveCount(0);
  });
});
