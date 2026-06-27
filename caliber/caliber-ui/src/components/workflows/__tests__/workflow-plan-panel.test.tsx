/**
 * Tests for the plan-to-build panel (the "Plan" view tab). The panel calls
 * `…/plan-build` (mocked via MSW), renders the authored workflow as a GraphDiff,
 * and applies the accepted manifest to the canvas via `onApply`.
 */

import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { CopilotEditResult, GraphDiff, WorkflowManifest } from "@/api/workflowTypes";
import { WorkflowPlanPanel } from "@/components/workflows/WorkflowPlanPanel";
import { render, screen, userEvent, waitFor } from "@/test/utils";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const PLAN_URL = `${API_BASE}/workflow-versions/WFV-1/plan-build`;

const MANIFEST = {
  schema_version: 1,
  workflow_id: "wf",
  name: "WF",
  nodes: { start: { id: "start", type: "start" } },
  edges: [],
} as unknown as WorkflowManifest;

const POPULATED = {
  ...MANIFEST,
  nodes: {
    start: { id: "start", type: "start" },
    agent: { id: "agent", type: "agent" },
  },
} as unknown as WorkflowManifest;

function envelope<T>(data: T): { data: T } {
  return { data };
}

function emptyDiff(overrides: Partial<GraphDiff> = {}): GraphDiff {
  return {
    added_nodes: [],
    removed_nodes: [],
    modified_nodes: [],
    added_edges: [],
    removed_edges: [],
    modified_edges: [],
    artifact_changes: [],
    deploy_gate_changes: [],
    empty: true,
    ...overrides,
  };
}

function result(overrides: Partial<CopilotEditResult> = {}): CopilotEditResult {
  return {
    proposed_manifest: { ...MANIFEST, name: "Triage WF" } as WorkflowManifest,
    summary: "3-step support triage",
    rationale: "Classifies, answers, then guards output",
    graph_diff: emptyDiff({ added_nodes: [{ id: "triage", type: "agent" }], empty: false }),
    valid: true,
    report: { valid: true, errors: [], warnings: [] },
    grounding: { tools: ["lookup_policy"], skills: [], eval_datasets: ["support-eval"] },
    usage: { input_tokens: 12, output_tokens: 6, cost_usd: 0.002 },
    ...overrides,
  };
}

function mockBuild(res: CopilotEditResult): void {
  server.use(http.post(PLAN_URL, () => HttpResponse.json(envelope(res))));
}

function renderPanel(manifest = MANIFEST, onApply = vi.fn()) {
  render(<WorkflowPlanPanel versionId="WFV-1" manifest={manifest} onApply={onApply} />);
  return { onApply };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("WorkflowPlanPanel", () => {
  it("renders the goal input and examples; submit starts disabled", () => {
    renderPanel();
    expect(screen.getByTestId("plan-input")).toBeInTheDocument();
    expect(screen.getAllByTestId("plan-example").length).toBeGreaterThan(0);
    expect(screen.getByTestId("plan-submit")).toBeDisabled();
  });

  it("fills the goal from an example chip", async () => {
    renderPanel();
    const chips = screen.getAllByTestId("plan-example");
    await userEvent.click(chips[0]!);
    expect(screen.getByTestId("plan-input")).toHaveValue(chips[0]!.textContent);
    expect(screen.getByTestId("plan-submit")).not.toBeDisabled();
  });

  it("builds a workflow and renders summary, diff, and grounding", async () => {
    mockBuild(result());
    renderPanel();
    await userEvent.type(screen.getByTestId("plan-input"), "triage support tickets");
    await userEvent.click(screen.getByTestId("plan-submit"));

    expect(await screen.findByTestId("plan-summary")).toHaveTextContent("3-step support triage");
    expect(screen.getByTestId("diff-added-node")).toHaveTextContent("triage");
    expect(screen.getByTestId("plan-grounding")).toHaveTextContent("1 tools");
    expect(screen.getByTestId("plan-grounding")).toHaveTextContent("1 datasets");
  });

  it("applies the authored manifest to the canvas on Accept", async () => {
    mockBuild(result());
    const { onApply } = renderPanel();
    await userEvent.type(screen.getByTestId("plan-input"), "triage support tickets");
    await userEvent.click(screen.getByTestId("plan-submit"));
    await userEvent.click(await screen.findByTestId("plan-accept"));

    expect(onApply).toHaveBeenCalledTimes(1);
    expect(onApply.mock.calls[0]![0]).toMatchObject({ name: "Triage WF", workflow_id: "wf" });
    await waitFor(() => expect(screen.queryByTestId("plan-summary")).not.toBeInTheDocument());
  });

  it("discards the proposal on Reject, keeping the goal", async () => {
    mockBuild(result());
    const { onApply } = renderPanel();
    await userEvent.type(screen.getByTestId("plan-input"), "triage support tickets");
    await userEvent.click(screen.getByTestId("plan-submit"));
    await userEvent.click(await screen.findByTestId("plan-reject"));

    expect(onApply).not.toHaveBeenCalled();
    expect(screen.queryByTestId("plan-summary")).not.toBeInTheDocument();
    expect(screen.getByTestId("plan-input")).toHaveValue("triage support tickets");
  });

  it("disables Accept for an empty (no-op) proposal — the fake-provider case", async () => {
    mockBuild(
      result({
        summary: "No LLM configured — manifest returned unchanged.",
        graph_diff: emptyDiff(),
      }),
    );
    renderPanel();
    await userEvent.type(screen.getByTestId("plan-input"), "do something");
    await userEvent.click(screen.getByTestId("plan-submit"));

    expect(await screen.findByTestId("plan-accept")).toBeDisabled();
    expect(screen.getByTestId("plan-empty-note")).toBeInTheDocument();
  });

  it("warns that building replaces a non-empty canvas", () => {
    renderPanel(POPULATED);
    expect(screen.getByTestId("plan-replace-note")).toBeInTheDocument();
  });

  it("does not warn on an empty/seed canvas", () => {
    renderPanel();
    expect(screen.queryByTestId("plan-replace-note")).not.toBeInTheDocument();
  });

  it("surfaces a server error", async () => {
    server.use(http.post(PLAN_URL, () => HttpResponse.json({ detail: "boom" }, { status: 502 })));
    renderPanel();
    await userEvent.type(screen.getByTestId("plan-input"), "break it");
    await userEvent.click(screen.getByTestId("plan-submit"));

    expect(await screen.findByTestId("plan-error")).toBeInTheDocument();
  });
});
