import { describe, expect, it } from "vitest";

import type {
  ManifestNode,
  ToolDefinition,
  WorkflowComponent,
  WorkflowManifest,
} from "@/api/workflowTypes";
import {
  autoMapCompatiblePorts,
  buildNodeExecutionBadgeMap,
  autoMapPorts,
  canConnectNodes,
  deriveEdgeMap,
  ensureAgentToolBindings,
  makeEdgeId,
  manifestToFlow,
  nodeColor,
  nodeFieldSetupChecks,
  nodeFieldValidationIssues,
  nodeGuide,
  nodeInputs,
  nodeLabel,
  nodeOutputs,
  nodeSubtitle,
  nodeValidationSummary,
  portSpecAssignable,
  portColor,
  registryRefForTool,
  templateManifest,
  toolBindingForDefinition,
  versionConstraintForTool,
} from "@/lib/workflowGraph";

function supportManifest(): WorkflowManifest {
  return {
    schema_version: 1,
    workflow_id: "wf",
    name: "WF",
    nodes: {
      start: {
        id: "start",
        type: "start",
        outputs: { user_message: { type: "string" } },
      },
      agent: {
        id: "agent",
        type: "agent",
        name: "support-agent",
        model: "inherit",
        instructions: { type: "inline", text: "hi" },
        tools: ["lookup_policy"],
        inputs: { input: { type: "string" } },
        outputs: { final_output: { type: "string" } },
        handoffs: [{ target: "billing" }],
      },
      billing: {
        id: "billing",
        type: "agent",
        name: "billing-agent",
        model: "inherit",
        instructions: { type: "inline", text: "b" },
        inputs: { input: { type: "string" } },
        outputs: { final_output: { type: "string" } },
      },
      final: {
        id: "final",
        type: "output",
        inputs: { response: { type: "string" } },
      },
    },
    edges: [
      { id: "e1", from: "start", to: "agent", map: { user_message: "input" } },
      {
        id: "e2",
        from: "agent",
        to: "final",
        map: { final_output: "response" },
      },
    ],
  };
}

function tool(name: string, version = "1.2"): ToolDefinition {
  return {
    tool_id: `TL-${name}`,
    name,
    version,
    description: "",
    module_path: "caliber.workflows.demo_tools",
    callable_name: name,
    input_schema: null,
    output_schema: null,
    side_effect_level: "read",
    requires_approval: false,
    allow_in_preview: true,
    secret_refs: [],
    test_cases: [],
    last_calibration: null,
    owner: "",
    status: "active",
    deprecated_at: null,
    successor_tool_id: null,
    created_at: "x",
    updated_at: "x",
  };
}

function workflowAgent(
  id: string,
  overrides: Partial<ManifestNode> = {},
): ManifestNode {
  return {
    id,
    type: "agent",
    name: id,
    model: "inherit",
    instructions: { type: "inline", text: `${id} instructions` },
    inputs: { input: { type: "string" } },
    outputs: { final_output: { type: "string" } },
    ...overrides,
  };
}

