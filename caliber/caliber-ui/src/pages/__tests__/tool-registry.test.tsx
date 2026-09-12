import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { caliberApi } from "@/api/caliberApi";
import type { ToolDefinition } from "@/api/workflowTypes";
import { ToolRegistry } from "@/pages/ToolRegistry";
import { server } from "@/test/server";
import { setActiveProjectId } from "@/workspace/activeWorkspace";

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
      properties: { order_id: { type: "string", description: "Order id" } },
      required: ["order_id"],
    },
    output_schema: {
      type: "object",
      properties: { status: { type: "string" } },
    },
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

function renderRegistry(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={["/tools"]}>
        <Routes>
          <Route path="/tools" element={<ToolRegistry />} />
          <Route path="/workflows/:workflowId" element={<div>WORKFLOW ROUTE</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  window.localStorage.clear();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

describe("ToolRegistry list", () => {
  it("filters the registry, toggles list/grid view, and opens a tool's workspace", async () => {
    const tools: ToolDefinition[] = [
      makeTool(),
      makeTool({
        tool_id: "TL-2",
        name: "send_refund",
        side_effect_level: "external_action",
        requires_approval: true,
        allow_in_preview: false,
        description: "",
      }),
    ];
    server.use(
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope(tools))),
      http.get(`${API_BASE}/tools/:toolId`, ({ params }) =>
        HttpResponse.json(envelope(tools.find((t) => t.tool_id === params.toolId) ?? tools[0])),
      ),
    );

    const user = userEvent.setup();
    renderRegistry();

    expect(await screen.findByTestId("tool-row-lookup_order")).toBeInTheDocument();
    expect(screen.getByTestId("tool-row-send_refund")).toBeInTheDocument();
    expect(screen.getByTestId("tool-tile-registry")).toHaveTextContent("2");
    expect(screen.getByTestId("tool-tile-approval")).toHaveTextContent("1");
    expect(screen.getByTestId("tool-tile-preview")).toHaveTextContent("1");

    // Search narrows the grid.
    await user.type(screen.getByLabelText("Search tools"), "refund");
    expect(screen.queryByTestId("tool-row-lookup_order")).not.toBeInTheDocument();
    expect(screen.getByTestId("tool-row-send_refund")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Search tools"));

    // Side-effect filter is additive with status.
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Filter by side effect" }),
      "external_action",
    );
    expect(screen.queryByTestId("tool-row-lookup_order")).not.toBeInTheDocument();
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Filter by status" }),
      "archived",
    );
    expect(screen.queryByTestId("tool-row-send_refund")).not.toBeInTheDocument();
    expect(screen.getByText("No tools match the current filters.")).toBeInTheDocument();

    // Reset filters and switch to list view.
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by side effect" }), "");
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by status" }), "");
    await user.click(screen.getByRole("button", { name: /list/i }));
    expect(await screen.findByTestId("tool-list-row-lookup_order")).toBeInTheDocument();

    // Opening from the list row's Open action enters the workspace.
    await user.click(
      within(screen.getByTestId("tool-list-row-lookup_order")).getByRole("button", { name: "Open" }),
    );
    expect(await screen.findByTestId("tool-workspace-header")).toHaveTextContent("lookup_order");
    expect(screen.getByTestId("tool-workspace-status-badge")).toHaveTextContent("Tested");

    await user.click(screen.getByRole("button", { name: "Back to tools" }));
    // View mode (list) persisted across the workspace round-trip.
    expect(await screen.findByTestId("tool-list-row-lookup_order")).toBeInTheDocument();
  });

  it("shows the load error", async () => {
    server.use(http.get(`${API_BASE}/tools`, () => HttpResponse.json({ detail: "tools down" }, { status: 500 })));
    renderRegistry();
    expect(await screen.findByText("Failed to load tools")).toBeInTheDocument();
    expect(screen.getByText("tools down")).toBeInTheDocument();
  });

  it("shows the no-tools empty state and toggles the register wizard", async () => {
    server.use(http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))));
    renderRegistry();
    expect(await screen.findByTestId("tools-empty")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByTestId("tool-register-action"));
    expect(await screen.findByTestId("tool-wizard")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Back to tools" }));
    expect(screen.queryByTestId("tool-wizard")).not.toBeInTheDocument();
  });
});

