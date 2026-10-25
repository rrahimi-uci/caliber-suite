import { describe, expect, it, vi } from "vitest";

import { TraceReplayGraph } from "@/components/workflows/TraceReplayGraph";
import { render, screen, userEvent } from "@/test/utils";

const manifest = {
  nodes: {
    tool: { id: "tool", type: "tool", label: "Lookup customer" },
    loop: { id: "loop", type: "for_each", label: "Process items" },
    child: { id: "child", type: "subworkflow", label: "Child workflow" },
    query: { id: "query", type: "knowledge_query", label: "Search knowledge" },
  },
  edges: [],
} as never;

const run = {
  workflow_run_id: "run-trace",
  workflow_id: "workflow-1",
  project_id: null,
  tenant_id: null,
  workflow_version_id: "version-1",
  deployment_alias: "prod",
  mlflow_run_id: null,
  trace_id: "trace-1",
  session_id: null,
  status: "failed",
  source: "editor",
  priority: 0,
  queued_at: "2026-01-01T00:00:00Z",
  started_at: "2026-01-01T00:00:01Z",
  completed_at: "2026-01-01T00:01:00Z",
  current_node_id: "query",
  summary: {
    node_path: ["tool", "loop", "child", "query"],
    error: "query failed",
    tags: { "caliber.prompt_version": "v5" },
    steps: [
      {
        node_id: "tool",
        node_type: "tool",
        status: "ok",
        duration_ms: 1500,
        output_by_port: {
          metadata: {
            tool_name: "lookup",
            registry_ref: "registry:lookup",
            binding_type: "registry",
            requires_approval: true,
            side_effect_level: "read",
            arguments: { q: "customer" },
          },
          text: "found customer",
        },
        tool_calls: [{ tool: "lookup" }],
      },
      {
        node_id: "loop",
        node_type: "for_each",
        status: "ok",
        output_by_port: {
          metadata: { count: 1, failed: 0, target_node_id: "tool", target_node_type: "tool" },
          results: [{ item: "one", status: "ok", output: "done", artifacts: [] }],
        },
        tool_calls: [],
      },
      {
        node_id: "child",
        node_type: "subworkflow",
        status: "ok",
        output_by_port: {
          result: {
            status: "blocked",
            workflow_id: "child-wf",
            alias: "staging",
            workflow_version_id: "child-v2",
            workflow_version_number: 2,
            steps: ["child-step"],
            output: "child result",
          },
        },
        tool_calls: [],
      },
      {
        node_id: "query",
        node_type: "knowledge_query",
        status: "error",
        duration_ms: 12_000,
        output_by_port: {
          result: {
            versions: [
              {
                retrieval_mode: "graph_hybrid",
                citations: [{ chunk_id: "c1", label: "Policy" }],
                retrieved_chunks: [{ chunk_id: "c1", source_key: "policy.md", score: 0.8, content: "policy" }],
                graph_context: {
                  matched_entities: ["A", "B", "C", "D"],
                  expanded_entities: ["E"],
                  age_graph_name: "graph",
                  age_seed_strategy: "text_only",
                  fallback_reason: "AGE unavailable",
                  age_fallback_reason: "connection refused",
                  fallback_retrieval_mode: "dense",
                  strict_age_retrieval: true,
                },
              },
            ],
          },
        },
        tool_calls: [],
      },
    ],
  },
} as never;

describe("TraceReplayGraph rendered states", () => {
  it("renders path diagnostics, recovery metadata, and verification action", async () => {
    const user = userEvent.setup();
    const onSelectNodeId = vi.fn();
    const onCreateVerification = vi.fn();

    render(
      <TraceReplayGraph
        manifest={manifest}
        run={run}
        selectedNodeId="tool"
        onSelectNodeId={onSelectNodeId}
        onCreateVerification={onCreateVerification}
        events={[
          {
            event_id: 1,
            workflow_run_id: "run-trace",
            project_id: null,
            sequence: 1,
            event_type: "workflow.run.queued",
            node_id: null,
            payload: null,
            created_at: "2026-01-01T00:00:00Z",
          },
          {
            event_id: 2,
            workflow_run_id: "run-trace",
            project_id: null,
            sequence: 2,
            event_type: "workflow.run.step",
            node_id: "tool",
            payload: { step: { node_id: "tool" } },
            created_at: "2026-01-01T00:00:02Z",
          },
          {
            event_id: 3,
            workflow_run_id: "run-trace",
            project_id: null,
            sequence: 3,
            event_type: "workflow.run.failed",
            node_id: "query",
            payload: null,
            created_at: "2026-01-01T00:00:03Z",
          },
        ] as never}
        checkpoints={[
          {
            checkpoint_id: "checkpoint-1",
            workflow_run_id: "run-trace",
            node_id: "query",
            sequence: 3,
            state_blob: { kind: "human_approval", wait_until: "2026-01-01" },
          },
        ] as never}
      />,
    );

    expect(screen.getByTestId("trace-replay")).toBeInTheDocument();
    expect(screen.getByText("Trace replay")).toBeInTheDocument();
    expect(screen.getByText("prompt version: v5")).toBeInTheDocument();
    expect(screen.getByText("error: query failed")).toBeInTheDocument();
    expect(screen.getByTestId("trace-create-verification")).toBeInTheDocument();
    expect(screen.getByText("AGE graph")).toBeInTheDocument();
    expect(screen.getByText(/Fallback/)).toBeInTheDocument();
    expect(screen.getByText(/Matched A, B, C/)).toBeInTheDocument();

    await user.click(screen.getByTestId("trace-create-verification"));
    await user.click(screen.getByTestId("trace-path-step-3"));
    expect(onCreateVerification).toHaveBeenCalledWith(run);
    expect(onSelectNodeId).toHaveBeenCalledWith("query");
  });

  it("renders the no-path fallback when a run has no recorded steps", () => {
    render(
      <TraceReplayGraph
        manifest={manifest}
        run={{ ...run, summary: null } as never}
      />,
    );

    expect(screen.getByText("No recorded path.")).toBeInTheDocument();
  });
});
