import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ManifestNode, WorkflowManifest } from "@/api/workflowTypes";
import { NodeDetailPanel } from "@/components/workflows/NodeDetailPanel";

function manifestWith(node: ManifestNode, overrides: Partial<WorkflowManifest> = {}): WorkflowManifest {
  return {
    schema_version: 1,
    workflow_id: "wf-matrix",
    name: "Matrix workflow",
    nodes: { [node.id]: node },
    edges: [],
    ...overrides,
  };
}

function renderNode(node: ManifestNode, overrides: Partial<WorkflowManifest> = {}) {
  return render(
    <NodeDetailPanel
      manifest={manifestWith(node, overrides)}
      nodeId={node.id}
      onClose={vi.fn()}
    />,
  );
}

describe("NodeDetailPanel optional and default state matrix", () => {
  it("renders default and explicit values for file, trigger, wait, bucket, and loop nodes", () => {
    const cases: ManifestNode[] = [
      { id: "manual", type: "start", trigger: { mode: "manual" } },
      {
        id: "cron",
        type: "start",
        trigger: { mode: "cron", cron: "", timezone: "", alias: "", enabled: false },
      },
      { id: "file", type: "file_input", path: 12 as unknown as string },
      { id: "folder", type: "folder_input", recursive: false },
      { id: "in", type: "input_bucket", bucket: "", prefix: "", recursive: false },
      { id: "out", type: "output_bucket", bucket: "", prefix: "", overwrite: false },
      { id: "folder-out", type: "output_folder", path: "", overwrite: false },
      { id: "until", type: "wait_until", wait_until: 1 as unknown as string },
      { id: "event", type: "wait_for_event" },
      { id: "event-zero", type: "wait_for_event", timeout_seconds: 0 },
      { id: "each", type: "for_each", target_node_id: "", item_input_port: "", max_items: 0 },
      { id: "loop", type: "loop", target_node_id: "", max_iterations: 0, stop_condition: "" },
      { id: "join", type: "join" },
      { id: "boundary", type: "error_boundary", target_node_id: "", compensate_with: "", fallback_text: 12 as unknown as string },
      { id: "sub", type: "subworkflow", timeout_seconds: 0 },
      { id: "mcp", type: "mcp_resource", server_id: "", tool_name: "", timeout_seconds: 0 },
    ];

    const { rerender } = renderNode(cases[0]);
    for (const node of cases.slice(1)) {
      rerender(
        <NodeDetailPanel
          manifest={manifestWith(node)}
          nodeId={node.id}
          onClose={vi.fn()}
        />,
      );
      expect(screen.getByTestId("node-detail-panel")).toBeInTheDocument();
    }

    rerender(
      <NodeDetailPanel
        manifest={manifestWith(cases[8])}
        nodeId={cases[8].id}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText("No timeout")).toBeInTheDocument();
  });

  it("renders knowledge, template, code, guardrail, router, approval, and note defaults", () => {
    const nodes: ManifestNode[] = [
      {
        id: "knowledge",
        type: "knowledge_query",
        version_ids: [],
        retrieval_modes: [],
        graph_overrides: {},
      },
      { id: "build", type: "knowledge_build", wait_for_completion: false, activate_when_complete: false },
      { id: "template", type: "template", template: "   " },
      { id: "python", type: "python_code", code: "   " },
      { id: "guard", type: "guardrail", on_failure: "block_retry", checks: [] },
      { id: "router", type: "router", branches: [{ to: "missing" }] },
      { id: "approval", type: "human_approval", required_role: "  ", approval_count: Number.NaN, timeout_behavior: undefined },
      { id: "note", type: "note", text: "" },
    ];

    const { rerender } = renderNode(nodes[0]);
    for (const node of nodes.slice(1)) {
      rerender(
        <NodeDetailPanel
          manifest={manifestWith(node)}
          nodeId={node.id}
          onClose={vi.fn()}
        />,
      );
      expect(screen.getByTestId("node-detail-panel")).toBeInTheDocument();
    }

    expect(screen.queryByText("Note")).not.toBeInTheDocument();
  });

  it("renders empty and populated tool bindings, agent sections, ports, policies, issues, and raw values", () => {
    const agent: ManifestNode = {
      id: "agent",
      type: "agent",
      instructions: { type: "inline", text: "" },
      tools: [],
      handoffs: [{ target: "next", description: "", condition: "", input_filter: "" }],
      execution_policy: { idempotent: false },
      inputs: { input: { type: "string" } },
      outputs: { output: { type: "structured" } },
      custom_object: { enabled: true },
      custom_scalar: "value",
    };
    const tool: ManifestNode = { id: "tool", type: "tool", tool_name: "mcp-search" };
    const richTool: ManifestNode = { id: "tool", type: "tool", tool_name: "mcp-search" };
    const manifest = manifestWith(richTool, {
      nodes: {
        agent,
        tool: richTool,
        source: { id: "source", type: "note", text: "source" },
        target: { id: "target", type: "note", text: "target" },
      },
      edges: [
        { id: "in-one", from: "missing-source", to: "agent", map: { input: "input" } },
        { id: "in-many", from: "source", to: "agent", map: { a: "a", b: "b" } },
        { id: "out-one", from: "agent", to: "missing-target", map: {} },
        { id: "out-many", from: "agent", to: "target", map: { output: "output", extra: "extra" } },
      ],
      tools: {
        "mcp-search": {
          type: "mcp_tool",
          server_id: "server-1",
          tool_name: "remote-search",
          tool_schema_version: "v1",
        },
      },
      validation_report: {
        valid: false,
        errors: [{ code: "bad", path: "nodes.agent", message: "Bad node", severity: "error" }],
        warnings: [],
      },
    });

    const { rerender } = render(
      <NodeDetailPanel
        manifest={manifest}
        nodeId="agent"
        validationReport={manifest.validation_report as never}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText("Additional properties")).toBeInTheDocument();
    expect(screen.getByText("Connections")).toBeInTheDocument();
    expect(screen.getByText("Runtime default")).toBeInTheDocument();
    expect(screen.getByText("No")).toBeInTheDocument();

    rerender(
      <NodeDetailPanel manifest={manifest} nodeId="tool" onClose={vi.fn()} />,
    );
    expect(screen.getByText("mcp_tool")).toBeInTheDocument();
    expect(screen.getByText("remote-search")).toBeInTheDocument();

    rerender(
      <NodeDetailPanel
        manifest={manifestWith(tool, { tools: { "mcp-search": null as never } })}
        nodeId="tool"
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByText("registered_function")).toBeInTheDocument();
  });
});
