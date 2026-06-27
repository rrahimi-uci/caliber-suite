import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { showToast } from "@/lib/toast";
import { WorkflowEditor, newNode } from "@/pages/WorkflowEditor";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const NOW = "2026-01-01T00:00:00Z";
const streamState = vi.hoisted(() => ({
  event: null as Record<string, unknown> | null,
}));

vi.mock("@/hooks/useEventStream", () => ({
  useEventStream: () => {
    const event = streamState.event;
    streamState.event = null;
    return event;
  },
}));

vi.mock("@/lib/toast", () => ({
  showToast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));

function envelope<T>(data: T): { data: T } {
  return { data };
}

function makeVersion(overrides: Record<string, unknown> = {}) {
  return {
    version_id: "WFV-1",
    workflow_id: "WF-1",
    version_number: 1,
    status: "draft",
    manifest: {
      schema_version: 1,
      workflow_id: "WF-1",
      name: "Support",
      nodes: {
        start: { id: "start", type: "start", outputs: { user_message: { type: "string" } } },
        support_agent: {
          id: "support_agent",
          type: "agent",
          name: "support-agent",
          model: "inherit",
          instructions: { type: "inline", text: "hi" },
          tools: [],
          inputs: { input: { type: "string" } },
          outputs: { final_output: { type: "string" } },
        },
        for_each: {
          id: "for_each",
          type: "for_each",
          target_node_id: null,
          item_input_port: "items",
          max_items: 100,
          inputs: { items: { type: "structured" } },
          outputs: {
            results: { type: "structured" },
            text: { type: "string" },
            metadata: { type: "structured" },
          },
        },
        final: { id: "final", type: "output", inputs: { response: { type: "string" } } },
      },
      edges: [
        { id: "e1", from: "start", to: "support_agent", map: { user_message: "input" } },
        { id: "e2", from: "support_agent", to: "final", map: { final_output: "response" } },
      ],
    },
    manifest_hash: "hash1",
    compiler_version: null,
    compiled_artifact_uri: null,
    validation_report: null,
    created_by: "@test",
    created_at: NOW,
    published_by: null,
    published_at: null,
    ...overrides,
  };
}

function makeWorkflow(overrides: Record<string, unknown> = {}) {
  return {
    workflow_id: "WF-1",
    name: "Support",
    description: "",
    owner: "@test",
    status: "active",
    default_experiment_id: null,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makeRun(overrides: Record<string, unknown> = {}) {
  return {
    workflow_run_id: "WR-1",
    workflow_id: "WF-1",
    project_id: null,
    tenant_id: null,
    workflow_version_id: "WFV-1",
    deployment_alias: "manual",
    mlflow_run_id: null,
    trace_id: "trace-1",
    session_id: null,
    status: "running",
    source: "editor",
    priority: 0,
    queued_at: NOW,
    started_at: NOW,
    completed_at: null,
    current_node_id: "support_agent",
    summary: {
      output: "run-output",
      node_path: ["start", "support_agent"],
      steps: [
        {
          node_id: "start",
          node_type: "start",
          status: "ok",
          output: "",
          tool_calls: [],
          handoff_target: null,
          detail: "",
          duration_ms: 0,
        },
      ],
    },
    ...overrides,
  };
}

function buildWorkflowRunLineage(
  runId: string,
  runs: Array<Record<string, unknown>>,
): Record<string, unknown> | null {
  const map = new Map<string, Record<string, unknown>>();
  for (const item of runs) {
    const itemRunId = String(item.workflow_run_id ?? "");
    if (itemRunId) map.set(itemRunId, item);
  }
  const run = map.get(runId);
  if (!run) return null;

  const childrenByParent = new Map<string, Array<Record<string, unknown>>>();
  for (const item of map.values()) {
    const parentId =
      typeof item.parent_run_id === "string" ? item.parent_run_id : null;
    if (!parentId) continue;
    const next = childrenByParent.get(parentId) ?? [];
    next.push(item);
    childrenByParent.set(parentId, next);
  }

  let missingParentId: string | null = null;
  let parentCount = 0;
  let cursor: Record<string, unknown> = run;
  const seen = new Set<string>([runId]);
  while (typeof cursor.parent_run_id === "string" && cursor.parent_run_id) {
    const parentId = cursor.parent_run_id;
    if (seen.has(parentId)) break;
    seen.add(parentId);
    const parent = map.get(parentId);
    if (!parent) {
      missingParentId = parentId;
      break;
    }
    parentCount += 1;
    cursor = parent;
  }

  const rootRunId = String(cursor.workflow_run_id ?? runId);
  const connected = new Map<string, Record<string, unknown>>();
  const queue = [rootRunId];
  while (queue.length > 0) {
    const currentId = queue.shift()!;
    if (connected.has(currentId)) continue;
    const current = map.get(currentId);
    if (!current) continue;
    connected.set(currentId, current);
    for (const child of childrenByParent.get(currentId) ?? []) {
      const childId = String(child.workflow_run_id ?? "");
      if (childId) queue.push(childId);
    }
  }

  const lineageRuns = [...connected.values()].sort((left, right) => {
    const leftAttempt = Math.max(1, Number(left.attempt_number ?? 1));
    const rightAttempt = Math.max(1, Number(right.attempt_number ?? 1));
    if (leftAttempt !== rightAttempt) return leftAttempt - rightAttempt;
    const leftTime = String(
      left.queued_at ?? left.started_at ?? left.completed_at ?? "",
    );
    const rightTime = String(
      right.queued_at ?? right.started_at ?? right.completed_at ?? "",
    );
    if (leftTime !== rightTime) return leftTime.localeCompare(rightTime);
    return String(left.workflow_run_id ?? "").localeCompare(
      String(right.workflow_run_id ?? ""),
    );
  });

  return {
    workflow_run_id: runId,
    root_run_id: rootRunId,
    total_attempts: lineageRuns.length,
    parent_count: parentCount,
    child_count: childrenByParent.get(runId)?.length ?? 0,
    missing_parent_id: missingParentId,
    truncated: false,
    runs: lineageRuns,
  };
}

vi.mock("@/components/workflows/Canvas", () => ({
  Canvas: (props: Record<string, (...args: unknown[]) => void>) => (
    <div data-testid="mock-canvas">
      <div data-testid="wf-node-support_agent" />
      {props.onConnect && (
        <>
          <button data-testid="canvas-connect" onClick={() => props.onConnect({ source: "start", target: "support_agent" })} />
          <button data-testid="canvas-connect-self" onClick={() => props.onConnect({ source: "start", target: "start" })} />
          <button data-testid="canvas-connect-incompatible" onClick={() => props.onConnect({ source: "support_agent", target: "for_each" })} />
        </>
      )}
      {props.onEdgeClick && (
        <button data-testid="canvas-edge-click" onClick={() => props.onEdgeClick("e1")} />
      )}
      {props.onNodeDoubleClick && (
        <button data-testid="canvas-node-double-click" onClick={() => props.onNodeDoubleClick("support_agent")} />
      )}
      {props.onQuickAdd && (
        <>
          <button data-testid="canvas-quick-add-start" onClick={() => props.onQuickAdd("start")} />
          <button data-testid="canvas-quick-add-support" onClick={() => props.onQuickAdd("support_agent")} />
        </>
      )}
      {props.onDropNode && (
        <button data-testid="canvas-drop-node" onClick={() => props.onDropNode("guardrail", { x: 10, y: 20 })} />
      )}
      {props.onConnectionDrop && (
        <button data-testid="canvas-connection-drop" onClick={() => props.onConnectionDrop("support_agent", { x: 10, y: 20 }, { x: 120, y: 220 })} />
      )}
    </div>
  ),
}));

vi.mock("@/components/workflows/Inspector", () => ({
  Inspector: (props: Record<string, (...args: unknown[]) => void>) => (
    <div
      data-testid="mock-inspector"
      data-selected-node-id={String(props.selectedNodeId ?? "")}
      data-focus-field-key={String(props.focusFieldKey ?? "")}
    >
      <button data-testid="inspector-change-name" onClick={() => props.onChangeNode("support_agent", { name: "renamed" })} />
      <button data-testid="inspector-change-tools" onClick={() => props.onChangeNode("support_agent", { tools: ["grep_files"] })} />
      <button data-testid="inspector-change-workflow" onClick={() => props.onChangeWorkflow({ description: "updated" })} />
      <button data-testid="inspector-delete-node" onClick={() => props.onDeleteNode("final")} />
    </div>
  ),
}));

vi.mock("@/components/workflows/ConnectMapPopover", () => ({
  ConnectMapPopover: (props: Record<string, (...args: unknown[]) => void>) => (
    <div data-testid="mock-connect-map">
      <button data-testid="map-change" onClick={() => props.onChange({ input: "user_message" })} />
      <button data-testid="map-done" onClick={() => props.onDone()} />
      <button data-testid="map-remove" onClick={() => props.onRemove()} />
    </div>
  ),
}));

vi.mock("@/components/workflows/CodeView", () => ({
  CodeView: (props: {
    onApplyManifest: (manifest: Record<string, unknown>) => void;
    loadPython: () => Promise<string>;
  }) => (
    <div data-testid="mock-code-view">
      <button
        data-testid="code-apply"
        onClick={() =>
          props.onApplyManifest({
            schema_version: 1,
            workflow_id: "WF-1",
            name: "Code Applied",
            nodes: {
              start: { id: "start", type: "start", outputs: { user_message: { type: "string" } } },
              final: { id: "final", type: "output", inputs: { response: { type: "string" } } },
            },
            edges: [{ id: "e-code", from: "start", to: "final", map: { user_message: "response" } }],
          })
        }
      />
      <button data-testid="code-export" onClick={() => void props.loadPython()} />
    </div>
  ),
}));

vi.mock("@/components/workflows/WorkflowCopilot", () => ({
  WorkflowCopilot: (props: {
    manifest: Record<string, unknown>;
    onApply: (manifest: Record<string, unknown>) => void;
  }) => (
    <div data-testid="mock-copilot">
      <button
        data-testid="copilot-apply"
        onClick={() => props.onApply({ ...props.manifest, name: "Copilot Applied" })}
      />
    </div>
  ),
}));

vi.mock("@/components/workflows/WorkflowPlanPanel", () => ({
  WorkflowPlanPanel: (props: {
    manifest: Record<string, unknown>;
    onApply: (manifest: Record<string, unknown>) => void;
  }) => (
    <div data-testid="mock-plan-panel">
      <button
        data-testid="plan-apply"
        onClick={() => props.onApply({ ...props.manifest, name: "Plan Applied" })}
      />
    </div>
  ),
}));

vi.mock("@/components/workflows/PublishDrawer", () => ({
  PublishDrawer: (props: Record<string, (...args: unknown[]) => void>) => (
    <div data-testid="publish-drawer">
      <button data-testid="publish-validate" onClick={() => props.onValidate()} />
      <button data-testid="publish-run" onClick={() => props.onPublish()} />
      <button data-testid="publish-close" onClick={() => props.onClose()} />
    </div>
  ),
}));

function makeQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
}

function renderEditor(options?: {
  initialPath?: string;
  queryClient?: QueryClient;
}) {
  const qc = options?.queryClient ?? makeQueryClient();
  const renderTree = () => (
    <QueryClientProvider client={qc}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }} initialEntries={[options?.initialPath ?? "/workflows/WF-1/editor/WFV-1"]}>
        <Routes>
          <Route path="/workflows/:workflowId/editor/:versionId" element={<WorkflowEditor />} />
          <Route path="/workflows" element={<div>WORKFLOWS ROUTE</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const rendered = render(renderTree());
  return {
    ...rendered,
    rerenderEditor: () => rendered.rerender(renderTree()),
  };
}

beforeAll(() => {
  class ResizeObserverMock {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  Object.defineProperty(globalThis, "ResizeObserver", {
    value: ResizeObserverMock,
    writable: true,
  });
  server.listen({ onUnhandledRequest: "error" });
});

beforeEach(() => {
  server.use(
    http.get(`${API_BASE}/workflows/:workflowId`, ({ params }) =>
      HttpResponse.json(
        envelope(makeWorkflow({ workflow_id: String(params.workflowId ?? "WF-1") })),
      ),
    ),
  );
});

afterEach(() => {
  streamState.event = null;
  vi.clearAllMocks();
  server.resetHandlers();
});

afterAll(() => {
  server.close();
});

describe("WorkflowEditor handler branches", () => {
  it("renders immediately from cached workflow-version data", () => {
    const queryClient = makeQueryClient();
    queryClient.setQueryData(["workflow-version", "WFV-1"], makeVersion());
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
    );

    renderEditor({ queryClient });

    expect(screen.getByTestId("workflow-editor")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-editor-loading")).not.toBeInTheDocument();
  });

  it("shows an actionable editor load error when the workflow version request fails", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json({ detail: "version unavailable" }, { status: 500 }),
      ),
    );

    renderEditor();

    expect(await screen.findByTestId("workflow-editor-load-error")).toHaveTextContent(
      "Workflow editor could not be loaded.",
    );
    expect(screen.getByTestId("workflow-editor-load-error")).toHaveTextContent(
      "version unavailable",
    );
    expect(screen.getByTestId("workflow-editor-retry")).toHaveTextContent("Retry loading");
  });

  it("handles quick-add/connect/map mutations and save/publish errors", async () => {
    let patchCalls = 0;
    let previewBody: Record<string, unknown> | null = null;
    let runBody: Record<string, unknown> | null = null;
    let resumeBody: Record<string, unknown> | null = null;
    const runById: Record<string, ReturnType<typeof makeRun>> = {
      "WR-1": makeRun({ status: "running", attempt_number: 1, parent_run_id: null }),
      "WR-2": makeRun({
        workflow_run_id: "WR-2",
        workflow_version_id: "WFV-2",
        session_id: "SESSION-existing",
        status: "completed",
        attempt_number: 2,
        parent_run_id: "WR-1",
        current_node_id: "historic_agent",
        summary: {
          output: "older-run-output",
          node_path: ["start", "historic_agent", "final"],
          steps: [
            {
              node_id: "historic_agent",
              node_type: "agent",
              status: "ok",
              output: "older-run-output",
              tool_calls: [],
              handoff_target: null,
              detail: "",
              duration_ms: 12,
            },
          ],
        },
      }),
      "WR-3": makeRun({
        workflow_run_id: "WR-3",
        session_id: "SESSION-existing",
        status: "failed",
        attempt_number: 3,
        parent_run_id: "WR-2",
        current_node_id: "support_agent",
        error_summary: "third attempt failed",
        summary: {
          output: "failed output",
          node_path: ["start", "support_agent"],
          steps: [
            {
              node_id: "support_agent",
              node_type: "agent",
              status: "error",
              output: "failed output",
              tool_calls: [],
              handoff_target: null,
              detail: "third attempt failed",
              duration_ms: 8,
            },
          ],
        },
      }),
    };
    const sessionMemoryBySessionId: Record<string, Array<Record<string, unknown>>> = {
      "SESSION-existing": [
        {
          workflow_id: "WF-1",
          node_id: "support_agent",
          session_id: "SESSION-existing",
          message_history: [
            { role: "user", content: "Need help with refunds" },
            { role: "assistant", content: "Here is the refund policy" },
          ],
          message_count: 2,
          turn_count: 1,
          created_at: NOW,
          updated_at: NOW,
          last_user_message: "Need help with refunds",
          last_assistant_message: "Here is the refund policy",
        },
      ],
    };
    const runCheckpointsById: Record<string, Array<Record<string, unknown>>> = {
      "WR-1": [
        {
          checkpoint_id: "CP-1",
          workflow_run_id: "WR-1",
          project_id: null,
          sequence: 1,
          node_id: "support_agent",
          state_blob: {
            kind: "human_approval",
            node_id: "support_agent",
            output: "waiting for approval",
            output_by_port: { request: "waiting for approval" },
          },
          created_at: NOW,
        },
      ],
      "WR-2": [
        {
          checkpoint_id: "CP-2",
          workflow_run_id: "WR-2",
          project_id: null,
          sequence: 1,
          node_id: "support_agent",
          state_blob: {
            kind: "wait_for_event",
            node_id: "support_agent",
            expected_event_name: "ticket.approved",
            input_by_port: { request: "Need help with refunds" },
          },
          created_at: NOW,
        },
      ],
    };
    const runEventsById: Record<string, Array<Record<string, unknown>>> = {
      "WR-1": [
        {
          event_id: 1,
          workflow_run_id: "WR-1",
          project_id: null,
          sequence: 1,
          event_type: "workflow.run.step",
          node_id: "support_agent",
          payload: { status: "ok" },
          created_at: NOW,
        },
      ],
    };
    const editorRuns = [runById["WR-3"], runById["WR-2"], runById["WR-1"]];
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () => HttpResponse.json(envelope(makeVersion()))),
      http.get(`${API_BASE}/workflow-versions/WFV-2`, () =>
        HttpResponse.json(
          envelope(
            makeVersion({
              version_id: "WFV-2",
              version_number: 2,
              manifest: {
                schema_version: 1,
                workflow_id: "WF-1",
                name: "Historical Support",
                nodes: {
                  start: { id: "start", type: "start", outputs: { user_message: { type: "string" } } },
                  historic_agent: {
                    id: "historic_agent",
                    type: "agent",
                    name: "historical-helper",
                    model: "inherit",
                    instructions: { type: "inline", text: "historical" },
                    tools: [],
                    inputs: { input: { type: "string" } },
                    outputs: { final_output: { type: "string" } },
                  },
                  final: { id: "final", type: "output", inputs: { response: { type: "string" } } },
                },
                edges: [
                  { id: "e1", from: "start", to: "historic_agent", map: { user_message: "input" } },
                  { id: "e2", from: "historic_agent", to: "final", map: { final_output: "response" } },
                ],
              },
            }),
          ),
        ),
      ),
      http.get(`${API_BASE}/tools`, () =>
        HttpResponse.json(envelope([
          {
            tool_id: "TL-GREP",
            name: "grep_files",
            version: "1.0",
            description: "grep",
            module_path: "caliber.tools",
            callable_name: "grep_files",
            input_schema: null,
            output_schema: null,
            side_effect_level: "read",
            requires_approval: false,
            allow_in_preview: true,
            secret_refs: [],
            owner: "@team",
            status: "active",
            deprecated_at: null,
            successor_tool_id: null,
            created_at: NOW,
            updated_at: NOW,
          },
        ])),
      ),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.patch(`${API_BASE}/workflow-versions/WFV-1`, () => {
        patchCalls += 1;
        return HttpResponse.json({ detail: "cannot save" }, { status: 500 });
      }),
      http.post(`${API_BASE}/workflow-versions/WFV-1/validate`, () =>
        HttpResponse.json(envelope({ valid: false, errors: [{ code: "x", path: "nodes", message: "bad", severity: "error" }], warnings: [] })),
      ),
      http.post(`${API_BASE}/workflow-versions/WFV-1/preview-run`, async ({ request }) => {
        previewBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            status: "completed",
            output: "ok",
            steps: [{ node_id: "start", node_type: "start", status: "ok", output: "", tool_calls: [], handoff_target: null, detail: "", duration_ms: 0 }],
            error: null,
          }),
        );
      }),
      http.post(`${API_BASE}/workflow-versions/WFV-1/publish`, () =>
        HttpResponse.json({ detail: "publish blocked" }, { status: 500 }),
      ),
      http.post(`${API_BASE}/workflow-runs`, async ({ request }) => {
        runBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(makeRun()), { status: 201 });
      }),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope(editorRuns)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, ({ params }) => {
        const runId = String(params.runId);
        return HttpResponse.json(envelope(runById[runId] ?? makeRun({ workflow_run_id: runId })));
      }),
      http.get(`${API_BASE}/workflow-runs/:runId/lineage`, ({ params }) => {
        const runId = String(params.runId);
        const lineage = buildWorkflowRunLineage(runId, editorRuns);
        if (!lineage) {
          return HttpResponse.json(
            { detail: `workflow run ${runId} not found` },
            { status: 404 },
          );
        }
        return HttpResponse.json(envelope(lineage));
      }),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, ({ params }) => {
        const runId = String(params.runId);
        if (runId === "WR-2") {
          return HttpResponse.json(
            envelope({
              workflow_run_id: "WR-2",
              workflow_id: "WF-1",
              workflow_version_id: "WFV-2",
              manifest_mode: "saved_version",
              manifest_hash: "hash2",
              manifest: makeVersion({
                version_id: "WFV-2",
                version_number: 2,
                manifest: {
                  schema_version: 1,
                  workflow_id: "WF-1",
                  name: "Historical Support",
                  nodes: {
                    start: { id: "start", type: "start", outputs: { user_message: { type: "string" } } },
                    historic_agent: {
                      id: "historic_agent",
                      type: "agent",
                      name: "historical-helper",
                      model: "inherit",
                      instructions: { type: "inline", text: "historical" },
                      tools: [],
                      inputs: { input: { type: "string" } },
                      outputs: { final_output: { type: "string" } },
                    },
                    final: { id: "final", type: "output", inputs: { response: { type: "string" } } },
                  },
                  edges: [
                    { id: "e1", from: "start", to: "historic_agent", map: { user_message: "input" } },
                    { id: "e2", from: "historic_agent", to: "final", map: { final_output: "response" } },
                  ],
                },
              }).manifest,
            }),
          );
        }
        return HttpResponse.json(
          envelope({
            workflow_run_id: runId,
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: runId === "WR-1" ? "snapshot" : "saved_version",
            manifest_hash: runId === "WR-1" ? "draft-hash" : "hash1",
            manifest: (runBody?.manifest as Record<string, unknown> | undefined) ?? makeVersion().manifest,
          }),
        );
      }),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, ({ params }) =>
        HttpResponse.json(envelope(runEventsById[String(params.runId)] ?? [])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, ({ params }) =>
        HttpResponse.json(envelope(runCheckpointsById[String(params.runId)] ?? [])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, ({ request }) => {
        const url = new URL(request.url);
        const sessionId = url.searchParams.get("session_id") ?? "";
        return HttpResponse.json(envelope(sessionMemoryBySessionId[sessionId] ?? []));
      }),
      http.post(`${API_BASE}/workflow-runs/WR-1/cancel`, () => {
        runById["WR-1"] = makeRun({ status: "waiting_event", current_node_id: "support_agent" });
        runCheckpointsById["WR-1"] = [
          {
            checkpoint_id: "CP-1-WAIT",
            workflow_run_id: "WR-1",
            project_id: null,
            sequence: 2,
            node_id: "support_agent",
            state_blob: {
              kind: "wait_for_event",
              node_id: "support_agent",
              expected_event_name: "ticket.approved",
              input_by_port: { request: "Need help with refunds" },
            },
            created_at: NOW,
          },
        ];
        runEventsById["WR-1"] = [
          ...runEventsById["WR-1"],
          {
            event_id: 2,
            workflow_run_id: "WR-1",
            project_id: null,
            sequence: 2,
            event_type: "workflow.run.waiting_event",
            node_id: "support_agent",
            payload: { status: "waiting_event" },
            created_at: NOW,
          },
        ];
        return HttpResponse.json(envelope(runById["WR-1"]));
      }),
      http.post(`${API_BASE}/workflow-runs/WR-1/resume`, async ({ request }) => {
        resumeBody = (await request.json()) as Record<string, unknown>;
        runById["WR-1"] = makeRun({ status: "queued" });
        runCheckpointsById["WR-1"] = [];
        runEventsById["WR-1"] = [
          ...runEventsById["WR-1"],
          {
            event_id: 3,
            workflow_run_id: "WR-1",
            project_id: null,
            sequence: 3,
            event_type: "workflow.run.queued",
            node_id: "support_agent",
            payload: { status: "queued" },
            created_at: NOW,
          },
        ];
        return HttpResponse.json(envelope(runById["WR-1"]));
      }),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("inspector-change-name"));
    await user.click(screen.getByTestId("inspector-change-tools"));
    await user.click(screen.getByTestId("inspector-change-workflow"));
    await user.click(screen.getByTestId("canvas-drop-node"));
    await user.click(screen.getByTestId("inspector-delete-node"));

    await user.click(screen.getByTestId("canvas-connect-self"));
    await user.click(screen.getByTestId("canvas-connect-incompatible"));
    expect(await screen.findByTestId("editor-message")).toHaveTextContent(
      "Cannot connect support_agent → for_each because their declared ports are incompatible.",
    );
    expect(screen.queryByTestId("mock-connect-map")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("canvas-connect"));
    expect(await screen.findByTestId("mock-connect-map")).toBeInTheDocument();
    await user.click(screen.getByTestId("map-change"));
    await user.click(screen.getByTestId("map-done"));
    await user.click(screen.getByTestId("canvas-edge-click"));
    expect(await screen.findByTestId("mock-connect-map")).toBeInTheDocument();
    await user.click(screen.getByTestId("map-done"));
    await user.click(screen.getByTestId("canvas-node-double-click"));

    await user.click(screen.getByTestId("canvas-connect"));
    expect(await screen.findByTestId("mock-connect-map")).toBeInTheDocument();
    await user.click(screen.getByTestId("map-remove"));

    await user.click(screen.getByTestId("canvas-quick-add-start"));
    await user.click((await screen.findAllByRole("button", { name: /Guardrail/i })).pop()!);
    await user.click(screen.getByTestId("canvas-quick-add-support"));
    await user.click((await screen.findAllByRole("button", { name: /Output/i })).pop()!);
    await user.click(screen.getByTestId("canvas-connection-drop"));
    await user.click((await screen.findAllByRole("button", { name: /Router/i })).pop()!);
    await user.type(screen.getByTestId("outline-search"), "support");
    expect(screen.getByTestId("outline-support_agent")).toBeInTheDocument();
    await user.click(screen.getByTestId("editor-auto-layout"));
    expect(await screen.findByTestId("editor-message")).toHaveTextContent("Canvas layout reset");

    await user.click(screen.getByTestId("editor-validate"));
    await user.click(screen.getByTestId("editor-preview"));
    await user.type(screen.getByTestId("preview-session-id"), "SESSION-editor");
    await user.click(screen.getByTestId("preview-run"));
    expect(await screen.findByTestId("preview-result")).toBeInTheDocument();
    expect(previewBody).toMatchObject({
      input: "What is your refund policy?",
      session_id: "SESSION-editor",
      manifest: expect.objectContaining({
        description: "updated",
      }),
    });
    expect(
      ((previewBody?.manifest as { nodes?: { support_agent?: { name?: string } } } | undefined)
        ?.nodes?.support_agent?.name),
    ).toBe("renamed");
    await user.click(screen.getByTestId("editor-run-monitor"));
    expect(await screen.findByTestId("run-monitor-panel")).toBeInTheDocument();
    expect(await screen.findByTestId("run-history-select-WR-2")).toBeInTheDocument();
    expect(screen.getByTestId("run-history-open-link-WR-2")).toHaveAttribute(
      "href",
      "/workflow-runs/WR-2",
    );
    await user.click(screen.getByTestId("run-history-refresh"));
    await user.click(screen.getByTestId("run-history-select-WR-2"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent("Loaded run WR-2"),
    );
    expect(await screen.findByTestId("run-trace-replay-section")).toHaveTextContent(
      "historical-helper",
    );
    expect(screen.getByTestId("run-monitor-manifest-note")).toHaveTextContent(
      "saved workflow version v2 (WFV-2)",
    );
    await user.click(screen.getByTestId("trace-path-step-1"));
    expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
      "not in the draft currently open in the inspector",
    );
    expect(await screen.findByTestId("workflow-session-memory-panel")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-session-memory-entry-support_agent")).toHaveTextContent(
      "Need help with refunds",
    );
    expect(await screen.findByTestId("workflow-run-file-panel")).toBeInTheDocument();
    expect(await screen.findByTestId("workflow-run-checkpoint-panel")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-checkpoint-detail")).toHaveTextContent(
      "ticket.approved",
    );
    expect(screen.getByTestId("workflow-run-lineage-panel")).toHaveTextContent("Attempt 2 of 3");
    expect(screen.getByTestId("workflow-run-lineage-item-WR-3")).toHaveTextContent("child");
    await user.click(screen.getByTestId("workflow-run-lineage-item-WR-3"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent("Loaded run WR-3"),
    );
    expect(await screen.findByTestId("workflow-run-lineage-panel")).toHaveTextContent("Attempt 3 of 3");
    await user.click(screen.getByTestId("workflow-run-lineage-item-WR-2"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent("Loaded run WR-2"),
    );
    await user.clear(screen.getByTestId("run-session-id"));
    await user.type(screen.getByTestId("run-session-id"), "SESSION-editor");
    await user.click(screen.getByTestId("run-execute"));
    expect(await screen.findByTestId("run-active-summary")).toHaveTextContent("WR-1");
    expect(await screen.findByTestId("run-active-open-link")).toHaveAttribute(
      "href",
      "/workflow-runs/WR-1",
    );
    expect(screen.getByTestId("run-active-summary")).toHaveTextContent("WFV-1");
    expect(await screen.findByTestId("run-output")).toHaveTextContent("run-output");
    expect(await screen.findByTestId("trace-replay")).toBeInTheDocument();
    expect(await screen.findByTestId("workflow-run-debugger")).toBeInTheDocument();
    expect(await screen.findByTestId("workflow-run-checkpoint-detail")).toHaveTextContent(
      "Human approval",
    );
    await user.click(screen.getByTestId("workflow-run-step-button-0"));
    expect(screen.getByTestId("mock-inspector")).toHaveAttribute("data-selected-node-id", "start");
    expect(runBody).toMatchObject({
      workflow_version_id: "WFV-1",
      workflow_id: "WF-1",
      alias: "manual",
      input: "What is your refund policy?",
      session_id: "SESSION-editor",
      source: "editor",
      manifest: expect.objectContaining({
        description: "updated",
      }),
    });
    expect(
      ((runBody?.manifest as { nodes?: { support_agent?: { name?: string } } } | undefined)
        ?.nodes?.support_agent?.name),
    ).toBe("renamed");
    await user.click(screen.getByTestId("run-pause"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent("Pause requested"),
    );
    const resumeEventNameInput = await screen.findByTestId("run-resume-event-name");
    await user.clear(resumeEventNameInput);
    await user.type(resumeEventNameInput, "ticket.approved");
    const resumeEventPayloadInput = await screen.findByTestId("run-resume-event-payload");
    await user.clear(resumeEventPayloadInput);
    fireEvent.change(screen.getByTestId("run-resume-event-payload"), {
      target: { value: '{"ticket_id":"T-42","approved":true}' },
    });
    await waitFor(() => expect(screen.getByTestId("run-resume")).toBeEnabled());
    await user.click(screen.getByTestId("run-resume"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent("resumed"),
    );
    expect(resumeBody).toEqual({
      event_name: "ticket.approved",
      event_payload: { ticket_id: "T-42", approved: true },
    });

    await user.click(screen.getByTestId("editor-save"));
    expect(await screen.findByTestId("editor-message")).toHaveTextContent("Save failed");
    expect(patchCalls).toBeGreaterThan(0);

    await user.click(screen.getByTestId("editor-publish"));
    expect(await screen.findByTestId("publish-drawer")).toBeInTheDocument();
    await user.click(screen.getByTestId("publish-validate"));
    await user.click(screen.getByTestId("publish-run"));
    expect(await screen.findByTestId("editor-message")).toHaveTextContent("Publish failed");
    await user.click(screen.getByTestId("publish-close"));
  }, 10000);

  it("falls back to the saved workflow version already loaded in the editor when run-manifest lookup fails", async () => {
    const currentRun = makeRun({
      workflow_run_id: "WR-CURRENT",
      status: "completed",
      summary: {
        manifest_mode: "saved_version",
        output: "current-run-output",
        node_path: ["start", "support_agent", "final"],
        steps: [
          {
            node_id: "support_agent",
            node_type: "agent",
            status: "ok",
            output: "current-run-output",
            tool_calls: [],
            handoff_target: null,
            detail: "",
            duration_ms: 12,
          },
        ],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion({ status: "published" }))),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([currentRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(envelope(currentRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          { detail: "workflow manifest for run WR-CURRENT is not available" },
          { status: 404 },
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-CURRENT"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent("Loaded run WR-CURRENT"),
    );

    expect(await screen.findByTestId("trace-replay")).toBeInTheDocument();
    expect(await screen.findByTestId("workflow-run-debugger")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("run-monitor-manifest-fallback-notice")).toHaveTextContent(
        "saved workflow version already loaded in the editor",
      ),
    );
    expect(screen.queryByText(/Failed to load workflow version/)).not.toBeInTheDocument();
  });

  it("turns empty run output into status-aware gate guidance", async () => {
    const waitingRun = makeRun({
      workflow_run_id: "WR-NO-OUTPUT",
      status: "waiting_approval",
      current_node_id: "approval",
      error_summary: null,
      summary: {
        node_path: ["start", "approval"],
        steps: [],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion({ status: "published" }))),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([waitingRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-NO-OUTPUT`, () =>
        HttpResponse.json(envelope(waitingRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-NO-OUTPUT/lineage`, () =>
        HttpResponse.json(envelope(buildWorkflowRunLineage("WR-NO-OUTPUT", [waitingRun]))),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-NO-OUTPUT/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-NO-OUTPUT/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-NO-OUTPUT/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-NO-OUTPUT/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-NO-OUTPUT",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-no-output",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-NO-OUTPUT"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent("Loaded run WR-NO-OUTPUT"),
    );

    expect(await screen.findByTestId("run-output")).toHaveTextContent(
      "This run is awaiting approval and has not recorded a final output yet.",
    );
    expect(screen.getByTestId("run-output")).toHaveTextContent(
      "Use the recovery diagnostics and checkpoint panels below to inspect the active gate.",
    );
  });

  it("falls back to a historical saved workflow version when the run-manifest lookup fails", async () => {
    const historicalRun = makeRun({
      workflow_run_id: "WR-HISTORICAL",
      workflow_version_id: "WFV-2",
      status: "completed",
      current_node_id: "historic_agent",
      summary: {
        manifest_mode: "saved_version",
        output: "older-run-output",
        node_path: ["start", "historic_agent", "final"],
        steps: [
          {
            node_id: "historic_agent",
            node_type: "agent",
            status: "ok",
            output: "older-run-output",
            tool_calls: [],
            handoff_target: null,
            detail: "historical handler",
            duration_ms: 11,
          },
        ],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion({ status: "published" }))),
      ),
      http.get(`${API_BASE}/workflow-versions/WFV-2`, () =>
        HttpResponse.json(
          envelope(
            makeVersion({
              version_id: "WFV-2",
              version_number: 2,
              status: "published",
              manifest: {
                schema_version: 1,
                workflow_id: "WF-1",
                name: "Historical Support",
                nodes: {
                  start: { id: "start", type: "start", outputs: { user_message: { type: "string" } } },
                  historic_agent: {
                    id: "historic_agent",
                    type: "agent",
                    name: "historical-helper",
                    model: "inherit",
                    instructions: { type: "inline", text: "historical" },
                    tools: [],
                    inputs: { input: { type: "string" } },
                    outputs: { final_output: { type: "string" } },
                  },
                  final: { id: "final", type: "output", inputs: { response: { type: "string" } } },
                },
                edges: [
                  { id: "e1", from: "start", to: "historic_agent", map: { user_message: "input" } },
                  { id: "e2", from: "historic_agent", to: "final", map: { final_output: "response" } },
                ],
              },
            }),
          ),
        ),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([historicalRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(envelope(historicalRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          { detail: "workflow manifest for run WR-HISTORICAL is not available" },
          { status: 404 },
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-HISTORICAL"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent("Loaded run WR-HISTORICAL"),
    );

    await waitFor(() =>
      expect(screen.getByTestId("run-trace-replay-section")).toHaveTextContent("historical-helper"),
    );
    expect(await screen.findByTestId("workflow-run-debugger")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("run-monitor-manifest-note")).toHaveTextContent(
        "saved workflow version v2 (WFV-2)",
      );
      expect(screen.getByTestId("run-monitor-manifest-fallback-notice")).toHaveTextContent(
        "persisted run manifest could not be loaded separately",
      );
    });
    expect(screen.queryByText(/Failed to load workflow version/)).not.toBeInTheDocument();
  });

  it("keeps the persisted saved workflow version label when the historical version row is gone but the run manifest exists", async () => {
    const persistedHistoricalManifest = {
      schema_version: 1,
      workflow_id: "WF-1",
      name: "Historical Support",
      nodes: {
        start: { id: "start", type: "start", outputs: { user_message: { type: "string" } } },
        historic_agent: {
          id: "historic_agent",
          type: "agent",
          name: "historical-helper",
          model: "inherit",
          instructions: { type: "inline", text: "historical" },
          tools: [],
          inputs: { input: { type: "string" } },
          outputs: { final_output: { type: "string" } },
        },
        final: { id: "final", type: "output", inputs: { response: { type: "string" } } },
      },
      edges: [
        { id: "e1", from: "start", to: "historic_agent", map: { user_message: "input" } },
        { id: "e2", from: "historic_agent", to: "final", map: { final_output: "response" } },
      ],
    };
    const historicalRun = makeRun({
      workflow_run_id: "WR-HISTORICAL-PERSISTED",
      workflow_version_id: "WFV-2",
      status: "completed",
      current_node_id: "historic_agent",
      summary: {
        manifest_mode: "saved_version",
        workflow_version_number: 2,
        output: "older-run-output",
        node_path: ["start", "historic_agent", "final"],
        steps: [
          {
            node_id: "historic_agent",
            node_type: "agent",
            status: "ok",
            output: "older-run-output",
            tool_calls: [],
            handoff_target: null,
            detail: "historical handler",
            duration_ms: 11,
          },
        ],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion({ status: "published" }))),
      ),
      http.get(`${API_BASE}/workflow-versions/WFV-2`, () =>
        HttpResponse.json(
          { detail: "workflow version WFV-2 not found" },
          { status: 404 },
        ),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([historicalRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(envelope(historicalRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-HISTORICAL-PERSISTED",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-2",
            manifest_mode: "saved_version",
            manifest_hash: "hash-historical-persisted",
            manifest: persistedHistoricalManifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(
      await screen.findByTestId("run-history-select-WR-HISTORICAL-PERSISTED"),
    );
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Loaded run WR-HISTORICAL-PERSISTED",
      ),
    );

    await waitFor(() =>
      expect(screen.getByTestId("run-trace-replay-section")).toHaveTextContent(
        "historical-helper",
      ),
    );
    expect(await screen.findByTestId("workflow-run-debugger")).toBeInTheDocument();
    expect(screen.getByTestId("run-monitor-manifest-note")).toHaveTextContent(
      "saved workflow version v2 (WFV-2)",
    );
    expect(screen.getByTestId("run-active-summary")).toHaveTextContent(
      "workflow v2",
    );
    expect(screen.getByTestId("run-active-summary")).toHaveTextContent("WFV-2");
    expect(screen.queryByText(/Failed to load saved workflow version/)).not.toBeInTheDocument();
  });

  it("reconstructs historical replay and debugging from run summary when both the run manifest and saved version row are gone", async () => {
    const historicalRun = makeRun({
      workflow_run_id: "WR-HISTORICAL-SYNTHETIC",
      workflow_version_id: "WFV-2",
      status: "completed",
      current_node_id: "legacy_agent",
      summary: {
        manifest_mode: "saved_version",
        workflow_version_number: 2,
        output: "older-run-output",
        node_path: ["start", "legacy_agent", "final"],
        steps: [
          {
            node_id: "start",
            node_type: "start",
            status: "ok",
            output: "legacy customer message",
            tool_calls: [],
            handoff_target: null,
            detail: "captured trigger input",
            duration_ms: 5,
            input_by_port: {},
            output_by_port: { user_message: "legacy customer message" },
          },
          {
            node_id: "legacy_agent",
            node_type: "agent",
            status: "ok",
            output: "older-run-output",
            tool_calls: [],
            handoff_target: null,
            detail: "historical handler",
            duration_ms: 11,
            input_by_port: { input: "legacy customer message" },
            output_by_port: { final_output: "older-run-output" },
          },
        ],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion({ status: "published" }))),
      ),
      http.get(`${API_BASE}/workflow-versions/WFV-2`, () =>
        HttpResponse.json(
          { detail: "workflow version WFV-2 not found" },
          { status: 404 },
        ),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([historicalRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(envelope(historicalRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          { detail: "workflow manifest for run WR-HISTORICAL-SYNTHETIC is not available" },
          { status: 404 },
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(
      await screen.findByTestId("run-history-select-WR-HISTORICAL-SYNTHETIC"),
    );
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Loaded run WR-HISTORICAL-SYNTHETIC",
      ),
    );

    expect(await screen.findByTestId("run-monitor-manifest-fallback-notice")).toHaveTextContent(
      "graph reconstructed from recorded run history and checkpoints",
    );
    expect(await screen.findByTestId("workflow-run-debugger")).toBeInTheDocument();
    expect(screen.getByTestId("run-trace-replay-section")).toHaveTextContent("legacy_agent");
    expect(screen.getByTestId("run-active-summary")).toHaveTextContent("workflow v2");
    expect(screen.getByTestId("run-active-summary")).toHaveTextContent("WFV-2");
    expect(screen.queryByText(/Failed to load saved workflow version/)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/Trace replay is unavailable because saved workflow version v2/),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/Manifest-aware debugging is unavailable because saved workflow version v2/),
    ).not.toBeInTheDocument();
  });

  it("turns unrecoverable historical manifest load failures into recovery guidance in the run monitor", async () => {
    const historicalRun = makeRun({
      workflow_run_id: "WR-HISTORICAL-ERROR",
      workflow_version_id: "WFV-2",
      status: "failed",
      current_node_id: null,
      summary: {
        manifest_mode: "saved_version",
        workflow_version_number: 2,
        output: "",
        node_path: [],
        steps: [],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion({ status: "published" }))),
      ),
      http.get(`${API_BASE}/workflow-versions/WFV-2`, () =>
        HttpResponse.json(
          { detail: "workflow version WFV-2 not found" },
          { status: 404 },
        ),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([historicalRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(envelope(historicalRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          { detail: "workflow manifest for run WR-HISTORICAL-ERROR is not available" },
          { status: 404 },
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-HISTORICAL-ERROR"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Loaded run WR-HISTORICAL-ERROR",
      ),
    );

    expect(await screen.findByTestId("run-trace-replay-manifest-error")).toHaveTextContent(
      "persisted run manifest and saved workflow version v2 (WFV-2) could not be restored",
    );
    expect(screen.getByTestId("run-trace-replay-manifest-error")).toHaveTextContent(
      "Use the debugger, recovery diagnostics, checkpoint trail, and retry lineage",
    );
    expect(screen.getByTestId("run-trace-replay-manifest-error")).toHaveTextContent(
      "Latest graph error: workflow manifest for run WR-HISTORICAL-ERROR is not available (saved-version fallback failed: workflow version WFV-2 not found)",
    );
    expect(screen.getByTestId("run-debugger-manifest-error")).toHaveTextContent(
      "persisted run manifest and saved workflow version v2 (WFV-2) could not be restored",
    );
    expect(screen.queryByText(/Failed to load v2 \(WFV-2\) for trace replay/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Failed to load v2 \(WFV-2\) for debugging/i)).not.toBeInTheDocument();
  });

  it("explains when replay and debugging cannot reconstruct a run graph from sparse history", async () => {
    const sparseRun = makeRun({
      workflow_run_id: "WR-SPARSE",
      workflow_version_id: null,
      current_node_id: null,
      status: "completed",
      summary: {
        output: "sparse-output",
        node_path: [],
        steps: [],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion({ status: "published" }))),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([sparseRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(envelope(sparseRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(envelope(null)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-SPARSE"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent("Loaded run WR-SPARSE"),
    );

    expect(screen.queryByTestId("run-monitor-manifest-fallback-notice")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-trace-replay-section")).toHaveTextContent(
      "recorded run history is too sparse to reconstruct it",
    );
    expect(screen.getByTestId("run-trace-replay-section")).toHaveTextContent(
      "Inspect the recovery and retry-lineage panels in this run monitor",
    );
    expect(screen.getByTestId("run-debugger-section")).toHaveTextContent(
      "recorded run history is too sparse to reconstruct it",
    );
    expect(screen.getByTestId("run-debugger-section")).toHaveTextContent(
      "Inspect the recovery and retry-lineage panels in this run monitor",
    );
    expect(
      screen.queryByText(/saved workflow version used by this run could not be resolved/i),
    ).not.toBeInTheDocument();
  });

  it("uses checkpoint guidance when sparse editor replay still has persisted checkpoint evidence", async () => {
    const sparseRun = makeRun({
      workflow_run_id: "WR-SPARSE-CHECKPOINT",
      workflow_version_id: null,
      current_node_id: null,
      status: "waiting_approval",
      summary: {
        output: "sparse-output",
        node_path: [],
        steps: [],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion({ status: "published" }))),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([sparseRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(envelope(sparseRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(envelope(null)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CHK-SPARSE",
              workflow_run_id: "WR-SPARSE-CHECKPOINT",
              project_id: null,
              sequence: 1,
              node_id: null,
              state_blob: {
                kind: "runtime_approval",
                output: "awaiting approval",
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-SPARSE-CHECKPOINT"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Loaded run WR-SPARSE-CHECKPOINT",
      ),
    );

    expect(screen.getByTestId("run-trace-replay-section")).toHaveTextContent(
      "Inspect the recovery, checkpoint, and retry-lineage panels in this run monitor",
    );
    expect(screen.getByTestId("run-debugger-section")).toHaveTextContent(
      "Inspect the recovery, checkpoint, and retry-lineage panels in this run monitor",
    );
  });

  it("applies code and copilot edits, exports python, and completes save/validate/publish", async () => {
    let patchBody: Record<string, unknown> | null = null;
    let exportCalls = 0;
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-versions/WFV-1/export/python`, () => {
        exportCalls += 1;
        return new Response("def run(): pass", {
          status: 200,
          headers: { "Content-Type": "text/plain" },
        });
      }),
      http.patch(`${API_BASE}/workflow-versions/WFV-1`, async ({ request }) => {
        patchBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope(makeVersion({ manifest_hash: "hash2", manifest: patchBody.manifest })),
        );
      }),
      http.post(`${API_BASE}/workflow-versions/WFV-1/validate`, () =>
        HttpResponse.json(envelope({ valid: true, errors: [], warnings: [] })),
      ),
      http.post(`${API_BASE}/workflow-versions/WFV-1/publish`, () =>
        HttpResponse.json(envelope(makeVersion({ status: "published", published_at: NOW }))),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-view-code"));
    expect(await screen.findByTestId("code-overlay")).toBeInTheDocument();
    await user.click(screen.getByTestId("code-export"));
    await waitFor(() => expect(exportCalls).toBe(1));
    await user.click(screen.getByTestId("code-apply"));
    expect(await screen.findByTestId("editor-message")).toHaveTextContent(
      "Applied manifest from code view",
    );

    await user.click(screen.getByTestId("editor-view-visual"));
    await user.click(screen.getByTestId("editor-copilot-toggle"));
    expect(await screen.findByTestId("copilot-panel")).toBeInTheDocument();
    await user.click(screen.getByTestId("copilot-apply"));
    expect(await screen.findByTestId("editor-message")).toHaveTextContent("Copilot edit applied");

    await user.click(screen.getByTestId("editor-save"));
    await waitFor(() => expect(patchBody).not.toBeNull());
    expect(await screen.findByTestId("editor-message")).toHaveTextContent("Saved draft.");

    await user.click(screen.getByTestId("editor-validate"));
    expect(await screen.findByTestId("editor-message")).toHaveTextContent("Valid.");

    await user.click(screen.getByTestId("editor-publish"));
    expect(await screen.findByTestId("publish-drawer")).toBeInTheDocument();
    await user.click(screen.getByTestId("publish-run"));
    expect(await screen.findByTestId("editor-message")).toHaveTextContent("Published.");
  });

  it("opens the Plan tab and applies a built workflow back onto the canvas", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    // Switching to the Plan tab reveals the plan-to-build surface…
    await user.click(screen.getByTestId("editor-view-plan"));
    expect(await screen.findByTestId("plan-overlay")).toBeInTheDocument();

    // …and accepting a built workflow applies it and drops back to Visual.
    await user.click(screen.getByTestId("plan-apply"));
    expect(await screen.findByTestId("editor-message")).toHaveTextContent("Workflow built from plan");
    await waitFor(() => expect(screen.queryByTestId("plan-overlay")).not.toBeInTheDocument());
  });

  it("routes a validation problem into the selected inspector field", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/workflow-versions/WFV-1/validate`, () =>
        HttpResponse.json(
          envelope({
            valid: false,
            errors: [
              {
                code: "missing_tool",
                path: "nodes.support_agent.tools",
                message: "Tool X not registered.",
                severity: "error",
              },
            ],
            warnings: [],
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-validate"));
    await user.click(await screen.findByTestId("problem-missing_tool"));

    expect(screen.getByTestId("mock-inspector")).toHaveAttribute(
      "data-selected-node-id",
      "support_agent",
    );
    expect(screen.getByTestId("mock-inspector")).toHaveAttribute(
      "data-focus-field-key",
      "tools",
    );
  });

  it("handles runtime approval approve and reject actions in the run monitor", async () => {
    const approveCalls: string[] = [];
    const rejectCalls: string[] = [];
    const waitingApprove = makeRun({
      workflow_run_id: "WR-APPROVE",
      status: "waiting_approval",
      current_node_id: "approval",
      trace_id: "trace-approve",
      summary: { output: null, node_path: ["start", "approval"], steps: [] },
    });
    const waitingReject = makeRun({
      workflow_run_id: "WR-REJECT",
      status: "waiting_approval",
      current_node_id: "approval",
      trace_id: "trace-reject",
      summary: { output: null, node_path: ["start", "approval"], steps: [] },
    });
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([waitingApprove, waitingReject])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, ({ params }) => {
        const runId = String(params.runId);
        return HttpResponse.json(envelope(runId === "WR-REJECT" ? waitingReject : waitingApprove));
      }),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, ({ params }) =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: `CP-${String(params.runId)}`,
              workflow_run_id: String(params.runId),
              project_id: null,
              sequence: 1,
              node_id: "approval",
              state_blob: {
                kind: "human_approval",
                node_id: "approval",
                output: "approval requested",
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, ({ params }) =>
        HttpResponse.json(
          envelope([
            {
              runtime_approval_id: `RA-${String(params.runId)}`,
              workflow_run_id: String(params.runId),
              project_id: null,
              node_id: "approval",
              status: "pending",
              requested_at: NOW,
              decided_at: null,
              decided_by: null,
              decision_reason: null,
              policy_snapshot: null,
            },
          ]),
        ),
      ),
      http.post(`${API_BASE}/workflow-runs/WR-APPROVE/approval/approve`, async ({ request }) => {
        const body = (await request.json()) as { runtime_approval_id?: string };
        approveCalls.push(body.runtime_approval_id ?? "");
        return HttpResponse.json(envelope(makeRun({ workflow_run_id: "WR-APPROVE", status: "running" })));
      }),
      http.post(`${API_BASE}/workflow-runs/WR-REJECT/approval/reject`, async ({ request }) => {
        const body = (await request.json()) as { runtime_approval_id?: string };
        rejectCalls.push(body.runtime_approval_id ?? "");
        return HttpResponse.json(envelope(makeRun({ workflow_run_id: "WR-REJECT", status: "failed" })));
      }),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();
    await user.click(screen.getByTestId("editor-run-monitor"));

    await user.click(await screen.findByTestId("run-history-select-WR-APPROVE"));
    expect(await screen.findByTestId("run-approval-actions")).toHaveTextContent("approval");
    expect(screen.getByTestId("run-approval-actions")).toHaveTextContent(
      "Approve to unlock Resume",
    );
    expect(await screen.findByTestId("workflow-run-recovery-panel")).toHaveTextContent(
      "Awaiting approval",
    );
    expect(screen.getByTestId("workflow-run-recovery-approvals")).toHaveTextContent(
      "RA-WR-APPROVE",
    );
    expect(screen.getByTestId("run-resume")).toBeDisabled();
    await user.click(screen.getByTestId("run-approve"));
    await waitFor(() => expect(approveCalls).toEqual(["RA-WR-APPROVE"]));
    expect(await screen.findByTestId("editor-run-message")).toHaveTextContent("Approval recorded");

    await user.click(await screen.findByTestId("run-history-select-WR-REJECT"));
    expect(await screen.findByTestId("run-approval-actions")).toHaveTextContent("approval");
    expect(screen.getByTestId("run-resume")).toBeDisabled();
    await user.click(screen.getByTestId("run-reject"));
    await waitFor(() => expect(rejectCalls).toEqual(["RA-WR-REJECT"]));
    expect(await screen.findByTestId("editor-run-message")).toHaveTextContent("Run WR-REJECT failed.");
  });

  it("only enables manual resume in the run monitor after an approval has been recorded", async () => {
    let resumeCalls = 0;
    const blockedRun = makeRun({
      workflow_run_id: "WR-BLOCKED",
      status: "waiting_approval",
      current_node_id: "tool_gate",
      trace_id: "trace-blocked",
      summary: { output: null, node_path: ["start", "tool_gate"], steps: [] },
    });
    const rejectedRun = makeRun({
      workflow_run_id: "WR-REJECTED",
      status: "waiting_approval",
      current_node_id: "tool_gate",
      trace_id: "trace-rejected",
      summary: { output: null, node_path: ["start", "tool_gate"], steps: [] },
    });
    const resumableRun = makeRun({
      workflow_run_id: "WR-RESUME",
      status: "waiting_approval",
      current_node_id: "tool_gate",
      trace_id: "trace-resume",
      summary: { output: null, node_path: ["start", "tool_gate"], steps: [] },
    });
    const version = makeVersion();

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(version)),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([blockedRun, rejectedRun, resumableRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, ({ params }) => {
        const runId = String(params.runId);
        const run =
          runId === "WR-BLOCKED"
            ? blockedRun
            : runId === "WR-REJECTED"
              ? rejectedRun
              : resumableRun;
        return HttpResponse.json(envelope(run));
      }),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, ({ params }) =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: `CP-${String(params.runId)}`,
              workflow_run_id: String(params.runId),
              project_id: null,
              sequence: 1,
              node_id: "tool_gate",
              state_blob: {
                kind: "runtime_approval",
                node_id: "tool_gate",
                input_by_port: {
                  input: "delete ticket T-300",
                },
                output: "delete ticket T-300",
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, ({ params }) => {
        const runId = String(params.runId);
        const approvals =
          runId === "WR-BLOCKED"
            ? []
            : runId === "WR-REJECTED"
              ? [
                  {
                    runtime_approval_id: "RA-REJECTED",
                    workflow_run_id: runId,
                    project_id: null,
                    node_id: "tool_gate",
                    status: "rejected",
                    requested_at: NOW,
                    decided_at: NOW,
                    decided_by: "@ops",
                    decision_reason: "Delete access denied.",
                    policy_snapshot: null,
                  },
                ]
              : [
                  {
                    runtime_approval_id: "RA-RESUME",
                    workflow_run_id: runId,
                    project_id: null,
                    node_id: "tool_gate",
                    status: "approved",
                    requested_at: NOW,
                    decided_at: NOW,
                    decided_by: "@ops",
                    decision_reason: "Approved for retry.",
                    policy_snapshot: null,
                  },
                ];
        return HttpResponse.json(envelope(approvals));
      }),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-RESUME",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash1",
            manifest: version.manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.post(`${API_BASE}/workflow-runs/WR-RESUME/resume`, async ({ request }) => {
        expect(await request.json()).toEqual({});
        resumeCalls += 1;
        return HttpResponse.json(envelope(makeRun({ workflow_run_id: "WR-RESUME", status: "queued" })));
      }),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();
    await user.click(screen.getByTestId("editor-run-monitor"));

    await user.click(await screen.findByTestId("run-history-select-WR-BLOCKED"));
    expect(await screen.findByTestId("workflow-run-recovery-warning")).toHaveTextContent(
      "no approved runtime approval record is attached",
    );
    expect(screen.getByTestId("run-resume")).toBeDisabled();

    await user.click(await screen.findByTestId("run-history-select-WR-REJECTED"));
    expect(await screen.findByTestId("workflow-run-recovery-panel")).toHaveTextContent(
      "Runtime approval rejected",
    );
    expect(screen.getByTestId("run-resume")).toBeDisabled();

    await user.click(await screen.findByTestId("run-history-select-WR-RESUME"));
    await waitFor(() => expect(screen.getByTestId("run-resume")).toBeEnabled());
    await user.click(screen.getByTestId("run-resume"));
    await waitFor(() => expect(resumeCalls).toBe(1));
    expect(await screen.findByTestId("editor-run-message")).toHaveTextContent("resumed");
  });

  it("does not let a duplicated waiting approval event overwrite approval feedback", async () => {
    const approveCalls: string[] = [];
    const waitingApprove = makeRun({
      workflow_run_id: "WR-APPROVE",
      status: "waiting_approval",
      current_node_id: "review",
      summary: {
        output: "",
        node_path: ["start", "review"],
        steps: [],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-components`, () =>
        HttpResponse.json(envelope({ schema_version: 1, components: [] })),
      ),
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          envelope({
            workflow_runs: {
              queue_enabled: true,
              supports_async_submit: true,
              supports_cancel: true,
              supports_retry: true,
              supports_resume: true,
              runtime_approvals_enabled: true,
              checkpointing_enabled: true,
            },
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([waitingApprove])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVE`, () =>
        HttpResponse.json(envelope(waitingApprove)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVE/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVE/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-WR-APPROVE",
              workflow_run_id: "WR-APPROVE",
              project_id: null,
              sequence: 1,
              node_id: "review",
              state_blob: {
                kind: "human_approval",
                node_id: "review",
                output: "approval requested",
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVE/approvals`, () =>
        HttpResponse.json(
          envelope([
            {
              runtime_approval_id: "RA-WR-APPROVE",
              workflow_run_id: "WR-APPROVE",
              project_id: null,
              node_id: "review",
              status: "pending",
              requested_at: NOW,
              decided_at: null,
              decided_by: null,
              decision_reason: null,
              policy_snapshot: null,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVE/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-APPROVE",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash1",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.post(`${API_BASE}/workflow-runs/WR-APPROVE/approval/approve`, async ({ request }) => {
        const body = (await request.json()) as { runtime_approval_id?: string };
        approveCalls.push(body.runtime_approval_id ?? "");
        return HttpResponse.json(envelope(waitingApprove));
      }),
    );

    const user = userEvent.setup();
    const view = renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();
    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-APPROVE"));

    streamState.event = {
      type: "workflow.run.waiting_approval",
      workflow_id: "WF-1",
      workflow_run_id: "WR-APPROVE",
      status: "waiting_approval",
      node_id: "review",
      runtime_approval_id: "RA-WR-APPROVE",
      event_id: 42,
      sequence: 7,
      created_at: NOW,
    };
    view.rerenderEditor();

    await user.click(await screen.findByTestId("run-approve"));
    await waitFor(() => expect(approveCalls).toEqual(["RA-WR-APPROVE"]));
    expect(await screen.findByTestId("editor-run-message")).toHaveTextContent("Approval recorded");

    streamState.event = {
      type: "workflow.run.waiting_approval",
      workflow_id: "WF-1",
      workflow_run_id: "WR-APPROVE",
      status: "waiting_approval",
      node_id: "review",
      runtime_approval_id: "RA-WR-APPROVE",
      event_id: 42,
      sequence: 7,
      created_at: NOW,
    };
    view.rerenderEditor();

    expect(await screen.findByTestId("editor-run-message")).toHaveTextContent("Approval recorded");
  });

  it("hides approval actions in the monitor when the workflow run queue is disabled", async () => {
    const waitingApprove = makeRun({
      workflow_run_id: "WR-APPROVE-QUEUE-DISABLED",
      status: "waiting_approval",
      current_node_id: "review",
      summary: {
        output: "",
        node_path: ["start", "review"],
        steps: [],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-components`, () =>
        HttpResponse.json(envelope({ schema_version: 1, components: [] })),
      ),
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          envelope({
            workflow_runs: {
              queue_enabled: false,
              supports_async_submit: true,
              supports_cancel: true,
              supports_retry: true,
              supports_resume: true,
              runtime_approvals_enabled: true,
              checkpointing_enabled: true,
            },
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([waitingApprove])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVE-QUEUE-DISABLED`, () =>
        HttpResponse.json(envelope(waitingApprove)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVE-QUEUE-DISABLED/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVE-QUEUE-DISABLED/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-WR-APPROVE-QUEUE-DISABLED",
              workflow_run_id: "WR-APPROVE-QUEUE-DISABLED",
              project_id: null,
              sequence: 1,
              node_id: "review",
              state_blob: {
                kind: "human_approval",
                node_id: "review",
                output: "approval requested",
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVE-QUEUE-DISABLED/approvals`, () =>
        HttpResponse.json(
          envelope([
            {
              runtime_approval_id: "RA-WR-APPROVE-QUEUE-DISABLED",
              workflow_run_id: "WR-APPROVE-QUEUE-DISABLED",
              project_id: null,
              node_id: "review",
              status: "pending",
              requested_at: NOW,
              decided_at: null,
              decided_by: null,
              decision_reason: null,
              policy_snapshot: null,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVE-QUEUE-DISABLED/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-APPROVE-QUEUE-DISABLED",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash1",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByTestId("editor-run-monitor"));
    expect(await screen.findByTestId("run-queue-capability-note")).toHaveTextContent(
      "This deployment has workflow execution disabled. Enable the run queue to start editor runs.",
    );
    await user.click(await screen.findByTestId("run-history-select-WR-APPROVE-QUEUE-DISABLED"));

    expect(await screen.findByTestId("run-approval-capability-note")).toHaveTextContent(
      "Approval actions are unavailable until the workflow run queue is enabled",
    );
    expect(screen.queryByTestId("run-approve")).not.toBeInTheDocument();
    expect(screen.queryByTestId("run-reject")).not.toBeInTheDocument();
  });

  it("disables paused approval resume in the monitor when checkpoint persistence is disabled", async () => {
    const waitingApprove = makeRun({
      workflow_run_id: "WR-APPROVE-NO-CHECKPOINTING",
      status: "waiting_approval",
      current_node_id: "review",
      summary: {
        output: "",
        node_path: ["start", "review"],
        steps: [],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-components`, () =>
        HttpResponse.json(envelope({ schema_version: 1, components: [] })),
      ),
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          envelope({
            workflow_runs: {
              queue_enabled: true,
              supports_async_submit: true,
              supports_cancel: true,
              supports_retry: true,
              supports_resume: true,
              runtime_approvals_enabled: true,
              checkpointing_enabled: false,
            },
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([waitingApprove])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVE-NO-CHECKPOINTING`, () =>
        HttpResponse.json(envelope(waitingApprove)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVE-NO-CHECKPOINTING/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVE-NO-CHECKPOINTING/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-WR-APPROVE-NO-CHECKPOINTING",
              workflow_run_id: "WR-APPROVE-NO-CHECKPOINTING",
              project_id: null,
              sequence: 1,
              node_id: "review",
              state_blob: {
                kind: "human_approval",
                node_id: "review",
                input_by_port: { input: "hello" },
                output: "approval requested",
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVE-NO-CHECKPOINTING/approvals`, () =>
        HttpResponse.json(
          envelope([
            {
              runtime_approval_id: "RA-WR-APPROVE-NO-CHECKPOINTING",
              workflow_run_id: "WR-APPROVE-NO-CHECKPOINTING",
              project_id: null,
              node_id: "review",
              status: "approved",
              requested_at: NOW,
              decided_at: NOW,
              decided_by: "@reviewer",
              decision_reason: "approved",
              policy_snapshot: null,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVE-NO-CHECKPOINTING/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-APPROVE-NO-CHECKPOINTING",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash1",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-APPROVE-NO-CHECKPOINTING"));

    expect(await screen.findByTestId("run-checkpoint-capability-note")).toHaveTextContent(
      "Persisted checkpoints are disabled for this deployment, so only live retry state is available here.",
    );
    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "Manual resume is unavailable until checkpoint persistence is enabled for workflow runs. Re-enable checkpointing for this deployment before continuing this paused approval run.",
    );
    expect(screen.getByTestId("run-resume")).toBeDisabled();
  });

  it("disables paused event resume in the monitor when the stored checkpoint is missing", async () => {
    const baseVersion = makeVersion();
    const eventVersion = makeVersion({
      manifest: {
        ...baseVersion.manifest,
        nodes: {
          ...baseVersion.manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_for_event",
            event_name: "ticket.approved",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
        },
      },
    });
    const waitingRun = makeRun({
      workflow_run_id: "WR-WAIT-EVENT-NO-CHECKPOINT",
      status: "waiting_event",
      current_node_id: "wait_gate",
      trace_id: "trace-wait-event-no-checkpoint",
      summary: {
        output: null,
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-MISSING",
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(eventVersion)),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-components`, () =>
        HttpResponse.json(envelope({ schema_version: 1, components: [] })),
      ),
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          envelope({
            workflow_runs: {
              queue_enabled: true,
              supports_async_submit: true,
              supports_cancel: true,
              supports_retry: true,
              supports_resume: true,
              runtime_approvals_enabled: true,
              checkpointing_enabled: true,
            },
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([waitingRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(envelope(waitingRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-WAIT-EVENT-NO-CHECKPOINT",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-wait-event-no-checkpoint",
            manifest: eventVersion.manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-WAIT-EVENT-NO-CHECKPOINT"));

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "paused run no longer has a stored checkpoint",
    );
    expect(screen.getByTestId("run-resume")).toBeDisabled();
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
  });

  it("hides cancel and retry actions in the monitor when the workflow run queue is disabled", async () => {
    const cancellableRun = makeRun({
      workflow_run_id: "WR-CANCEL-QUEUE-DISABLED",
      status: "running",
      current_node_id: "support_agent",
    });
    const retryableRun = makeRun({
      workflow_run_id: "WR-RETRY-QUEUE-DISABLED",
      status: "failed",
      completed_at: NOW,
    });
    const runsById: Record<string, Record<string, unknown>> = {
      "WR-CANCEL-QUEUE-DISABLED": cancellableRun,
      "WR-RETRY-QUEUE-DISABLED": retryableRun,
    };

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-components`, () =>
        HttpResponse.json(envelope({ schema_version: 1, components: [] })),
      ),
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          envelope({
            workflow_runs: {
              queue_enabled: false,
              supports_async_submit: true,
              supports_cancel: true,
              supports_retry: true,
              supports_resume: true,
              runtime_approvals_enabled: true,
              checkpointing_enabled: true,
            },
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([cancellableRun, retryableRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, ({ params }) =>
        HttpResponse.json(envelope(runsById[String(params.runId)])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, ({ params }) =>
        HttpResponse.json(
          envelope({
            workflow_run_id: String(params.runId),
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash1",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-CANCEL-QUEUE-DISABLED"));
    expect(screen.getByTestId("run-pause")).toBeDisabled();

    await user.click(await screen.findByTestId("run-history-select-WR-RETRY-QUEUE-DISABLED"));
    expect(screen.getByTestId("run-retry")).toBeDisabled();
  });

  it("disables paused run resume controls in the monitor when the workflow run queue is disabled", async () => {
    const baseVersion = makeVersion();
    const eventVersion = makeVersion({
      manifest: {
        ...baseVersion.manifest,
        nodes: {
          ...baseVersion.manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_for_event",
            event_name: "ticket.approved",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
        },
      },
    });
    const waitingRun = makeRun({
      workflow_run_id: "WR-RESUME-QUEUE-DISABLED",
      status: "waiting_event",
      current_node_id: "wait_gate",
      trace_id: "trace-resume-queue-disabled",
      summary: {
        output: null,
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-RESUME-QUEUE-DISABLED",
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(eventVersion)),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-components`, () =>
        HttpResponse.json(envelope({ schema_version: 1, components: [] })),
      ),
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          envelope({
            workflow_runs: {
              queue_enabled: false,
              supports_async_submit: true,
              supports_cancel: true,
              supports_retry: true,
              supports_resume: true,
              runtime_approvals_enabled: true,
              checkpointing_enabled: true,
            },
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([waitingRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(envelope(waitingRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-RESUME-QUEUE-DISABLED",
              workflow_run_id: "WR-RESUME-QUEUE-DISABLED",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                expected_event_name: "ticket.approved",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-RESUME-QUEUE-DISABLED",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-resume-queue-disabled",
            manifest: eventVersion.manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-RESUME-QUEUE-DISABLED"));

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "Manual and event-match resume are unavailable until the workflow run queue is enabled",
    );
    expect(screen.getByTestId("run-resume")).toBeDisabled();
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
  });

  it("shows scheduled wait messaging and resumes wait_until runs without event payload", async () => {
    let resumeBody: Record<string, unknown> | null = null;
    const baseVersion = makeVersion();
    const scheduledVersion = makeVersion({
      manifest: {
        ...baseVersion.manifest,
        nodes: {
          ...baseVersion.manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_until",
            wait_until: "2026-02-01T10:00:00",
            timezone: "UTC",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
        },
      },
    });
    const waitingRun = makeRun({
      workflow_run_id: "WR-WAIT-UNTIL",
      status: "waiting_event",
      current_node_id: "wait_gate",
      trace_id: "trace-wait-until",
      summary: { output: null, node_path: ["start", "wait_gate"], steps: [] },
    });
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(scheduledVersion)),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([waitingRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(envelope(waitingRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-WAIT-UNTIL",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-wait-until",
            manifest: scheduledVersion.manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-WAIT-UNTIL",
              workflow_run_id: "WR-WAIT-UNTIL",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_until",
                node_id: "wait_gate",
                wait_until: "2026-02-01T10:00:00",
                timezone: "UTC",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/workflow-runs/WR-WAIT-UNTIL/resume`, async ({ request }) => {
        resumeBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope(makeRun({ workflow_run_id: "WR-WAIT-UNTIL", status: "queued" })),
        );
      }),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();
    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-WAIT-UNTIL"));

    expect(await screen.findByTestId("run-wait-until-config")).toHaveTextContent(
      "paused until 2026-02-01T10:00:00 (UTC)",
    );
    expect(await screen.findByTestId("workflow-run-recovery-panel")).toHaveTextContent(
      "Scheduled resume",
    );
    expect(screen.getByTestId("workflow-run-recovery-panel")).toHaveTextContent(
      "2026-02-01T10:00:00 (UTC)",
    );
    expect(screen.queryByTestId("run-resume-event-name")).not.toBeInTheDocument();
    expect(screen.queryByTestId("run-resume-event-payload")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-status-badge")).toHaveClass(
      "bg-sky-50",
      "text-sky-700",
      "border-sky-200",
    );
    expect(screen.getByTestId("run-pause")).toBeDisabled();

    await user.click(screen.getByTestId("run-resume"));

    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent("resumed"),
    );
    expect(resumeBody).toEqual({});
  });

  it("shows queue-disabled scheduled wait guidance in the monitor when wait_until runs cannot resume", async () => {
    const baseVersion = makeVersion();
    const scheduledVersion = makeVersion({
      manifest: {
        ...baseVersion.manifest,
        nodes: {
          ...baseVersion.manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_until",
            wait_until: "2026-02-01T10:00:00",
            timezone: "UTC",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
        },
      },
    });
    const waitingRun = makeRun({
      workflow_run_id: "WR-WAIT-UNTIL-QUEUE-DISABLED",
      status: "waiting_event",
      current_node_id: "wait_gate",
      trace_id: "trace-wait-until-queue-disabled",
      summary: { output: null, node_path: ["start", "wait_gate"], steps: [] },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(scheduledVersion)),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-components`, () =>
        HttpResponse.json(envelope({ schema_version: 1, components: [] })),
      ),
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          envelope({
            workflow_runs: {
              queue_enabled: false,
              supports_async_submit: true,
              supports_cancel: true,
              supports_retry: true,
              supports_resume: true,
              runtime_approvals_enabled: true,
              checkpointing_enabled: true,
            },
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([waitingRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(envelope(waitingRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-WAIT-UNTIL-QUEUE-DISABLED",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-wait-until-queue-disabled",
            manifest: scheduledVersion.manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-WAIT-UNTIL-QUEUE-DISABLED",
              workflow_run_id: "WR-WAIT-UNTIL-QUEUE-DISABLED",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_until",
                node_id: "wait_gate",
                wait_until: "2026-02-01T10:00:00",
                timezone: "UTC",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-WAIT-UNTIL-QUEUE-DISABLED"));

    expect(await screen.findByTestId("run-wait-until-config")).toHaveTextContent(
      "Automatic and manual resume are unavailable until the workflow run queue is enabled for this deployment.",
    );
    expect(screen.getByTestId("run-resume")).toBeDisabled();
  });

  it("reconciles waiting-event run snapshots from recorded step history", async () => {
    const baseVersion = makeVersion();
    const eventVersion = makeVersion({
      manifest: {
        ...baseVersion.manifest,
        nodes: {
          ...baseVersion.manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_for_event",
            event_name: "ticket.approved",
            inputs: { input: { type: "string" } },
            outputs: {
              output: { type: "string" },
              event_payload: { type: "structured" },
              event_name: { type: "string" },
            },
          },
        },
      },
    });
    const runFromRow = makeRun({
      workflow_run_id: "WR-WAIT-EVENT-HISTORY",
      status: "running",
      current_node_id: "wait_gate",
      trace_id: "trace-wait-event-history",
      summary: {
        output: null,
        node_path: ["start", "wait_gate"],
        steps: [],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(eventVersion)),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([runFromRow])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-WAIT-EVENT-HISTORY`, () =>
        HttpResponse.json(envelope(runFromRow)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-WAIT-EVENT-HISTORY/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-WAIT-EVENT-HISTORY",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-wait-event-history",
            manifest: eventVersion.manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-WAIT-EVENT-HISTORY/events`, () =>
        HttpResponse.json(
          envelope([
            {
              event_id: 1,
              workflow_run_id: "WR-WAIT-EVENT-HISTORY",
              project_id: null,
              sequence: 1,
              event_type: "workflow.run.started",
              node_id: "start",
              payload: {},
              created_at: NOW,
            },
            {
              event_id: 2,
              workflow_run_id: "WR-WAIT-EVENT-HISTORY",
              project_id: null,
              sequence: 2,
              event_type: "workflow.run.step",
              node_id: "wait_gate",
              payload: {
                step: {
                  node_id: "wait_gate",
                  node_type: "wait_for_event",
                  status: "blocked",
                  output: "",
                  tool_calls: [],
                  handoff_target: null,
                  detail: "waiting_event:wait_gate",
                  duration_ms: 0,
                },
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-WAIT-EVENT-HISTORY/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-WAIT-EVENT-HISTORY/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();
    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-WAIT-EVENT-HISTORY"));

    expect(await screen.findByTestId("run-status-badge")).toHaveAttribute(
      "data-status",
      "waiting_event",
    );
    expect(await screen.findByTestId("run-waiting-event-config")).toHaveTextContent(
      "Inject the external event payload",
    );
  });

  it("matches waiting runs by external event from the run monitor", async () => {
    let resumeByEventBody: Record<string, unknown> | null = null;
    const baseVersion = makeVersion();
    const eventVersion = makeVersion({
      manifest: {
        ...baseVersion.manifest,
        nodes: {
          ...baseVersion.manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_for_event",
            event_name: "ticket.approved",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
        },
      },
    });
    const waitingRun = makeRun({
      workflow_run_id: "WR-WAIT-EVENT",
      status: "waiting_event",
      current_node_id: "wait_gate",
      trace_id: "trace-wait-event",
      summary: {
        output: null,
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-WAIT-EVENT",
      },
    });
    const queuedRun = {
      ...waitingRun,
      status: "queued",
      summary: {
        ...(waitingRun.summary ?? {}),
        status: "queued",
      },
    };
    const runById: Record<string, Record<string, unknown>> = {
      "WR-WAIT-EVENT": waitingRun,
    };
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(eventVersion)),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([runById["WR-WAIT-EVENT"]])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, ({ params }) =>
        HttpResponse.json(envelope(runById[String(params.runId)])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-WAIT-EVENT",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-wait-event",
            manifest: eventVersion.manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-WAIT-EVENT",
              workflow_run_id: "WR-WAIT-EVENT",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                expected_event_name: "ticket.approved",
                correlation_key: "ticket_id",
                correlation_value: "T-42",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/workflow-runs/resume-by-event`, async ({ request }) => {
        resumeByEventBody = (await request.json()) as Record<string, unknown>;
        runById["WR-WAIT-EVENT"] = queuedRun;
        return HttpResponse.json(envelope(queuedRun));
      }),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();
    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-WAIT-EVENT"));

    expect(await screen.findByTestId("run-waiting-event-config")).toHaveTextContent(
      "Inject the external event payload",
    );
    expect(screen.getByTestId("run-resume-event-name")).toHaveValue("ticket.approved");
    await user.clear(screen.getByTestId("run-resume-event-payload"));
    fireEvent.change(screen.getByTestId("run-resume-event-payload"), {
      target: { value: '{"ticket_id":"T-42","approved":true}' },
    });

    await user.click(screen.getByTestId("run-resume-by-event"));

    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Matched event ticket.approved to run WR-WAIT-EVENT",
      ),
    );
    expect(resumeByEventBody).toEqual({
      workflow_id: "WF-1",
      event_name: "ticket.approved",
      event_payload: { ticket_id: "T-42", approved: true },
    });
  });

  it("requires the checkpoint correlation field before matching a wait_for_event run in the monitor", async () => {
    const baseVersion = makeVersion();
    const eventVersion = makeVersion({
      manifest: {
        ...baseVersion.manifest,
        nodes: {
          ...baseVersion.manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_for_event",
            event_name: "ticket.approved",
            correlation_key: "ticket_id",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
        },
      },
    });
    const waitingRun = makeRun({
      workflow_run_id: "WR-WAIT-EVENT-CORRELATION",
      status: "waiting_event",
      current_node_id: "wait_gate",
      trace_id: "trace-wait-event-correlation",
      summary: {
        output: null,
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-WAIT-EVENT-CORRELATION",
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(eventVersion)),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([waitingRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(envelope(waitingRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-WAIT-EVENT-CORRELATION",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-wait-event-correlation",
            manifest: eventVersion.manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-WAIT-EVENT-CORRELATION",
              workflow_run_id: "WR-WAIT-EVENT-CORRELATION",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                expected_event_name: "ticket.approved",
                correlation_key: "ticket_id",
                correlation_value: "T-42",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-WAIT-EVENT-CORRELATION"));

    expect(
      (await screen.findByTestId("run-resume-event-payload") as HTMLTextAreaElement).value,
    ).toContain('"ticket_id": "T-42"');

    fireEvent.change(screen.getByTestId("run-resume-event-payload"), {
      target: { value: '{"ticket_id":"T-99","approved":true}' },
    });

    expect(await screen.findByTestId("run-resume-by-event-capability-note")).toHaveTextContent(
      "requires correlation field ticket_id=T-42",
    );
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
    expect(screen.getByTestId("run-resume")).toBeEnabled();
  });

  it("disables workflow-wide event matching when the checkpoint never captured the required correlation value", async () => {
    const baseVersion = makeVersion();
    const eventVersion = makeVersion({
      manifest: {
        ...baseVersion.manifest,
        nodes: {
          ...baseVersion.manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_for_event",
            event_name: "ticket.approved",
            correlation_key: "ticket_id",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
        },
      },
    });
    const waitingRun = makeRun({
      workflow_run_id: "WR-WAIT-EVENT-CORRELATION-MISSING",
      status: "waiting_event",
      current_node_id: "wait_gate",
      trace_id: "trace-wait-event-correlation-missing",
      summary: {
        output: null,
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-WAIT-EVENT-CORRELATION-MISSING",
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(eventVersion)),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([waitingRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(envelope(waitingRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-WAIT-EVENT-CORRELATION-MISSING",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-wait-event-correlation-missing",
            manifest: eventVersion.manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-WAIT-EVENT-CORRELATION-MISSING",
              workflow_run_id: "WR-WAIT-EVENT-CORRELATION-MISSING",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                expected_event_name: "ticket.approved",
                correlation_key: "ticket_id",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByTestId("editor-run-monitor"));
    await user.click(
      await screen.findByTestId("run-history-select-WR-WAIT-EVENT-CORRELATION-MISSING"),
    );

    expect(await screen.findByTestId("run-resume-by-event-capability-note")).toHaveTextContent(
      "requires correlation field ticket_id",
    );
    expect(screen.getByTestId("run-resume-by-event-capability-note")).toHaveTextContent(
      "did not capture a correlation value",
    );
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
    expect(screen.getByTestId("run-resume")).toBeEnabled();
  });

  it("disables workflow-wide event matching for legacy wait_event checkpoints in the monitor", async () => {
    const waitingRun = makeRun({
      workflow_run_id: "WR-WAIT-EVENT-LEGACY",
      status: "waiting_event",
      current_node_id: "wait_gate",
      trace_id: "trace-wait-event-legacy",
      summary: {
        output: null,
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-WAIT-EVENT-LEGACY",
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([waitingRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(envelope(waitingRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-WAIT-EVENT-LEGACY",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-wait-event-legacy",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-WAIT-EVENT-LEGACY",
              workflow_run_id: "WR-WAIT-EVENT-LEGACY",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_event",
                node_id: "wait_gate",
                event_name: "ticket.approved",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-WAIT-EVENT-LEGACY"));

    expect(await screen.findByTestId("run-resume-by-event-capability-note")).toHaveTextContent(
      "legacy wait_event shape",
    );
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
    expect(screen.getByTestId("run-resume")).toBeEnabled();
  });

  it("disables wait_for_event resume actions when the typed event name no longer matches the configured gate", async () => {
    const baseVersion = makeVersion();
    const eventVersion = makeVersion({
      manifest: {
        ...baseVersion.manifest,
        nodes: {
          ...baseVersion.manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_for_event",
            event_name: "ticket.approved",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
        },
      },
    });
    const waitingRun = makeRun({
      workflow_run_id: "WR-WAIT-EVENT-MISMATCH",
      status: "waiting_event",
      current_node_id: "wait_gate",
      trace_id: "trace-wait-event-mismatch",
      summary: {
        output: null,
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-WAIT-EVENT-MISMATCH",
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(eventVersion)),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([waitingRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(envelope(waitingRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-WAIT-EVENT-MISMATCH",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-wait-event-mismatch",
            manifest: eventVersion.manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-WAIT-EVENT-MISMATCH",
              workflow_run_id: "WR-WAIT-EVENT-MISMATCH",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                expected_event_name: "ticket.approved",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-WAIT-EVENT-MISMATCH"));

    const eventNameInput = await screen.findByTestId("run-resume-event-name");
    await user.clear(eventNameInput);
    await user.type(eventNameInput, "ticket.rejected");

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "configured for event ticket.approved",
    );
    expect(screen.getByTestId("run-resume-capability-note")).toHaveTextContent(
      "current event name is ticket.rejected",
    );
    expect(screen.getByTestId("run-resume")).toBeDisabled();
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
  });

  it("guides wait_for_event runs toward event matching when manual resume is disabled", async () => {
    const baseVersion = makeVersion();
    const eventVersion = makeVersion({
      manifest: {
        ...baseVersion.manifest,
        nodes: {
          ...baseVersion.manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_for_event",
            event_name: "ticket.approved",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
        },
      },
    });
    const waitingRun = makeRun({
      workflow_run_id: "WR-WAIT-EVENT-NO-RESUME",
      status: "waiting_event",
      current_node_id: "wait_gate",
      trace_id: "trace-wait-event-disabled",
      summary: {
        output: null,
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-WAIT-EVENT-NO-RESUME",
      },
    });
    const runById: Record<string, Record<string, unknown>> = {
      "WR-WAIT-EVENT-NO-RESUME": waitingRun,
    };
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(eventVersion)),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([runById["WR-WAIT-EVENT-NO-RESUME"]])),
      ),
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          envelope({
            workflow_runs: {
              queue_enabled: true,
              supports_async_submit: true,
              supports_cancel: false,
              supports_retry: false,
              supports_resume: false,
              runtime_approvals_enabled: false,
              checkpointing_enabled: true,
              event_backend: "database",
            },
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, ({ params }) =>
        HttpResponse.json(envelope(runById[String(params.runId)])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-WAIT-EVENT-NO-RESUME",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-wait-event",
            manifest: eventVersion.manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-WAIT-EVENT-NO-RESUME",
              workflow_run_id: "WR-WAIT-EVENT-NO-RESUME",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                expected_event_name: "ticket.approved",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();
    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-WAIT-EVENT-NO-RESUME"));

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "Use the event-match controls below to resume this event gate.",
    );
    expect(screen.getByTestId("run-waiting-event-config")).toHaveTextContent(
      "Inject the external event payload",
    );
    expect(screen.getByTestId("run-resume-by-event")).toBeEnabled();
  });

  it("disables event matching in the monitor when checkpoint persistence is disabled", async () => {
    const baseVersion = makeVersion();
    const eventVersion = makeVersion({
      manifest: {
        ...baseVersion.manifest,
        nodes: {
          ...baseVersion.manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_for_event",
            event_name: "ticket.approved",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
        },
      },
    });
    const waitingRun = makeRun({
      workflow_run_id: "WR-WAIT-EVENT-NO-CHECKPOINTING",
      status: "waiting_event",
      current_node_id: "wait_gate",
      trace_id: "trace-wait-event-no-checkpointing",
      summary: {
        output: null,
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-WAIT-EVENT-NO-CHECKPOINTING",
      },
    });
    const runById: Record<string, Record<string, unknown>> = {
      "WR-WAIT-EVENT-NO-CHECKPOINTING": waitingRun,
    };

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(eventVersion)),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([runById["WR-WAIT-EVENT-NO-CHECKPOINTING"]])),
      ),
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json(
          envelope({
            workflow_runs: {
              queue_enabled: true,
              supports_async_submit: true,
              supports_cancel: false,
              supports_retry: false,
              supports_resume: false,
              runtime_approvals_enabled: false,
              checkpointing_enabled: false,
              event_backend: "database",
            },
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, ({ params }) =>
        HttpResponse.json(envelope(runById[String(params.runId)])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-WAIT-EVENT-NO-CHECKPOINTING",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-wait-event-no-checkpointing",
            manifest: eventVersion.manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-WAIT-EVENT-NO-CHECKPOINTING",
              workflow_run_id: "WR-WAIT-EVENT-NO-CHECKPOINTING",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                expected_event_name: "ticket.approved",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByTestId("editor-run-monitor"));
    await user.click(
      await screen.findByTestId("run-history-select-WR-WAIT-EVENT-NO-CHECKPOINTING"),
    );

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "event-match resume are unavailable until checkpoint persistence is enabled",
    );
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
    expect(screen.getByTestId("run-resume")).toBeDisabled();
  });

  it("retries a failed monitored run from the selected checkpoint", async () => {
    let retryBody: Record<string, unknown> | null = null;
    let runHistoryRequests = 0;
    const failedRun = makeRun({
      workflow_run_id: "WR-FAILED",
      status: "failed",
      completed_at: NOW,
      summary: {
        output: "failed output",
        node_path: ["start", "support_agent"],
        steps: [
          {
            node_id: "support_agent",
            node_type: "agent",
            status: "error",
            output: "failed output",
            tool_calls: [],
            handoff_target: null,
            detail: "worker failed after checkpoint",
            duration_ms: 9,
          },
        ],
        resume_checkpoint_id: "CP-2",
      },
    });
    const retriedRun = makeRun({
      workflow_run_id: "WR-FAILED-RETRY",
      status: "queued",
      current_node_id: null,
      completed_at: null,
      summary: {
        output: "",
        node_path: [],
        steps: [],
        retry_of: "WR-FAILED",
        retry_mode: "checkpoint",
        resume_checkpoint_id: "CP-1",
        resume_checkpoint_run_id: "WR-FAILED",
      },
    });
    const runsById: Record<string, Record<string, unknown>> = {
      "WR-FAILED": failedRun,
      "WR-FAILED-RETRY": retriedRun,
    };
    const runCheckpointsById: Record<string, Array<Record<string, unknown>>> = {
      "WR-FAILED": [
        {
          checkpoint_id: "CP-1",
          workflow_run_id: "WR-FAILED",
          project_id: null,
          sequence: 1,
          node_id: "support_agent",
          state_blob: {
            kind: "human_approval",
            node_id: "support_agent",
            output: "approval requested",
          },
          created_at: NOW,
        },
        {
          checkpoint_id: "CP-2",
          workflow_run_id: "WR-FAILED",
          project_id: null,
          sequence: 2,
          node_id: "support_agent",
          state_blob: {
            kind: "wait_for_event",
            node_id: "support_agent",
            expected_event_name: "ticket.approved",
          },
          created_at: NOW,
        },
      ],
      "WR-FAILED-RETRY": [],
    };

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, ({ request }) => {
        runHistoryRequests += 1;
        const currentRuns = runHistoryRequests > 1 ? [retriedRun, failedRun] : [failedRun];
        const limit = new URL(request.url).searchParams.get("limit");
        if (!limit) {
          return HttpResponse.json(envelope(currentRuns));
        }
        return HttpResponse.json({ data: currentRuns, next_cursor: null });
      }),
      http.get(`${API_BASE}/workflow-runs/:runId`, ({ params }) =>
        HttpResponse.json(
          envelope(
            runsById[String(params.runId)]
            ?? makeRun({ workflow_run_id: String(params.runId) }),
          ),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, ({ params }) =>
        HttpResponse.json(envelope(runCheckpointsById[String(params.runId)] ?? [])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/workflow-runs/WR-FAILED/retry`, async ({ request }) => {
        retryBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(envelope(retriedRun));
      }),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();
    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-FAILED"));

    expect(await screen.findByTestId("workflow-run-checkpoint-retry")).toBeInTheDocument();
    await user.click(screen.getByTestId("workflow-run-checkpoint-item-1"));
    await user.click(screen.getByTestId("workflow-run-checkpoint-retry"));

    await waitFor(() =>
      expect(retryBody).toEqual({ checkpoint_id: "CP-1" }),
    );
    expect(await screen.findByTestId("editor-run-message")).toHaveTextContent(
      "Retry from checkpoint CP-1 queued as WR-FAILED-RETRY.",
    );
    expect(screen.getByTestId("run-active-summary")).toHaveTextContent("WR-FAILED-RETRY");
    await waitFor(() => expect(runHistoryRequests).toBeGreaterThan(1));
    expect(await screen.findByTestId("run-history-select-WR-FAILED-RETRY")).toBeInTheDocument();
    expect(await screen.findByTestId("workflow-run-checkpoint-panel")).toHaveTextContent(
      "resumes from CP-1 captured on WR-FAILED",
    );
    expect(screen.getByTestId("workflow-run-checkpoint-item-source")).toHaveTextContent(
      "Inherited",
    );
    expect(screen.queryByTestId("workflow-run-checkpoint-retry")).not.toBeInTheDocument();
  });

  it("turns stale retry, approval, resume, and event-match monitor failures into recovery guidance", async () => {
    const eventVersion = makeVersion({
      manifest: {
        ...makeVersion().manifest,
        nodes: {
          ...makeVersion().manifest.nodes,
          wait_gate: {
            id: "wait_gate",
            type: "wait_for_event",
            event_name: "ticket.approved",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
          tool_gate: {
            id: "tool_gate",
            type: "tool",
            tool_name: "lookup_policy",
            inputs: { input: { type: "string" } },
            outputs: { output: { type: "string" } },
          },
        },
      },
    });
    const retryRun = makeRun({
      workflow_run_id: "WR-RETRY-STALE",
      status: "failed",
      completed_at: NOW,
      summary: {
        output: "",
        node_path: ["start", "support_agent"],
        steps: [],
      },
    });
    const approveRun = makeRun({
      workflow_run_id: "WR-APPROVE-STALE",
      status: "waiting_approval",
      current_node_id: "tool_gate",
      trace_id: "trace-approve-stale",
      summary: { output: null, node_path: ["start", "tool_gate"], steps: [] },
    });
    const resumeRun = makeRun({
      workflow_run_id: "WR-RESUME-STALE",
      status: "waiting_approval",
      current_node_id: "tool_gate",
      trace_id: "trace-resume-stale",
      summary: {
        output: null,
        node_path: ["start", "tool_gate"],
        steps: [],
        resume_checkpoint_id: "CP-RESUME-STALE",
      },
    });
    const eventRun = makeRun({
      workflow_run_id: "WR-EVENT-STALE",
      status: "waiting_event",
      current_node_id: "wait_gate",
      trace_id: "trace-event-stale",
      summary: {
        output: null,
        node_path: ["start", "wait_gate"],
        steps: [],
        resume_checkpoint_id: "CP-EVENT-STALE",
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(eventVersion)),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([retryRun, approveRun, resumeRun, eventRun])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, ({ params }) => {
        const runId = String(params.runId);
        const run =
          runId === "WR-RETRY-STALE"
            ? retryRun
            : runId === "WR-APPROVE-STALE"
              ? approveRun
              : runId === "WR-RESUME-STALE"
                ? resumeRun
                : eventRun;
        return HttpResponse.json(envelope(run));
      }),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, ({ params }) => {
        const runId = String(params.runId);
        if (runId === "WR-RETRY-STALE") {
          return HttpResponse.json(
            envelope([
              {
                checkpoint_id: "CP-RETRY-STALE",
                workflow_run_id: runId,
                project_id: null,
                sequence: 1,
                node_id: "support_agent",
                state_blob: {
                  kind: "human_approval",
                  node_id: "support_agent",
                  output: "approval requested",
                },
                created_at: NOW,
              },
            ]),
          );
        }
        if (runId === "WR-APPROVE-STALE" || runId === "WR-RESUME-STALE") {
          return HttpResponse.json(
            envelope([
              {
                checkpoint_id: runId === "WR-APPROVE-STALE" ? "CP-APPROVE-STALE" : "CP-RESUME-STALE",
                workflow_run_id: runId,
                project_id: null,
                sequence: 1,
                node_id: "tool_gate",
                state_blob: {
                  kind: "runtime_approval",
                  node_id: "tool_gate",
                  input_by_port: {
                    input: "delete ticket T-300",
                  },
                },
                created_at: NOW,
              },
            ]),
          );
        }
        return HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-EVENT-STALE",
              workflow_run_id: runId,
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                input_by_port: { input: "hello" },
                expected_event_name: "ticket.approved",
                correlation_key: "ticket_id",
                correlation_value: "T-42",
              },
              created_at: NOW,
            },
          ]),
        );
      }),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, ({ params }) => {
        const runId = String(params.runId);
        if (runId === "WR-APPROVE-STALE") {
          return HttpResponse.json(
            envelope([
              {
                runtime_approval_id: "RA-APPROVE-STALE",
                workflow_run_id: runId,
                project_id: null,
                node_id: "tool_gate",
                status: "pending",
                requested_at: NOW,
                decided_at: null,
                decided_by: null,
                decision_reason: null,
                policy_snapshot: null,
              },
            ]),
          );
        }
        if (runId === "WR-RESUME-STALE") {
          return HttpResponse.json(
            envelope([
              {
                runtime_approval_id: "RA-RESUME-STALE",
                workflow_run_id: runId,
                project_id: null,
                node_id: "tool_gate",
                status: "approved",
                requested_at: NOW,
                decided_at: NOW,
                decided_by: "@ops",
                decision_reason: "approved",
                policy_snapshot: null,
              },
            ]),
          );
        }
        return HttpResponse.json(envelope([]));
      }),
      http.post(`${API_BASE}/workflow-runs/WR-RETRY-STALE/retry`, () =>
        HttpResponse.json(
          { detail: "workflow run retry checkpoint is missing its input snapshot" },
          { status: 409 },
        ),
      ),
      http.post(`${API_BASE}/workflow-runs/WR-APPROVE-STALE/approval/approve`, () =>
        HttpResponse.json(
          { detail: "workflow run approval checkpoint is missing its input snapshot" },
          { status: 409 },
        ),
      ),
      http.post(`${API_BASE}/workflow-runs/WR-RESUME-STALE/resume`, () =>
        HttpResponse.json({ detail: "workflow run has no resume checkpoint" }, { status: 409 }),
      ),
      http.post(`${API_BASE}/workflow-runs/resume-by-event`, () =>
        HttpResponse.json(
          {
            detail:
              "event 'ticket.approved' reached waiting workflow runs with resume checkpoints missing correlation_value for their configured correlation_key: WR-EVENT-STALE",
          },
          { status: 409 },
        ),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();
    await user.click(screen.getByTestId("editor-run-monitor"));

    await user.click(await screen.findByTestId("run-history-select-WR-RETRY-STALE"));
    await user.click(await screen.findByTestId("workflow-run-checkpoint-retry"));
    expect(await screen.findByTestId("editor-run-message")).toHaveTextContent(
      "Retry failed because this run's stored checkpoint or manifest snapshot is no longer healthy.",
    );
    expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
      "Inspect the recovery, checkpoint, lineage, and debugger panels in this run monitor before retrying from a different checkpoint or starting a new attempt.",
    );
    expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
      "Latest backend detail: workflow run retry checkpoint is missing its input snapshot",
    );

    await user.click(await screen.findByTestId("run-history-select-WR-APPROVE-STALE"));
    await user.click(await screen.findByTestId("run-approve"));
    expect(await screen.findByTestId("editor-run-message")).toHaveTextContent(
      "Approve failed because this paused approval state is no longer healthy.",
    );
    expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
      "Refresh approval history and inspect the recovery, checkpoint, and debugger panels in this run monitor before trying again.",
    );
    expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
      "Latest backend detail: workflow run approval checkpoint is missing its input snapshot",
    );

    await user.click(await screen.findByTestId("run-history-select-WR-RESUME-STALE"));
    await waitFor(() => expect(screen.getByTestId("run-resume")).toBeEnabled());
    await user.click(screen.getByTestId("run-resume"));
    expect(await screen.findByTestId("editor-run-message")).toHaveTextContent(
      "Resume failed because this paused run is no longer resumable from its stored checkpoint.",
    );
    expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
      "Inspect the recovery, checkpoint, lineage, and debugger panels in this run monitor before retrying from a healthy checkpoint or starting a new attempt.",
    );
    expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
      "Latest backend detail: workflow run has no resume checkpoint",
    );

    await user.click(await screen.findByTestId("run-history-select-WR-EVENT-STALE"));
    await user.clear(screen.getByTestId("run-resume-event-payload"));
    fireEvent.change(screen.getByTestId("run-resume-event-payload"), {
      target: { value: '{"ticket_id":"T-42","approved":true}' },
    });
    await user.click(await screen.findByTestId("run-resume-by-event"));
    expect(await screen.findByTestId("editor-run-message")).toHaveTextContent(
      "External event resume failed because no safe waiting run could be selected for this event.",
    );
    expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
      "Inspect the recovery, checkpoint, and lineage panels in this run monitor, then resume the target run directly or add the required event correlation before retrying.",
    );
    expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
      "Latest backend detail: event 'ticket.approved' reached waiting workflow runs with resume checkpoints missing correlation_value for their configured correlation_key: WR-EVENT-STALE",
    );
  });

  it("disables editor resume actions when the active checkpoint drifts from the run node", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(
          envelope([
            makeRun({
              workflow_run_id: "WR-WAIT-EVENT-DRIFT",
              status: "waiting_event",
              current_node_id: "wait_gate",
              summary: {
                output: "",
                node_path: ["start", "wait_gate"],
                steps: [],
                resume_checkpoint_id: "CP-WAIT-EVENT-DRIFT",
              },
            }),
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(
          envelope(
            makeRun({
              workflow_run_id: "WR-WAIT-EVENT-DRIFT",
              status: "waiting_event",
              current_node_id: "wait_gate",
              summary: {
                output: "",
                node_path: ["start", "wait_gate"],
                steps: [],
                resume_checkpoint_id: "CP-WAIT-EVENT-DRIFT",
              },
            }),
          ),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-WAIT-EVENT-DRIFT",
              workflow_run_id: "WR-WAIT-EVENT-DRIFT",
              project_id: null,
              sequence: 1,
              node_id: "tool_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "other_gate",
                expected_event_name: "ticket.approved",
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();
    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-WAIT-EVENT-DRIFT"));

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "stored checkpoint no longer matches this run's active node",
    );
    expect(screen.getByTestId("run-resume")).toBeDisabled();
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
  });

  it("disables editor resume actions when a wait-for-event checkpoint loses its expected event name", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(
          envelope([
            makeRun({
              workflow_run_id: "WR-WAIT-EVENT-MISSING-NAME",
              status: "waiting_event",
              current_node_id: "wait_gate",
              summary: {
                output: "",
                node_path: ["start", "wait_gate"],
                steps: [],
                resume_checkpoint_id: "CP-WAIT-EVENT-MISSING-NAME",
              },
            }),
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(
          envelope(
            makeRun({
              workflow_run_id: "WR-WAIT-EVENT-MISSING-NAME",
              status: "waiting_event",
              current_node_id: "wait_gate",
              summary: {
                output: "",
                node_path: ["start", "wait_gate"],
                steps: [],
                resume_checkpoint_id: "CP-WAIT-EVENT-MISSING-NAME",
              },
            }),
          ),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-WAIT-EVENT-MISSING-NAME",
              workflow_run_id: "WR-WAIT-EVENT-MISSING-NAME",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: {
                kind: "wait_for_event",
                node_id: "wait_gate",
                input_by_port: { input: "hello" },
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();
    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-WAIT-EVENT-MISSING-NAME"));

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "wait-for-event checkpoint has no expected event name",
    );
    expect(screen.getByTestId("run-resume")).toBeDisabled();
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
  });

  it("disables editor resume actions when the active wait checkpoint payload is corrupt", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(
          envelope([
            makeRun({
              workflow_run_id: "WR-WAIT-EVENT-CORRUPT",
              status: "waiting_event",
              current_node_id: "wait_gate",
              summary: {
                output: "",
                node_path: ["start", "wait_gate"],
                steps: [],
                resume_checkpoint_id: "CP-WAIT-EVENT-CORRUPT",
              },
            }),
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId`, () =>
        HttpResponse.json(
          envelope(
            makeRun({
              workflow_run_id: "WR-WAIT-EVENT-CORRUPT",
              status: "waiting_event",
              current_node_id: "wait_gate",
              summary: {
                output: "",
                node_path: ["start", "wait_gate"],
                steps: [],
                resume_checkpoint_id: "CP-WAIT-EVENT-CORRUPT",
              },
            }),
          ),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CP-WAIT-EVENT-CORRUPT",
              workflow_run_id: "WR-WAIT-EVENT-CORRUPT",
              project_id: null,
              sequence: 1,
              node_id: "wait_gate",
              state_blob: ["corrupt-checkpoint-payload"],
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();
    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-WAIT-EVENT-CORRUPT"));

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "checkpoint payload is corrupt",
    );
    expect(screen.getByTestId("run-resume")).toBeDisabled();
    expect(screen.getByTestId("run-resume-by-event")).toBeDisabled();
  });

  it("polls session memory while the run monitor is watching a live run", async () => {
    const liveRun = makeRun({
      workflow_run_id: "WR-LIVE",
      session_id: "SESSION-live",
      status: "running",
      current_node_id: "support_agent",
      summary: {
        output: "monitoring",
        node_path: ["start", "support_agent"],
        steps: [],
      },
    });
    let sessionEntries: Array<Record<string, unknown>> = [];

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([liveRun])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/workflow-runs`, () =>
        HttpResponse.json(envelope(liveRun), { status: 201 }),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-LIVE`, () =>
        HttpResponse.json(envelope(liveRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-LIVE/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-LIVE/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-LIVE/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, ({ request }) => {
        const url = new URL(request.url);
        const sessionId = url.searchParams.get("session_id");
        return HttpResponse.json(envelope(sessionId === "SESSION-live" ? sessionEntries : []));
      }),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.clear(screen.getByTestId("run-session-id"));
    await user.type(screen.getByTestId("run-session-id"), "SESSION-live");
    await user.click(screen.getByTestId("run-execute"));

    expect(await screen.findByTestId("workflow-session-memory-empty")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-session-memory-empty")).toHaveTextContent(
      "This run may still be executing",
    );
    expect(screen.getByTestId("workflow-session-memory-empty")).toHaveTextContent(
      "refresh this panel after the next assistant turn is recorded",
    );

    sessionEntries = [
      {
        workflow_id: "WF-1",
        node_id: "support_agent",
        session_id: "SESSION-live",
        message_history: [
          { role: "user", content: "Customer asked about refunds" },
          { role: "assistant", content: "Shared refund guidance" },
        ],
        message_count: 2,
        turn_count: 1,
        created_at: NOW,
        updated_at: NOW,
        last_user_message: "Customer asked about refunds",
        last_assistant_message: "Shared refund guidance",
      },
    ];

    await waitFor(
      () =>
        expect(
          screen.getByTestId("workflow-session-memory-entry-support_agent"),
        ).toHaveTextContent("Shared refund guidance"),
      { timeout: 3500 },
    );
  });

  it("explains how to enable shared memory when the selected run has no session id", async () => {
    const runWithoutSession = makeRun({
      workflow_run_id: "WR-NO-SESSION",
      session_id: null,
      status: "completed",
      summary: {
        output: "done",
        node_path: ["start", "support_agent"],
        steps: [],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([runWithoutSession])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-NO-SESSION`, () =>
        HttpResponse.json(envelope(runWithoutSession)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-NO-SESSION/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-NO-SESSION/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-NO-SESSION/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-NO-SESSION/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-NO-SESSION",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-no-session",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-NO-SESSION"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent("Loaded run WR-NO-SESSION"),
    );

    expect(await screen.findByTestId("workflow-session-memory-missing")).toHaveTextContent(
      "This completed run did not set a shared session_id",
    );
    expect(screen.getByTestId("workflow-session-memory-missing")).toHaveTextContent(
      "Inspect the debugger or final outputs",
    );
    expect(screen.getByTestId("workflow-session-memory-missing")).toHaveTextContent(
      "rerun it with the same session_id",
    );
  });

  it("turns session-memory lookup failures into completed-run recovery guidance", async () => {
    const runWithSession = makeRun({
      workflow_run_id: "WR-SESSION-ERROR",
      session_id: "SESSION-error",
      status: "completed",
      summary: {
        output: "done",
        node_path: ["start", "support_agent"],
        steps: [],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([runWithSession])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-SESSION-ERROR`, () =>
        HttpResponse.json(envelope(runWithSession)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-SESSION-ERROR/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-SESSION-ERROR/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-SESSION-ERROR/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-SESSION-ERROR/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-SESSION-ERROR",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-session-error",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(
          { detail: "session memory backend timed out" },
          { status: 503 },
        ),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-SESSION-ERROR"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Loaded run WR-SESSION-ERROR",
      ),
    );

    expect(await screen.findByTestId("workflow-session-memory-error")).toHaveTextContent(
      "Session memory could not be loaded for this completed run",
    );
    expect(screen.getByTestId("workflow-session-memory-error")).toHaveTextContent(
      "Inspect the debugger, final outputs, and generated artifacts",
    );
    expect(screen.getByTestId("workflow-session-memory-error")).toHaveTextContent(
      "rerun with the same session_id",
    );
    expect(screen.getByTestId("workflow-session-memory-error")).toHaveTextContent(
      "Latest lookup error: session memory backend timed out",
    );
  });

  it("turns checkpoint lookup failures into completed-run recovery guidance in the run monitor", async () => {
    const runWithCheckpointError = makeRun({
      workflow_run_id: "WR-CHECKPOINT-ERROR",
      status: "completed",
      summary: {
        output: "done",
        node_path: ["start", "support_agent"],
        steps: [],
        resume_checkpoint_id: "CHK-error",
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([runWithCheckpointError])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-CHECKPOINT-ERROR`, () =>
        HttpResponse.json(envelope(runWithCheckpointError)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-CHECKPOINT-ERROR/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-CHECKPOINT-ERROR/checkpoints`, () =>
        HttpResponse.json(
          { detail: "checkpoint store timed out" },
          { status: 503 },
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-CHECKPOINT-ERROR/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-CHECKPOINT-ERROR/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-CHECKPOINT-ERROR",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-checkpoint-error",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-CHECKPOINT-ERROR"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Loaded run WR-CHECKPOINT-ERROR",
      ),
    );

    expect(await screen.findByTestId("workflow-run-checkpoints-error")).toHaveTextContent(
      "Resume checkpoints could not be loaded for this completed run.",
    );
    expect(screen.getByTestId("workflow-run-checkpoints-error")).toHaveTextContent(
      "Inspect the recovery, debugger, final outputs, and generated artifacts",
    );
    expect(screen.getByTestId("workflow-run-checkpoints-error")).toHaveTextContent(
      "retry this lookup if you need the stored checkpoint trail restored",
    );
    expect(screen.getByTestId("workflow-run-checkpoints-error")).toHaveTextContent(
      "Latest checkpoint error: checkpoint store timed out",
    );
  });

  it("turns approval lookup failures into stopped-run recovery guidance in the run monitor", async () => {
    const runWithApprovalError = makeRun({
      workflow_run_id: "WR-APPROVAL-ERROR",
      status: "failed",
      current_node_id: "tool_gate",
      summary: {
        output: "",
        node_path: ["start", "tool_gate"],
        steps: [],
        resume_checkpoint_id: "CHK-approval-error",
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([runWithApprovalError])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL-ERROR`, () =>
        HttpResponse.json(envelope(runWithApprovalError)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL-ERROR/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL-ERROR/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CHK-approval-error",
              workflow_run_id: "WR-APPROVAL-ERROR",
              project_id: null,
              sequence: 1,
              node_id: "tool_gate",
              state_blob: {
                kind: "runtime_approval",
                node_id: "tool_gate",
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL-ERROR/approvals`, () =>
        HttpResponse.json(
          { detail: "approval store timed out" },
          { status: 503 },
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL-ERROR/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-APPROVAL-ERROR",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-approval-error",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-APPROVAL-ERROR"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Loaded run WR-APPROVAL-ERROR",
      ),
    );

    expect(await screen.findByTestId("workflow-run-recovery-approvals-error")).toHaveTextContent(
      "Runtime approval history could not be loaded for this stopped run.",
    );
    expect(screen.getByTestId("workflow-run-recovery-approvals-error")).toHaveTextContent(
      "Recovery diagnostics may still show checkpoints and lifecycle history",
    );
    expect(screen.getByTestId("workflow-run-recovery-approvals-error")).toHaveTextContent(
      "Latest approval error: approval store timed out",
    );
    expect(screen.getByTestId("workflow-run-recovery-panel")).toHaveTextContent("Terminal state");
  });

  it("shows an explicit resume note when approval records fail to load for a paused monitored run", async () => {
    const pausedRun = makeRun({
      workflow_run_id: "WR-APPROVAL-LOAD",
      status: "waiting_approval",
      current_node_id: "tool_gate",
      summary: {
        resume_checkpoint_id: "CHK-approval-load",
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([pausedRun])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL-LOAD`, () =>
        HttpResponse.json(envelope(pausedRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL-LOAD/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL-LOAD/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "CHK-approval-load",
              workflow_run_id: "WR-APPROVAL-LOAD",
              project_id: null,
              sequence: 1,
              node_id: "tool_gate",
              state_blob: {
                kind: "runtime_approval",
                node_id: "tool_gate",
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL-LOAD/approvals`, () =>
        HttpResponse.json(
          { detail: "approval store timed out" },
          { status: 503 },
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL-LOAD/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-APPROVAL-LOAD",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-approval-load",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-APPROVAL-LOAD"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Loaded run WR-APPROVAL-LOAD",
      ),
    );

    expect(await screen.findByTestId("run-resume-capability-note")).toHaveTextContent(
      "Manual resume is unavailable because runtime approval records could not be loaded.",
    );
    expect(screen.getByTestId("run-resume-capability-note")).toHaveTextContent(
      "inspect recovery diagnostics before continuing this run",
    );
    expect(screen.getByTestId("run-approval-capability-note")).toHaveTextContent(
      "Approval actions are unavailable because runtime approval records could not be loaded.",
    );
    expect(screen.getByTestId("run-approval-capability-note")).toHaveTextContent(
      "inspect recovery diagnostics before continuing this run",
    );
    expect(screen.getByTestId("run-resume")).toBeDisabled();
  });

  it("turns inherited source-checkpoint lookup failures into stopped-run guidance in the run monitor", async () => {
    const retriedRun = makeRun({
      workflow_run_id: "WR-RETRIED",
      status: "failed",
      summary: {
        output: "",
        node_path: ["start", "support_agent"],
        steps: [],
        resume_checkpoint_id: "SRC-CHK-1",
        resume_checkpoint_run_id: "WR-SOURCE",
        retry_mode: "checkpoint",
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([retriedRun])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-RETRIED`, () =>
        HttpResponse.json(envelope(retriedRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-RETRIED/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-RETRIED/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-SOURCE/checkpoints`, () =>
        HttpResponse.json(
          { detail: "source checkpoint store timed out" },
          { status: 503 },
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-RETRIED/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-RETRIED/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-RETRIED",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-retried",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-RETRIED"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Loaded run WR-RETRIED",
      ),
    );

    expect(await screen.findByTestId("workflow-run-checkpoint-source-error")).toHaveTextContent(
      "CALIBER could not load the original source checkpoint details",
    );
    expect(screen.getByTestId("workflow-run-checkpoint-source-error")).toHaveTextContent(
      "Inspect the lineage, recovery, and debugger panels to trace where the inherited resume path failed",
    );
    expect(screen.getByTestId("workflow-run-checkpoint-source-error")).toHaveTextContent(
      "Latest source checkpoint error: source checkpoint store timed out",
    );
  });

  it("keeps the run monitor alive when persisted run events cannot be loaded", async () => {
    const runWithEventsError = makeRun({
      workflow_run_id: "WR-EVENTS-ERROR",
      status: "completed",
      summary: {
        output: "done",
        node_path: ["start", "support_agent"],
        steps: [],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([runWithEventsError])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-EVENTS-ERROR`, () =>
        HttpResponse.json(envelope(runWithEventsError)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-EVENTS-ERROR/events`, () =>
        HttpResponse.json(
          { detail: "event store timed out" },
          { status: 503 },
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-EVENTS-ERROR/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-EVENTS-ERROR/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-EVENTS-ERROR/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-EVENTS-ERROR",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-events-error",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-EVENTS-ERROR"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Loaded run WR-EVENTS-ERROR",
      ),
    );

    expect(await screen.findByTestId("run-trace-replay-events-error")).toHaveTextContent(
      "Persisted run events could not be loaded for this completed run.",
    );
    expect(screen.getByTestId("run-trace-replay-events-error")).toHaveTextContent(
      "inspect the recovery panel, final outputs, and generated artifacts",
    );
    expect(screen.getByTestId("run-trace-replay-events-error")).toHaveTextContent(
      "Latest event error: event store timed out",
    );
    expect(screen.getByTestId("run-debugger-events-error")).toHaveTextContent(
      "manifest-aware debugging are unavailable until event history is restored",
    );
    expect(screen.getByTestId("workflow-run-recovery-events-error")).toHaveTextContent(
      "Recovery timeline events could not be loaded for this completed run.",
    );
    expect(screen.getByTestId("workflow-run-recovery-events-error")).toHaveTextContent(
      "Use the final outputs and debugger state above to reconstruct how this execution finished.",
    );
    expect(screen.getByTestId("workflow-run-recovery-events-error")).toHaveTextContent(
      "Latest recovery event error: event store timed out",
    );
    expect(screen.getByTestId("run-checkpoints-section")).toBeInTheDocument();
    expect(screen.getByTestId("run-recovery-section")).toBeInTheDocument();
  });

  it("keeps fallback retry lineage visible when canonical lineage lookup fails in the run monitor", async () => {
    const lineageRuns = [
      makeRun({
        workflow_run_id: "WR-L1",
        status: "failed",
        attempt_number: 1,
        parent_run_id: null,
      }),
      makeRun({
        workflow_run_id: "WR-L2",
        status: "failed",
        attempt_number: 2,
        parent_run_id: "WR-L1",
      }),
      makeRun({
        workflow_run_id: "WR-L3",
        status: "queued",
        attempt_number: 3,
        parent_run_id: "WR-L2",
      }),
    ];

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope(lineageRuns)),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-L2`, () =>
        HttpResponse.json(envelope(lineageRuns[1])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-L2/lineage`, () =>
        HttpResponse.json(
          { detail: "lineage store timed out" },
          { status: 503 },
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-L2/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-L2/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-L2/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-L2/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-L2",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash-lineage-error",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-L2"));
    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent("Loaded run WR-L2"),
    );

    expect(await screen.findByTestId("workflow-run-lineage-error")).toHaveTextContent(
      "Canonical retry lineage could not be loaded for this stopped run.",
    );
    expect(screen.getByTestId("workflow-run-lineage-error")).toHaveTextContent(
      "CALIBER is showing the nearest retry chain reconstructed from the loaded runs instead",
    );
    expect(screen.getByTestId("workflow-run-lineage-error")).toHaveTextContent(
      "Latest lineage error: lineage store timed out",
    );
    expect(screen.getByTestId("workflow-run-lineage-panel")).toHaveTextContent("Attempt 2 of 3");
    expect(screen.getByTestId("workflow-run-lineage-item-WR-L1")).toHaveTextContent("root");
    expect(screen.getByTestId("workflow-run-lineage-item-WR-L3")).toHaveTextContent("child");
  });

  it("keeps the monitored run in recent history when the server list is stale", async () => {
    const liveRun = makeRun({
      workflow_run_id: "WR-STALE-HISTORY",
      session_id: "SESSION-stale-history",
      status: "running",
      current_node_id: "support_agent",
      summary: {
        output: "monitoring",
        node_path: ["start", "support_agent"],
        steps: [],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.post(`${API_BASE}/workflow-runs`, () =>
        HttpResponse.json(envelope(liveRun), { status: 201 }),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STALE-HISTORY`, () =>
        HttpResponse.json(envelope(liveRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STALE-HISTORY/lineage`, () =>
        HttpResponse.json(
          envelope(buildWorkflowRunLineage("WR-STALE-HISTORY", [liveRun])),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STALE-HISTORY/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STALE-HISTORY/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STALE-HISTORY/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STALE-HISTORY/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-STALE-HISTORY",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash1",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    expect(screen.getByTestId("run-history-list")).toHaveTextContent(
      "No recent workflow runs exist for this draft yet.",
    );
    expect(screen.getByTestId("run-history-list")).toHaveTextContent(
      "Use Run to create the first editor execution",
    );

    await user.click(screen.getByTestId("run-execute"));

    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Run WR-STALE-HISTORY is running.",
      ),
    );
    expect(
      await screen.findByTestId("run-history-select-WR-STALE-HISTORY"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("run-history-list")).not.toHaveTextContent(
      "No recent workflow runs yet.",
    );
  });

  it("surfaces artifact persistence badges in recent run history before recovery panels are opened", async () => {
    const persistedRun = makeRun({
      workflow_run_id: "WR-HISTORY-PERSIST",
      status: "completed",
      completed_at: NOW,
      current_node_id: "final",
      summary: {
        output: "persisted output",
        node_path: ["start", "support_agent", "final"],
        steps: [
          {
            node_id: "support_agent",
            node_type: "agent",
            status: "ok",
            output: "persisted output",
            tool_calls: [],
            handoff_target: null,
            detail: "",
            duration_ms: 0,
          },
        ],
        artifact_persistence: {
          status: "persisted",
          bucket: "caliber-suite",
          object_count: 3,
          artifact_names: ["kg.json", "report.html"],
        },
      },
    });
    const failedRun = makeRun({
      workflow_run_id: "WR-HISTORY-FAIL",
      status: "completed",
      completed_at: NOW,
      current_node_id: "final",
      trace_id: "trace-history-fail",
      summary: {
        output: "failed upload output",
        node_path: ["start", "support_agent", "final"],
        steps: [
          {
            node_id: "support_agent",
            node_type: "agent",
            status: "ok",
            output: "failed upload output",
            tool_calls: [],
            handoff_target: null,
            detail: "",
            duration_ms: 0,
          },
        ],
        artifact_persistence: {
          status: "failed",
          bucket: "caliber-suite",
          object_count: 3,
          artifact_names: ["kg.json"],
          error: "RuntimeError: object store offline",
        },
      },
    });
    const runs = [failedRun, persistedRun];

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope(runs)),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/:runId`, ({ params }) => {
        const run = runs.find((item) => item.workflow_run_id === String(params.runId ?? ""));
        return HttpResponse.json(envelope(run ?? persistedRun));
      }),
      http.get(`${API_BASE}/workflow-runs/:runId/lineage`, ({ params }) =>
        HttpResponse.json(
          envelope(buildWorkflowRunLineage(String(params.runId ?? ""), runs)),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/manifest`, ({ params }) =>
        HttpResponse.json(
          envelope({
            workflow_run_id: String(params.runId ?? "WR-HISTORY-PERSIST"),
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash1",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/:runId/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));

    expect(
      await screen.findByTestId("run-history-artifact-persistence-WR-HISTORY-PERSIST"),
    ).toHaveTextContent("2 artifacts stored");
    expect(
      screen.getByTestId("run-history-artifact-persistence-WR-HISTORY-FAIL"),
    ).toHaveTextContent("Artifact upload failed");
    expect(
      screen.getByTestId("run-history-artifact-persistence-WR-HISTORY-FAIL"),
    ).toHaveAttribute("title", expect.stringContaining("object store offline"));

    await user.click(screen.getByTestId("run-history-select-WR-HISTORY-FAIL"));
    expect(await screen.findByTestId("active-run-artifact-persistence")).toHaveTextContent(
      "Artifact upload failed",
    );
  });

  it("loads more recent run history from the paged backend when requested", async () => {
    const newestRun = makeRun({
      workflow_run_id: "WR-PAGED-NEW",
      status: "completed",
      completed_at: NOW,
      current_node_id: "final",
      summary: {
        output: "newest output",
        node_path: ["start", "support_agent", "final"],
        steps: [],
      },
    });
    const olderRun = makeRun({
      workflow_run_id: "WR-PAGED-OLD",
      status: "completed",
      completed_at: "2025-12-31T23:00:00Z",
      current_node_id: "final",
      summary: {
        output: "older output",
        node_path: ["start", "support_agent", "final"],
        steps: [],
      },
    });
    const queryLog: Array<{ cursor: string | null; limit: string | null }> = [];

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, ({ request }) => {
        const url = new URL(request.url);
        const limit = url.searchParams.get("limit");
        const cursor = url.searchParams.get("cursor");
        if (!limit) {
          return HttpResponse.json(envelope([newestRun, olderRun]));
        }
        queryLog.push({ cursor, limit });
        if (cursor === "1") {
          return HttpResponse.json({ data: [olderRun], next_cursor: null });
        }
        return HttpResponse.json({ data: [newestRun], next_cursor: "1" });
      }),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));

    expect(await screen.findByTestId("run-history-select-WR-PAGED-NEW")).toBeInTheDocument();
    expect(screen.queryByTestId("run-history-select-WR-PAGED-OLD")).not.toBeInTheDocument();
    expect(screen.getByTestId("run-history-load-more")).toBeInTheDocument();

    await user.click(screen.getByTestId("run-history-load-more"));

    expect(await screen.findByTestId("run-history-select-WR-PAGED-OLD")).toBeInTheDocument();
    expect(queryLog).toEqual([
      { cursor: null, limit: "12" },
      { cursor: "1", limit: "12" },
    ]);
  });

  it("falls back to the recent run index in the monitor when paged run history is unavailable", async () => {
    const recentRun = makeRun({
      workflow_run_id: "WR-FALLBACK-RECENT",
      status: "completed",
      completed_at: NOW,
      current_node_id: "final",
      summary: {
        output: "recent output",
        node_path: ["start", "support_agent", "final"],
        steps: [],
      },
    });
    const olderRun = makeRun({
      workflow_run_id: "WR-FALLBACK-OLDER",
      trace_id: "trace-fallback-older",
      status: "failed",
      completed_at: "2025-12-31T23:00:00Z",
      current_node_id: "final",
      summary: {
        output: "older output",
        node_path: ["start", "support_agent", "final"],
        steps: [],
      },
    });
    let pagedRequests = 0;
    let fallbackRequests = 0;

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, ({ request }) => {
        const limit = new URL(request.url).searchParams.get("limit");
        if (!limit) {
          fallbackRequests += 1;
          return HttpResponse.json(envelope([recentRun, olderRun]));
        }
        pagedRequests += 1;
        return HttpResponse.json(
          { detail: "run history paging unavailable" },
          { status: 503 },
        );
      }),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));

    expect(await screen.findByTestId("run-history-query-fallback")).toHaveTextContent(
      "Full run-history paging is temporarily unavailable. Showing the recent run index instead.",
    );
    expect(await screen.findByTestId("run-history-select-WR-FALLBACK-RECENT")).toBeInTheDocument();
    expect(screen.getByTestId("run-history-select-WR-FALLBACK-OLDER")).toBeInTheDocument();
    expect(screen.queryByTestId("run-history-load-more")).not.toBeInTheDocument();
    expect(pagedRequests).toBeGreaterThan(0);
    expect(fallbackRequests).toBeGreaterThan(0);
  });

  it("refreshes the recent run index fallback when a started event is the first live signal and paged history is unavailable", async () => {
    const existingRun = makeRun({
      workflow_run_id: "WR-FALLBACK-BASE",
      status: "completed",
      trace_id: "trace-fallback-base",
      completed_at: NOW,
      current_node_id: "final",
      summary: {
        output: "base output",
        node_path: ["start", "support_agent", "final"],
        steps: [],
      },
    });
    const startedRun = makeRun({
      workflow_run_id: "WR-FALLBACK-STARTED",
      status: "running",
      trace_id: "trace-fallback-started",
      completed_at: null,
      current_node_id: "start",
      summary: {
        output: "",
        node_path: ["start"],
        steps: [],
      },
    });
    let pagedRequests = 0;
    let fallbackRequests = 0;

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, ({ request }) => {
        const limit = new URL(request.url).searchParams.get("limit");
        if (!limit) {
          fallbackRequests += 1;
          const runs = fallbackRequests > 1 ? [startedRun, existingRun] : [existingRun];
          return HttpResponse.json(envelope(runs));
        }
        pagedRequests += 1;
        return HttpResponse.json(
          { detail: "run history paging unavailable" },
          { status: 503 },
        );
      }),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    const view = renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    expect(await screen.findByTestId("run-history-query-fallback")).toBeInTheDocument();
    expect(await screen.findByTestId("run-history-select-WR-FALLBACK-BASE")).toBeInTheDocument();
    expect(screen.queryByTestId("run-history-select-WR-FALLBACK-STARTED")).not.toBeInTheDocument();
    await waitFor(() => expect(fallbackRequests).toBe(1));

    streamState.event = {
      type: "workflow.run.started",
      workflow_id: "WF-1",
      workflow_run_id: "WR-FALLBACK-STARTED",
      status: "running",
      event_id: 11,
      sequence: 2,
      created_at: NOW,
    };
    view.rerenderEditor();

    await waitFor(() => expect(fallbackRequests).toBeGreaterThan(1));
    expect(await screen.findByTestId("run-history-select-WR-FALLBACK-STARTED")).toBeInTheDocument();
    expect(pagedRequests).toBeGreaterThan(1);
  });

  it("refreshes recent run history when a started event is the first live signal for a new run", async () => {
    const startedRun = makeRun({
      workflow_run_id: "WR-HISTORY-STARTED",
      status: "running",
      trace_id: "trace-history-started",
      completed_at: null,
      current_node_id: "start",
      summary: {
        output: "",
        node_path: ["start"],
        steps: [],
      },
    });
    let runHistoryRequests = 0;

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, ({ request }) => {
        runHistoryRequests += 1;
        const currentRuns = runHistoryRequests > 1 ? [startedRun] : [];
        const limit = new URL(request.url).searchParams.get("limit");
        if (!limit) {
          return HttpResponse.json(envelope(currentRuns));
        }
        return HttpResponse.json({ data: currentRuns, next_cursor: null });
      }),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    const view = renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await waitFor(() => expect(runHistoryRequests).toBe(1));

    streamState.event = {
      type: "workflow.run.started",
      workflow_id: "WF-1",
      workflow_run_id: "WR-HISTORY-STARTED",
      status: "running",
      event_id: 9,
      sequence: 2,
      created_at: NOW,
    };
    view.rerenderEditor();

    await waitFor(() => expect(runHistoryRequests).toBeGreaterThan(1));
    expect(await screen.findByTestId("run-history-select-WR-HISTORY-STARTED")).toBeInTheDocument();
  });

  it("turns idle run-monitor sections into guided empty states when no run is selected", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));

    expect(screen.getByTestId("run-recovery-section")).toHaveTextContent(
      "inspect blocked-run diagnostics, approval gates, and wait states",
    );
    expect(screen.getByTestId("run-lineage-section")).toHaveTextContent(
      "inspect retry lineage, ancestor attempts, and checkpoint retries",
    );
    expect(screen.getByTestId("run-trace-replay-section")).toHaveTextContent(
      "replay its executed node path and checkpoint flow",
    );
    expect(screen.getByTestId("run-debugger-section")).toHaveTextContent(
      "inspect step telemetry, tool calls, and persisted event history",
    );
    expect(screen.getByTestId("run-files-section")).toHaveTextContent(
      "inspect generated files, uploads, and node-level artifacts",
    );
    expect(screen.getByTestId("run-checkpoints-section")).toHaveTextContent(
      "inspect persisted checkpoints, resume gates, and inherited retry sources",
    );
    expect(screen.getByTestId("run-session-memory-section")).toHaveTextContent(
      "inspect shared session memory, node conversation state, and reusable agent context",
    );
  });

  it("streams workflow step events into the run monitor before the next polling refresh", async () => {
    const liveRun = makeRun({
      workflow_run_id: "WR-STREAM",
      session_id: "SESSION-stream",
      status: "running",
      current_node_id: "start",
      summary: {
        output: "",
        node_path: ["start"],
        steps: [],
      },
    });
    const runEventsById: Record<string, Array<Record<string, unknown>>> = {
      "WR-STREAM": [
        {
          event_id: 1,
          workflow_run_id: "WR-STREAM",
          project_id: null,
          sequence: 1,
          event_type: "workflow.run.started",
          node_id: "start",
          payload: {},
          created_at: NOW,
        },
      ],
    };

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([liveRun])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM`, () =>
        HttpResponse.json(envelope(liveRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM/events`, () =>
        HttpResponse.json(envelope(runEventsById["WR-STREAM"])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-STREAM",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash1",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    const view = renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-STREAM"));
    expect(await screen.findByTestId("workflow-run-debugger-empty")).toHaveTextContent(
      "No recorded step details yet",
    );

    streamState.event = {
      type: "workflow.run.step",
      workflow_id: "WF-1",
      workflow_run_id: "WR-STREAM",
      step: {
        node_id: "support_agent",
        node_type: "agent",
        status: "ok",
        output: "Policy-backed answer",
        tool_calls: [{ tool: "lookup_policy", result: { policy: "refund" } }],
        handoff_target: null,
        detail: "grounded in policy",
        duration_ms: 14,
      },
    };
    view.rerenderEditor();

    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-event-timeline")).toHaveTextContent(
        "workflow.run.step",
      ),
    );
    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent("support_agent");
    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent(
      "Policy-backed answer",
    );
    expect(screen.getByTestId("workflow-run-step-tools")).toHaveTextContent("lookup_policy");
  });

  it("uses lifecycle-aware stream messaging when a monitored run receives a cancel request", async () => {
    const liveRun = makeRun({
      workflow_run_id: "WR-STREAM-CANCEL",
      status: "running",
      current_node_id: "support_agent",
      summary: {
        output: "",
        node_path: ["start", "support_agent"],
        steps: [],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([liveRun])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-CANCEL`, () =>
        HttpResponse.json(envelope(liveRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-CANCEL/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-CANCEL/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-CANCEL/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-CANCEL/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-STREAM-CANCEL",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash1",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    const view = renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-STREAM-CANCEL"));

    streamState.event = {
      type: "workflow.run.cancel_requested",
      workflow_id: "WF-1",
      workflow_run_id: "WR-STREAM-CANCEL",
      reason: "operator stop",
      event_id: 12,
      sequence: 3,
      created_at: NOW,
    };
    view.rerenderEditor();

    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Run WR-STREAM-CANCEL has a cancel request pending: operator stop.",
      ),
    );
  });

  it("uses lifecycle-aware stream messaging when a monitored run receives runtime approval", async () => {
    const liveRun = makeRun({
      workflow_run_id: "WR-STREAM-APPROVE",
      status: "waiting_approval",
      current_node_id: "review",
      summary: {
        output: "",
        node_path: ["start", "review"],
        steps: [],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([liveRun])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-APPROVE`, () =>
        HttpResponse.json(envelope(liveRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-APPROVE/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-APPROVE/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-APPROVE/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-APPROVE/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-STREAM-APPROVE",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash1",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    const view = renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-STREAM-APPROVE"));

    streamState.event = {
      type: "workflow.run.approval.approved",
      workflow_id: "WF-1",
      workflow_run_id: "WR-STREAM-APPROVE",
      runtime_approval_id: "RA-STREAM-APPROVE",
      status: "waiting_approval",
      event_id: 13,
      sequence: 4,
      created_at: NOW,
    };
    view.rerenderEditor();

    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Approval recorded for WR-STREAM-APPROVE.",
      ),
    );
  });

  it("uses lifecycle-aware stream messaging when a monitored run approval is rejected", async () => {
    const liveRun = makeRun({
      workflow_run_id: "WR-STREAM-REJECT",
      status: "waiting_approval",
      current_node_id: "review",
      summary: {
        output: "",
        node_path: ["start", "review"],
        steps: [],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([liveRun])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-REJECT`, () =>
        HttpResponse.json(
          envelope(
            makeRun({
              workflow_run_id: "WR-STREAM-REJECT",
              status: "failed",
              current_node_id: null,
              error_summary: "unsafe tool scope",
              error_code: "approval_rejected",
              summary: {
                output: "",
                error: "unsafe tool scope",
                node_path: ["start", "review"],
                steps: [],
              },
            }),
          ),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-REJECT/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-REJECT/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-REJECT/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-REJECT/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-STREAM-REJECT",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash1",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    const view = renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-STREAM-REJECT"));

    streamState.event = {
      type: "workflow.run.approval.rejected",
      workflow_id: "WF-1",
      workflow_run_id: "WR-STREAM-REJECT",
      runtime_approval_id: "RA-STREAM-REJECT",
      reason: "unsafe tool scope",
      status: "failed",
      event_id: 14,
      sequence: 5,
      created_at: NOW,
    };
    view.rerenderEditor();

    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Runtime approval rejected for WR-STREAM-REJECT: unsafe tool scope.",
      ),
    );
  });

  it("uses lifecycle-aware stream messaging when a monitored run is retried and follows the new attempt", async () => {
    const failedRun = makeRun({
      workflow_run_id: "WR-STREAM-RETRY-OLD",
      status: "failed",
      current_node_id: "support_agent",
      summary: {
        output: "failed",
        error: "seeded failure",
        node_path: ["start", "support_agent"],
        steps: [],
      },
    });
    const retriedRun = makeRun({
      workflow_run_id: "WR-STREAM-RETRY-NEW",
      status: "queued",
      current_node_id: null,
      summary: {
        output: "",
        node_path: [],
        steps: [],
        retry_of: "WR-STREAM-RETRY-OLD",
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([failedRun, retriedRun])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-RETRY-OLD`, () =>
        HttpResponse.json(envelope(failedRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-RETRY-NEW`, () =>
        HttpResponse.json(envelope(retriedRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-RETRY-NEW/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-RETRY-NEW/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-RETRY-NEW/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-RETRY-OLD/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-RETRY-OLD/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-RETRY-OLD/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    const view = renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-STREAM-RETRY-OLD"));

    streamState.event = {
      type: "workflow.run.retried",
      workflow_id: "WF-1",
      workflow_run_id: "WR-STREAM-RETRY-OLD",
      retried_run_id: "WR-STREAM-RETRY-NEW",
      checkpoint_id: "CP-1",
      event_id: 15,
      sequence: 6,
      created_at: NOW,
    };
    view.rerenderEditor();

    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Run WR-STREAM-RETRY-OLD retried as WR-STREAM-RETRY-NEW.",
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("run-active-summary")).toHaveTextContent(
        "WR-STREAM-RETRY-NEW",
      ),
    );
  });

  it("uses lifecycle-aware stream messaging when a monitored run is recovered and re-queued", async () => {
    const liveRun = makeRun({
      workflow_run_id: "WR-STREAM-RECOVERED",
      status: "running",
      current_node_id: "support_agent",
      summary: {
        output: "",
        node_path: ["start", "support_agent"],
        steps: [],
      },
    });

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([liveRun])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-RECOVERED`, () =>
        HttpResponse.json(
          envelope(
            makeRun({
              workflow_run_id: "WR-STREAM-RECOVERED",
              status: "queued",
              current_node_id: null,
              summary: {
                output: "",
                node_path: ["start", "support_agent"],
                steps: [],
              },
            }),
          ),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-RECOVERED/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-RECOVERED/checkpoints`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-RECOVERED/approvals`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STREAM-RECOVERED/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-STREAM-RECOVERED",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash1",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    const view = renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-STREAM-RECOVERED"));

    streamState.event = {
      type: "workflow.run.recovered",
      workflow_id: "WF-1",
      workflow_run_id: "WR-STREAM-RECOVERED",
      status: "queued",
      reason: "lease_expired",
      worker_id: "worker-7",
      event_id: 14,
      sequence: 5,
      created_at: NOW,
    };
    view.rerenderEditor();

    await waitFor(() =>
      expect(screen.getByTestId("editor-run-message")).toHaveTextContent(
        "Run WR-STREAM-RECOVERED recovered and re-queued: worker lease expired.",
      ),
    );
  });

  it("reacts to workflow pause, resume, and archive events by gating editor runs", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1`, () =>
        HttpResponse.json(envelope(makeWorkflow({ status: "active" }))),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    const view = renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();
    await user.click(screen.getByTestId("editor-run-monitor"));

    const runButton = screen.getByTestId("run-execute");
    await waitFor(() => expect(runButton).toBeEnabled());

    streamState.event = {
      type: "workflow.paused",
      workflow_id: "WF-1",
      event_id: 21,
      sequence: 8,
      created_at: NOW,
    };
    view.rerenderEditor();

    await waitFor(() =>
      expect(screen.getByTestId("editor-message")).toHaveTextContent(
        "Workflow paused. New runs are disabled until it is resumed.",
      ),
    );
    await waitFor(() => expect(runButton).toBeDisabled());
    expect(runButton).toHaveAttribute("title", "Resume this workflow before running it");
    expect(screen.getByTestId("run-workflow-paused-note")).toHaveTextContent(
      "Resume it before launching a new editor run.",
    );

    streamState.event = {
      type: "workflow.resumed",
      workflow_id: "WF-1",
      event_id: 22,
      sequence: 9,
      created_at: NOW,
    };
    view.rerenderEditor();

    await waitFor(() =>
      expect(screen.getByTestId("editor-message")).toHaveTextContent(
        "Workflow resumed. New runs can be started again.",
      ),
    );
    await waitFor(() => expect(runButton).toBeEnabled());
    expect(screen.queryByTestId("run-workflow-paused-note")).not.toBeInTheDocument();

    streamState.event = {
      type: "workflow.updated",
      workflow_id: "WF-1",
      status: "archived",
      event_id: 23,
      sequence: 10,
      created_at: NOW,
    };
    view.rerenderEditor();

    await waitFor(() =>
      expect(screen.getByTestId("editor-message")).toHaveTextContent(
        "Workflow archived. New runs are disabled until it is restored.",
      ),
    );
    await waitFor(() => expect(runButton).toBeDisabled());
    expect(runButton).toHaveAttribute("title", "Archived workflows cannot be run");
    expect(screen.getByTestId("run-workflow-archived-note")).toHaveTextContent(
      "Create or restore an active workflow version before running it.",
    );
  });

  it("explains when deployment run capabilities fail to load", async () => {
    server.use(
      http.get(`${API_BASE}/capabilities`, () =>
        HttpResponse.json({ detail: "capabilities unavailable" }, { status: 500 }),
      ),
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1`, () =>
        HttpResponse.json(envelope(makeWorkflow({ status: "active" }))),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();
    await user.click(screen.getByTestId("editor-run-monitor"));

    const runButton = screen.getByTestId("run-execute");
    await waitFor(() => expect(runButton).toBeDisabled());
    expect(runButton).toHaveAttribute(
      "title",
      "Workflow run capabilities could not be loaded. Refresh the page or verify deployment settings/API health.",
    );
    expect(await screen.findByTestId("run-capabilities-unavailable-note")).toHaveTextContent(
      "Run controls stay disabled until the capabilities check succeeds.",
    );
    expect(screen.getByTestId("run-capabilities-unavailable-note")).toHaveTextContent(
      "verify the CALIBER API and workflow-run settings",
    );
  });

  it("redirects back to the workflow list when the current workflow is deleted elsewhere", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1`, () =>
        HttpResponse.json(envelope(makeWorkflow({ status: "active" }))),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const view = renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    streamState.event = {
      type: "workflow.deleted",
      workflow_id: "WF-1",
      name: "Support",
      event_id: 24,
      sequence: 11,
      created_at: NOW,
    };
    view.rerenderEditor();

    await waitFor(() =>
      expect(screen.getByText("WORKFLOWS ROUTE")).toBeInTheDocument(),
    );
    expect(showToast.info).toHaveBeenCalledWith('Workflow "Support" was deleted.');
  });

  it("refreshes approvals and checkpoints immediately when a monitored run enters waiting approval", async () => {
    const liveRun = makeRun({
      workflow_run_id: "WR-APPROVAL",
      status: "running",
      current_node_id: "support_agent",
      summary: {
        output: "",
        node_path: ["start", "support_agent"],
        steps: [],
      },
    });
    const runApprovalsById: Record<string, Array<Record<string, unknown>>> = {
      "WR-APPROVAL": [],
    };
    const runCheckpointsById: Record<string, Array<Record<string, unknown>>> = {
      "WR-APPROVAL": [],
    };

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([liveRun])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL`, () =>
        HttpResponse.json(envelope(liveRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL/checkpoints`, () =>
        HttpResponse.json(envelope(runCheckpointsById["WR-APPROVAL"])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL/approvals`, () =>
        HttpResponse.json(envelope(runApprovalsById["WR-APPROVAL"])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-APPROVAL/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-APPROVAL",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash1",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    const view = renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-APPROVAL"));
    expect(await screen.findByTestId("workflow-run-recovery-panel")).toHaveTextContent(
      "Actively executing",
    );

    liveRun.status = "waiting_approval";
    liveRun.current_node_id = "human_gate";
    liveRun.summary = {
      output: "",
      node_path: ["start", "human_gate"],
      steps: [],
      resume_checkpoint_id: "WRCK-APPROVAL",
    };
    runApprovalsById["WR-APPROVAL"] = [
      {
        runtime_approval_id: "RA-1",
        workflow_run_id: "WR-APPROVAL",
        project_id: null,
        node_id: "human_gate",
        status: "pending",
        required_role: "caliber.approver",
        requested_at: NOW,
        decided_at: null,
        decided_by: null,
        reason: null,
        policy_snapshot: {
          required_role: "caliber.approver",
          approval_count: 1,
          timeout_behavior: "block",
        },
      },
    ];
    runCheckpointsById["WR-APPROVAL"] = [
      {
        checkpoint_id: "WRCK-APPROVAL",
        workflow_run_id: "WR-APPROVAL",
        project_id: null,
        sequence: 1,
        node_id: "human_gate",
        state_blob: {
          kind: "human_approval",
          node_id: "human_gate",
          approval_count: 1,
        },
        created_at: NOW,
      },
    ];
    streamState.event = {
      type: "workflow.run.waiting_approval",
      workflow_id: "WF-1",
      workflow_run_id: "WR-APPROVAL",
      status: "waiting_approval",
      node_id: "human_gate",
      runtime_approval_id: "RA-1",
    };
    view.rerenderEditor();

    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-recovery-panel")).toHaveTextContent(
        "Awaiting approval",
      ),
    );
    expect(screen.getByTestId("workflow-run-recovery-panel")).toHaveTextContent("RA-1");
    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-checkpoint-item-1")).toHaveTextContent(
        "Human approval",
      ),
    );
  });

  it("labels runtime approval gates distinctly in the run monitor when a tool-gated run pauses", async () => {
    const liveRun = makeRun({
      workflow_run_id: "WR-TOOL-APPROVAL",
      status: "running",
      current_node_id: "support_agent",
      summary: {
        output: "",
        node_path: ["start", "support_agent"],
        steps: [],
      },
    });
    const runApprovalsById: Record<string, Array<Record<string, unknown>>> = {
      "WR-TOOL-APPROVAL": [],
    };
    const runCheckpointsById: Record<string, Array<Record<string, unknown>>> = {
      "WR-TOOL-APPROVAL": [],
    };

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([liveRun])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-TOOL-APPROVAL`, () =>
        HttpResponse.json(envelope(liveRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-TOOL-APPROVAL/events`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-TOOL-APPROVAL/checkpoints`, () =>
        HttpResponse.json(envelope(runCheckpointsById["WR-TOOL-APPROVAL"])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-TOOL-APPROVAL/approvals`, () =>
        HttpResponse.json(envelope(runApprovalsById["WR-TOOL-APPROVAL"])),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-TOOL-APPROVAL/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-TOOL-APPROVAL",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash1",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    const view = renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-TOOL-APPROVAL"));
    expect(await screen.findByTestId("workflow-run-recovery-panel")).toHaveTextContent(
      "Actively executing",
    );

    liveRun.status = "waiting_approval";
    liveRun.current_node_id = "tool_gate";
    liveRun.summary = {
      output: "",
      node_path: ["start", "tool_gate"],
      steps: [],
      resume_checkpoint_id: "WRCK-TOOL-APPROVAL",
    };
    runApprovalsById["WR-TOOL-APPROVAL"] = [
      {
        runtime_approval_id: "RA-TOOL-1",
        workflow_run_id: "WR-TOOL-APPROVAL",
        project_id: null,
        node_id: "tool_gate",
        status: "pending",
        requested_at: NOW,
        decided_at: null,
        decided_by: null,
        reason: null,
        policy_snapshot: {
          timeout_behavior: "block",
        },
      },
    ];
    runCheckpointsById["WR-TOOL-APPROVAL"] = [
      {
        checkpoint_id: "WRCK-TOOL-APPROVAL",
        workflow_run_id: "WR-TOOL-APPROVAL",
        project_id: null,
        sequence: 1,
        node_id: "tool_gate",
        state_blob: {
          kind: "runtime_approval",
          node_id: "tool_gate",
          input_by_port: {
            input: "delete ticket T-300",
          },
          output: "delete ticket T-300",
        },
        created_at: NOW,
      },
    ];
    streamState.event = {
      type: "workflow.run.waiting_approval",
      workflow_id: "WF-1",
      workflow_run_id: "WR-TOOL-APPROVAL",
      status: "waiting_approval",
      node_id: "tool_gate",
      runtime_approval_id: "RA-TOOL-1",
    };
    view.rerenderEditor();

    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-recovery-panel")).toHaveTextContent(
        "Awaiting runtime approval",
      ),
    );
    expect(screen.getByTestId("workflow-run-recovery-panel")).toHaveTextContent("RA-TOOL-1");
    expect(screen.getByTestId("run-approval-actions")).toHaveTextContent(
      "Awaiting runtime approval for node tool_gate",
    );
    await waitFor(() =>
      expect(screen.getByTestId("workflow-run-checkpoint-item-1")).toHaveTextContent(
        "Runtime approval",
      ),
    );
  });

  it("reconciles a stale queued snapshot with newer persisted waiting approval events", async () => {
    const staleQueuedRun = makeRun({
      workflow_run_id: "WR-STALE",
      status: "queued",
      current_node_id: null,
      summary: {
        output: "",
        node_path: ["start"],
        steps: [],
      },
    });
    const persistedEvents = [
      {
        event_id: 1,
        workflow_run_id: "WR-STALE",
        project_id: null,
        sequence: 1,
        event_type: "workflow.run.queued",
        node_id: null,
        payload: {},
        created_at: NOW,
      },
      {
        event_id: 2,
        workflow_run_id: "WR-STALE",
        project_id: null,
        sequence: 2,
        event_type: "workflow.run.started",
        node_id: null,
        payload: {},
        created_at: NOW,
      },
      {
        event_id: 3,
        workflow_run_id: "WR-STALE",
        project_id: null,
        sequence: 3,
        event_type: "workflow.run.step",
        node_id: "review",
        payload: {
          step: {
            node_id: "review",
            node_type: "human_approval",
            status: "blocked",
            output: "approval requested",
            tool_calls: [],
            handoff_target: null,
            detail: "paused for review",
            duration_ms: 0,
          },
        },
        created_at: NOW,
      },
      {
        event_id: 4,
        workflow_run_id: "WR-STALE",
        project_id: null,
        sequence: 4,
        event_type: "workflow.run.waiting_approval",
        node_id: "review",
        payload: {
          node_id: "review",
          runtime_approval_id: "RA-STALE",
        },
        created_at: NOW,
      },
    ];

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/workflows/WF-1/runs`, () =>
        HttpResponse.json(envelope([staleQueuedRun])),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/prompts`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/skills`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-runs/WR-STALE`, () =>
        HttpResponse.json(envelope(staleQueuedRun)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STALE/events`, () =>
        HttpResponse.json(envelope(persistedEvents)),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STALE/checkpoints`, () =>
        HttpResponse.json(
          envelope([
            {
              checkpoint_id: "WRCK-STALE",
              workflow_run_id: "WR-STALE",
              project_id: null,
              sequence: 1,
              node_id: "review",
              state_blob: {
                kind: "human_approval",
                node_id: "review",
                output: "approval requested",
              },
              created_at: NOW,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STALE/approvals`, () =>
        HttpResponse.json(
          envelope([
            {
              runtime_approval_id: "RA-STALE",
              workflow_run_id: "WR-STALE",
              project_id: null,
              node_id: "review",
              status: "pending",
              requested_at: NOW,
              decided_at: null,
              decided_by: null,
              decision_reason: null,
              policy_snapshot: null,
            },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-runs/WR-STALE/manifest`, () =>
        HttpResponse.json(
          envelope({
            workflow_run_id: "WR-STALE",
            workflow_id: "WF-1",
            workflow_version_id: "WFV-1",
            manifest_mode: "saved_version",
            manifest_hash: "hash1",
            manifest: makeVersion().manifest,
          }),
        ),
      ),
      http.get(`${API_BASE}/workflows/WF-1/session-memory`, () =>
        HttpResponse.json(envelope([])),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("editor-run-monitor"));
    await user.click(await screen.findByTestId("run-history-select-WR-STALE"));

    await waitFor(() =>
      expect(screen.getByTestId("run-status-badge")).toHaveTextContent("Awaiting approval"),
    );
    expect(screen.getByTestId("run-active-summary")).toHaveTextContent("review");
    expect(screen.getByTestId("run-approval-actions")).toHaveTextContent(
      "Approve to unlock Resume",
    );
    expect(screen.getByTestId("workflow-run-recovery-panel")).toHaveTextContent(
      "Awaiting approval",
    );
  });

  it("uses backend starter node templates for dropped and quick-added nodes", async () => {
    let previewBody: Record<string, unknown> | null = null;

    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-components`, () =>
        HttpResponse.json(
          envelope({
            schema_version: 1,
            components: [
              {
                type: "guardrail",
                label: "Guardrail",
                category: "Safety",
                description: "Guardrail",
                docs: [],
                default_inputs: {
                  response: { type: "string", description: "", schema: null },
                },
                default_outputs: {
                  safe: { type: "string", description: "", schema: null },
                },
                starter_node: {
                  id: "__CALIBER_NODE_ID__",
                  type: "guardrail",
                  mode: "post_agent",
                  inputs: { response: { type: "string" } },
                  outputs: { safe: { type: "string" } },
                  on_failure: "warn",
                  max_retries: 2,
                  checks: [{ custom_rule: {} }],
                },
                fields: [],
                setup_checks: [],
              },
              {
                type: "output",
                label: "Output",
                category: "Inputs & Outputs",
                description: "Output",
                docs: [],
                default_inputs: {
                  answer: { type: "string", description: "", schema: null },
                },
                default_outputs: {},
                starter_node: {
                  id: "__CALIBER_NODE_ID__",
                  type: "output",
                  inputs: { answer: { type: "string" } },
                },
                fields: [],
                setup_checks: [],
              },
            ],
          }),
        ),
      ),
      http.post(`${API_BASE}/workflow-versions/WFV-1/preview-run`, async ({ request }) => {
        previewBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(
          envelope({
            status: "completed",
            output: "ok",
            steps: [
              {
                node_id: "start",
                node_type: "start",
                status: "ok",
                output: "",
                tool_calls: [],
                handoff_target: null,
                detail: "",
                duration_ms: 0,
              },
            ],
            error: null,
          }),
        );
      }),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("canvas-drop-node"));
    await user.click(screen.getByTestId("canvas-quick-add-support"));
    await user.click((await screen.findAllByRole("button", { name: /Output/i })).pop()!);
    await user.click(screen.getByTestId("editor-preview"));
    await user.click(screen.getByTestId("preview-run"));

    await waitFor(() => expect(previewBody).not.toBeNull());
    const manifest = previewBody!.manifest as { nodes: Record<string, Record<string, unknown>> };

    expect(manifest.nodes.guardrail).toMatchObject({
      on_failure: "warn",
      max_retries: 2,
      checks: [{ custom_rule: {} }],
      outputs: { safe: { type: "string" } },
    });
    expect(manifest.nodes.output).toMatchObject({
      inputs: { answer: { type: "string" } },
    });
  });

  it("rejects unsupported workflow node types instead of silently creating notes", () => {
    expect(() => newNode("experimental_step", "experimental_step")).toThrow(
      'Unsupported workflow node type "experimental_step".',
    );
  });

  it("surfaces an editor error when a backend component type is unsupported", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-versions/WFV-1`, () =>
        HttpResponse.json(envelope(makeVersion())),
      ),
      http.get(`${API_BASE}/tools`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/mcp-servers`, () => HttpResponse.json(envelope([]))),
      http.get(`${API_BASE}/workflow-components`, () =>
        HttpResponse.json(
          envelope({
            schema_version: 1,
            components: [
              {
                type: "experimental_step",
                label: "Experimental Step",
                category: "Labs",
                description: "Backend-only prototype node.",
                docs: ["This node is not yet supported by the current editor build."],
                default_inputs: {},
                default_outputs: {},
                fields: [],
                setup_checks: [],
              },
            ],
          }),
        ),
      ),
    );

    const user = userEvent.setup();
    renderEditor();
    expect(await screen.findByTestId("workflow-editor")).toBeInTheDocument();

    await user.click(screen.getByTestId("palette-experimental_step"));

    expect(await screen.findByTestId("editor-message")).toHaveTextContent(
      'Unsupported workflow node type "experimental_step".',
    );
    expect(showToast.error).toHaveBeenCalledWith(
      'Unsupported workflow node type "experimental_step". Refresh the page or update the workflow component catalog before adding it.',
    );
  });
});
