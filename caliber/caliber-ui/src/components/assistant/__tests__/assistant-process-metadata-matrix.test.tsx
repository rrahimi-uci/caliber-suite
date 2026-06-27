import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  AssistantProcessSteps,
  processStepsFromMetadata,
} from "@/components/assistant/AssistantProcessSteps";
import { toolCallsFromMetadata } from "@/components/assistant/ToolCallList";


describe("toolCallsFromMetadata", () => {
  it.each([
    [undefined, 0],
    [{}, 0],
    [{ tool_calls: "bad" }, 0],
    [{ tool_calls: [null, 1, "bad"] }, 0],
    [
      {
        tool_calls: [
          { name: "list_skills", arguments: {}, result_summary: "ok", ok: true },
        ],
      },
      1,
    ],
    [
      {
        tool_calls: [
          { name: "list_skills", arguments: {}, result_summary: "ok", ok: true },
          { name: "get_skill", arguments: { id: "1" }, result_summary: "ok", ok: false },
        ],
      },
      2,
    ],
  ])("extracts tool calls from %p", (metadata, expectedCount) => {
    expect(toolCallsFromMetadata(metadata as Record<string, unknown> | undefined)).toHaveLength(
      expectedCount,
    );
  });
});


describe("processStepsFromMetadata", () => {
  it.each([
    [
      {
        process_steps: [{ key: "thinking", label: "Thinking", tone: "neutral" }],
      },
      ["Thinking"],
    ],
    [
      {
        process_steps: [{ key: "review", label: "Review required", tone: "warning" }],
      },
      ["Review required"],
    ],
    [
      {
        process_steps: [{ key: "published", label: "Published", tone: "success" }],
      },
      ["Published"],
    ],
    [
      {
        process_steps: [{ key: "failed", label: "Failed", tone: "error" }],
      },
      ["Failed"],
    ],
  ])("prefers persisted process steps from metadata %p", (metadata, expectedLabels) => {
    expect(processStepsFromMetadata(metadata as Record<string, unknown>)).toEqual(
      expectedLabels.map((label) => expect.objectContaining({ label })),
    );
  });

  it.each([
    [{ process_steps: "bad" }],
    [{ process_steps: [{ key: 1, label: "Thinking" }] }],
    [{ process_steps: [{ key: "thinking", label: 1 }] }],
    [{ process_steps: [{ key: "thinking", label: "Thinking", tone: "loud" }] }],
  ])("falls back when persisted process steps are invalid: %p", (metadata) => {
    const labels = processStepsFromMetadata(metadata as Record<string, unknown>).map(
      (step) => step.label,
    );
    expect(labels).toEqual(["Thinking"]);
  });

  it.each([
    [{ tool_calls: [] }, ["Thinking"]],
    [
      {
        tool_calls: [{ name: "preview", arguments: {}, result_summary: "ok", ok: true }],
      },
      ["Thinking", "1 action"],
    ],
    [
      {
        tool_calls: [
          { name: "preview", arguments: {}, result_summary: "ok", ok: true },
          { name: "publish", arguments: {}, result_summary: "ok", ok: true },
        ],
      },
      ["Thinking", "2 actions"],
    ],
  ])("derives tool-action counts from metadata %p", (metadata, expectedLabels) => {
    const labels = processStepsFromMetadata(metadata as Record<string, unknown>).map(
      (step) => step.label,
    );
    expect(labels).toEqual(expectedLabels);
  });

  it.each([
    [
      {
        tool_calls: [{ name: "preview", arguments: {}, result_summary: "ok", ok: true }],
      },
      "success",
    ],
    [
      {
        tool_calls: [{ name: "preview", arguments: {}, result_summary: "failed", ok: false }],
      },
      "warning",
    ],
  ])("derives action tone from tool results for %p", (metadata, expectedTone) => {
    const steps = processStepsFromMetadata(metadata as Record<string, unknown>);
    expect(steps[1].tone).toBe(expectedTone);
  });

  it.each([
    [
      {
        questions: [{ question: "Which bucket?" }],
      },
      ["Thinking", "Needs input"],
    ],
    [
      {
        tool_calls: [{ name: "preview", arguments: {}, result_summary: "ok", ok: true }],
        questions: [{ question: "Which bucket?" }],
      },
      ["Thinking", "1 action", "Needs input"],
    ],
  ])("adds needs-input state when questions are present for %p", (metadata, expectedLabels) => {
    const labels = processStepsFromMetadata(metadata as Record<string, unknown>).map(
      (step) => step.label,
    );
    expect(labels).toEqual(expectedLabels);
  });

  it.each([
    [{ error: true }, ["Thinking", "Error"]],
    [
      {
        tool_calls: [{ name: "preview", arguments: {}, result_summary: "ok", ok: true }],
        error: true,
      },
      ["Thinking", "1 action", "Error"],
    ],
  ])("adds error state when metadata marks the turn failed for %p", (metadata, expectedLabels) => {
    const labels = processStepsFromMetadata(metadata as Record<string, unknown>).map(
      (step) => step.label,
    );
    expect(labels).toEqual(expectedLabels);
  });
});


describe("AssistantProcessSteps", () => {
  it("renders nothing when there are no steps", () => {
    const { container } = render(<AssistantProcessSteps steps={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it.each([
    [{ key: "thinking", label: "Thinking", tone: "neutral" }, "border-slate-200"],
    [{ key: "published", label: "Published", tone: "success" }, "border-emerald-200"],
    [{ key: "review", label: "Review required", tone: "warning" }, "border-amber-200"],
    [{ key: "error", label: "Error", tone: "error" }, "border-red-200"],
  ])("renders tone-specific styling for %p", (step, classFragment) => {
    render(<AssistantProcessSteps steps={[step]} />);
    const chip = screen.getByText(step.label);
    expect(chip.className).toContain(classFragment);
  });

  it("renders multiple chips in order", () => {
    render(
      <AssistantProcessSteps
        steps={[
          { key: "thinking", label: "Thinking", tone: "neutral" },
          { key: "actions", label: "2 actions", tone: "success" },
          { key: "review", label: "Review required", tone: "warning" },
        ]}
      />,
    );
    const container = screen.getByTestId("assistant-process-steps");
    expect(container).toHaveTextContent("Thinking");
    expect(container).toHaveTextContent("2 actions");
    expect(container).toHaveTextContent("Review required");
  });
});
