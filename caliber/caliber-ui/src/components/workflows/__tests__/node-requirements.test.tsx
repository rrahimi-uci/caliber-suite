import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  WorkflowComponent,
  WorkflowComponentRequirement,
} from "@/api/workflowTypes";
import { NodePalette } from "@/components/workflows/NodePalette";
import { WorkflowComponentSchemaSummary } from "@/components/workflows/WorkflowComponentSchemaSummary";

const REAL_LLM: WorkflowComponentRequirement = {
  id: "real_llm",
  label:
    "A real LLM provider. With the default fake provider an agent only echoes a canned reply.",
  met: false,
};

function component(
  type: WorkflowComponent["type"],
  requirements: WorkflowComponentRequirement[] | undefined,
): WorkflowComponent {
  return {
    type,
    label: type,
    category: "Server Components",
    description: `The ${type} node.`,
    docs: [],
    default_inputs: {},
    default_outputs: {},
    fields: [],
    requirements,
  };
}

describe("the palette's prerequisite badges", () => {
  it("shows a badge, with the full sentence on hover, for each prerequisite not met", () => {
    render(
      <NodePalette
        onAddNode={vi.fn()}
        components={[
          component("agent", [REAL_LLM]),
          component("input_bucket", [
            { id: "object_store", label: "A remote object store.", met: false },
          ]),
        ]}
      />,
    );
    const badge = screen.getByTestId("palette-needs-agent-real_llm");
    expect(badge).toHaveTextContent("Needs a real LLM");
    expect(badge).toHaveAttribute("title", REAL_LLM.label);
    expect(
      screen.getByTestId("palette-needs-input_bucket-object_store"),
    ).toHaveTextContent("Needs an object store");
  });

  it("shows no badge once the prerequisite is met", () => {
    render(
      <NodePalette
        onAddNode={vi.fn()}
        components={[component("agent", [{ ...REAL_LLM, met: true }])]}
      />,
    );
    expect(
      screen.queryByTestId("palette-needs-agent-real_llm"),
    ).not.toBeInTheDocument();
  });

  it("shows a muted badge when the server cannot say, rather than claiming it is unmet", () => {
    render(
      <NodePalette
        onAddNode={vi.fn()}
        components={[
          component("mcp_resource", [
            { id: "mcp_server", label: "A registered MCP server.", met: null },
          ]),
        ]}
      />,
    );
    const badge = screen.getByTestId("palette-needs-mcp_resource-mcp_server");
    expect(badge).toHaveTextContent("Needs an MCP server");
    expect(badge.className).toContain("zinc");
    expect(badge.className).not.toContain("amber");
  });

  it("shows nothing for a node with no prerequisites, or from a server that sends none", () => {
    render(
      <NodePalette
        onAddNode={vi.fn()}
        components={[component("start", []), component("output", undefined)]}
      />,
    );
    expect(screen.queryByTestId(/^palette-needs-/)).not.toBeInTheDocument();
  });

  it("falls back to the raw id for a prerequisite it has no short name for", () => {
    render(
      <NodePalette
        onAddNode={vi.fn()}
        components={[
          component("agent", [{ id: "gpu", label: "A GPU.", met: false }]),
        ]}
      />,
    );
    expect(screen.getByTestId("palette-needs-agent-gpu")).toHaveTextContent(
      "Needs gpu",
    );
  });

  it("repeats the prerequisites, with their state, in the component reference", () => {
    render(
      <NodePalette
        onAddNode={vi.fn()}
        components={[
          component("wait_for_event", [
            {
              id: "run_queue",
              label: "The run queue, with checkpointing.",
              met: true,
            },
            { id: "real_llm", label: REAL_LLM.label, met: false },
          ]),
        ]}
      />,
    );
    fireEvent.click(screen.getByTestId("palette-inspect-wait_for_event"));
    const reference = screen.getByTestId("workflow-component-requirements");
    expect(
      within(reference).getByTestId("workflow-component-requirement-run_queue"),
    ).toHaveTextContent("Configured");
    expect(
      within(reference).getByTestId("workflow-component-requirement-real_llm"),
    ).toHaveTextContent("Not configured");
  });
});

describe("WorkflowComponentSchemaSummary requirements", () => {
  it("omits the section when there is nothing to require", () => {
    render(
      <WorkflowComponentSchemaSummary component={component("start", [])} />,
    );
    expect(
      screen.queryByTestId("workflow-component-requirements"),
    ).not.toBeInTheDocument();
  });

  it("labels an undeterminable prerequisite as one to check", () => {
    render(
      <WorkflowComponentSchemaSummary
        component={component("mcp_resource", [
          { id: "mcp_server", label: "A registered MCP server.", met: null },
        ])}
      />,
    );
    expect(
      screen.getByTestId("workflow-component-requirement-mcp_server"),
    ).toHaveTextContent("Check");
  });
});