describe("Tool Workspace — Spec stage", () => {
  it("renders implementation facts, schema, and source; shows unavailable-source", async () => {
    server.use(
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([makeTool()]))),
      http.get(`${API_BASE}/tools/:toolId`, () => HttpResponse.json(envelope(makeTool()))),
    );
    const user = userEvent.setup();
    renderRegistry();
    await user.click(await screen.findByTestId("tool-open-lookup_order"));

    expect(await screen.findByTestId("tool-spec-implementation")).toBeInTheDocument();
    expect(screen.getByTestId("tool-spec-id")).toHaveTextContent("TL-1");
    expect(screen.getByTestId("tool-spec-callable")).toHaveTextContent(
      "caliber.workflows.demo_tools.lookup_order",
    );
    expect(await screen.findByTestId("tool-spec-signature")).toBeInTheDocument();
    expect(await screen.findByTestId("tool-spec-source")).toBeInTheDocument();

    // Re-render with source unavailable.
    server.use(
      http.get(`${API_BASE}/tools/:toolId/source`, () =>
        HttpResponse.json(
          envelope({
            module_path: "tools.docs",
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
    await user.click(screen.getByRole("button", { name: "Sandbox" }));
    await user.click(screen.getByRole("button", { name: "Spec" }));
    expect(await screen.findByTestId("tool-spec-source-unavailable")).toHaveTextContent(
      "module not importable",
    );
  });
});

describe("Tool Workspace — Sandbox stage", () => {
  it("persists a durable sandbox run after invoking the tool", async () => {
    const savedRuns: Array<Record<string, unknown>> = [];
    server.use(
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([makeTool()]))),
      http.post(`${API_BASE}/tools/:toolId/test-run`, async ({ request }) => {
        const body = (await request.json()) as { input: Record<string, unknown> };
        return HttpResponse.json(
          envelope({ tool_id: "TL-1", output: { echo: body.input }, mocked: false, duration_ms: 4, error: null }),
        );
      }),
      http.post(`${API_BASE}/tools/test-runs`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        savedRuns.push(body);
        return HttpResponse.json(envelope({ test_run_id: "TTR-1" }), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    renderRegistry();
    await user.click(await screen.findByTestId("tool-open-lookup_order"));
    await user.click(await screen.findByRole("button", { name: "Sandbox" }));

    await user.click(screen.getByRole("button", { name: "Test Run" }));
    await waitFor(() => expect(savedRuns).toHaveLength(1));
    expect(savedRuns[0]).toMatchObject({ tool_id: "TL-1", kind: "sandbox" });
  });
});

describe("Tool Workspace — Fixtures stage", () => {
  it("saves fixture cases and refreshes the tool detail", async () => {
    let saveCount = 0;
    server.use(
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([makeTool()]))),
      http.put(`${API_BASE}/tools/:toolId/test-cases`, async ({ request }) => {
        saveCount += 1;
        const body = (await request.json()) as { test_cases: unknown[] };
        return HttpResponse.json(envelope({ tool_id: "TL-1", test_cases: body.test_cases }));
      }),
    );

    const user = userEvent.setup();
    renderRegistry();
    await user.click(await screen.findByTestId("tool-open-lookup_order"));
    await user.click(await screen.findByRole("button", { name: "Fixtures" }));

    expect(await screen.findByTestId("tool-fixtures-calibration")).toBeInTheDocument();
    await user.type(screen.getByTestId("tool-fixtures-calibration-case-name"), "case one");
    await user.click(screen.getByTestId("tool-fixtures-calibration-save"));
    await waitFor(() => expect(screen.getByTestId("tool-fixtures-calibration-saved")).toBeInTheDocument());
    expect(saveCount).toBe(1);
  });
});

describe("Tool Workspace — Test Runs stage", () => {
  it("shows the empty history state, then pins a baseline and computes regressions", async () => {
    server.use(http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([makeTool()]))));

    const user = userEvent.setup();
    renderRegistry();
    await user.click(await screen.findByTestId("tool-open-lookup_order"));
    await user.click(await screen.findByRole("button", { name: "Test Runs" }));
    expect(await screen.findByText(/No saved runs yet/)).toBeInTheDocument();

    const runA = {
      test_run_id: "TTR-1",
      tool_id: "TL-1",
      tool_version: "1.0",
      kind: "sandbox",
      test_set_size: 1,
      passed_count: 0,
      failed_count: 1,
      partial_count: 0,
      overall_score: 0,
      trace_id: null,
      mlflow_run_id: null,
      created_by: "@test",
      status: "completed",
      created_at: "2025-01-02T00:00:00Z",
      completed_at: "2025-01-02T00:00:00Z",
    };
    const runB = { ...runA, test_run_id: "TTR-2", overall_score: 1, passed_count: 1, failed_count: 0, created_at: "2025-01-03T00:00:00Z" };
    const details: Record<string, unknown> = {
      "TTR-1": { ...runA, results: [{ name: "case-1", input: { a: 1 }, output: { ok: false }, error: "boom", verdict: "fail", score: 0 }] },
      "TTR-2": { ...runB, results: [{ name: "case-1", input: { a: 1 }, output: { ok: true }, verdict: "pass", score: 1 }] },
    };
    let baselineRunId: string | null = null;

    server.use(
      http.get(`${API_BASE}/tools/:toolId/workspace`, () =>
        HttpResponse.json(
          envelope({
            version: "1.0",
            side_effect_level: "read",
            status: "active",
            lifecycle: "Tested",
            last_run: null,
            baseline_run_id: baselineRunId,
            baseline_run: null,
            has_fixtures: false,
            last_calibration_score: null,
          }),
        ),
      ),
      http.get(`${API_BASE}/tools/test-runs`, () => HttpResponse.json(envelope([runB, runA]))),
      http.get(`${API_BASE}/tools/test-runs/:testRunId`, ({ params }) =>
        HttpResponse.json(envelope(details[String(params.testRunId)])),
      ),
      http.post(`${API_BASE}/tools/:toolId/baseline`, async ({ request }) => {
        const body = (await request.json()) as { test_run_id: string };
        baselineRunId = body.test_run_id;
        return HttpResponse.json(envelope({ baseline_run_id: baselineRunId }));
      }),
    );

    // Re-enter the Runs stage now that history is non-empty.
    await user.click(screen.getByRole("button", { name: "Spec" }));
    await user.click(screen.getByRole("button", { name: "Test Runs" }));

    expect(await screen.findByTestId("tool-workspace-run-results")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Set as baseline" }));
    await waitFor(() => expect(screen.getByTestId("tool-run-baseline-marker")).toBeInTheDocument());

    await user.click(screen.getByLabelText("View run TTR-1"));
    expect(await screen.findByTestId("tool-workspace-run-comparison")).toBeInTheDocument();
    expect(screen.getByTestId("tool-run-score-delta")).toHaveTextContent("-100%");
    expect(screen.getByTestId("tool-run-regressions")).toHaveTextContent("1 regression");

    // Kind filter narrows the fetch and clears the viewed run.
    await user.selectOptions(screen.getByLabelText("Filter runs by kind"), "hardening");
  });

  it("surfaces a baseline-pin error", async () => {
    const run = {
      test_run_id: "TTR-1",
      tool_id: "TL-1",
      tool_version: "1.0",
      kind: "sandbox",
      test_set_size: 0,
      passed_count: 0,
      failed_count: 0,
      partial_count: 0,
      overall_score: null,
      trace_id: null,
      mlflow_run_id: null,
      created_by: "@test",
      status: "completed",
      created_at: "2025-01-02T00:00:00Z",
      completed_at: "2025-01-02T00:00:00Z",
    };
    server.use(
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([makeTool()]))),
      http.get(`${API_BASE}/tools/test-runs`, () => HttpResponse.json(envelope([run]))),
      http.get(`${API_BASE}/tools/test-runs/:testRunId`, () => HttpResponse.json(envelope({ ...run, results: [] }))),
      http.post(`${API_BASE}/tools/:toolId/baseline`, () =>
        HttpResponse.json({ detail: "baseline denied" }, { status: 500 }),
      ),
    );

    const user = userEvent.setup();
    renderRegistry();
    await user.click(await screen.findByTestId("tool-open-lookup_order"));
    await user.click(await screen.findByRole("button", { name: "Test Runs" }));
    await user.click(await screen.findByRole("button", { name: "Set as baseline" }));
    expect(await screen.findByText("baseline denied")).toBeInTheDocument();
  });
});

