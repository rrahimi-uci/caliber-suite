import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import type { ToolDefinition } from "@/api/workflowTypes";
import { ToolManagePanel } from "@/components/tools/ToolManagePanel";
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


function renderPanel(tool: ToolDefinition, onNewVersion = vi.fn()): ReturnType<typeof vi.fn> {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <ToolManagePanel tool={tool} onNewVersion={onNewVersion} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return onNewVersion;
}

function stubToolFamily(tool: ToolDefinition = makeTool()): void {
  server.use(
    http.get(`${API_BASE}/tools/:toolId/versions`, () => HttpResponse.json(envelope([tool]))),
  );
}


function asViewer(): void {
  server.use(
    http.get(`${API_BASE}/me`, () =>
      HttpResponse.json(envelope({ user_id: "@viewer", scopes: ["caliber.viewer"], is_admin: false })),
    ),
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("ToolManagePanel", () => {
  it("shows the agent binding and the version history to everyone, and no edit controls to a viewer", async () => {
    asViewer();
    stubToolFamily();
    renderPanel(makeTool());

    expect(await screen.findByTestId("tool-agent-binding")).toHaveTextContent("lookup_order");
    expect(screen.getByTestId("tool-versions")).toBeInTheDocument();
    expect(screen.queryByTestId("tool-edit-description")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tool-save")).not.toBeInTheDocument();
    // Making a new version registers a tool: admins only.
    expect(screen.queryByTestId("tool-new-version")).not.toBeInTheDocument();
  });

  it("starts a new version of the tool for an admin", async () => {
    stubToolFamily();
    const tool = makeTool();
    const onNewVersion = renderPanel(tool);

    await userEvent.click(await screen.findByTestId("tool-new-version"));
    expect(onNewVersion).toHaveBeenCalledWith(tool);
  });

  it("edits and saves a tool as admin", async () => {
    let current = makeTool();
    server.use(
      http.patch(`${API_BASE}/tools/:toolId`, async ({ request }) => {
        const body = (await request.json()) as Partial<ToolDefinition>;
        current = { ...current, ...body };
        return HttpResponse.json(envelope(current));
      }),
    );
    stubToolFamily();

    const user = userEvent.setup();
    renderPanel(current);
    await screen.findByTestId("tool-edit-description");

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
    expect(current.requires_approval).toBe(true);
    expect(current.allow_in_preview).toBe(false);
  });

  it("surfaces a save error without losing the edited draft", async () => {
    server.use(
      http.patch(`${API_BASE}/tools/:toolId`, () => HttpResponse.json({ detail: "save denied" }, { status: 500 })),
    );
    stubToolFamily();

    const user = userEvent.setup();
    renderPanel(makeTool());
    await screen.findByTestId("tool-edit-description");
    await user.type(screen.getByTestId("tool-edit-description"), " (edited)");
    await user.click(screen.getByTestId("tool-save"));

    expect(await screen.findByTestId("tool-save-error")).toHaveTextContent("save denied");
    expect(screen.getByTestId("tool-edit-description")).toHaveValue("Lookup an order by id (edited)");
  });
});