describe("manifestToFlow", () => {
  it("produces one flow node per manifest node", () => {
    const { nodes } = manifestToFlow(supportManifest());
    expect(nodes.map((n) => n.id).sort()).toEqual([
      "agent",
      "billing",
      "final",
      "start",
    ]);
    expect(nodes.every((n) => n.type === "caliber")).toBe(true);
  });

  it("lays out by BFS depth (start left of agent left of final)", () => {
    const { nodes } = manifestToFlow(supportManifest());
    const xOf = (id: string): number =>
      nodes.find((n) => n.id === id)!.position.x;
    expect(xOf("start")).toBeLessThan(xOf("agent"));
    expect(xOf("agent")).toBeLessThan(xOf("final"));
  });

  it("renders data edges and dashed handoff edges", () => {
    const { edges } = manifestToFlow(supportManifest());
    const ids = edges.map((e) => e.id);
    expect(ids).toContain("e1");
    expect(ids).toContain("handoff_agent_0_billing");
    const handoff = edges.find((e) => e.id === "handoff_agent_0_billing");
    expect(handoff?.animated).toBe(true);
  });

  it("assigns unique ids when multiple handoffs target the same agent", () => {
    const duplicated = supportManifest();
    duplicated.nodes.agent = {
      ...duplicated.nodes.agent,
      handoffs: [{ target: "billing" }, { target: "billing" }],
    };

    const { edges } = manifestToFlow(duplicated);
    const handoffIds = edges
      .filter(
        (edge) =>
          edge.source === "agent" &&
          edge.target === "billing" &&
          edge.label === "handoff",
      )
      .map((edge) => edge.id);

    expect(handoffIds).toEqual([
      "handoff_agent_0_billing",
      "handoff_agent_1_billing",
    ]);
  });

  it("renders loop-target reference edges and uses them for layout depth", () => {
    const manifest: WorkflowManifest = {
      schema_version: 1,
      workflow_id: "wf-loop",
      name: "Loop Workflow",
      nodes: {
        start: {
          id: "start",
          type: "start",
          outputs: { user_message: { type: "string" } },
        },
        for_each: {
          id: "for_each",
          type: "for_each",
          target_node_id: "worker",
          inputs: { items: { type: "structured" } },
          outputs: {
            text: { type: "string" },
            results: { type: "structured" },
            metadata: { type: "structured" },
          },
        },
        worker: {
          id: "worker",
          type: "agent",
          name: "item-worker",
          model: "inherit",
          instructions: { type: "inline", text: "Process one item." },
          tools: [],
          inputs: { input: { type: "string" } },
          outputs: { final_output: { type: "string" } },
        },
        final: {
          id: "final",
          type: "output",
          inputs: { response: { type: "string" } },
        },
      },
      edges: [
        {
          id: "e_start_loop",
          from: "start",
          to: "for_each",
          map: { user_message: "items" },
        },
        {
          id: "e_loop_final",
          from: "for_each",
          to: "final",
          map: { text: "response" },
        },
      ],
    };

    const { edges, nodes } = manifestToFlow(manifest);
    expect(
      edges.find((edge) => edge.id === "for_each_target_for_each_worker"),
    ).toMatchObject({
      source: "for_each",
      target: "worker",
      label: "loop target",
    });
    const xOf = (id: string): number =>
      nodes.find((node) => node.id === id)!.position.x;
    expect(xOf("for_each")).toBeLessThan(xOf("worker"));
  });

  it("renders bounded loop target reference edges and uses them for layout depth", () => {
    const manifest: WorkflowManifest = {
      schema_version: 1,
      workflow_id: "wf-repeat",
      name: "Repeat Workflow",
      nodes: {
        start: {
          id: "start",
          type: "start",
          outputs: { user_message: { type: "string" } },
        },
        loop: {
          id: "loop",
          type: "loop",
          target_node_id: "worker",
          max_iterations: 4,
          stop_condition: "iteration >= 2",
          inputs: {
            input: { type: "string" },
            state: { type: "structured" },
          },
          outputs: {
            output: { type: "string" },
            result: { type: "structured" },
            iterations: { type: "structured" },
            metadata: { type: "structured" },
          },
        },
        worker: {
          id: "worker",
          type: "python_code",
          code: 'return {"text": input or run_input}',
          inputs: { input: { type: "string" } },
          outputs: { text: { type: "string" } },
        },
        final: {
          id: "final",
          type: "output",
          inputs: { response: { type: "string" } },
        },
      },
      edges: [
        {
          id: "e_start_loop",
          from: "start",
          to: "loop",
          map: { user_message: "input" },
        },
        {
          id: "e_loop_final",
          from: "loop",
          to: "final",
          map: { output: "response" },
        },
      ],
    };

    const { edges, nodes } = manifestToFlow(manifest);
    expect(edges.find((edge) => edge.id === "loop_target_loop_worker")).toMatchObject({
      source: "loop",
      target: "worker",
      label: "loop target",
    });
    const xOf = (id: string): number =>
      nodes.find((node) => node.id === id)!.position.x;
    expect(xOf("loop")).toBeLessThan(xOf("worker"));
  });

  it("renders error-boundary protected and compensation references", () => {
    const manifest: WorkflowManifest = {
      schema_version: 1,
      workflow_id: "wf-boundary",
      name: "Boundary Workflow",
      nodes: {
        start: {
          id: "start",
          type: "start",
          outputs: { user_message: { type: "string" } },
        },
        boundary: {
          id: "boundary",
          type: "error_boundary",
          target_node_id: "worker",
          compensate_with: "recover",
          fallback_text: "",
          inputs: { input: { type: "string" } },
          outputs: {
            output: { type: "string" },
            error: { type: "structured" },
          },
        },
        worker: {
          id: "worker",
          type: "agent",
          name: "fragile-worker",
          model: "inherit",
          instructions: { type: "inline", text: "Do the main work." },
          tools: [],
          inputs: { input: { type: "string" } },
          outputs: { final_output: { type: "string" } },
        },
        recover: {
          id: "recover",
          type: "python_code",
          code: 'return {"text": input or run_input, "result": {"ok": True}}',
          inputs: {
            input: { type: "string" },
            context: { type: "structured" },
          },
          outputs: {
            text: { type: "string" },
            result: { type: "structured" },
            metadata: { type: "structured" },
          },
        },
        final: {
          id: "final",
          type: "output",
          inputs: { response: { type: "string" } },
        },
      },
      edges: [
        {
          id: "e_start_boundary",
          from: "start",
          to: "boundary",
          map: { user_message: "input" },
        },
        {
          id: "e_boundary_final",
          from: "boundary",
          to: "final",
          map: { output: "response" },
        },
      ],
    };

    const { edges, nodes } = manifestToFlow(manifest);
    expect(
      edges.find((edge) => edge.id === "error_boundary_target_boundary_worker"),
    ).toMatchObject({
      source: "boundary",
      target: "worker",
      label: "protected",
    });
    expect(
      edges.find(
        (edge) => edge.id === "error_boundary_compensate_boundary_recover",
      ),
    ).toMatchObject({
      source: "boundary",
      target: "recover",
      label: "compensates",
    });
    const xOf = (id: string): number =>
      nodes.find((node) => node.id === id)!.position.x;
    expect(xOf("boundary")).toBeLessThan(xOf("worker"));
    expect(xOf("boundary")).toBeLessThan(xOf("recover"));
  });

  it("labels edges with the data map", () => {
    const { edges } = manifestToFlow(supportManifest());
    const e1 = edges.find((e) => e.id === "e1");
    expect(e1?.label).toBe("user_message→input");
  });

  it("attaches backend component metadata when provided", () => {
    const component = {
      type: "agent",
      label: "Agent Runtime",
      category: "Server Components",
      description: "Backend agent description.",
      docs: ["Server-side tip."],
      default_inputs: {},
      default_outputs: {},
      fields: [],
      setup_checks: [],
    } satisfies WorkflowComponent;

    const { nodes } = manifestToFlow(
      supportManifest(),
      {},
      new Map<WorkflowComponent["type"], WorkflowComponent>([
        ["agent", component],
      ]),
    );

    expect(
      nodes.find((node) => node.id === "agent")?.data.componentSpec,
    ).toEqual(component);
    expect(
      nodes.find((node) => node.id === "start")?.data.componentSpec,
    ).toBeNull();
  });
});

