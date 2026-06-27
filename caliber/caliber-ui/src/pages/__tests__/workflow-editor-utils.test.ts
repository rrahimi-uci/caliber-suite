import { describe, expect, it } from "vitest";

import type { WorkflowComponent } from "@/api/workflowTypes";
import { newNode } from "@/pages/WorkflowEditor";

function makeComponentStarter(
  type: WorkflowComponent["type"],
  starter_node: Record<string, unknown>,
): WorkflowComponent {
  return {
    type,
    label: type,
    category: "Test",
    description: "",
    docs: [],
    default_inputs: {},
    default_outputs: {},
    fields: [],
    starter_node,
  };
}

describe("WorkflowEditor newNode", () => {
  it("builds agent and guardrail nodes", () => {
    const agent = newNode("agent", "agent");
    const guardrail = newNode("guardrail", "guardrail");
    expect(agent.type).toBe("agent");
    expect(agent.id).toBe("agent");
    expect(agent.inputs).toHaveProperty("history");
    expect(agent.outputs).toHaveProperty("history");
    expect(guardrail.type).toBe("guardrail");
    expect(guardrail.id).toBe("guardrail");
  });

  it("builds file and folder input nodes", () => {
    const fileInput = newNode("file_input", "file_input");
    const folderInput = newNode("folder_input", "folder_input");
    expect(fileInput.type).toBe("file_input");
    expect(fileInput).toHaveProperty("path");
    expect(folderInput.type).toBe("folder_input");
    expect(folderInput).toHaveProperty("recursive", true);
  });

  it("builds router, mcp resource, approval, output and start nodes", () => {
    expect(newNode("router", "router").type).toBe("router");
    expect(newNode("mcp_resource", "mcp_resource").type).toBe("mcp_resource");
    expect(newNode("human_approval", "human_approval")).toMatchObject({
      type: "human_approval",
      required_role: "caliber.approver",
      approval_count: 1,
      timeout_behavior: "block",
    });
    expect(newNode("output", "output").type).toBe("output");
    expect(newNode("start", "start").type).toBe("start");
  });

  it("builds orchestration and control-flow nodes", () => {
    expect(newNode("wait_until", "wait_until").type).toBe("wait_until");
    expect(newNode("wait_for_event", "wait_for_event")).toMatchObject({
      type: "wait_for_event",
      timeout_seconds: null,
    });
    expect(newNode("parallel", "parallel").type).toBe("parallel");
    expect(newNode("join", "join").type).toBe("join");
    expect(newNode("for_each", "for_each").type).toBe("for_each");
    expect(newNode("loop", "loop")).toMatchObject({
      type: "loop",
      max_iterations: 10,
      stop_condition: "",
    });
    expect(newNode("error_boundary", "error_boundary").type).toBe("error_boundary");
    expect(newNode("subworkflow", "subworkflow").type).toBe("subworkflow");
    expect(newNode("tool", "tool")).toMatchObject({
      type: "tool",
      tool_name: "",
    });
    expect(newNode("knowledge_query", "knowledge_query")).toMatchObject({
      type: "knowledge_query",
      retrieval_modes: [],
      inputs: expect.objectContaining({
        retrieval_modes: { type: "structured" },
      }),
    });
    expect(newNode("template", "template")).toMatchObject({
      type: "template",
      template: "{{input}}",
      output_format: "text",
      missing_variable_mode: "preserve",
    });
    expect(newNode("external_app", "external_app").type).toBe("external_app");
    expect(newNode("python_code", "python_code").type).toBe("python_code");
    expect(newNode("webhook", "webhook")).toMatchObject({
      type: "webhook",
      url: "",
      method: "POST",
      headers: {},
      timeout_seconds: 30,
      inputs: expect.objectContaining({ payload: { type: "structured" } }),
      outputs: expect.objectContaining({ response: { type: "structured" } }),
    });
    expect(newNode("api_request", "api_request")).toMatchObject({
      type: "api_request",
      mode: "url",
      url: "",
      method: "GET",
      curl: "",
      body: "",
      headers: {},
      timeout_seconds: 30,
      outputs: expect.objectContaining({ response: { type: "structured" } }),
    });
  });

  it("rejects unsupported workflow node types", () => {
    expect(() => newNode("something_else", "note")).toThrow(
      'Unsupported workflow node type "something_else".',
    );
  });

  it("prefers backend starter node templates when provided", () => {
    const node = newNode(
      "agent",
      "agent_2",
      makeComponentStarter("agent", {
        id: "__CALIBER_NODE_ID__",
        type: "agent",
        name: "__CALIBER_NODE_ID__",
        model: "gpt-5.4-mini",
        instructions: { type: "inline", text: "From starter template." },
        inputs: { request: { type: "string" } },
        outputs: { answer: { type: "string" } },
      }),
    );

    expect(node).toMatchObject({
      id: "agent_2",
      type: "agent",
      name: "agent_2",
      model: "gpt-5.4-mini",
      instructions: { type: "inline", text: "From starter template." },
      inputs: { request: { type: "string" } },
      outputs: { answer: { type: "string" } },
    });
  });

  it("materializes dynamic starter timestamps from backend templates", () => {
    const node = newNode(
      "wait_until",
      "wait_gate",
      makeComponentStarter("wait_until", {
        id: "__CALIBER_NODE_ID__",
        type: "wait_until",
        wait_until: "__CALIBER_NOW_PLUS_60S_ISO__",
        timezone: "UTC",
        inputs: { input: { type: "string" } },
        outputs: { output: { type: "string" } },
      }),
    );

    expect(node.id).toBe("wait_gate");
    expect(node.type).toBe("wait_until");
    expect(node.wait_until).not.toBe("__CALIBER_NOW_PLUS_60S_ISO__");
    expect(Number.isNaN(Date.parse(String(node.wait_until)))).toBe(false);
  });
});
