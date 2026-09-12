import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import type { ToolDefinition } from "@/api/workflowTypes";
import { ToolDetail } from "@/pages/ToolDetail";
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

function renderDetail(toolId = "TL-1"): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={[`/tools/${toolId}`]}>
        <Routes>
          <Route path="/tools/:toolId" element={<ToolDetail />} />
          <Route path="/tools" element={<div>TOOLS LIST ROUTE</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function stubToolFamily(tool: ToolDefinition = makeTool()): void {
  server.use(
    http.get(`${API_BASE}/tools/:toolId/versions`, () => HttpResponse.json(envelope([tool]))),
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("ToolDetail", () => {
  it("shows a loading state, then the tool detail, hiding admin controls for a non-admin viewer", async () => {
    server.use(
      http.get(`${API_BASE}/tools/:toolId`, () => HttpResponse.json(envelope(makeTool()))),
      http.get(`${API_BASE}/me`, () =>
        HttpResponse.json(envelope({ user_id: "@viewer", scopes: ["caliber.viewer"], is_admin: false })),
      ),
    );
    stubToolFamily();

    renderDetail();
    expect(screen.getByText("Loading tool…")).toBeInTheDocument();

    expect(await screen.findByTestId("tool-detail")).toBeInTheDocument();
    expect(screen.getByTestId("tool-id")).toHaveTextContent("TL-1");
    expect(screen.getByTestId("tool-module")).toHaveTextContent("caliber.workflows.demo_tools");
    expect(screen.getByTestId("tool-callable")).toHaveTextContent("lookup_order");

    // Non-admin: no edit form, no deprecate/archive controls.
    expect(screen.queryByTestId("tool-edit-description")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tool-deprecate")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tool-archive")).not.toBeInTheDocument();
  });

  it("shows the not-found error with a link back to tools", async () => {
    server.use(
      http.get(`${API_BASE}/tools/:toolId`, () => HttpResponse.json({ detail: "no such tool" }, { status: 404 })),
    );
    renderDetail();

    const alert = await screen.findByTestId("tool-detail-error");
    expect(alert).toHaveTextContent("no such tool");
    expect(screen.getByRole("link", { name: "Back to tools" })).toHaveAttribute("href", "/tools");
  });

  it("edits and saves a tool as admin, then shows a refresh warning when the follow-up fetch fails", async () => {
    let current = makeTool();
    server.use(
      http.get(`${API_BASE}/tools/:toolId`, () => HttpResponse.json(envelope(current))),
      http.patch(`${API_BASE}/tools/:toolId`, async ({ request }) => {
        const body = (await request.json()) as Partial<ToolDefinition>;
        current = { ...current, ...body };
        return HttpResponse.json(envelope(current));
      }),
    );
    stubToolFamily();

    const user = userEvent.setup();
    renderDetail();
    await screen.findByTestId("tool-detail");

    await user.clear(screen.getByTestId("tool-edit-description"));
    await user.type(screen.getByTestId("tool-edit-description"), "Updated description");
    await user.selectOptions(screen.getByTestId("tool-edit-side-effect"), "write");
    await user.selectOptions(screen.getByTestId("tool-edit-status"), "deprecated");
    await user.clear(screen.getByTestId("tool-edit-owner"));
    await user.type(screen.getByTestId("tool-edit-owner"), "@new-owner");
    await user.type(screen.getByTestId("tool-edit-successor"), "TL-2");
    await user.click(screen.getByTestId("tool-edit-requires-approval"));
    await user.click(screen.getByTestId("tool-edit-allow-preview"));

    await user.click(screen.getByTestId("tool-save"));
    await waitFor(() => expect(screen.getByTestId("tool-save-status")).toHaveTextContent("Saved"));
    expect(current.description).toBe("Updated description");
    expect(current.side_effect_level).toBe("write");
    expect(current.status).toBe("deprecated");
    expect(current.owner).toBe("@new-owner");
    expect(current.successor_tool_id).toBe("TL-2");

    // A follow-up refetch (invalidated by the save) failing keeps the last
    // loaded tool on screen but surfaces a non-fatal warning banner.
    server.use(
      http.get(`${API_BASE}/tools/:toolId`, () => HttpResponse.json({ detail: "refresh failed" }, { status: 500 })),
    );
    await user.click(screen.getByTestId("tool-save"));
    expect(await screen.findByTestId("tool-detail-refresh-warning")).toHaveTextContent("refresh failed");
    // The page keeps rendering the last-good tool rather than blanking out.
    expect(screen.getByTestId("tool-detail")).toBeInTheDocument();
  });

  it("surfaces a save error without losing the edited draft", async () => {
    server.use(
      http.get(`${API_BASE}/tools/:toolId`, () => HttpResponse.json(envelope(makeTool()))),
      http.patch(`${API_BASE}/tools/:toolId`, () => HttpResponse.json({ detail: "save denied" }, { status: 500 })),
    );
    stubToolFamily();

    const user = userEvent.setup();
    renderDetail();
    await screen.findByTestId("tool-detail");
    await user.click(screen.getByTestId("tool-save"));
    expect(await screen.findByTestId("tool-save-error")).toHaveTextContent("save denied");
  });

  it("deprecates and archives a tool, and surfaces an archive error", async () => {
    let current = makeTool();
    server.use(
      http.get(`${API_BASE}/tools/:toolId`, () => HttpResponse.json(envelope(current))),
      http.patch(`${API_BASE}/tools/:toolId`, async ({ request }) => {
        const body = (await request.json()) as Partial<ToolDefinition>;
        current = { ...current, ...body };
        return HttpResponse.json(envelope(current));
      }),
      http.post(`${API_BASE}/tools/:toolId/archive`, () =>
        HttpResponse.json({ detail: "archive denied" }, { status: 500 }),
      ),
    );
    stubToolFamily();

    const user = userEvent.setup();
    renderDetail();
    await screen.findByTestId("tool-detail");

    expect(screen.getByTestId("tool-deprecate")).toBeEnabled();
    await user.click(screen.getByTestId("tool-deprecate"));
    await waitFor(() => expect(screen.getByTestId("tool-deprecate")).toBeDisabled());
    expect(await screen.findByText("v1.0 · 🟢 read · deprecated")).toBeInTheDocument();

    await user.click(screen.getByTestId("tool-archive"));
    expect(await screen.findByTestId("tool-archive-error")).toHaveTextContent("archive denied");
  });

  it("renders the implementation panel and the unavailable-source branch", async () => {
    server.use(
      http.get(`${API_BASE}/tools/:toolId`, () => HttpResponse.json(envelope(makeTool()))),
      http.get(`${API_BASE}/tools/:toolId/source`, () =>
        HttpResponse.json(
          envelope({
            module_path: "caliber.workflows.demo_tools",
            callable_name: "lookup_order",
            available: false,
            signature: null,
            doc: null,
            source: "",
            error: "module not importable",
          }),
        ),
      ),
    );
    stubToolFamily();

    renderDetail();
    await screen.findByTestId("tool-detail");
    expect(screen.getByTestId("tool-implementation")).toBeInTheDocument();
    expect(await screen.findByTestId("tool-source-unavailable")).toHaveTextContent(
      "module not importable",
    );
    expect(screen.queryByTestId("tool-signature")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tool-source")).not.toBeInTheDocument();
  });

  it("shows the available-source branch with the callable signature", async () => {
    server.use(http.get(`${API_BASE}/tools/:toolId`, () => HttpResponse.json(envelope(makeTool()))));
    stubToolFamily();

    renderDetail();
    await screen.findByTestId("tool-detail");
    expect(await screen.findByTestId("tool-signature")).toBeInTheDocument();
    expect(await screen.findByTestId("tool-source")).toBeInTheDocument();
  });

  it("runs the tool with valid input, rejects invalid JSON, and shows run errors", async () => {
    server.use(http.get(`${API_BASE}/tools/:toolId`, () => HttpResponse.json(envelope(makeTool()))));
    stubToolFamily();

    const user = userEvent.setup();
    renderDetail();
    await screen.findByTestId("tool-detail");

    // Before any run: empty state.
    expect(screen.getByTestId("tool-run-empty")).toBeInTheDocument();

    // Invalid JSON is rejected client-side without hitting the network.
    fireEvent.change(screen.getByTestId("tool-run-input"), { target: { value: "{bad" } });
    await user.click(screen.getByTestId("tool-run"));
    expect(await screen.findByTestId("tool-run-input-error")).toBeInTheDocument();

    // A valid JSON object runs successfully.
    server.use(
      http.post(`${API_BASE}/tools/:toolId/test-run`, async ({ request }) => {
        const body = (await request.json()) as { input: Record<string, unknown> };
        return HttpResponse.json(
          envelope({ tool_id: "TL-1", output: { echoed: body.input }, mocked: false, duration_ms: 4, error: null }),
        );
      }),
    );
    fireEvent.change(screen.getByTestId("tool-run-input"), {
      target: { value: '{"order_id":"O-1"}' },
    });
    await user.click(screen.getByTestId("tool-run"));
    expect(await screen.findByTestId("tool-run-result")).toHaveTextContent("O-1");
    expect(screen.queryByTestId("tool-run-input-error")).not.toBeInTheDocument();

    // A run that reaches the backend but fails shows the run error.
    server.use(
      http.post(`${API_BASE}/tools/:toolId/test-run`, () =>
        HttpResponse.json({ detail: "tool crashed" }, { status: 500 }),
      ),
    );
    await user.click(screen.getByTestId("tool-run"));
    expect(await screen.findByTestId("tool-run-error")).toHaveTextContent("tool crashed");
  });

  it("shows usage rows and the empty-usage state", async () => {
    server.use(
      http.get(`${API_BASE}/tools/:toolId`, () => HttpResponse.json(envelope(makeTool()))),
      http.get(`${API_BASE}/tools/:toolId/usage`, () =>
        HttpResponse.json(
          envelope({
            tool_id: "TL-1",
            name: "lookup_order",
            usage: [{ workflow_id: "WF-9", version_id: "V-1", version_number: 2, status: "published" }],
          }),
        ),
      ),
    );
    stubToolFamily();

    renderDetail();
    await screen.findByTestId("tool-detail");
    expect(await screen.findByText("WF-9")).toBeInTheDocument();
    expect(screen.getByText(/v2 \(published\)/)).toBeInTheDocument();
    expect(screen.queryByTestId("tool-usage-empty")).not.toBeInTheDocument();
  });

  it("shows the empty usage state when the tool is unreferenced", async () => {
    server.use(http.get(`${API_BASE}/tools/:toolId`, () => HttpResponse.json(envelope(makeTool()))));
    stubToolFamily();

    renderDetail();
    await screen.findByTestId("tool-detail");
    expect(await screen.findByTestId("tool-usage-empty")).toBeInTheDocument();
  });
});