describe("node helpers", () => {
  it("nodeLabel prefers agent name", () => {
    const nodes = supportManifest().nodes;
    expect(nodeLabel(nodes.agent!)).toBe("support-agent");
    expect(nodeLabel(nodes.final!)).toBe("final");
  });

  it("nodeLabel prefers a custom display label over name/id", () => {
    // a custom label wins even over an agent's name
    expect(
      nodeLabel({ id: "agent", type: "agent", name: "support-agent", label: "Triage" }),
    ).toBe("Triage");
    // blank label falls back to id
    expect(nodeLabel({ id: "tool_1", type: "tool", label: "  " })).toBe("tool_1");
  });

  it("nodeSubtitle summarizes agent tools", () => {
    expect(nodeSubtitle(supportManifest().nodes.agent!)).toContain("1 tool");
    expect(
      nodeSubtitle({
        id: "folder_input",
        type: "folder_input",
        pattern: "*.md",
        max_files: 3,
      }),
    ).toContain("*.md");
    expect(
      nodeSubtitle({
        id: "python",
        type: "python_code",
        timeout_seconds: 9,
      }),
    ).toContain("9s timeout");
  });

  it("nodeSubtitle covers storage, orchestration, routing, and fallback nodes", () => {
    expect(
      nodeSubtitle({
        id: "bucket_in",
        type: "input_bucket",
        bucket: "logs",
        prefix: "service/",
        max_files: 7,
      }),
    ).toBe("logs/service/ · 7 objects");
    expect(nodeSubtitle({ id: "bucket_out", type: "output_bucket" })).toBe(
      "(no bucket) · artifacts",
    );
    expect(
      nodeSubtitle({
        id: "folder_out",
        type: "output_folder",
        path: "/tmp/out",
      }),
    ).toBe("/tmp/out · artifacts");
    expect(
      nodeSubtitle({
        id: "wait",
        type: "wait_until",
        wait_until: "2026-01-01T00:00:00Z",
      }),
    ).toContain("wait");
    expect(
      nodeSubtitle({
        id: "event",
        type: "wait_for_event",
        event_name: "object.created",
      }),
    ).toBe("object.created · wait");
    expect(nodeSubtitle({ id: "parallel", type: "parallel" })).toBe("fan-out");
    expect(nodeSubtitle({ id: "join", type: "join" })).toBe("fan-in barrier");
    expect(nodeSubtitle({ id: "loop", type: "for_each", max_items: 3 })).toBe(
      "3 max items",
    );
    expect(
      nodeSubtitle({ id: "repeat", type: "loop", max_iterations: 4 }),
    ).toBe("4 max iterations");
    expect(
      nodeSubtitle({
        id: "boundary",
        type: "error_boundary",
        target_node_id: "agent",
      }),
    ).toBe("agent · guarded");
    expect(
      nodeSubtitle({
        id: "sub",
        type: "subworkflow",
        workflow_id: "WF-1",
        alias: "staging",
      }),
    ).toBe("WF-1@staging");
    expect(
      nodeSubtitle({ id: "tool", type: "tool", tool_name: "lookup_policy" }),
    ).toBe("lookup_policy · direct");
    expect(nodeSubtitle({ id: "mcp", type: "mcp_resource" })).toBe(
      "server · tool",
    );
    expect(
      nodeSubtitle({
        id: "hook",
        type: "webhook",
        method: "PUT",
        url: "https://api.test/x",
      }),
    ).toBe("PUT · https://api.test/x");
    expect(nodeSubtitle({ id: "hook2", type: "webhook" })).toBe("POST · (no url)");
    expect(
      nodeSubtitle({ id: "api", type: "api_request", method: "PUT", url: "https://a.test" }),
    ).toBe("PUT · https://a.test");
    expect(
      nodeSubtitle({ id: "api2", type: "api_request", mode: "curl", curl: "curl x" }),
    ).toBe("cURL request");
    expect(
      nodeSubtitle({
        id: "kb",
        type: "knowledge_query",
        knowledge_base_id: "KB-1",
        retrieval_modes: ["dense", "age_graph"],
      }),
    ).toBe("KB-1 · dense + age_graph");
    expect(
      nodeSubtitle({
        id: "template",
        type: "template",
        output_format: "json",
        missing_variable_mode: "error",
      }),
    ).toBe("json template · missing error");
    expect(
      nodeSubtitle({
        id: "legacy",
        type: "external_app",
        entrypoint: "support.ticketing:handle_request",
      }),
    ).toBe("support.ticketing:handle_request · bridge");
    expect(
      nodeSubtitle({
        id: "router",
        type: "router",
        branches: [{ condition: "x", to: "agent" }],
      }),
    ).toBe("1 branch(es)");
    expect(nodeSubtitle({ id: "review", type: "human_approval" })).toBe(
      "1 approval · caliber.approver",
    );
    expect(nodeSubtitle({ id: "custom", type: "custom_node" })).toBe(
      "custom_node",
    );
  });

  it("nodeColor is type-coded", () => {
    expect(nodeColor("agent")).not.toEqual(nodeColor("guardrail"));
    expect(nodeColor("start")).toEqual(nodeColor("output"));
    expect(nodeColor("file_input")).not.toEqual(nodeColor("folder_input"));
    expect(nodeColor("webhook")).not.toEqual(nodeColor("mcp_resource"));
  });

  it("nodeGuide flags a webhook missing its URL", () => {
    const missing = nodeGuide({ id: "hook", type: "webhook" });
    expect(missing.summary).toContain("HTTP");
    expect(missing.missingLabels).toContain("Provide a request URL");
    const ready = nodeGuide({
      id: "hook",
      type: "webhook",
      url: "https://api.test/x",
    });
    expect(ready.missingLabels).not.toContain("Provide a request URL");
  });

  it("nodeGuide checks the right field per API Request mode", () => {
    const label = "Provide a URL or cURL command";
    expect(nodeGuide({ id: "a", type: "api_request" }).missingLabels).toContain(label);
    // URL mode satisfied by url; cURL mode satisfied by curl
    expect(
      nodeGuide({ id: "a", type: "api_request", url: "https://x.test" }).missingLabels,
    ).not.toContain(label);
    expect(
      nodeGuide({ id: "a", type: "api_request", mode: "curl", curl: "curl x" }).missingLabels,
    ).not.toContain(label);
    // URL alone does NOT satisfy cURL mode
    expect(
      nodeGuide({ id: "a", type: "api_request", mode: "curl", url: "https://x.test" })
        .missingLabels,
    ).toContain(label);
  });

  it("portColor maps known data types and falls back for unknown values", () => {
    expect(portColor("string")).toBe("#4F46E5");
    expect(portColor("structured")).toBe("#DC2626");
    expect(portColor("messages")).toBe("#C026D3");
    expect(portColor("boolean")).toBe("#CA8A04");
    expect(portColor("void")).toBe("#6B7280");
    expect(portColor("mystery")).toBe("#6B7280");
  });

  it("nodeGuide and nodeValidationSummary expose readiness guidance", () => {
    const guide = nodeGuide({
      id: "knowledge",
      type: "knowledge_query",
      knowledge_base_id: "",
      version_ids: [],
      retrieval_modes: ["dense"],
    });
    expect(guide.summary).toContain("knowledge base");
    expect(guide.missingLabels).toContain(
      "Select a knowledge base or pinned versions",
    );

    const summary = nodeValidationSummary(
      {
        id: "knowledge",
        type: "knowledge_query",
        knowledge_base_id: "",
        version_ids: [],
        retrieval_modes: ["dense"],
      },
      {
        valid: false,
        errors: [
          {
            code: "missing_knowledge_target",
            path: "nodes.knowledge.knowledge_base_id",
            message: "Select a knowledge base or pinned version.",
            severity: "error",
          },
        ],
        warnings: [],
      },
    );
    expect(summary.severity).toBe("error");
    expect(summary.errors).toBe(1);
    expect(summary.title).toContain("Select a knowledge base");

    const externalGuide = nodeGuide({
      id: "legacy",
      type: "external_app",
      entrypoint: "",
    });
    expect(externalGuide.summary).toContain("external app");
    expect(externalGuide.missingLabels).toContain(
      "Set the external app entrypoint",
    );

    const templateGuide = nodeGuide({
      id: "template",
      type: "template",
      template: "",
    });
    expect(templateGuide.summary).toContain(
      "no-code text prompt or JSON payload",
    );
    expect(templateGuide.missingLabels).toContain("Provide a template");

    const toolGuide = nodeGuide({
      id: "tool",
      type: "tool",
      tool_name: "",
    });
    expect(toolGuide.summary).toContain("registered tool binding");
    expect(toolGuide.missingLabels).toContain("Select a tool binding");

    const loopGuide = nodeGuide({
      id: "loop",
      type: "for_each",
    });
    expect(loopGuide.summary).toContain("optionally invoke");
    expect(loopGuide.missingLabels).toEqual([]);

    const boundedLoopGuide = nodeGuide({
      id: "repeat",
      type: "loop",
    });
    expect(boundedLoopGuide.summary).toContain("stop condition");
    expect(boundedLoopGuide.missingLabels).toEqual(["Select a loop target"]);

    const boundaryGuide = nodeGuide({
      id: "boundary",
      type: "error_boundary",
    });
    expect(boundaryGuide.summary).toContain("fallback handling");
    expect(boundaryGuide.missingLabels).toEqual([]);
  });

  it("nodeGuide merges backend component metadata with graph-aware checks", () => {
    const manifest: WorkflowManifest = {
      schema_version: 1,
      workflow_id: "wf-router",
      name: "Router Workflow",
      nodes: {
        router: {
          id: "router",
          type: "router",
          branches: [
            { condition: { intent: "sales" }, to: "sales" },
            { condition: { intent: "support" }, to: "support" },
          ],
        },
        sales: workflowAgent("sales"),
        support: workflowAgent("support"),
      },
      edges: [
        {
          id: "e_router_sales",
          from: "router",
          to: "sales",
          map: { route: "input" },
        },
      ],
    };
    const component = {
      type: "router",
      label: "Router",
      category: "Logic",
      description: "Backend router description.",
      docs: ["Backend tip."],
      default_inputs: {},
      default_outputs: {},
      fields: [],
      setup_checks: [
        {
          label: "Add at least one branch",
          help: "Define the branch destinations and routing conditions.",
          kind: "non_empty_list",
          field: "branches",
        },
        {
          label: "Connect every branch target with an outgoing edge",
          help: "Each configured branch should point to a real node and also have a matching outgoing edge from this router.",
          kind: "router_branch_edges_connected",
        },
      ],
    } satisfies WorkflowComponent;

    const guide = nodeGuide(
      manifest.nodes.router,
      component,
      manifest,
    );

    expect(guide.summary).toBe("Backend router description.");
    expect(guide.tips).toEqual(["Backend tip."]);
    expect(guide.missingLabels).toEqual([
      "Connect every branch target with an outgoing edge",
    ]);
    expect(
      guide.checks.filter(
        (check) =>
          check.label === "Connect every branch target with an outgoing edge",
      ),
    ).toHaveLength(1);
  });

  it("surfaces orchestration topology issues directly in node guidance", () => {
    const manifest: WorkflowManifest = {
      schema_version: 1,
      workflow_id: "wf-orchestration",
      name: "Orchestration Workflow",
      nodes: {
        parallel: { id: "parallel", type: "parallel" },
        branch: workflowAgent("branch"),
        join_sparse: { id: "join_sparse", type: "join" },
        join_duplicate: { id: "join_duplicate", type: "join" },
        left: workflowAgent("left"),
        right: workflowAgent("right"),
        note: { id: "note", type: "note", text: "not executable" },
        loop: { id: "loop", type: "loop", target_node_id: "note" },
        for_each: {
          id: "for_each",
          type: "for_each",
          target_node_id: "note",
        },
        boundary: {
          id: "boundary",
          type: "error_boundary",
          target_node_id: "note",
          compensate_with: "missing",
        },
        sub: {
          id: "sub",
          type: "subworkflow",
          workflow_id: "wf-orchestration",
        },
      },
      edges: [
        {
          id: "e_parallel_branch",
          from: "parallel",
          to: "branch",
          map: {},
        },
        {
          id: "e_left_join_sparse",
          from: "left",
          to: "join_sparse",
          map: { final_output: "left" },
        },
        {
          id: "e_left_join_duplicate",
          from: "left",
          to: "join_duplicate",
          map: { final_output: "shared" },
        },
        {
          id: "e_right_join_duplicate",
          from: "right",
          to: "join_duplicate",
          map: { final_output: "shared" },
        },
      ],
    };

    expect(
      nodeGuide(manifest.nodes.parallel, null, manifest).missingLabels,
    ).toEqual(["Add at least two downstream branches"]);
    expect(
      nodeGuide(manifest.nodes.join_sparse, null, manifest).missingLabels,
    ).toEqual(["Connect at least two upstream branches"]);
    expect(
      nodeGuide(manifest.nodes.join_duplicate, null, manifest).missingLabels,
    ).toEqual(["Use distinct join input ports per branch"]);
    expect(nodeGuide(manifest.nodes.loop, null, manifest).missingLabels).toEqual(
      ["Choose an executable loop target"],
    );
    expect(
      nodeGuide(manifest.nodes.for_each, null, manifest).missingLabels,
    ).toEqual(["Use an executable target when set"]);
    expect(
      nodeGuide(manifest.nodes.boundary, null, manifest).missingLabels,
    ).toEqual([
      "Protect an executable target when set",
      "Use an executable compensation node when set",
    ]);
    expect(nodeGuide(manifest.nodes.sub, null, manifest).missingLabels).toEqual(
      ["Avoid calling this workflow recursively"],
    );
  });

  it("evaluates server-backed graph-aware setup check kinds", () => {
    const manifest: WorkflowManifest = {
      schema_version: 1,
      workflow_id: "wf-server-checks",
      name: "Server Checks",
      nodes: {
        parallel: { id: "parallel", type: "parallel" },
        branch: workflowAgent("branch"),
        note: { id: "note", type: "note", text: "not executable" },
        loop: { id: "loop", type: "loop", target_node_id: "note" },
        sub: {
          id: "sub",
          type: "subworkflow",
          workflow_id: "wf-server-checks",
        },
      },
      edges: [
        {
          id: "e_parallel_branch",
          from: "parallel",
          to: "branch",
          map: {},
        },
      ],
    };

    const parallelGuide = nodeGuide(
      manifest.nodes.parallel,
      {
        type: "parallel",
        label: "Parallel",
        category: "Orchestration",
        description: "Parallel fan-out",
        docs: [],
        default_inputs: {},
        default_outputs: {},
        fields: [],
        setup_checks: [
          {
            label: "Add at least two downstream branches",
            help: "Connect this parallel node to at least two downstream branches before using it as a fan-out barrier.",
            kind: "minimum_outgoing_edges",
            minimum: 2,
          },
        ],
      },
      manifest,
    );
    expect(parallelGuide.missingLabels).toEqual([
      "Add at least two downstream branches",
    ]);

    const loopGuide = nodeGuide(
      manifest.nodes.loop,
      {
        type: "loop",
        label: "Loop",
        category: "Orchestration",
        description: "Repeat one step",
        docs: [],
        default_inputs: {},
        default_outputs: {},
        fields: [
          {
            key: "target_node_id",
            label: "Target node",
            type: "string",
            required: true,
            default: "",
            description: "Executable node repeated by this loop.",
            constraints: {},
            examples: [],
          },
        ],
        setup_checks: [
          {
            label: "Choose an executable loop target",
            help: "The selected loop target should point to an executable node in this workflow.",
            kind: "target_node_executable_if_set",
            field: "target_node_id",
          },
        ],
      },
      manifest,
    );
    expect(loopGuide.missingLabels).toEqual([
      "Choose an executable loop target",
    ]);

    const subworkflowGuide = nodeGuide(
      manifest.nodes.sub,
      {
        type: "subworkflow",
        label: "Subworkflow",
        category: "Orchestration",
        description: "Call a child workflow",
        docs: [],
        default_inputs: {},
        default_outputs: {},
        fields: [
          {
            key: "workflow_id",
            label: "Workflow ID",
            type: "string",
            required: true,
            default: "",
            description: "Workflow invoked by this node.",
            constraints: {},
            examples: [],
          },
        ],
        setup_checks: [
          {
            label: "Avoid calling this workflow recursively",
            help: "Choose a different published child workflow instead of pointing this node back at the current workflow.",
            kind: "not_current_workflow_id",
            field: "workflow_id",
          },
        ],
      },
      manifest,
    );
    expect(subworkflowGuide.missingLabels).toEqual([
      "Avoid calling this workflow recursively",
    ]);
  });

  it("matches field-scoped validation issues on exact and nested paths", () => {
    const issues = nodeFieldValidationIssues(
      {
        valid: false,
        errors: [
          {
            code: "missing_prompt_ref",
            path: "nodes.agent.instructions.ref",
            message: "Prompt ref is missing.",
            severity: "error",
          },
        ],
        warnings: [
          {
            code: "guardrail_param_warning",
            path: "nodes.agent.instructions",
            message: "Instruction policy warning.",
            severity: "warning",
          },
        ],
      },
      "agent",
      "instructions",
    );

    expect(issues).toHaveLength(2);
    expect(issues.map((issue) => issue.code)).toEqual([
      "missing_prompt_ref",
      "guardrail_param_warning",
    ]);
  });

  it("returns setup checks linked to a specific field", () => {
    const checks = nodeFieldSetupChecks(
      {
        id: "knowledge",
        type: "knowledge_query",
        knowledge_base_id: "",
        version_ids: [],
        retrieval_modes: ["dense"],
      },
      {
        type: "knowledge_query",
        label: "Knowledge Query",
        category: "Integrations",
        description: "Knowledge lookup",
        docs: [],
        default_inputs: {},
        default_outputs: {},
        fields: [],
        setup_checks: [
          {
            label: "Select a knowledge base or pinned versions",
            help: "Choose the target knowledge base or pin explicit KB versions for this query.",
            kind: "any_non_empty",
            fields: ["knowledge_base_id", "version_ids"],
          },
        ],
      },
      "knowledge_base_id",
    );

    expect(checks).toHaveLength(1);
    expect(checks[0]).toMatchObject({
      label: "Select a knowledge base or pinned versions",
      satisfied: false,
    });
  });

  it("treats mapped inputs as satisfying file paths and KB build/query wiring", () => {
    const manifest: WorkflowManifest = {
      schema_version: 1,
      workflow_id: "wf-setup",
      name: "Setup Wiring",
      nodes: {
        start: {
          id: "start",
          type: "start",
          outputs: {
            incoming_path: { type: "string" },
            selected_versions: { type: "structured" },
          },
        },
        file: {
          id: "file",
          type: "file_input",
          path: "",
          inputs: { path: { type: "string" } },
          outputs: { text: { type: "string" } },
        },
        output_folder: {
          id: "output_folder",
          type: "output_folder",
          path: "",
          inputs: { path: { type: "string" }, input: { type: "string" } },
          outputs: { files: { type: "structured" } },
        },
        knowledge: {
          id: "knowledge",
          type: "knowledge_query",
          knowledge_base_id: "",
          version_ids: [],
          retrieval_modes: [],
          inputs: {
            question: { type: "string" },
            version_ids: { type: "structured" },
          },
          outputs: {
            text: { type: "string" },
            result: { type: "structured" },
          },
        },
        build: {
          id: "build",
          type: "knowledge_build",
          knowledge_base_id: "KB-1",
          chunking_strategy: "",
          embedding_model: "",
          chunking_config: {},
          graph_config: null,
          activate_when_complete: false,
          wait_for_completion: false,
          wait_timeout_seconds: 300,
          inputs: {
            input: { type: "string" },
            chunking_strategy: { type: "string" },
            embedding_model: { type: "string" },
            chunking_config: { type: "structured" },
            graph_config: { type: "structured" },
          },
          outputs: {
            text: { type: "string" },
            result: { type: "structured" },
          },
        },
      },
      edges: [
        {
          id: "e_start_file",
          from: "start",
          to: "file",
          map: { incoming_path: "path" },
        },
        {
          id: "e_start_versions",
          from: "start",
          to: "knowledge",
          map: { selected_versions: "version_ids" },
        },
        {
          id: "e_start_output_folder",
          from: "start",
          to: "output_folder",
          map: { destination_path: "path" },
        },
        {
          id: "e_start_chunker",
          from: "start",
          to: "build",
          map: { selected_chunker: "chunking_strategy" },
        },
        {
          id: "e_start_embedder",
          from: "start",
          to: "build",
          map: { selected_embedder: "embedding_model" },
        },
      ],
      tools: {},
    };

    expect(
      nodeGuide(manifest.nodes.file, null, manifest).missingLabels,
    ).toEqual([]);
    expect(
      nodeGuide(manifest.nodes.output_folder, null, manifest).missingLabels,
    ).toEqual([]);
    expect(
      nodeGuide(manifest.nodes.knowledge, null, manifest).missingLabels,
    ).toEqual([]);
    expect(
      nodeGuide(manifest.nodes.build, null, manifest).missingLabels,
    ).toEqual([]);

    const knowledgeComponent = {
      type: "knowledge_query",
      label: "Knowledge Query",
      category: "Integrations",
      description: "Knowledge lookup",
      docs: [],
      default_inputs: {},
      default_outputs: {},
      fields: [],
      setup_checks: [
        {
          label: "Select a knowledge base or pinned versions",
          help: "Choose the target knowledge base or pin explicit KB versions for this query.",
          kind: "any_non_empty",
          fields: ["knowledge_base_id", "version_ids"],
        },
      ],
    } satisfies WorkflowComponent;

    const checks = nodeFieldSetupChecks(
      manifest.nodes.knowledge,
      knowledgeComponent,
      "knowledge_base_id",
      manifest,
    );
    expect(checks).toHaveLength(1);
    expect(checks[0]).toMatchObject({
      label: "Select a knowledge base or pinned versions",
      satisfied: true,
    });

    const knowledgeBuildComponent = {
      type: "knowledge_build",
      label: "Knowledge Build",
      category: "Integrations",
      description: "Knowledge build",
      docs: [],
      default_inputs: {},
      default_outputs: {},
      fields: [],
      setup_checks: [
        {
          label: "Select a knowledge base",
          help: "Choose the existing knowledge base this node should refresh.",
          kind: "non_empty_string",
          field: "knowledge_base_id",
        },
        {
          label: "Choose a chunking strategy",
          help: "Set the chunker directly or map one into the chunking_strategy input.",
          kind: "non_empty_string",
          field: "chunking_strategy",
        },
        {
          label: "Choose an embedding model",
          help: "Set the embedding model directly or map one into the embedding_model input.",
          kind: "non_empty_string",
          field: "embedding_model",
        },
      ],
    } satisfies WorkflowComponent;

    const buildChecks = nodeFieldSetupChecks(
      manifest.nodes.build,
      knowledgeBuildComponent,
      "chunking_strategy",
      manifest,
    );
    expect(buildChecks).toHaveLength(1);
    expect(buildChecks[0]).toMatchObject({
      label: "Choose a chunking strategy",
      satisfied: true,
    });

    const buildSummary = nodeValidationSummary(
      manifest.nodes.build,
      null,
      knowledgeBuildComponent,
      manifest,
    );
    expect(buildSummary.severity).toBe("ok");
    expect(buildSummary.title).toBe("Configuration checklist complete.");

    const summary = nodeValidationSummary(
      manifest.nodes.knowledge,
      null,
      knowledgeComponent,
      manifest,
    );
    expect(summary.severity).toBe("ok");
    expect(summary.title).toBe("Configuration checklist complete.");
  });

  it("buildNodeExecutionBadgeMap overlays preview, live, and terminal run states", () => {
    const active = buildNodeExecutionBadgeMap({
      previewSteps: [{ node_id: "agent", status: "ok" }],
      runSteps: [{ node_id: "guardrail", status: "skipped" }],
      runStatus: "waiting_approval",
      currentNodeId: "review",
    });
    expect(active.agent).toMatchObject({
      status: "ok",
      source: "preview",
      tone: "success",
    });
    expect(active.guardrail).toMatchObject({
      status: "skipped",
      source: "run",
      tone: "neutral",
    });
    expect(active.review).toMatchObject({
      status: "waiting_approval",
      label: "approval",
      source: "run",
      current: true,
    });

    const failed = buildNodeExecutionBadgeMap({
      runSteps: [
        { node_id: "agent", status: "ok" },
        { node_id: "router", status: "ok" },
      ],
      runStatus: "failed",
    });
    expect(failed.router).toMatchObject({
      status: "failed",
      source: "run",
      tone: "error",
      current: false,
    });
  });
});

