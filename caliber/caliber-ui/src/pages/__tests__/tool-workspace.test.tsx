import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import type { ToolDefinition } from "@/api/workflowTypes";
import type {
  ToolTestRunDetail,
  ToolTestRunSummary,
  ToolWorkspaceResponse,
} from "@/api/types";

// Mock the API client so these tests target the Workspace wiring (open/back,
// durable run persistence, set-baseline, diff) without driving real network.
vi.mock("@/api/caliberApi", () => ({
  caliberApi: {
    listTools: vi.fn(),
    getTool: vi.fn(),
    getToolSource: vi.fn(),
    getToolUsage: vi.fn(),
    getToolWorkspace: vi.fn(),
    testRunTool: vi.fn(),
    saveToolTestRun: vi.fn(),
    listToolTestRuns: vi.fn(),
    getToolTestRun: vi.fn(),
    setToolBaseline: vi.fn(),
    getMe: vi.fn(),
  },
}));

import { caliberApi } from "@/api/caliberApi";
import { ToolRegistry } from "@/pages/ToolRegistry";

const mockApi = vi.mocked(caliberApi);
const NOW = "2026-01-01T00:00:00Z";

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

function workspace(overrides: Partial<ToolWorkspaceResponse> = {}): ToolWorkspaceResponse {
  return {
    version: "1.0",
    side_effect_level: "read",
    status: "active",
    lifecycle: "Tested",
    last_run: null,
    baseline_run_id: null,
    baseline_run: null,
    has_fixtures: false,
    last_calibration_score: null,
    ...overrides,
  };
}

function summary(overrides: Partial<ToolTestRunSummary> = {}): ToolTestRunSummary {
  return {
    test_run_id: "TTR-1",
    tool_id: "TL-1",
    tool_version: "1.0",
    kind: "sandbox",
    test_set_size: 1,
    passed_count: 1,
    failed_count: 0,
    partial_count: 0,
    overall_score: 1,
    trace_id: null,
    mlflow_run_id: null,
    created_by: "@test",
    status: "completed",
    created_at: "2025-01-02T10:00:00Z",
    completed_at: "2025-01-02T10:01:00Z",
    ...overrides,
  };
}

function detail(overrides: Partial<ToolTestRunDetail> = {}): ToolTestRunDetail {
  return {
    ...summary(),
    results: [
      {
        name: "case-a",
        input: { order_id: "1" },
        output: { status: "open" },
        error: null,
        verdict: "pass",
        score: 1,
        duration_ms: 4,
        reasoning: "Returned a status.",
      },
    ],
    ...overrides,
  };
}

