import { describe, expect, it } from "vitest";

import type { ManifestNode, WorkflowManifest } from "@/api/workflowTypes";
import {
  buildNodePalette,
  autoMapCompatiblePorts,
  autoMapPorts,
  canConnectNodes,
  compatibleSourceOutputs,
  componentSetupCheckTouchesField,
  deriveEdgeMap,
  evaluateComponentSetupCheck,
  buildNodeExecutionBadgeMap,
  ensureAgentToolBindings,
  isWorkflowNodeType,
  makeEdgeId,
  manifestToFlow,
  nodeInputs,
  nodeOutputs,
  nodeFieldValidationIssues,
  nodeGuide,
  nodeValidationSummary,
  nodeColor,
  nodeExecutionColor,
  nodeExecutionLabel,
  nodeExecutionTone,
  nodeSubtitle,
  portSpecAssignable,
  portColor,
  registryRefForTool,
  toolBindingForDefinition,
  versionConstraintForTool,
} from "@/lib/workflowGraph";

const node = (type: string, overrides: Record<string, unknown> = {}): ManifestNode => ({
  id: "node",
  type,
  ...overrides,
} as ManifestNode);

const manifest: WorkflowManifest = {
  schema_version: 1,
  workflow_id: "wf",
  name: "Workflow",
  nodes: {
    node: node("agent", { inputs: { input: { type: "string" } } }),
    target: node("tool"),
  },
  edges: [
    { id: "e1", from: "source", to: "node", map: { output: "input" } },
    { id: "e2", from: "node", to: "target", map: {} },
  ],
};