describe("templateManifest", () => {
  it("single_agent has start/agent/final wired", () => {
    const m = templateManifest("single_agent", "w1", "W1");
    expect(Object.keys(m.nodes).sort()).toEqual(["agent", "final", "start"]);
    expect(m.edges).toHaveLength(2);
    expect(m.nodes.agent?.inputs).toHaveProperty("history");
    expect(m.nodes.agent?.outputs).toHaveProperty("history");
  });

  it("guarded_pipeline inserts a guardrail", () => {
    const m = templateManifest("guarded_pipeline", "w1", "W1");
    expect(m.nodes.guardrail).toBeDefined();
    expect(m.edges).toHaveLength(3);
  });

  it("multi_agent_handoff adds a specialist agent plus a conditional handoff", () => {
    const m = templateManifest("multi_agent_handoff", "w1", "W1");
    expect(Object.keys(m.nodes).sort()).toEqual([
      "agent",
      "billing",
      "final",
      "start",
    ]);
    expect((m.nodes.agent as Record<string, unknown>).handoffs).toEqual([
      {
        target: "billing",
        description: "Handle billing, invoices, and refunds.",
        condition:
          "'billing' in input or 'invoice' in input or 'refund' in input",
        input_filter:
          "Billing handoff\nCustomer request: {{input}}\nCoordinator draft: {{final_output}}",
      },
    ]);
  });

  it("blank has only start + output", () => {
    const m = templateManifest("blank", "w1", "W1");
    expect(Object.keys(m.nodes).sort()).toEqual(["final", "start"]);
    expect(m.edges).toHaveLength(0);
  });

  it("parallel_fanout wires a parallel branch into a join", () => {
    const m = templateManifest("parallel_fanout", "w1", "W1");
    expect(Object.keys(m.nodes).sort()).toEqual([
      "final",
      "join_all",
      "parallel",
      "research",
      "start",
      "writer",
    ]);
    expect((m.nodes.join_all as Record<string, unknown>).type).toBe("join");
    expect(m.edges).toEqual([
      {
        id: "e_start_parallel",
        from: "start",
        to: "parallel",
        map: { user_message: "input" },
      },
      {
        id: "e_parallel_research",
        from: "parallel",
        to: "research",
        map: { output: "input" },
      },
      {
        id: "e_parallel_writer",
        from: "parallel",
        to: "writer",
        map: { output: "input" },
      },
      {
        id: "e_research_join",
        from: "research",
        to: "join_all",
        map: { final_output: "research" },
      },
      {
        id: "e_writer_join",
        from: "writer",
        to: "join_all",
        map: { final_output: "draft" },
      },
      {
        id: "e_join_final",
        from: "join_all",
        to: "final",
        map: { output: "response" },
      },
    ]);
  });

  it("hitl_review wires agent → PII-redact guardrail → human approval → output", () => {
    const m = templateManifest("hitl_review", "w1", "W1");
    expect(Object.keys(m.nodes).sort()).toEqual([
      "agent",
      "final",
      "pii_guard",
      "review",
      "start",
    ]);
    expect(m.edges).toHaveLength(4);
    const guard = m.nodes.pii_guard as Record<string, unknown>;
    expect(guard.on_failure).toBe("redact");
    expect(guard.checks).toEqual([
      { pii_detection: { entities: ["email", "ssn", "phone", "credit_card"] } },
    ]);
    expect((m.nodes.review as Record<string, unknown>).type).toBe(
      "human_approval",
    );
  });

  it("for_each_loop wires a for-each node to a reusable worker agent", () => {
    const m = templateManifest("for_each_loop", "w1", "W1");
    expect(Object.keys(m.nodes).sort()).toEqual([
      "final",
      "for_each",
      "start",
      "worker",
    ]);
    expect((m.nodes.for_each as Record<string, unknown>).target_node_id).toBe(
      "worker",
    );
    expect(m.edges).toEqual([
      {
        id: "e_start_loop",
        from: "start",
        to: "for_each",
        map: { user_message: "items" },
      },
      {
        id: "e_loop_final",
        from: "for_each",
        to: "final",
        map: { text: "response" },
      },
    ]);
  });

  it("refinement_loop wires a bounded loop to a reusable editor agent", () => {
    const m = templateManifest("refinement_loop", "w1", "W1");
    expect(Object.keys(m.nodes).sort()).toEqual([
      "editor",
      "final",
      "loop",
      "start",
    ]);
    expect((m.nodes.loop as Record<string, unknown>).type).toBe("loop");
    expect((m.nodes.loop as Record<string, unknown>).target_node_id).toBe(
      "editor",
    );
    expect((m.nodes.loop as Record<string, unknown>).max_iterations).toBe(3);
    expect((m.nodes.loop as Record<string, unknown>).stop_condition).toBe(
      "iteration >= 2",
    );
    expect(m.edges).toEqual([
      {
        id: "e_start_loop",
        from: "start",
        to: "loop",
        map: { user_message: "input" },
      },
      {
        id: "e_loop_final",
        from: "loop",
        to: "final",
        map: { output: "response" },
      },
    ]);
  });

  it("knowledge_rag wires start → knowledge query → output", () => {
    const m = templateManifest("knowledge_rag", "w1", "W1");
    expect(Object.keys(m.nodes).sort()).toEqual([
      "final",
      "knowledge",
      "start",
    ]);
    expect((m.nodes.knowledge as Record<string, unknown>).type).toBe(
      "knowledge_query",
    );
    expect(
      (m.nodes.knowledge as Record<string, unknown>).retrieval_modes,
    ).toEqual([]);
    expect(m.edges).toEqual([
      {
        id: "e_start_knowledge",
        from: "start",
        to: "knowledge",
        map: { user_message: "question" },
      },
      {
        id: "e_knowledge_final",
        from: "knowledge",
        to: "final",
        map: { answer: "response" },
      },
    ]);
  });

  it("knowledge_age pins AGE graph retrieval on the knowledge query node", () => {
    const m = templateManifest("knowledge_age", "w1", "W1");
    expect((m.nodes.knowledge as Record<string, unknown>).type).toBe(
      "knowledge_query",
    );
    expect(
      (m.nodes.knowledge as Record<string, unknown>).retrieval_modes,
    ).toEqual(["age_graph"]);
  });

  it("graph_hybrid_rag pins GraphRAG hybrid retrieval on the knowledge query node", () => {
    const m = templateManifest("graph_hybrid_rag", "w1", "W1");
    expect((m.nodes.knowledge as Record<string, unknown>).type).toBe(
      "knowledge_query",
    );
    expect(
      (m.nodes.knowledge as Record<string, unknown>).retrieval_modes,
    ).toEqual(["graph_hybrid"]);
  });

  it("knowledge_age_build presets an AGE-native knowledge build", () => {
    const m = templateManifest("knowledge_age_build", "w1", "W1");
    expect((m.nodes.build_graph as Record<string, unknown>).type).toBe(
      "knowledge_build",
    );
    expect(
      (m.nodes.build_graph as Record<string, unknown>).chunking_strategy,
    ).toBe("recursive");
    expect(
      (m.nodes.build_graph as Record<string, unknown>).embedding_model,
    ).toBe("sentence-transformers/all-MiniLM-L6-v2");
    expect(
      ((m.nodes.build_graph as Record<string, unknown>).graph_config as Record<
        string,
        unknown
      >).output_target,
    ).toBe("object_store_and_age");
  });

  it("event_resume wires a wait gate before the release agent", () => {
    const m = templateManifest("event_resume", "w1", "W1");
    expect((m.nodes.wait_gate as Record<string, unknown>).type).toBe(
      "wait_for_event",
    );
    expect((m.nodes.wait_gate as Record<string, unknown>).event_name).toBe(
      "documents.ready",
    );
    expect((m.nodes.agent as Record<string, unknown>).name).toBe(
      "release-agent",
    );
    expect(m.edges).toEqual([
      {
        id: "e_start_wait",
        from: "start",
        to: "wait_gate",
        map: { user_message: "input" },
      },
      {
        id: "e_wait_agent",
        from: "wait_gate",
        to: "agent",
        map: { output: "input" },
      },
      {
        id: "e_agent_final",
        from: "agent",
        to: "final",
        map: { final_output: "response" },
      },
    ]);
  });

  it("templates carry workflow_id and runtime-pinned policy", () => {
    const m = templateManifest("single_agent", "wid-x", "Name");
    expect(m.workflow_id).toBe("wid-x");
    expect((m.runtime as Record<string, unknown>).sdk_version_policy).toBe(
      "runtime-pinned",
    );
    expect(
      (
        (m.runtime as Record<string, unknown>).session as Record<
          string,
          unknown
        >
      ).type,
    ).toBe("none");
  });
});

