/**
 * Tests for the in-canvas NL copilot (P4). The dock calls `…/copilot-edit`
 * (mocked via MSW), renders the proposal as a GraphDiff, and applies the
 * accepted manifest to the canvas via `onApply`.
 */

import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type {
  CopilotEditResult,
  GraphDiff,
  PreviewResult,
  PreviewStep,
  WorkflowManifest,
} from "@/api/workflowTypes";
import { WorkflowCopilot } from "@/components/workflows/WorkflowCopilot";
import { render, screen, userEvent, waitFor } from "@/test/utils";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const COPILOT_URL = `${API_BASE}/workflow-versions/WFV-1/copilot-edit`;
const PREVIEW_URL = `${API_BASE}/workflow-versions/WFV-1/preview-run`;

const MANIFEST = {
  schema_version: 1,
  workflow_id: "wf",
  name: "WF",
  nodes: { start: { id: "start", type: "start" } },
  edges: [],
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
    proposed_manifest: { ...MANIFEST, name: "Edited" } as WorkflowManifest,
    summary: "Add a PII guardrail",
    rationale: "Protects user data",
    graph_diff: emptyDiff({ added_nodes: [{ id: "g1", type: "guardrail" }], empty: false }),
    valid: true,
    report: { valid: true, errors: [], warnings: [] },
    grounding: { tools: ["lookup_policy", "get_order"], skills: [], eval_datasets: ["support-eval"] },
    usage: { input_tokens: 10, output_tokens: 5, cost_usd: 0.001 },
    ...overrides,
  };
}

function mockEdit(res: CopilotEditResult): void {
  server.use(http.post(COPILOT_URL, () => HttpResponse.json(envelope(res))));
}

function step(overrides: Partial<PreviewStep> = {}): PreviewStep {
  return {
    node_id: "agent",
    node_type: "agent",
    status: "ok",
    output: "hi",
    tool_calls: [],
    handoff_target: null,
    detail: "",
    ...overrides,
  };
}

function previewResult(steps: PreviewStep[], overrides: Partial<PreviewResult> = {}): PreviewResult {
  return {
    workflow_run_id: "WR-1",
    status: "completed",
    output: "done",
    error: null,
    tokens: 10,
    tags: {},
    steps,
    guardrail_results: [],
    preview: true,
    ...overrides,
  };
}

function mockPreview(res: PreviewResult): void {
  server.use(http.post(PREVIEW_URL, () => HttpResponse.json(envelope(res))));
}

