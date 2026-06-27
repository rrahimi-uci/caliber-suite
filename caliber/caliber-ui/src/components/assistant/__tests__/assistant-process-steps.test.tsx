import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  AssistantProcessSteps,
  processStepsFromMetadata,
} from "@/components/assistant/AssistantProcessSteps";

describe("processStepsFromMetadata", () => {
  it("prefers persisted process steps when present", () => {
    const steps = processStepsFromMetadata({
      process_steps: [
        { key: "thinking", label: "Thinking", tone: "neutral" },
        { key: "review", label: "Review required", tone: "warning" },
      ],
    });
    expect(steps.map((step) => step.label)).toEqual(["Thinking", "Review required"]);
  });

  it("falls back to tool calls, questions, and errors for older messages", () => {
    const steps = processStepsFromMetadata({
      tool_calls: [{ name: "preview_workflow_draft", arguments: {}, result_summary: "ok", ok: true }],
      questions: [{ question: "Which tool name?" }],
      error: true,
    });
    expect(steps.map((step) => step.label)).toEqual([
      "Thinking",
      "1 action",
      "Needs input",
      "Error",
    ]);
  });
});

describe("AssistantProcessSteps", () => {
  it("renders compact process chips", () => {
    render(
      <AssistantProcessSteps
        steps={[
          { key: "thinking", label: "Thinking", tone: "neutral" },
          { key: "published", label: "Published", tone: "success" },
        ]}
      />,
    );
    expect(screen.getByTestId("assistant-process-steps")).toHaveTextContent("Thinking");
    expect(screen.getByTestId("assistant-process-steps")).toHaveTextContent("Published");
  });
});
