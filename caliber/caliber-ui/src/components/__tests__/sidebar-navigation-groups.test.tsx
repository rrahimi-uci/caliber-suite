/**
 * Sidebar information architecture and group collapsing.
 *
 * Two properties matter more than the grouping itself and are easy to lose:
 * no destination may become unreachable, and no operator-facing signal may be
 * traded away for whitespace.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { Sidebar } from "@/components/Sidebar";
import { PREVIEW_DESTINATIONS } from "@/lib/maturity";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

/** Every destination the sidebar is responsible for reaching. */
const ALL_DESTINATIONS = [
  "/",
  "/workflows",
  "/cookbooks",
  "/agents",
  "/aria/plans",
  "/prompts",
  "/skills",
  "/tools",
  "/knowledge-bases",
  "/object-store",
  "/mcp-servers",
  "/openapi-integrations",
  "/gateway",
  "/eval-datasets",
  "/judges",
  "/evaluations",
  "/observability",
  "/review-queues",
  "/releases",
  "/audit-log",
  "/administration",
  "/settings",
];

const GROUP_IDS = ["core", "extended", "operate", "preview", "admin"];

// The core journey leads, then the rest of the supported alpha, then operations, then
// previews. A Preview pill is part of the link's text content.
const EXPECTED_NAV_LABELS = [
  "Dashboard",
  "Prompts",
  "Test Sets",
  "Evaluations",
  "Releases",
  "Skills",
  "Tools",
  "Workflows",
  "Knowledge Bases",
  "Object Store",
  "Judges",
  "Agents",
  "Observability",
  "Review Queues",
  "Audit Log",
  "LLM Gateway",
  "MCP ServersPreview",
  "OpenAPI IntegrationsPreview",
  "CookbooksPreview",
  "PlansPreview",
  "Administration",
  "Settings",
  "Docs",
];

function envelope<T>(data: T): { data: T } {
  return { data };
}

/**
 * Resolves when the sidebar has asked for the Aria plans. That poll only starts once
 * ``/capabilities`` has said Aria is on, so by then every flagged destination is rendered too:
 * the previews arrive with the capabilities, and a test must not look for them before that.
 */
let flagsReady: Promise<void> = Promise.resolve();

async function renderSidebar(initialPath = "/"): Promise<void> {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={[initialPath]}
      >
        <Sidebar health="ok" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await flagsReady;
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
  window.localStorage.clear();
  flagsReady = Promise.resolve();
});
afterAll(() => server.close());

function stubPlans(): void {
  flagsReady = new Promise((resolve) => {
    server.use(
      http.get(`${API_BASE}/aria/plans`, () => {
        resolve();
        return HttpResponse.json(envelope([]));
      }),
    );
  });
}

