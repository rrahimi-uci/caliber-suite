/**
 * The sidebar and the alpha feature flags.
 *
 * With a flag off (the default) its destinations are not in the navigation, and nothing polls
 * the routes behind it. While capabilities load, or when they cannot be loaded, every flag reads
 * as off: a clean alpha never flashes a destination it does not support.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { Sidebar } from "@/components/Sidebar";
import { capabilitiesBody } from "@/test/handlers";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const GATED_LABELS = [
  "MCP Servers",
  "OpenAPI Integrations",
  "Cookbooks",
  "Plans",
];

function renderSidebar(initialPath = "/"): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={[initialPath]}
      >
        <Sidebar health="ok" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Serve capabilities with the given areas on, and count requests to the Aria plans poll. */
function serve(enabled: Parameters<typeof capabilitiesBody>[0]): {
  planPolls: () => number;
} {
  let polls = 0;
  server.use(
    http.get(`${API_BASE}/capabilities`, () =>
      HttpResponse.json(capabilitiesBody(enabled)),
    ),
    http.get(`${API_BASE}/aria/plans`, () => {
      polls += 1;
      return HttpResponse.json({ data: [] });
    }),
  );
  return { planPolls: () => polls };
}

async function openPreviewGroup(): Promise<HTMLElement> {
  const toggle = await screen.findByTestId("nav-group-toggle-preview");
  if (toggle.getAttribute("aria-expanded") === "false") {
    await userEvent.click(toggle);
  }
  return document.getElementById("nav-group-preview") as HTMLElement;
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
  window.localStorage.clear();
});
afterAll(() => server.close());

describe("Sidebar feature flags", () => {
  it("lists every gated destination when every flag is on", async () => {
    serve({
      aria: true,
      integrations: true,
      cookbooks: true,
      release_candidates: true,
    });
    renderSidebar();
    const panel = await openPreviewGroup();
    for (const label of ["LLM Gateway", ...GATED_LABELS]) {
      expect(await within(panel).findByText(label)).toBeInTheDocument();
    }
    expect(screen.getByTestId("nav-group-toggle-preview")).toHaveTextContent(
      /^Integrations$/,
    );
  });

  it("hides every gated destination, and the preview framing, when every flag is off", async () => {
    const { planPolls } = serve({});
    renderSidebar();
    const panel = await openPreviewGroup();

    expect(await within(panel).findByText("LLM Gateway")).toBeInTheDocument();
    // Let the capabilities settle: the destinations must stay absent *after* the answer too.
    await waitFor(() =>
      expect(screen.getByTestId("nav-group-toggle-preview")).toHaveTextContent(
        /^Integrations$/,
      ),
    );
    for (const label of GATED_LABELS) {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
    expect(screen.queryByTestId("nav-preview-pill")).not.toBeInTheDocument();
    // The group is not empty (LLM Gateway stays), so it stays, with the same neutral title.
    // Aria is off, so the sidebar must not poll its plans (that route answers 404).
    expect(planPolls()).toBe(0);
  });

  it("shows only the areas whose flag is on", async () => {
    const { planPolls } = serve({ integrations: true });
    renderSidebar();
    const panel = await openPreviewGroup();

    expect(await within(panel).findByText("MCP Servers")).toBeInTheDocument();
    expect(within(panel).getByText("OpenAPI Integrations")).toBeInTheDocument();
    expect(within(panel).queryByText("Cookbooks")).not.toBeInTheDocument();
    expect(within(panel).queryByText("Plans")).not.toBeInTheDocument();
    expect(screen.getByTestId("nav-group-toggle-preview")).toHaveTextContent(
      /^Integrations$/,
    );
    expect(planPolls()).toBe(0);
  });

  it("polls the plans badge once Aria is on", async () => {
    const { planPolls } = serve({ aria: true });
    renderSidebar();
    const panel = await openPreviewGroup();
    expect(await within(panel).findByText("Plans")).toBeInTheDocument();
    await waitFor(() => expect(planPolls()).toBeGreaterThan(0));
  });

  it("shows no gated destination while capabilities are still loading", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, async () => {
        await delay(80);
        return HttpResponse.json(
          capabilitiesBody({ aria: true, integrations: true, cookbooks: true }),
        );
      }),
      http.get(`${API_BASE}/aria/plans`, () => HttpResponse.json({ data: [] })),
    );
    renderSidebar();
    const panel = await openPreviewGroup();
    expect(within(panel).getByText("LLM Gateway")).toBeInTheDocument();
    for (const label of GATED_LABELS) {
      expect(within(panel).queryByText(label)).not.toBeInTheDocument();
    }
    // ...and they arrive once the server has said they are on.
    expect(await within(panel).findByText("Cookbooks")).toBeInTheDocument();
  });

  it("shows no gated destination when capabilities cannot be loaded", async () => {
    const polls = { count: 0 };
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          { detail: "boom", status_code: 500 },
          { status: 500 },
        ),
      ),
      http.get(`${API_BASE}/aria/plans`, () => {
        polls.count += 1;
        return HttpResponse.json({ data: [] });
      }),
    );
    renderSidebar();
    const panel = await openPreviewGroup();
    await waitFor(() =>
      expect(screen.getByTestId("nav-group-toggle-preview")).toHaveTextContent(
        /^Integrations$/,
      ),
    );
    expect(within(panel).getByText("LLM Gateway")).toBeInTheDocument();
    for (const label of GATED_LABELS) {
      expect(within(panel).queryByText(label)).not.toBeInTheDocument();
    }
    expect(polls.count).toBe(0);
  });

  it("still opens the group a deep link to a hidden area belongs to", async () => {
    serve({});
    renderSidebar("/cookbooks");
    // The Cookbooks route renders a "disabled" panel; the sidebar around it should still open
    // the group it would live in, not fall back to the core journey.
    expect(
      await screen.findByTestId("nav-group-toggle-preview"),
    ).toHaveAttribute("aria-expanded", "true");
    expect(screen.queryByText("Cookbooks")).not.toBeInTheDocument();
  });
});
