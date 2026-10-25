import { describe, expect, it, vi } from "vitest";

import { WorkflowRunDebugger } from "@/components/workflows/WorkflowRunDebugger";
import { render, screen, userEvent } from "@/test/utils";

const run = {
  workflow_run_id: "run-rich",
  workflow_id: "workflow-1",
  project_id: null,
  tenant_id: null,
  workflow_version_id: "version-1",
  deployment_alias: "manual",
  mlflow_run_id: null,
  trace_id: null,
  session_id: null,
  status: "waiting_approval",
  source: "editor",
  priority: 0,
  queued_at: "2026-01-01T00:00:00Z",
  started_at: "2026-01-01T00:00:01Z",
  completed_at: null,
  current_node_id: "query",
  summary: {
    resume_checkpoint_id: "checkpoint-1",
    steps: [
      {
        node_id: "agent",
        node_type: "agent",
        status: "ok",
        output: "answer",
        duration_ms: 10,
        tokens: 100,
        prompt_tokens: 80,
        completion_tokens: 20,
        cached_prompt_tokens: 5,
        cost_usd: 0.12,
        model: "gpt-test",
        prompt_version: "v3",
        input_by_port: { input: "question" },
        output_by_port: { output: "answer" },
        tool_calls: [],
      },
      {
        node_id: "tool",
        node_type: "tool",
        status: "ok",
        output: "lookup result",
        output_by_port: {
          metadata: {
            tool_name: "lookup_customer",
            registry_ref: "registry:customer",
            binding_type: "registry",
            requires_approval: true,
            side_effect_level: "read",
            module_path: "crm.customer",
            callable_name: "lookup",
            server_id: "crm",
            remote_tool_name: "lookup_customer",
            arguments: { customer_id: "C-1" },
          },
          text: "customer found",
        },
        tool_calls: [{ tool: "lookup_customer", arguments: { customer_id: "C-1" } }],
      },
      {
        node_id: "loop",
        node_type: "for_each",
        status: "ok",
        output_by_port: {
          metadata: {
            count: 2,
            failed: 1,
            target_node_id: "send",
            target_node_type: "tool",
            artifacts: { "item-1": { key: "a" } },
          },
          results: [
            { item: "first", status: "ok", output: "done", tool_calls: [], artifacts: [] },
            { item: { label: "second" }, error: "failed", artifacts: [{}] },
          ],
        },
        tool_calls: [],
      },
      {
        node_id: "join",
        node_type: "join",
        status: "ok",
        output_by_port: { merged: { left: 1, right: 2 }, output: { text: "merged" } },
        tool_calls: [],
      },
      {
        node_id: "guard",
        node_type: "error_boundary",
        status: "error",
        output_by_port: {
          error: {
            message: "child failed",
            target_node_id: "risky",
            target_node_type: "tool",
            compensation_node_id: "cleanup",
            compensation_node_type: "tool",
            compensation_outputs: { text: "cleaned" },
            artifacts: { log: { key: "run.log" } },
          },
        },
        tool_calls: [],
      },
      {
        node_id: "child",
        node_type: "subworkflow",
        status: "ok",
        output_by_port: {
          result: {
            status: "completed",
            workflow_id: "child-workflow",
            alias: "prod",
            workflow_version_id: "child-version",
            workflow_version_number: 4,
            tokens: 7,
            steps: ["child-start", "child-end"],
            output: "child output",
          },
        },
        tool_calls: [],
      },
      {
        node_id: "build",
        node_type: "knowledge_build",
        status: "ok",
        output_by_port: {
          status: "completed",
          knowledge_base: { knowledge_base_id: "KB-1", active_version_id: "KBV-1" },
          version: {
            knowledge_base_version_id: "KBV-1",
            version_number: 2,
            chunking_strategy: "recursive",
            embedding_model: "embed-test",
            graph_config: {
              output_target: "age",
              extractor_backend: "llm",
              default_retrieval_mode: "age_graph",
              retrieval_strength: "strong",
            },
            summary: { age_sync_status: "synced" },
          },
          run: { knowledge_base_run_id: "KBR-1", status: "completed" },
          result: {
            await_completion: { requested: true, status: "completed", timeout_seconds: 30 },
            activation: { requested: true, status: "activated", active_version_id: "KBV-1" },
          },
        },
        tool_calls: [],
      },
      {
        node_id: "query",
        node_type: "knowledge_query",
        status: "ok",
        output_by_port: {
          result: {
            versions: [
              {
                retrieval_mode: "age_graph",
                citations: [{ chunk_id: "chunk-1", label: "Policy" }],
                retrieved_chunks: [
                  {
                    chunk_id: "chunk-1",
                    source_name: "policy.md",
                    source_key: "policy.md",
                    score: 0.9,
                    content: "Refund policy",
                    matched_entity_labels: ["Refund"],
                  },
                ],
                graph_context: {
                  matched_entities: ["Refund"],
                  expanded_entities: ["Order"],
                  age_graph_name: "caliber_graph",
                  age_status: "ready",
                  age_seed_strategy: "entity_then_text",
                  age_matched_chunk_count: 1,
                  age_traversal_hops: 2,
                  age_candidate_pool_size: 8,
                  age_dense_rerank_weight: 0.25,
                  retrieval_strength: "strong",
                  minimum_relationship_weight: 0.4,
                  fallback_reason: "none",
                  strict_age_retrieval: true,
                  query_override_active: true,
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

const manifest = { nodes: {}, edges: [] } as never;

describe("WorkflowRunDebugger rendered states", () => {
  it("renders the empty evidence state for a run without recorded steps", () => {
    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={{ ...run, status: "completed", summary: null } as never}
        events={[]}
        checkpoints={[]}
      />,
    );

    expect(screen.getByTestId("workflow-run-debugger-empty")).toHaveTextContent(
      "No recorded step details",
    );
  });

  it("renders telemetry, orchestration, knowledge diagnostics, snapshots, and selection", async () => {
    const user = userEvent.setup();
    const onSelectNodeId = vi.fn();

    render(
      <WorkflowRunDebugger
        manifest={manifest}
        run={run}
        onSelectNodeId={onSelectNodeId}
        checkpoints={[
          {
            checkpoint_id: "checkpoint-1",
            workflow_run_id: "run-rich",
            node_id: "query",
            sequence: 8,
            state_blob: { kind: "human_approval", wait_until: "2026-01-01T01:00:00Z" },
          },
        ] as never}
        events={[
          {
            event_id: 1,
            workflow_run_id: "run-rich",
            project_id: null,
            sequence: 1,
            event_type: "workflow.run.started",
            node_id: null,
            payload: null,
            created_at: "2026-01-01T00:00:01Z",
          },
          {
            event_id: 2,
            workflow_run_id: "run-rich",
            project_id: null,
            sequence: 8,
            event_type: "workflow.run.waiting_approval",
            node_id: "query",
            payload: null,
            created_at: "2026-01-01T00:00:08Z",
          },
        ] as never}
      />,
    );

    expect(screen.getByTestId("workflow-run-debugger")).toBeInTheDocument();
    expect(screen.getByText("8 recorded steps")).toBeInTheDocument();
    expect(screen.getByText("gpt-test")).toBeInTheDocument();
    expect(screen.getByText("lookup_customer")).toBeInTheDocument();
    expect(screen.getByText(/2 items/)).toBeInTheDocument();
    expect(screen.getByText("child-workflow")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-step-knowledge-build-6")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-step-knowledge-7")).toBeInTheDocument();

    await user.click(screen.getByTestId("workflow-run-step-button-1"));
    expect(onSelectNodeId).toHaveBeenCalledWith("tool");
    expect(screen.getByTestId("workflow-run-step-detail")).toHaveTextContent("tool");
  });
});