describe("Tool Workspace — Hardening stage", () => {
  it("runs the deterministic suite and persists a durable suite run", async () => {
    const savedRuns: Array<Record<string, unknown>> = [];
    server.use(
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([makeTool()]))),
      http.post(`${API_BASE}/tools/:toolId/calibrate`, () =>
        HttpResponse.json(
          envelope({
            tool_id: "TL-1",
            pass_rate: 1,
            total: 1,
            passed: 1,
            cases: [{ name: "case one", passed: true, output: { ok: true }, error: null, duration_ms: 2 }],
            ran_at: "2025-01-01T00:00:00Z",
          }),
        ),
      ),
      http.post(`${API_BASE}/tools/test-runs`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        savedRuns.push(body);
        return HttpResponse.json(envelope({ test_run_id: "TTR-suite" }), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    renderRegistry();
    await user.click(await screen.findByTestId("tool-open-lookup_order"));
    await user.click(await screen.findByRole("button", { name: "Hardening" }));

    expect(await screen.findByTestId("tool-hardening-deterministic")).toBeInTheDocument();
    expect(screen.getByTestId("tool-hardening-llm")).toBeInTheDocument();
    await user.type(screen.getByTestId("tool-hardening-calibration-case-name"), "case one");
    await user.click(screen.getByTestId("tool-hardening-calibrate-btn"));

    await waitFor(() => expect(screen.getByTestId("tool-hardening-calibration-result")).toBeInTheDocument());
    await waitFor(() => expect(savedRuns).toHaveLength(1));
    expect(savedRuns[0]).toMatchObject({ tool_id: "TL-1", kind: "suite" });
  });

  it("generates and runs LLM-judged unit tests and persists a durable hardening run", async () => {
    const savedRuns: Array<Record<string, unknown>> = [];
    server.use(http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([makeTool()]))));
    server.use(
      http.post(`${API_BASE}/tools/test-runs`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        savedRuns.push(body);
        return HttpResponse.json(envelope({ test_run_id: "TTR-hardening" }), { status: 201 });
      }),
    );

    vi.spyOn(caliberApi, "getAssistantConfig").mockResolvedValue({
      engine: "fake",
      model: "gpt-4o-mini",
      provider: "openai",
      reasoning: "medium",
      enabled: true,
      disabled_intents: [],
      disabled_domains: [],
      available_models: [{ id: "gpt-4o-mini", name: "GPT-4o Mini", provider: "openai" }],
    });
    vi.spyOn(caliberApi, "createAssistantSession").mockResolvedValue({
      session_id: "ASST-1",
      title: "session",
      owner: "@test",
      status: "active",
      goal: "",
      metadata_: {},
      active_draft_id: null,
      created_at: NOW,
      updated_at: NOW,
    });
    vi.spyOn(caliberApi, "sendAssistantMessage").mockImplementation(async (_sessionId, body) => {
      const content = body.content.includes("Generate the unit test cases now")
        ? JSON.stringify([
            {
              input: { order_id: "1" },
              expectedOutput: { status: "open" },
              expectedBehavior: "returns order status",
              tags: ["happy"],
            },
          ])
        : '{"verdict":"pass","score":1,"reasoning":"Matches."}';
      return {
        assistant_message: {
          message_id: "m-1",
          session_id: "ASST-1",
          role: "assistant",
          content,
          metadata_: {},
          sequence_number: 1,
          created_at: NOW,
        },
        questions: [],
        draft_updates: [],
        run: null,
      };
    });
    vi.spyOn(caliberApi, "testRunTool").mockResolvedValue({
      tool_id: "TL-1",
      output: { status: "open" },
      mocked: false,
      duration_ms: 3,
      error: null,
    });

    const user = userEvent.setup();
    renderRegistry();
    await user.click(await screen.findByTestId("tool-open-lookup_order"));
    await user.click(await screen.findByRole("button", { name: "Hardening" }));

    await user.click(screen.getByTestId("tool-tests-generate"));
    expect(await screen.findByText("Unit Test 1")).toBeInTheDocument();
    await user.click(screen.getByTestId("tool-tests-run"));

    await waitFor(() => expect(savedRuns).toHaveLength(1));
    expect(savedRuns[0]).toMatchObject({ tool_id: "TL-1", kind: "hardening" });
  });
});