function renderDock(onApply = vi.fn()) {
  render(<WorkflowCopilot versionId="WFV-1" manifest={MANIFEST} onApply={onApply} />);
  return { onApply };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("WorkflowCopilot", () => {
  it("renders the prompt input and suggestions; submit starts disabled", () => {
    renderDock();
    expect(screen.getByTestId("copilot-input")).toBeInTheDocument();
    expect(screen.getAllByTestId("copilot-suggestion").length).toBeGreaterThan(0);
    expect(screen.getByTestId("copilot-submit")).toBeDisabled();
  });

  it("fills the input from a suggestion chip", async () => {
    renderDock();
    const chips = screen.getAllByTestId("copilot-suggestion");
    await userEvent.click(chips[0]!);
    expect(screen.getByTestId("copilot-input")).toHaveValue(chips[0]!.textContent);
    expect(screen.getByTestId("copilot-submit")).not.toBeDisabled();
  });

  it("proposes a change and renders summary, diff, and grounding", async () => {
    mockEdit(result());
    renderDock();
    await userEvent.type(screen.getByTestId("copilot-input"), "add a guardrail");
    await userEvent.click(screen.getByTestId("copilot-submit"));

    expect(await screen.findByTestId("copilot-summary")).toHaveTextContent("Add a PII guardrail");
    expect(screen.getByTestId("diff-added-node")).toHaveTextContent("g1");
    expect(screen.getByTestId("copilot-grounding")).toHaveTextContent("2 tools");
    expect(screen.getByTestId("copilot-grounding")).toHaveTextContent("1 datasets");
  });

  it("applies the proposed manifest to the canvas on Accept", async () => {
    mockEdit(result());
    const { onApply } = renderDock();
    await userEvent.type(screen.getByTestId("copilot-input"), "add a guardrail");
    await userEvent.click(screen.getByTestId("copilot-submit"));
    await userEvent.click(await screen.findByTestId("copilot-accept"));

    expect(onApply).toHaveBeenCalledTimes(1);
    expect(onApply.mock.calls[0]![0]).toMatchObject({ name: "Edited", workflow_id: "wf" });
    // Proposal panel clears after applying.
    await waitFor(() => expect(screen.queryByTestId("copilot-summary")).not.toBeInTheDocument());
  });

  it("discards the proposal on Reject without applying", async () => {
    mockEdit(result());
    const { onApply } = renderDock();
    await userEvent.type(screen.getByTestId("copilot-input"), "add a guardrail");
    await userEvent.click(screen.getByTestId("copilot-submit"));
    await userEvent.click(await screen.findByTestId("copilot-reject"));

    expect(onApply).not.toHaveBeenCalled();
    expect(screen.queryByTestId("copilot-summary")).not.toBeInTheDocument();
    // Instruction is retained so the user can refine.
    expect(screen.getByTestId("copilot-input")).toHaveValue("add a guardrail");
  });

  it("disables Accept for an empty (no-op) proposal — the fake-provider case", async () => {
    mockEdit(
      result({
        summary: "No LLM configured — manifest returned unchanged.",
        graph_diff: emptyDiff(),
      }),
    );
    renderDock();
    await userEvent.type(screen.getByTestId("copilot-input"), "do something");
    await userEvent.click(screen.getByTestId("copilot-submit"));

    expect(await screen.findByTestId("copilot-accept")).toBeDisabled();
    expect(screen.getByText(/no changes proposed/i)).toBeInTheDocument();
  });

  it("warns when the proposal fails validation", async () => {
    mockEdit(
      result({
        valid: false,
        report: {
          valid: false,
          errors: [{ code: "tool", path: "nodes.agent", message: "unresolved", severity: "error" }],
          warnings: [],
        },
      }),
    );
    renderDock();
    await userEvent.type(screen.getByTestId("copilot-input"), "use a ghost tool");
    await userEvent.click(screen.getByTestId("copilot-submit"));

    expect(await screen.findByTestId("copilot-invalid")).toHaveTextContent("1 validation error");
    // Still applicable — the user decides.
    expect(screen.getByTestId("copilot-accept")).not.toBeDisabled();
  });

  it("surfaces a server error", async () => {
    server.use(
      http.post(COPILOT_URL, () => HttpResponse.json({ detail: "boom" }, { status: 502 })),
    );
    renderDock();
    await userEvent.type(screen.getByTestId("copilot-input"), "break it");
    await userEvent.click(screen.getByTestId("copilot-submit"));

    expect(await screen.findByTestId("copilot-error")).toBeInTheDocument();
  });

  // ── Iterate loop: run preview → inspect per-step → fix ──

  it("runs a preview and shows per-step statuses with a fix on failing steps", async () => {
    mockPreview(
      previewResult([
        step({ node_id: "agent", status: "ok" }),
        step({ node_id: "guardrail", node_type: "guardrail", status: "blocked", detail: "empty output" }),
      ]),
    );
    renderDock();
    await userEvent.click(screen.getByTestId("copilot-run-preview"));

    const steps = await screen.findAllByTestId("copilot-preview-step");
    expect(steps).toHaveLength(2);
    expect(screen.getByTestId("copilot-preview-output")).toHaveTextContent("completed");
    // Only the non-ok step offers a fix.
    expect(screen.getAllByTestId("copilot-fix-step")).toHaveLength(1);
  });

  it("closes the loop: 'Fix with copilot' feeds the failing step into a new edit", async () => {
    mockPreview(
      previewResult([
        step({ node_id: "guardrail", node_type: "guardrail", status: "blocked", detail: "empty output" }),
      ]),
    );
    mockEdit(result({ summary: "Loosen the guardrail" }));
    renderDock();
    await userEvent.click(screen.getByTestId("copilot-run-preview"));
    await userEvent.click(await screen.findByTestId("copilot-fix-step"));

    // The failing step is described in the instruction…
    const input = screen.getByTestId("copilot-input") as HTMLTextAreaElement;
    expect(input.value).toContain("guardrail");
    expect(input.value).toContain("blocked");
    // …and a fresh proposal comes back (loop closed).
    expect(await screen.findByTestId("copilot-summary")).toHaveTextContent("Loosen the guardrail");
  });

  it("surfaces a preview error", async () => {
    server.use(
      http.post(PREVIEW_URL, () => HttpResponse.json({ detail: "cannot preview" }, { status: 400 })),
    );
    renderDock();
    await userEvent.click(screen.getByTestId("copilot-run-preview"));

    expect(await screen.findByTestId("copilot-preview-error")).toBeInTheDocument();
  });
});