describe("Sidebar navigation groups", () => {
  it("reaches every destination once all groups are open", async () => {
    stubPlans();
    const user = userEvent.setup();
    await renderSidebar();

    for (const id of GROUP_IDS) {
      const toggle = await screen.findByTestId(`nav-group-toggle-${id}`);
      if (toggle.getAttribute("aria-expanded") === "false") await user.click(toggle);
    }

    // The regrouping must not quietly drop a destination. Asserting the set —
    // rather than a count — names the missing route when one goes.
    const hrefs = new Set(
      screen
        .getAllByRole("link")
        .map((link) => link.getAttribute("href"))
        .filter((href): href is string => Boolean(href)),
    );
    for (const destination of ALL_DESTINATIONS) {
      expect(hrefs, `missing destination ${destination}`).toContain(destination);
    }
  });

  it("leads with the core journey, then the extended areas, operations, and previews", async () => {
    stubPlans();
    const user = userEvent.setup();
    await renderSidebar();

    for (const id of GROUP_IDS) {
      const toggle = await screen.findByTestId(`nav-group-toggle-${id}`);
      if (toggle.getAttribute("aria-expanded") === "false") await user.click(toggle);
    }

    expect(
      within(screen.getByLabelText("CALIBER navigation"))
        .getAllByRole("link")
        .map((link) => link.textContent?.trim()),
    ).toEqual(EXPECTED_NAV_LABELS);
  });

  it("puts the integrations and the previews together at the end of the work areas", async () => {
    stubPlans();
    const user = userEvent.setup();
    await renderSidebar();

    await user.click(await screen.findByTestId("nav-group-toggle-preview"));
    const panel = document.getElementById("nav-group-preview");
    expect(panel).not.toBeNull();
    for (const label of ["LLM Gateway", "MCP Servers", "OpenAPI Integrations", "Cookbooks", "Plans"]) {
      expect(within(panel as HTMLElement).getByText(label)).toBeInTheDocument();
    }
  });

  it("labels exactly the scope decision's preview areas, in the nav", async () => {
    stubPlans();
    const user = userEvent.setup();
    await renderSidebar();
    for (const id of GROUP_IDS) {
      const toggle = await screen.findByTestId(`nav-group-toggle-${id}`);
      if (toggle.getAttribute("aria-expanded") === "false") await user.click(toggle);
    }

    const labelled = screen
      .getAllByTestId("nav-preview-pill")
      .map((pill) => pill.closest("a")?.getAttribute("href"))
      .sort();

    // The same list the docs scope decision ("LABEL preview", section 2.2) names:
    // MCP servers, OpenAPI integrations, cookbooks, and the Aria assistant and plans.
    expect(labelled).toEqual([...PREVIEW_DESTINATIONS].sort());
    // Core journey destinations never carry the pill.
    for (const core of ["/prompts", "/eval-datasets", "/evaluations", "/releases"]) {
      const link = document.querySelector(`a[href="${core}"]`);
      expect(link, `missing ${core}`).not.toBeNull();
      expect(link?.querySelector('[data-testid="nav-preview-pill"]')).toBeNull();
    }
  });

  it("keeps a preview link's accessible name free of the pill, and describes it", async () => {
    stubPlans();
    const user = userEvent.setup();
    await renderSidebar();
    await user.click(await screen.findByTestId("nav-group-toggle-preview"));

    const link = screen.getByRole("link", { name: "Cookbooks" });
    expect(link).toHaveAccessibleDescription("Preview");
  });

  it("ties the nav's preview set to the documented scope decision", () => {
    const doc = readFileSync(
      resolve(__dirname, "../../../../../docs/caliber-alpha-gaps.md"),
      "utf8",
    );
    const labelRows = doc
      .split("\n")
      .filter((line) => line.startsWith("|") && /\*\*LABEL preview\*\*/.test(line));
    expect(labelRows.length).toBeGreaterThan(0);
    const row = labelRows.join(" ");
    for (const phrase of ["MCP servers", "OpenAPI integrations", "cookbooks"]) {
      expect(row).toContain(phrase);
    }
    expect(PREVIEW_DESTINATIONS).toEqual(
      expect.arrayContaining(["/mcp-servers", "/openapi-integrations", "/cookbooks", "/aria/plans"]),
    );
  });

  it("opens the group holding the current route and leaves the rest closed", async () => {
    stubPlans();
    await renderSidebar("/mcp-servers");

    expect(await screen.findByTestId("nav-group-toggle-preview")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByTestId("nav-group-toggle-operate")).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    // The point of the change: the first viewport no longer lists everything.
    expect(screen.queryByText("Audit Log")).toBeNull();
  });

  it("falls back to the core journey when no group owns the route", async () => {
    stubPlans();
    await renderSidebar("/");

    // Dashboard sits outside every group. Opening nothing would greet a new
    // user with five headers and zero destinations.
    expect(await screen.findByTestId("nav-group-toggle-core")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByText("Prompts")).toBeInTheDocument();
  });

  it("persists which groups the user opened", async () => {
    stubPlans();
    const user = userEvent.setup();
    await renderSidebar();
    await user.click(await screen.findByTestId("nav-group-toggle-extended"));
    expect(screen.getByText("Judges")).toBeInTheDocument();

    cleanup();
    await renderSidebar();

    expect(await screen.findByTestId("nav-group-toggle-extended")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByText("Judges")).toBeInTheDocument();
  });

  it("opens and closes a group from the keyboard", async () => {
    stubPlans();
    const user = userEvent.setup();
    await renderSidebar();

    const toggle = await screen.findByTestId("nav-group-toggle-operate");
    toggle.focus();
    await user.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Audit Log")).toBeInTheDocument();

    await user.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Audit Log")).toBeNull();
  });

  it("associates each toggle with the panel it controls", async () => {
    stubPlans();
    await renderSidebar();

    for (const id of GROUP_IDS) {
      const toggle = await screen.findByTestId(`nav-group-toggle-${id}`);
      expect(toggle).toHaveAttribute("aria-controls", `nav-group-${id}`);
      expect(document.getElementById(`nav-group-${id}`)).not.toBeNull();
    }
  });

  /**
   * The badge exists so a plan that stopped for you is not lost. Collapsing the
   * group it lives in must not be a way to silence it — and it must surface
   * exactly once, or a label query cannot say which element it means.
   */
  it("rolls a hidden badge up to its group header, exactly once", async () => {
    server.use(
      http.get(`${API_BASE}/aria/plans`, () =>
        HttpResponse.json(
          envelope([
            {
              plan_id: "A",
              session_id: null,
              goal: "g",
              status: "paused",
              autonomy: "approve_plan",
              owner: "@me",
              created_at: "",
              updated_at: "",
              step_count: 0,
            },
          ]),
        ),
      ),
    );
    const user = userEvent.setup();
    await renderSidebar("/observability");

    // Operate owns this route, so the preview group — which holds Plans — starts closed.
    const badge = await screen.findByLabelText(/awaiting your input/i);
    expect(badge).toHaveTextContent("1");
    expect(screen.getByTestId("nav-group-toggle-preview")).toContainElement(badge);

    await user.click(screen.getByTestId("nav-group-toggle-preview"));
    const openBadge = await screen.findByLabelText(/awaiting your input/i);
    expect(openBadge).toHaveTextContent("1");
    expect(screen.getByTestId("nav-group-toggle-preview")).not.toContainElement(openBadge);
  });
});