describe("Tool Workspace — Publish stage", () => {
  it("deprecates and archives as admin, and shows project access without an active project", async () => {
    // A stateful GET so refetch-after-mutation reflects the new status
    // instead of the (stateless) default handler reverting it to "active".
    let current = makeTool();
    server.use(
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([current]))),
      http.get(`${API_BASE}/tools/:toolId`, () => HttpResponse.json(envelope(current))),
      http.patch(`${API_BASE}/tools/:toolId`, async ({ request }) => {
        const body = (await request.json()) as Partial<ToolDefinition>;
        current = { ...current, ...body };
        return HttpResponse.json(envelope(current));
      }),
      http.post(`${API_BASE}/tools/:toolId/archive`, () => {
        current = { ...current, status: "archived" };
        return HttpResponse.json(envelope(current));
      }),
    );

    const user = userEvent.setup();
    renderRegistry();
    await user.click(await screen.findByTestId("tool-open-lookup_order"));
    await user.click(await screen.findByRole("button", { name: "Publish" }));

    expect(await screen.findByTestId("tool-publish-status")).toBeInTheDocument();
    expect(screen.getByTestId("tool-publish-usage-empty")).toBeInTheDocument();
    expect(screen.getByTestId("tool-publish-access")).toHaveTextContent(
      "Select a project to see resource permissions.",
    );

    await user.click(screen.getByTestId("tool-publish-deprecate"));
    await waitFor(() => expect(screen.getByTestId("tool-publish-deprecate")).toBeDisabled());
    expect(await screen.findByText("deprecated")).toBeInTheDocument();

    await user.click(screen.getByTestId("tool-publish-archive"));
    await waitFor(() => expect(screen.getByTestId("tool-publish-archive")).toBeDisabled());
  });

  it("shows the non-admin fallback, an archive error, and project access with permission", async () => {
    setActiveProjectId("proj-1");
    server.use(
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([makeTool()]))),
      http.get(`${API_BASE}/me`, () =>
        HttpResponse.json(envelope({ user_id: "@viewer", scopes: ["caliber.viewer"], is_admin: false })),
      ),
      http.get(`${API_BASE}/projects/proj-1`, () =>
        HttpResponse.json(
          envelope({
            project_id: "proj-1",
            name: "Proj",
            description: "",
            owner: "@sarah",
            status: "active",
            created_at: NOW,
            updated_at: NOW,
            access_role: "editor",
            permissions: ["resource.publish"],
          }),
        ),
      ),
      http.post(`${API_BASE}/tools/:toolId/archive`, () =>
        HttpResponse.json({ detail: "archive denied" }, { status: 500 }),
      ),
    );

    const user = userEvent.setup();
    renderRegistry();
    await user.click(await screen.findByTestId("tool-open-lookup_order"));
    await user.click(await screen.findByRole("button", { name: "Publish" }));

    expect(await screen.findByText("Deprecate / archive actions require admin access.")).toBeInTheDocument();
    expect(screen.queryByTestId("tool-publish-deprecate")).not.toBeInTheDocument();
    expect(screen.getByTestId("tool-publish-access")).toHaveTextContent("editor");
    expect(screen.getByText("You can publish project resources.")).toBeInTheDocument();
  });

  it("shows tool usage rows when the tool is referenced by a workflow", async () => {
    server.use(
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([makeTool()]))),
      http.get(`${API_BASE}/tools/:toolId/usage`, () =>
        HttpResponse.json(
          envelope({
            tool_id: "TL-1",
            name: "lookup_order",
            usage: [{ workflow_id: "WF-9", version_id: "V-1", version_number: 3, status: "published" }],
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderRegistry();
    await user.click(await screen.findByTestId("tool-open-lookup_order"));
    await user.click(await screen.findByRole("button", { name: "Publish" }));

    expect(await screen.findByText("WF-9")).toBeInTheDocument();
    expect(screen.getByText(/v3 \(published\)/)).toBeInTheDocument();
    expect(screen.queryByTestId("tool-publish-usage-empty")).not.toBeInTheDocument();
  });
});
