import { describe, expect, it } from "vitest";

import type { ToolDefinition, WorkflowManifest } from "@/api/workflowTypes";
import { ensureAgentToolBindings } from "@/lib/workflowGraph";

function manifestWithAgent(tools: string[]): WorkflowManifest {
  return {
    schema_version: 1,
    workflow_id: "WF-1",
    name: "WF",
    nodes: {
      start: { id: "start", type: "start", outputs: { msg: { type: "string" } } },
      agent: {
        id: "agent",
        type: "agent",
        name: "agent",
        model: "inherit",
        instructions: { type: "inline", text: "hi" },
        tools,
        inputs: { input: { type: "string" } },
        outputs: { final_output: { type: "string" } },
      },
      final: { id: "final", type: "output", inputs: { response: { type: "string" } } },
    },
    edges: [
      { id: "e1", from: "start", to: "agent", map: { msg: "input" } },
      { id: "e2", from: "agent", to: "final", map: { final_output: "response" } },
    ],
    tools: {},
  };
}

function registryTool(name: string): ToolDefinition {
  return {
    tool_id: `TL-${name}`,
    name,
    version: "1.0",
    description: "",
    module_path: "caliber.tools",
    callable_name: name,
    input_schema: null,
    output_schema: null,
    side_effect_level: "read",
    requires_approval: false,
    allow_in_preview: true,
    secret_refs: [],
    test_cases: [],
    last_calibration: null,
    owner: "@ops",
    status: "active",
    deprecated_at: null,
    successor_tool_id: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

describe("ensureAgentToolBindings", () => {
  it("creates registered tool bindings", () => {
    const manifest = manifestWithAgent(["lookup_policy"]);
    const next = ensureAgentToolBindings(manifest, [registryTool("lookup_policy")]);
    expect(next.tools?.lookup_policy).toMatchObject({
      registry_ref: "tool.lookup_policy.v1",
      version_constraint: ">=1.0,<2.0",
    });
  });

  it("creates MCP tool bindings from agent tool refs", () => {
    const manifest = manifestWithAgent(["mcp:Docs/search_docs"]);
    const next = ensureAgentToolBindings(manifest, [], [
      {
        server_id: "MCP-1",
        name: "Docs",
        description: "",
        transport: "stdio",
        uri: "",
        command: "npx docs-mcp",
        args: [],
        env: {},
        headers: {},
        auth_type: "none",
        auth_config: {},
        discovered_tools: [{ name: "search_docs", description: "Search docs" }],
        tool_policies: {},
        icon: "",
        status: "active",
        last_connected_at: null,
        connection_error: null,
        owner: "",
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    ]);
    expect(next.tools?.["mcp:Docs/search_docs"]).toMatchObject({
      type: "mcp_tool",
      server_id: "MCP-1",
      tool_name: "search_docs",
      side_effect_level: "read",
    });
  });
});

