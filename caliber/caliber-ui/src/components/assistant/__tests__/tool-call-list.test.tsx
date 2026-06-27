/**
 * Tests for ToolCallList — the "Actions" strip surfacing the tools Aria ran.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ToolCallList, toolCallsFromMetadata } from "@/components/assistant/ToolCallList";

describe("toolCallsFromMetadata", () => {
  it("returns [] for missing or malformed metadata", () => {
    expect(toolCallsFromMetadata(undefined)).toEqual([]);
    expect(toolCallsFromMetadata({})).toEqual([]);
    expect(toolCallsFromMetadata({ tool_calls: "nope" })).toEqual([]);
  });

  it("extracts well-formed tool calls", () => {
    const calls = toolCallsFromMetadata({
      tool_calls: [{ name: "preview_workflow_draft", arguments: {}, result_summary: "ok", ok: true }],
    });
    expect(calls).toHaveLength(1);
    expect(calls[0]?.name).toBe("preview_workflow_draft");
  });
});

describe("ToolCallList", () => {
  it("renders nothing when there are no tool calls", () => {
    const { container } = render(<ToolCallList toolCalls={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders each tool call with its name and outcome", () => {
    render(
      <ToolCallList
        toolCalls={[
          { name: "get_workflow_run_trace", arguments: { run_id: "WR-1" }, result_summary: "3 spans", ok: true },
          { name: "run_workflow", arguments: {}, result_summary: "error: denied", ok: false },
        ]}
      />,
    );
    expect(screen.getByTestId("assistant-tool-calls")).toHaveTextContent("Actions · 2");
    expect(screen.getByText("get_workflow_run_trace")).toBeInTheDocument();
    expect(screen.getByText("run_workflow")).toBeInTheDocument();
  });
});