function renderRegistry(): void {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        initialEntries={["/tools"]}
      >
        <Routes>
          <Route path="/tools" element={<ToolRegistry />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openWorkspace(): Promise<void> {
  await userEvent.click(await screen.findByTestId("tool-open-lookup_order"));
  await screen.findByTestId("tool-workspace-header");
}

beforeEach(() => {
  mockApi.listTools.mockResolvedValue([makeTool()]);
  mockApi.getTool.mockResolvedValue(makeTool());
  mockApi.getToolSource.mockResolvedValue({
    module_path: "caliber.workflows.demo_tools",
    callable_name: "lookup_order",
    available: true,
    signature: "lookup_order(order_id: str) -> dict",
    doc: "",
    source: "def lookup_order(order_id):\n    return {}\n",
    error: null,
  });
  mockApi.getToolUsage.mockResolvedValue({
    tool_id: "TL-1",
    name: "lookup_order",
    usage: [],
  });
  mockApi.getToolWorkspace.mockResolvedValue(workspace());
  mockApi.testRunTool.mockResolvedValue({
    tool_id: "TL-1",
    output: { status: "open" },
    mocked: false,
    duration_ms: 7,
    error: null,
  });
  mockApi.saveToolTestRun.mockResolvedValue(summary());
  mockApi.listToolTestRuns.mockResolvedValue([]);
  mockApi.getToolTestRun.mockResolvedValue(detail());
  mockApi.setToolBaseline.mockResolvedValue({ baseline_run_id: "TTR-1" });
  mockApi.getMe.mockResolvedValue({ is_admin: true } as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("ToolWorkspace", () => {
  it("opens a tool into the Workspace with a header + six stage tabs, and back returns", async () => {
    renderRegistry();
    await openWorkspace();

    expect(screen.getByTestId("tool-workspace-header")).toHaveTextContent("lookup_order");
    expect(screen.getByTestId("tool-workspace-status-badge")).toHaveTextContent("Tested");
    for (const label of ["Spec", "Sandbox", "Fixtures", "Test Runs", "Hardening", "Publish"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }

    await userEvent.click(screen.getByRole("button", { name: "Back to tools" }));
    expect(await screen.findByTestId("tool-row-lookup_order")).toBeInTheDocument();
    expect(screen.queryByTestId("tool-workspace-header")).not.toBeInTheDocument();
  });

  it("uses the lifecycle vocabulary — 'Hardening' is present and 'Calibration' is gone", async () => {
    renderRegistry();
    await openWorkspace();

    expect(screen.getByRole("button", { name: "Hardening" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Calibration" })).not.toBeInTheDocument();
    expect(screen.queryByText("Calibration")).not.toBeInTheDocument();
  });

  it("persists a Sandbox run as kind:'sandbox' so it appears under Test Runs", async () => {
    renderRegistry();
    await openWorkspace();
    await userEvent.click(screen.getByRole("button", { name: "Sandbox" }));

    // Run a single sandbox invoke ("Test Run" exact — distinct from the "Test
    // Runs" tab).
    await userEvent.click(await screen.findByRole("button", { name: "Test Run" }));
    expect(await screen.findByText("Success")).toBeInTheDocument();

    await waitFor(() => expect(mockApi.saveToolTestRun).toHaveBeenCalled());
    const payload = mockApi.saveToolTestRun.mock.calls[0]![0];
    expect(payload.tool_id).toBe("TL-1");
    expect(payload.kind).toBe("sandbox");
    expect(payload.results).toHaveLength(1);
    expect(payload.results[0]!.verdict).toBe("pass");

    // The persisted run is now listed under Test Runs.
    mockApi.listToolTestRuns.mockResolvedValue([summary()]);
    await userEvent.click(screen.getByRole("button", { name: "Test Runs" }));
    const history = await screen.findByTestId("tool-workspace-run-history");
    expect(within(history).getByText(/sandbox/)).toBeInTheDocument();
    expect(within(history).getByText(/1 pass/)).toBeInTheDocument();
  });

  it("Test Runs: set-baseline calls setToolBaseline", async () => {
    mockApi.listToolTestRuns.mockResolvedValue([summary()]);
    renderRegistry();
    await openWorkspace();
    await userEvent.click(screen.getByRole("button", { name: "Test Runs" }));

    // The latest run auto-views; pin it as baseline.
    await userEvent.click(await screen.findByRole("button", { name: /Set as baseline/i }));
    await waitFor(() =>
      expect(mockApi.setToolBaseline).toHaveBeenCalledWith("TL-1", "TTR-1"),
    );
  });

  it("Test Runs: a second run renders a baseline diff + regression", async () => {
    // History has two runs; the baseline is the (passing) older one.
    mockApi.listToolTestRuns.mockResolvedValue([
      summary({ test_run_id: "TTR-2", overall_score: 0, passed_count: 0, failed_count: 1 }),
      summary({ test_run_id: "TTR-1" }),
    ]);
    mockApi.getToolWorkspace.mockResolvedValue(workspace({ baseline_run_id: "TTR-1" }));
    // The viewed (newest) run now fails the case that passed in the baseline.
    mockApi.getToolTestRun.mockImplementation(async (id: string) => {
      if (id === "TTR-1") return detail({ test_run_id: "TTR-1" });
      return detail({
        test_run_id: "TTR-2",
        overall_score: 0,
        passed_count: 0,
        failed_count: 1,
        results: [
          {
            name: "case-a",
            input: { order_id: "1" },
            output: null,
            error: "boom",
            verdict: "fail",
            score: 0,
            duration_ms: 2,
            reasoning: "Errored.",
          },
        ],
      });
    });

    renderRegistry();
    await openWorkspace();
    await userEvent.click(screen.getByRole("button", { name: "Test Runs" }));

    const comparison = await screen.findByTestId("tool-workspace-run-comparison");
    expect(comparison).toBeInTheDocument();
    expect(within(comparison).getByTestId("tool-run-score-delta")).toHaveTextContent("-100%");
    expect(within(comparison).getByText(/1 regression/)).toBeInTheDocument();
  });

  it("Publish stage exposes deprecate/archive + where-used", async () => {
    renderRegistry();
    await openWorkspace();
    await userEvent.click(screen.getByRole("button", { name: "Publish" }));

    expect(await screen.findByTestId("tool-publish-deprecate")).toBeInTheDocument();
    expect(screen.getByTestId("tool-publish-archive")).toBeInTheDocument();
    expect(screen.getByTestId("tool-publish-usage-empty")).toBeInTheDocument();
  });
});