describe("connect/map helpers", () => {
  const agent = supportManifest().nodes.agent!;
  const final = supportManifest().nodes.final!;

  it("nodeOutputs / nodeInputs read declared ports", () => {
    expect(nodeOutputs(agent)).toEqual(["final_output"]);
    expect(nodeInputs(final)).toEqual(["response"]);
  });

  it("autoMapPorts matches by name first, then positionally", () => {
    expect(autoMapPorts(["a", "b"], ["a", "z"])).toEqual({ a: "a", b: "z" });
    expect(autoMapPorts(["final_output"], ["response"])).toEqual({
      final_output: "response",
    });
  });

  it("portSpecAssignable mirrors the runtime compatibility rules", () => {
    expect(portSpecAssignable({ type: "messages" }, { type: "string" })).toBe(
      true,
    );
    expect(portSpecAssignable({ type: "string" }, { type: "messages" })).toBe(
      false,
    );
    expect(
      portSpecAssignable({ type: "structured" }, { type: "structured" }),
    ).toBe(true);
    expect(portSpecAssignable({ type: "string" }, { type: "structured" })).toBe(
      false,
    );
  });

  it("autoMapCompatiblePorts skips incompatible outputs before falling back positionally", () => {
    const source: ManifestNode = {
      id: "source",
      type: "agent",
      outputs: {
        tool_calls: { type: "structured" },
        final_output: { type: "string" },
      },
    };
    const target: ManifestNode = {
      id: "target",
      type: "output",
      inputs: {
        response: { type: "string" },
      },
    };
    expect(autoMapCompatiblePorts(source, target)).toEqual({
      final_output: "response",
    });
  });

  it("canConnectNodes requires at least one compatible output/input pair", () => {
    expect(canConnectNodes(agent, final)).toBe(true);
    expect(
      canConnectNodes(
        {
          id: "source",
          type: "agent",
          outputs: { tool_calls: { type: "structured" } },
        },
        {
          id: "target",
          type: "output",
          inputs: { response: { type: "string" } },
        },
      ),
    ).toBe(false);
    expect(canConnectNodes({ id: "note-a", type: "note" }, final)).toBe(false);
  });

  it("deriveEdgeMap produces at least one mapping", () => {
    const map = deriveEdgeMap(agent, final);
    expect(map).toEqual({ final_output: "response" });
  });

  it("deriveEdgeMap leaves typed mismatches unmapped so the editor can warn", () => {
    expect(
      deriveEdgeMap(
        {
          id: "source",
          type: "agent",
          outputs: { tool_calls: { type: "structured" } },
        },
        {
          id: "target",
          type: "output",
          inputs: { response: { type: "string" } },
        },
      ),
    ).toEqual({});
  });

  it("deriveEdgeMap falls back to placeholder ports when nodes have no ports", () => {
    expect(
      deriveEdgeMap(
        { id: "note-a", type: "note" },
        { id: "note-b", type: "note" },
      ),
    ).toEqual({ output: "input" });
  });

  it("makeEdgeId avoids collisions", () => {
    const existing = new Set(["e_a_b", "e_a_b_2"]);
    expect(makeEdgeId("a", "b", existing)).toBe("e_a_b_3");
    expect(makeEdgeId("a", "c", existing)).toBe("e_a_c");
  });
});

