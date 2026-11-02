import { expect, test, type Locator, type Page } from "@playwright/test";

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
 *    alone, and measure at least 4.5:1 (WCAG AA for normal text) in both the light and the dark
 *    theme, against the colour actually painted (a screenshot sample, not the computed
 *    background, which misses a translucent tint over the sidebar's gradient). The measurement is
 *    of the status chips and a disabled item's label only, not an audit of the whole page.
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

/**
 * WCAG contrast ratio of an element's text against the colour actually painted behind it.
 *
 * `getComputedStyle(...).backgroundColor` is not enough: a chip's tint is translucent in the dark
 * theme and the sidebar behind it is a gradient image, so the colour a person sees is neither.
 * The text colour is read from the computed style; the background is the most common pixel of a
 * real screenshot of the element (text and icon pixels are the minority), so it includes the
 * translucent tint composited over whatever is behind it, in the theme that is on.
 */
async function paintedContrast(locator: Locator): Promise<number> {
  const png = (await locator.screenshot()).toString("base64");
  const textColor = await locator.evaluate((element) => getComputedStyle(element).color);
  return locator.page().evaluate(
    async ({ base64, color }) => {
      const image = new Image();
      image.src = `data:image/png;base64,${base64}`;
      await image.decode();
      const canvas = document.createElement("canvas");
      canvas.width = image.width;
      canvas.height = image.height;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("no 2d context");
      context.drawImage(image, 0, 0);
      const { data } = context.getImageData(0, 0, canvas.width, canvas.height);
      const counts = new Map<string, number>();
      for (let i = 0; i < data.length; i += 4) {
        const key = `${data[i]},${data[i + 1]},${data[i + 2]}`;
        counts.set(key, (counts.get(key) ?? 0) + 1);
      }
      const [background] = [...counts.entries()].sort((a, b) => b[1] - a[1])[0] ?? [];
      if (!background) throw new Error("empty screenshot");
      const match = color.match(/rgba?\(([^)]+)\)/);
      if (!match?.[1]) throw new Error(`unparseable colour: ${color}`);
      const text = match[1].split(/[ ,/]+/).map(Number).slice(0, 3);
      const paint = background.split(",").map(Number);
      const luminance = (rgb: number[]): number => {
        const [r = 0, g = 0, b = 0] = rgb.map((c) => {
          const s = c / 255;
          return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
        });
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
      };
      const a = luminance(text);
      const b = luminance(paint);
      return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    },
    { base64: png, color: textColor },
  );
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

  test("the support panel is a keyboard-operable disclosure and each status is a word, not a colour alone", async ({
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

    await page.keyboard.press("Space");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByTestId("adapter-support-list")).toHaveCount(0);
  });

  for (const theme of ["light", "dark"] as const) {
    test(`every status chip and the disabled item's label are at least 4.5:1 against what is painted, in the ${theme} theme`, async ({
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

      const html = page.locator("html");
      const isDark = async (): Promise<boolean> =>
        ((await html.getAttribute("class")) ?? "").includes("dark");
      if ((await isDark()) !== (theme === "dark")) {
        await page
          .getByRole("button", { name: theme === "dark" ? /Switch to dark mode/ : /Switch to light mode/ })
          .click();
      }
      await expect.poll(isDark).toBe(theme === "dark");

      await page.getByTestId("nav-support-toggle").click();
      for (const status of ["supported", "preview", "unavailable"]) {
        const chip = page
          .locator(
            `[data-testid="adapter-support-list"] [data-testid="adapter-support-chip"][data-status="${status}"]`,
          )
          .first();
        await chip.scrollIntoViewIfNeeded();
        const ratio = await paintedContrast(chip);
        expect(ratio, `${status} chip, ${theme} theme`).toBeGreaterThanOrEqual(4.5);
      }
      // The same chips on the navigation items, over the item's own background.
      for (const label of ["Release candidates", "OpenAPI Integrations"]) {
        const chip = navItem(page, label).getByTestId("adapter-support-chip");
        await chip.scrollIntoViewIfNeeded();
        expect(await paintedContrast(chip), `${label} chip, ${theme} theme`).toBeGreaterThanOrEqual(4.5);
      }
      // A disabled item's label is not dimmed below readable.
      const label = page.getByTestId("nav-disabled-mcp_servers").locator("span.flex-1");
      await label.scrollIntoViewIfNeeded();
      expect(await paintedContrast(label), `disabled label, ${theme} theme`).toBeGreaterThanOrEqual(4.5);
    });
  }

  test("the reason is on the item for a sighted person: a popover on hover and on keyboard focus", async ({
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

    const popover = page.getByTestId("nav-support-text-mcp_servers");
    await expect(popover).toBeHidden();
    await page.getByTestId("nav-disabled-mcp_servers").focus();
    await expect(popover).toBeVisible();
    await expect(popover).toHaveText(`Unavailable. ${UNAVAILABLE_MCP.message}`);
    await page.getByTestId("nav-disabled-mcp_servers").blur();
    await expect(popover).toBeHidden();

    const preview = page.getByTestId("nav-support-text-openapi_integrations");
    await navItem(page, "OpenAPI Integrations").hover();
    await expect(preview).toBeVisible();
    await expect(preview).toHaveText(`Preview. ${PREVIEW_OPENAPI.message}`);
    await page.mouse.move(700, 400);
    await expect(preview).toBeHidden();
  });
});
