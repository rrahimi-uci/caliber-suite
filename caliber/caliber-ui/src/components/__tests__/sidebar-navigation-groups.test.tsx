/**
 * Sidebar information architecture and group collapsing.
 *
 * Two properties matter more than the grouping itself and are easy to lose:
 * no destination may become unreachable, and no operator-facing signal may be
 * traded away for whitespace.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { Sidebar } from "@/components/Sidebar";
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

const GROUP_IDS = ["build", "resources", "integrations", "evaluate", "operate", "admin"];

const EXPECTED_NAV_LABELS = [
  "Dashboard",
  "Workflows",
  "Cookbooks",
  "Agents",
  "Plans",
  "Prompts",
  "Tools",
  "Skills",
  "Knowledge Bases",
  "Object Store",
  "LLM Gateway",
  "MCP Servers",
  "OpenAPI Integrations",
  "Test Sets",
  "Judges",
  "Evaluations",
  "Releases",
  "Observability",
  "Review Queues",
  "Audit Log",
  "Administration",
  "Settings",
  "Docs",
];

function envelope<T>(data: T): { data: T } {
  return { data };
}

function renderSidebar(initialPath = "/"): void {
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
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
  window.localStorage.clear();
});
afterAll(() => server.close());

function stubPlans(): void {
  server.use(http.get(`${API_BASE}/aria/plans`, () => HttpResponse.json(envelope([]))));
}

describe("Sidebar navigation groups", () => {
  it("reaches every destination once all groups are open", async () => {
    stubPlans();
    const user = userEvent.setup();
    renderSidebar();

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

  it("orders destinations by build, integration, evaluation, and operating lifecycle", async () => {
    stubPlans();
    const user = userEvent.setup();
    renderSidebar();

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

  it("groups MCP Servers and the LLM Gateway together as integrations", async () => {
    stubPlans();
    const user = userEvent.setup();
    renderSidebar();

    await user.click(await screen.findByTestId("nav-group-toggle-integrations"));
    const panel = document.getElementById("nav-group-integrations");
    expect(panel).not.toBeNull();
    expect(within(panel as HTMLElement).getByText("MCP Servers")).toBeInTheDocument();
    expect(within(panel as HTMLElement).getByText("LLM Gateway")).toBeInTheDocument();
  });

  it("opens the group holding the current route and leaves the rest closed", async () => {
    stubPlans();
    renderSidebar("/mcp-servers");

    expect(await screen.findByTestId("nav-group-toggle-integrations")).toHaveAttribute(
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

  it("falls back to Build when no group owns the route", async () => {
    stubPlans();
    renderSidebar("/");

    // Dashboard sits outside every group. Opening nothing would greet a new
    // user with six headers and zero destinations.
    expect(await screen.findByTestId("nav-group-toggle-build")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByText("Workflows")).toBeInTheDocument();
  });

  it("persists which groups the user opened", async () => {
    stubPlans();
    const user = userEvent.setup();
    renderSidebar();
    await user.click(await screen.findByTestId("nav-group-toggle-evaluate"));
    expect(screen.getByText("Judges")).toBeInTheDocument();

    cleanup();
    renderSidebar();

    expect(await screen.findByTestId("nav-group-toggle-evaluate")).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByText("Judges")).toBeInTheDocument();
  });

  it("opens and closes a group from the keyboard", async () => {
    stubPlans();
    const user = userEvent.setup();
    renderSidebar();

    const toggle = await screen.findByTestId("nav-group-toggle-operate");
    toggle.focus();
    await user.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Releases")).toBeInTheDocument();

    await user.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Releases")).toBeNull();
  });

  it("associates each toggle with the panel it controls", async () => {
    stubPlans();
    renderSidebar();

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
    renderSidebar("/observability");

    // Operate owns this route, so Build — which holds Plans — starts closed.
    const badge = await screen.findByLabelText(/awaiting your input/i);
    expect(badge).toHaveTextContent("1");
    expect(screen.getByTestId("nav-group-toggle-build")).toContainElement(badge);

    await user.click(screen.getByTestId("nav-group-toggle-build"));
    const openBadge = await screen.findByLabelText(/awaiting your input/i);
    expect(openBadge).toHaveTextContent("1");
    expect(screen.getByTestId("nav-group-toggle-build")).not.toContainElement(openBadge);
  });
});