describe("agent tool bindings", () => {
  it("derives registry refs and version constraints from tool versions", () => {
    const definition = tool("grep_files", "2.4.1");

    expect(registryRefForTool(definition)).toBe("tool.grep_files.v2");
    expect(versionConstraintForTool(definition)).toBe(">=2.0,<3.0");
    expect(registryRefForTool(tool("bad_version", "latest"))).toBe(
      "tool.bad_version.v1",
    );
    expect(versionConstraintForTool(tool("bad_version", "latest"))).toBe(
      ">=1.0,<2.0",
    );
    expect(toolBindingForDefinition(definition)).toMatchObject({
      registry_ref: "tool.grep_files.v2",
      version_constraint: ">=2.0,<3.0",
      requires_approval: false,
    });
  });

  it("adds top-level manifest bindings for agent-selected registered tools", () => {
    const manifest = supportManifest();
    manifest.nodes.agent!.tools = ["lookup_policy", "grep_files"];
    manifest.tools = {
      lookup_policy: {
        registry_ref: "tool.lookup_policy.v1",
        version_constraint: ">=1.0,<2.0",
      },
    };

    const synced = ensureAgentToolBindings(manifest, [
      tool("grep_files", "1.0"),
    ]);

    expect(synced.tools?.lookup_policy).toEqual(manifest.tools.lookup_policy);
    expect(synced.tools?.grep_files).toMatchObject({
      registry_ref: "tool.grep_files.v1",
      version_constraint: ">=1.0,<2.0",
    });
  });

  it("adds top-level manifest bindings for direct tool nodes", () => {
    const manifest = supportManifest();
    manifest.nodes.tool_lookup = {
      id: "tool_lookup",
      type: "tool",
      tool_name: "grep_files",
      inputs: {
        input: { type: "string" },
        arguments: { type: "structured" },
      },
      outputs: {
        text: { type: "string" },
        result: { type: "structured" },
        metadata: { type: "structured" },
      },
    };

    const synced = ensureAgentToolBindings(manifest, [
      tool("grep_files", "1.0"),
    ]);

    expect(synced.tools?.grep_files).toMatchObject({
      registry_ref: "tool.grep_files.v1",
      version_constraint: ">=1.0,<2.0",
    });
  });

  it("ignores malformed MCP tool refs and unknown MCP servers", () => {
    const manifest = supportManifest();
    manifest.nodes.agent!.tools = [
      "mcp:",
      "mcp:NoSlash",
      "mcp:Unknown/search",
      "mcp:Docs/search",
    ];

    const synced = ensureAgentToolBindings(
      manifest,
      [],
      [
        {
          server_id: "MCP-DOCS",
          name: "Docs",
          description: "",
          transport: "stdio",
          uri: "",
          command: "",
          args: [],
          env: {},
          headers: {},
          auth_type: "none",
          auth_config: {},
          discovered_tools: [{ name: "search", description: "Search" }],
          tool_policies: {},
          icon: "",
          status: "active",
          last_connected_at: null,
          connection_error: null,
          owner: "",
          created_at: "x",
          updated_at: "x",
        },
      ],
    );

    expect(synced.tools?.["mcp:Docs/search"]).toMatchObject({
      type: "mcp_tool",
      server_id: "MCP-DOCS",
      tool_name: "search",
    });
    expect(synced.tools?.["mcp:Unknown/search"]).toBeUndefined();
    expect(synced.tools?.["mcp:NoSlash"]).toBeUndefined();
  });
});
