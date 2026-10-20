/**
 * A tool has one page: its workspace. Making the next version of it starts in the Publish stage
 * and ends in the new version's own workspace.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import type { ToolDefinition } from "@/api/workflowTypes";
import { ToolRegistry } from "@/pages/ToolRegistry";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-06-08T12:00:00Z";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function makeTool(overrides: Partial<ToolDefinition> = {}): ToolDefinition {
  return {
    tool_id: "TL-1",
    name: "lookup_order",
    version: "1.0",
    description: "Lookup an order by id",
    module_path: "caliber.workflows.demo_tools",
    callable_name: "lookup_order",
    input_schema: {
      type: "object",
      properties: { order_id: { type: "string" } },
      required: ["order_id"],
    },
    output_schema: { type: "object", properties: { status: { type: "string" } } },
    side_effect_level: "read",
    requires_approval: false,
    allow_in_preview: true,
    secret_refs: [],
    test_cases: [],
    last_calibration: null,
    owner: "@team",
    status: "active",
    deprecated_at: null,
    successor_tool_id: null,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}



function Probe(): JSX.Element {
  const location = useLocation();
  return <div data-testid="at">{`${location.pathname}${location.search}`}</div>;
}

function serveTools(tools: ToolDefinition[]): { registered: Array<Record<string, unknown>> } {
  const registered: Array<Record<string, unknown>> = [];
  server.use(
    http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope(tools))),
    http.get(`${API_BASE}/tools/:toolId`, ({ params }) => {
      const found = tools.find((tool) => tool.tool_id === params.toolId);
      return found
        ? HttpResponse.json(envelope(found))
        : HttpResponse.json({ detail: "no such tool" }, { status: 404 });
    }),
    http.get(`${API_BASE}/tools/:toolId/versions`, () => HttpResponse.json(envelope(tools))),
    http.get(`${API_BASE}/tools/:toolId/usage`, ({ params }) =>
      HttpResponse.json(envelope({ tool_id: params.toolId, name: "lookup_order", usage: [] })),
    ),
    http.post(`${API_BASE}/tools`, async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      registered.push(body);
      return HttpResponse.json(
        envelope(makeTool({ ...(body as Partial<ToolDefinition>), tool_id: "TL-2" })),
        { status: 201 },
      );
    }),
  );
  return { registered };
}

function renderAt(entry: string): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={[entry]}
      >
        <Probe />
        <Routes>
          <Route path="/tools" element={<ToolRegistry />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("a tool's new version, from its workspace", () => {
  it("is offered in the Publish stage, opens the wizard on the tool, registers, and lands in the new workspace", async () => {
    const { registered } = serveTools([makeTool()]);
    const user = userEvent.setup();
    renderAt("/tools?tool=TL-1");

    await screen.findByTestId("tool-workspace-header");
    await user.click(screen.getByRole("button", { name: "Publish", exact: true }));
    await user.click(await screen.findByTestId("tool-new-version"));

    expect(screen.getByTestId("at")).toHaveTextContent("/tools?new_version_of=TL-1");
    expect(
      await screen.findByRole("heading", { name: "New version of lookup_order" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("wiz-version")).toHaveValue("1.1");

    for (let i = 0; i < 4; i += 1) await user.click(screen.getByTestId("wizard-next"));
    await user.click(screen.getByTestId("wizard-submit"));

    await waitFor(() => expect(registered).toHaveLength(1));
    expect(registered[0]).toMatchObject({ name: "lookup_order", version: "1.1" });
    await waitFor(() => expect(screen.getByTestId("at")).toHaveTextContent("/tools?tool=TL-2"));
  });

  it("goes back to the tool it was started from", async () => {
    serveTools([makeTool()]);
    const user = userEvent.setup();
    renderAt("/tools?new_version_of=TL-1");

    await screen.findByRole("heading", { name: "New version of lookup_order" });
    await user.click(screen.getByTestId("tool-new-version-back"));
    expect(screen.getByTestId("at")).toHaveTextContent("/tools?tool=TL-1");
    expect(await screen.findByTestId("tool-workspace-header")).toHaveTextContent("lookup_order");
  });

  it("says so when the tool to version cannot be loaded", async () => {
    serveTools([]);
    renderAt("/tools?new_version_of=TL-404");
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the tool to version");
  });

  it("shows a recoverable error, not an endless 'Loading tool…', for a tool id that does not exist", async () => {
    serveTools([]);
    const user = userEvent.setup();
    renderAt("/tools?tool=TL-gone");

    expect(await screen.findByTestId("tool-workspace-error")).toHaveTextContent(
      "Could not load this tool",
    );
    expect(screen.queryByText("Loading tool…")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Back to tools" }));
    expect(screen.getByTestId("at")).toHaveTextContent(/^\/tools$/);
  });

  it("keeps the last loaded tool on screen, with a warning, when a refresh fails", async () => {
    serveTools([makeTool()]);
    const user = userEvent.setup();
    renderAt("/tools?tool=TL-1");
    await screen.findByTestId("tool-workspace-header");

    // The record is re-read after an action (here: a stage change refetches on focus-like events);
    // make the next read fail and force one.
    server.use(
      http.patch(`${API_BASE}/tools/:toolId`, () =>
        HttpResponse.json(envelope(makeTool({ status: "deprecated" }))),
      ),
      http.get(`${API_BASE}/tools/:toolId`, () =>
        HttpResponse.json({ detail: "tool refresh unavailable" }, { status: 503 }),
      ),
    );
    await user.click(screen.getByRole("button", { name: "Publish", exact: true }));
    await user.click(await screen.findByTestId("tool-publish-deprecate"));

    expect(await screen.findByTestId("tool-workspace-refresh-warning")).toHaveTextContent(
      "tool refresh unavailable",
    );
    expect(screen.getByTestId("tool-workspace-header")).toHaveTextContent("lookup_order");
  });

  it("does not offer a new version of a tool CALIBER ships itself", async () => {
    serveTools([makeTool({ builtin: true })]);
    const user = userEvent.setup();
    renderAt("/tools?tool=TL-1");

    await screen.findByTestId("tool-workspace-header");
    await user.click(screen.getByRole("button", { name: "Publish", exact: true }));
    await screen.findByTestId("tool-publish-status");
    expect(screen.queryByTestId("tool-new-version")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tool-manage")).not.toBeInTheDocument();
  });
});