describe("workflow graph presentation and setup branches", () => {
  it("covers execution status labels, tones, and colors", () => {
    for (const status of ["ok", "completed"]) expect(nodeExecutionTone(status)).toBe("success");
    for (const status of ["running", "resuming", "queued", "waiting_approval"]) expect(nodeExecutionTone(status)).toBe("info");
    for (const status of ["waiting_event", "blocked"]) expect(nodeExecutionTone(status)).toBe("warning");
    for (const status of ["failed", "error", "rejected"]) expect(nodeExecutionTone(status)).toBe("error");
    for (const status of ["cancelled", "expired", "skipped", "other"]) expect(nodeExecutionTone(status)).toBe("neutral");
    expect(nodeExecutionLabel("waiting_approval")).toBe("approval");
    expect(nodeExecutionLabel("waiting_event")).toBe("event");
    expect(nodeExecutionLabel("completed")).toBe("done");
    expect(nodeExecutionLabel("failed")).toBe("error");
    expect(nodeExecutionLabel("custom_status")).toBe("custom status");
    for (const status of ["ok", "completed"]) expect(nodeExecutionColor(status)).toBe("#22C55E");
    for (const status of ["running", "resuming"]) expect(nodeExecutionColor(status)).toBe("#0EA5E9");
    for (const status of ["queued", "waiting_approval"]) expect(nodeExecutionColor(status)).toBe("#8B5CF6");
    for (const status of ["waiting_event", "blocked"]) expect(nodeExecutionColor(status)).toBe("#F59E0B");
    for (const status of ["failed", "error", "rejected"]) expect(nodeExecutionColor(status)).toBe("#EF4444");
    for (const status of ["cancelled", "expired", "skipped"]) expect(nodeExecutionColor(status)).toBe("#71717A");
    expect(nodeExecutionColor("unknown")).toBe("#6B7280");
  });

  it("covers node subtitles for every supported node family", () => {
    const cases: Array<[string, Record<string, unknown>, string]> = [
      ["agent", { model: "gpt", tools: ["a"] }, "gpt · 1 tool"],
      ["guardrail", {}, "post_agent · 0 check(s)"],
      ["file_input", { path: "/tmp/a" }, "/tmp/a · file"],
      ["folder_input", {}, "**/* · 50 files"],
      ["input_bucket", { bucket: "b", prefix: "p" }, "b/p · 50 objects"],
      ["output_bucket", {}, "(no bucket) · artifacts"],
      ["output_folder", {}, "(no path) · artifacts"],
      ["wait_until", {}, "until time · wait"],
      ["wait_for_event", {}, "event · wait"],
      ["parallel", {}, "fan-out"],
      ["join", {}, "fan-in barrier"],
      ["for_each", {}, "100 max items"],
      ["loop", {}, "10 max iterations"],
      ["error_boundary", {}, "target · guarded"],
      ["subworkflow", {}, "workflow@prod"],
      ["tool", {}, "tool · direct"],
      ["mcp_resource", {}, "server · tool"],
      ["webhook", {}, "POST · (no url)"],
      ["api_request", {}, "GET · (no url)"],
      ["knowledge_query", {}, "knowledge base · KB default"],
      ["knowledge_build", {}, "knowledge base · chunker · embedder"],
      ["template", {}, "text template · missing preserve"],
      ["data_transform", {}, "mapping · deterministic"],
      ["review_queue_enqueue", {}, "review queue · audited"],
      ["external_app", {}, "entrypoint · bridge"],
      ["python_code", {}, "5s timeout · sandboxed"],
      ["router", {}, "0 branch(es)"],
      ["human_approval", {}, "1 approval · caliber.approver"],
      ["unknown", {}, "unknown"],
    ];
    for (const [type, overrides, expected] of cases) expect(nodeSubtitle(node(type, overrides))).toBe(expected);
    expect(nodeSubtitle(node("agent", { model: null, tools: [] }))).toBe("inherit · 0 tools");
    expect(nodeSubtitle(node("api_request", { mode: "curl" }))).toBe("cURL request");
    expect(nodeSubtitle(node("knowledge_query", { knowledge_base_id: "kb", retrieval_modes: ["dense", "graph"] }))).toBe("kb · dense + graph");
    expect(nodeSubtitle(node("human_approval", { approval_count: 2, required_role: "reviewer" }))).toBe("2 approvals · reviewer");
  });

  it("covers node and port color fallbacks", () => {
    for (const type of ["agent", "file_input", "folder_input", "input_bucket", "output_bucket", "output_folder", "wait_until", "wait_for_event", "external_app", "python_code", "router", "guardrail", "human_approval", "start", "output", "unknown"]) {
      expect(nodeColor(type)).toMatch(/^#/);
    }
    for (const type of ["string", "structured", "messages", "boolean", "void", "unknown"]) expect(portColor(type)).toMatch(/^#/);
  });

  it("evaluates all server setup check kinds and field matching rules", () => {
    const check = (kind: string, extra: Record<string, unknown> = {}) => ({ kind, field: "value", ...extra }) as never;
    const mappedManifest = {
      ...manifest,
      nodes: { ...manifest.nodes, node: node("agent", { inputs: { value: { type: "string" } } }) },
      edges: [{ ...manifest.edges[0]!, map: { output: "value" } }, manifest.edges[1]!],
    } as WorkflowManifest;
    expect(evaluateComponentSetupCheck(node("agent", { value: "x" }), check("non_empty_string"))).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent"), check("non_empty_string"))).toBe(false);
    expect(evaluateComponentSetupCheck(node("agent", { inputs: { value: {} } }), check("non_empty_string"), mappedManifest)).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent", { value: ["x"] }), check("non_empty_list"))).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent"), check("non_empty_list"))).toBe(false);
    expect(evaluateComponentSetupCheck(node("agent", { a: {}, b: ["x"] }), check("any_non_empty", { fields: ["a", "b"] }))).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent"), check("any_non_empty", { field: "value" }))).toBe(false);
    expect(evaluateComponentSetupCheck(node("agent", { instructions: { type: "inline", text: "hello" } }), check("instructions_present"))).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent", { instructions: { type: "mlflow_prompt", ref: "prompts:/a" } }), check("instructions_present"))).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent", { instructions: { type: "other" } }), check("instructions_present"))).toBe(false);
    expect(evaluateComponentSetupCheck(node("agent", { value: 2 }), check("minimum_number", { minimum: 2 }))).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent", { value: "2" }), check("minimum_number"))).toBe(false);
    expect(evaluateComponentSetupCheck(node("agent", { value: 1 }), check("minimum_number", { minimum: 2 }))).toBe(false);
    expect(evaluateComponentSetupCheck(node("agent"), check("minimum_outgoing_edges", { minimum: 1 }), null)).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent"), check("minimum_outgoing_edges", { minimum: 1 }), manifest)).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent"), check("minimum_incoming_edges", { minimum: 1 }), manifest)).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent"), check("distinct_incoming_target_ports"), manifest)).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent", { target: "target" }), check("target_node_executable_if_set", { field: "target" }), manifest)).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent", { target: "missing" }), check("target_node_executable_if_set", { field: "target" }), manifest)).toBe(false);
    expect(evaluateComponentSetupCheck(node("agent", { workflow: "wf" }), check("not_current_workflow_id", { field: "workflow" }), manifest)).toBe(false);
    expect(evaluateComponentSetupCheck(node("agent", { workflow: "other" }), check("not_current_workflow_id", { field: "workflow" }), manifest)).toBe(true);
    expect(evaluateComponentSetupCheck(node("router", { branches: [] }), check("router_branch_edges_connected"), manifest)).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent"), check("unknown"), manifest)).toBe(false);
    expect(componentSetupCheckTouchesField({ field: "value" } as never, "value")).toBe(true);
    expect(componentSetupCheckTouchesField({ fields: ["value"] } as never, "value")).toBe(true);
    expect(componentSetupCheckTouchesField({ fields: ["other"] } as never, "value")).toBe(false);
  });

  it("covers empty, duplicate, and invalid graph setup decisions", () => {
    const check = (kind: string, extra: Record<string, unknown> = {}) =>
      ({ kind, field: "value", ...extra }) as never;
    const duplicatePorts = {
      ...manifest,
      edges: [
        { id: "a", from: "source", to: "node", map: { one: "value", two: "value" } },
      ],
    } as WorkflowManifest;

    expect(evaluateComponentSetupCheck(node("agent"), check("minimum_incoming_edges"), null)).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent"), check("distinct_incoming_target_ports"), null)).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent"), check("target_node_executable_if_set"), manifest)).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent", { value: " " }), check("target_node_executable_if_set"), manifest)).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent"), check("not_current_workflow_id"), manifest)).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent", { workflow: " " }), check("not_current_workflow_id"), manifest)).toBe(true);
    expect(evaluateComponentSetupCheck(node("agent"), check("router_branch_edges_connected"), manifest)).toBe(true);
    expect(evaluateComponentSetupCheck(node("router", { branches: [{ to: "missing" }] }), check("router_branch_edges_connected"), manifest)).toBe(false);
    expect(evaluateComponentSetupCheck(node("router", { branches: [{ to: "target" }] }), check("router_branch_edges_connected"), duplicatePorts)).toBe(false);
    expect(evaluateComponentSetupCheck(node("agent"), check("distinct_incoming_target_ports"), duplicatePorts)).toBe(false);

    expect(nodeFieldValidationIssues(null, "node", "value")).toEqual([]);
    expect(nodeGuide(node("router", { branches: [{ to: "missing" }] }), undefined, manifest).missingLabels).toContain(
      "Connect every branch target with an outgoing edge",
    );
    expect(nodeValidationSummary(node("agent"), {
      valid: true,
      errors: [],
      warnings: [{ code: "warn", path: "nodes.node.model", message: "Check model", severity: "warning" }],
    })).toMatchObject({ severity: "warning", warnings: 1 });
    expect(nodeValidationSummary(node("note"), null)).toMatchObject({ severity: "ok" });
  });

  it("covers execution badge precedence and graph layout fallbacks", () => {
    expect(buildNodeExecutionBadgeMap({
      previewSteps: [{ node_id: null }, { node_id: "preview", status: " " }],
      runSteps: [{ node_id: "run", status: "completed" }],
      runStatus: "failed",
    })).toMatchObject({ run: { status: "failed" } });
    expect(buildNodeExecutionBadgeMap({
      runSteps: [{ node_id: "run" }],
      runStatus: "running",
      currentNodeId: "current",
    })).toMatchObject({ current: { current: true, status: "running" } });
    expect(buildNodeExecutionBadgeMap({ runStatus: "failed" })).toEqual({});

    const empty: WorkflowManifest = {
      schema_version: 1,
      workflow_id: "wf-empty",
      name: "Empty",
      nodes: {
        orphan: { id: "orphan", type: "note" },
      },
      edges: [],
    };
    expect(manifestToFlow(empty, { orphan: { x: 12, y: 24 } }).nodes[0]?.position).toEqual({ x: 12, y: 24 });
    expect(isWorkflowNodeType("agent")).toBe(true);
    expect(isWorkflowNodeType("not-a-node")).toBe(false);
    const custom = buildNodePalette([{
      type: "agent",
      label: "Backend agent",
      category: "Custom",
      description: "custom",
      docs: [],
      fields: [],
      setup_checks: [],
      default_inputs: { input: {} },
      default_outputs: {},
      legacy: true,
      legacy_replacement: "tool",
    } as never, {
      type: "server_extension",
      label: "Server extension",
      category: "Custom",
      description: "extra",
      docs: [],
      fields: [],
      setup_checks: [],
    } as never]);
    expect(custom.find((item) => item.type === "agent")).toMatchObject({
      label: "Backend agent",
      fieldCount: 0,
      defaultInputCount: 1,
      legacy: true,
    });
    expect(custom.find((item) => item.type === "server_extension")).toMatchObject({
      label: "Server extension",
      legacy: false,
      legacyReplacement: null,
    });
    expect(buildNodePalette([])).toBeTruthy();
  });

  it("covers port contracts, edge mapping, and registry binding fallbacks", () => {
    const source = node("agent", {
      outputs: {
        answer: { type: "string" },
        payload: { type: "structured" },
        context_output: { type: "string" },
      },
    });
    const target = node("tool", {
      inputs: {
        answer: { type: "string" },
        context: { type: "messages" },
        payload: { type: "structured" },
      },
    });

    expect(nodeOutputs(source)).toEqual(["answer", "payload", "context_output"]);
    expect(nodeInputs(target)).toEqual(["answer", "context", "payload"]);
    expect(portSpecAssignable(null, null)).toBe(true);
    expect(portSpecAssignable({ type: "string" }, { type: "string" })).toBe(true);
    expect(portSpecAssignable({ type: "messages" }, { type: "string" })).toBe(true);
    expect(portSpecAssignable({ type: "string" }, { type: "messages" })).toBe(false);
    expect(compatibleSourceOutputs(source, target, "context")).toEqual([
      "answer",
      "context_output",
    ]);
    expect(canConnectNodes(source, target)).toBe(true);
    expect(canConnectNodes(node("note"), target)).toBe(false);
    expect(canConnectNodes(source, node("note"))).toBe(false);
    expect(autoMapCompatiblePorts(source, target)).toEqual({
      answer: "answer",
      payload: "payload",
      context_output: "context",
    });

    expect(autoMapPorts(["answer", "extra"], ["answer", "context"])).toEqual({
      answer: "answer",
      extra: "context",
    });
    expect(deriveEdgeMap(node("note"), target)).toEqual({ output: "answer" });
    expect(deriveEdgeMap(source, node("note"))).toEqual({ answer: "input" });
    expect(deriveEdgeMap(source, target)).toEqual({
      answer: "answer",
      payload: "payload",
      context_output: "context",
    });
    const used = new Set(["e_source_target", "e_source_target_2"]);
    expect(makeEdgeId("source", "target", new Set())).toBe("e_source_target");
    expect(makeEdgeId("source", "target", used)).toBe("e_source_target_3");

    const readTool = { name: "lookup", version: "2.4.0", requires_approval: false, side_effect_level: "read" } as never;
    const actionTool = { name: "write", version: "bad", requires_approval: true, side_effect_level: "external_action" } as never;
    expect(registryRefForTool(readTool)).toBe("tool.lookup.v2");
    expect(versionConstraintForTool(actionTool)).toBe(">=1.0,<2.0");
    expect(toolBindingForDefinition(readTool)).toMatchObject({ timeout_seconds: 30, max_retries: 0 });
    expect(toolBindingForDefinition(actionTool)).toMatchObject({ timeout_seconds: 60, requires_approval: true });

    const bound = ensureAgentToolBindings(
      {
        ...manifest,
        tools: { existing: { type: "registry_tool" } },
        nodes: {
          agent: node("agent", { tools: ["lookup", "existing", "mcp:Gateway/search", "mcp:bad"] }),
          tool: node("tool", { tool_name: "write" }),
        },
      } as never,
      [readTool, actionTool],
      [
        {
          server_id: "srv-1",
          name: "Gateway",
          tool_policies: { search: { side_effect_level: "read", requires_approval: false } },
        },
      ] as never,
    );
    expect(bound.tools).toMatchObject({
      existing: { type: "registry_tool" },
      lookup: { registry_ref: "tool.lookup.v2" },
      write: { registry_ref: "tool.write.v1" },
      "mcp:Gateway/search": {
        type: "mcp_tool",
        server_id: "srv-1",
        side_effect_level: "read",
        requires_approval: false,
      },
    });
  });
});
