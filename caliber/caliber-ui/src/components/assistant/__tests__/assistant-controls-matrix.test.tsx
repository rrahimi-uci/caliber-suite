import { describe, expect, it, vi } from "vitest";

import { render, screen, userEvent } from "@/test/utils";
import { ApprovalModeSelector } from "@/components/assistant/ApprovalModeSelector";
import { ArtifactTypeSelector } from "@/components/assistant/ArtifactTypeSelector";
import { DraftStatusBadge } from "@/components/assistant/DraftStatusBadge";
import { ModeSelector } from "@/components/assistant/ModeSelector";
import { QuestionList } from "@/components/assistant/QuestionList";
import { TestPanel } from "@/components/assistant/TestPanel";
import { ValidationPanel } from "@/components/assistant/ValidationPanel";
import type { ClarifyingQuestion, TestReport, ValidationReport } from "@/api/assistantTypes";

describe("ModeSelector", () => {
  it.each([
    ["chat", "Chat", "Ask questions — Aria answers without creating artifacts."],
    ["build", "Design", "Aria designs, authors, and edits artifacts."],
    ["plan", "Plan", "Aria outlines an approach before building."],
  ])("renders the current mode %s", (value, label, title) => {
    render(<ModeSelector value={value as "chat" | "build" | "plan"} onChange={() => {}} />);
    const selector = screen.getByTestId("assistant-mode-selector");
    expect(selector).toHaveTextContent(label);
    expect(selector).toHaveAttribute("title", title);
  });

  it.each([
    ["chat", "Chat"],
    ["build", "Design"],
    ["plan", "Plan"],
  ])("changes to %s from the dropdown", async (nextMode, label) => {
    const onChange = vi.fn();
    render(<ModeSelector value="build" onChange={onChange} />);
    await userEvent.click(screen.getByTestId("assistant-mode-selector"));
    await userEvent.click(screen.getAllByText(label).at(-1)!);
    expect(onChange).toHaveBeenCalledWith(nextMode);
  });

  it("stays inert when disabled", async () => {
    const onChange = vi.fn();
    render(<ModeSelector value="build" onChange={onChange} disabled />);
    await userEvent.click(screen.getByTestId("assistant-mode-selector"));
    expect(screen.queryByText("Chat")).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("ApprovalModeSelector", () => {
  it.each([
    ["manual", "Ask first", "Aria proposes changes and asks before every validate, test, approve, or publish action."],
    [
      "auto_safe",
      "Approve for me",
      "Aria can auto-run safe validation and test steps, but still asks before approval or publish.",
    ],
    [
      "auto_all",
      "Legacy auto",
      "Legacy policy: Aria can run safe and reversible mutation tools, but approval and publish still require a separate authority.",
    ],
    [
      "agent_review",
      "Agent review",
      "Aria validates and tests, then an independent approver-scoped agent reviews the immutable draft. Publishing remains separate.",
    ],
    [
      "full_autonomy",
      "Full autonomy",
      "An independent reviewer agent approves a passing immutable draft, then a distinct operator-scoped release service publishes it.",
    ],
  ])("renders the current approval mode %s", (value, label, title) => {
    render(
      <ApprovalModeSelector
        value={value as "manual" | "auto_safe" | "auto_all" | "agent_review" | "full_autonomy"}
        onChange={() => {}}
      />,
    );
    const selector = screen.getByTestId("assistant-approval-selector");
    expect(selector).toHaveTextContent(label);
    expect(selector).toHaveAttribute("title", title);
  });

  it.each([
    ["manual", "Ask first"],
    ["auto_safe", "Approve for me"],
    ["auto_all", "Legacy auto"],
    ["agent_review", "Agent review"],
    ["full_autonomy", "Full autonomy"],
  ])("changes to %s from the dropdown", async (nextMode, label) => {
    const onChange = vi.fn();
    render(<ApprovalModeSelector value="manual" onChange={onChange} />);
    await userEvent.click(screen.getByTestId("assistant-approval-selector"));
    await userEvent.click(screen.getAllByText(label).at(-1)!);
    expect(onChange).toHaveBeenCalledWith(nextMode);
  });

  it("stays inert when disabled", async () => {
    const onChange = vi.fn();
    render(<ApprovalModeSelector value="manual" onChange={onChange} disabled />);
    await userEvent.click(screen.getByTestId("assistant-approval-selector"));
    expect(screen.queryByText("Approve for me")).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("disables autonomous policies when their service identities are not ready", async () => {
    const onChange = vi.fn();
    render(
      <ApprovalModeSelector
        value="manual"
        onChange={onChange}
        autonomy={{
          agent_review_ready: false,
          full_autonomy_ready: false,
          reviewer_configured: false,
          release_configured: false,
        }}
      />,
    );
    await userEvent.click(screen.getByTestId("assistant-approval-selector"));
    expect(screen.getAllByText("Not configured")).toHaveLength(2);
    expect(screen.getByText("Agent review").closest("button")).toBeDisabled();
    expect(screen.getByText("Full autonomy").closest("button")).toBeDisabled();
  });
});

describe("DraftStatusBadge", () => {
  it.each([
    ["draft", "bg-slate-100"],
    ["validating", "bg-blue-100"],
    ["validated", "bg-emerald-100"],
    ["validation_failed", "bg-red-100"],
    ["testing", "bg-blue-100"],
    ["tested", "bg-emerald-100"],
    ["test_failed", "bg-red-100"],
    ["reviewing", "bg-blue-100"],
    ["review_rejected", "bg-red-100"],
    ["review_failed", "bg-red-100"],
    ["approved", "bg-amber-100"],
    ["publishing", "bg-blue-100"],
    ["published", "bg-green-100"],
    ["publish_failed", "bg-red-100"],
    ["mystery_status", "bg-gray-100"],
  ])("renders status %s with the right tone", (status, classFragment) => {
    render(<DraftStatusBadge status={status} />);
    const badge = screen.getByText(status.replace(/_/g, " "));
    expect(badge.className).toContain(classFragment);
  });
});

describe("ArtifactTypeSelector", () => {
  it.each([
    ["tool", "Tool"],
    ["skill", "Skill"],
    ["prompt", "Prompt"],
    ["workflow", "Workflow"],
    ["mcp_server", "MCP Server"],
  ])("selects artifact type %s", async (artifactType, label) => {
    const onChange = vi.fn();
    render(<ArtifactTypeSelector value={null} onChange={onChange} />);
    await userEvent.click(screen.getByText(label));
    expect(onChange).toHaveBeenCalledWith(artifactType);
  });
});

describe("QuestionList", () => {
  it("renders nothing when there are no questions", () => {
    const { container } = render(<QuestionList questions={[]} onAnswer={() => {}} />);
    expect(container.firstChild).toBeNull();
  });

  it.each([
    [
      [
        {
          question: "What should it be named?",
          field: "name",
          options: ["alpha", "beta"],
        },
      ],
      ["alpha", "beta"],
    ],
    [[{ question: "Which bucket?", field: "bucket", options: ["docs"] }], ["docs"]],
  ])("renders option pills for selectable questions", (questions, options) => {
    render(<QuestionList questions={questions as ClarifyingQuestion[]} onAnswer={() => {}} />);
    for (const option of options as string[]) {
      expect(screen.getByText(option)).toBeInTheDocument();
    }
  });

  it("renders free-text clarifying prompts without option pills", () => {
    render(
      <QuestionList
        questions={[
          {
            question: "Describe the desired behavior",
            field: "desc",
            options: [],
          },
        ]}
        onAnswer={() => {}}
      />,
    );
    expect(screen.getByText("Describe the desired behavior")).toBeInTheDocument();
  });

  it.each([["alpha"], ["beta"], ["docs"]])("calls onAnswer for option %s", async (option) => {
    const onAnswer = vi.fn();
    render(
      <QuestionList
        questions={[
          {
            question: "Choose one",
            field: "name",
            options: ["alpha", "beta", "docs"],
          },
        ]}
        onAnswer={onAnswer}
      />,
    );
    await userEvent.click(screen.getByText(option));
    expect(onAnswer).toHaveBeenCalledWith(option);
  });
});

describe("ValidationPanel", () => {
  it("renders the empty-state placeholder when no report exists", () => {
    render(<ValidationPanel report={null} />);
    expect(screen.getByText(/no validation report yet/i)).toBeInTheDocument();
  });

  it.each([
    [{ valid: true, errors: [], warnings: [] }, "Valid"],
    [{ valid: false, errors: ["Missing field"], warnings: [] }, "Invalid"],
    [{ valid: true, errors: [], warnings: ["Consider adding a description"] }, "Valid"],
    [
      {
        valid: false,
        errors: ["Missing field"],
        warnings: ["Consider adding a description"],
      },
      "Invalid",
    ],
  ])("renders validation report state %j", (report, label) => {
    render(<ValidationPanel report={report as ValidationReport} />);
    expect(screen.getByText(label)).toBeInTheDocument();
    for (const error of report.errors) {
      expect(screen.getByText(error)).toBeInTheDocument();
    }
    for (const warning of report.warnings) {
      expect(screen.getByText(warning)).toBeInTheDocument();
    }
  });
});

describe("TestPanel", () => {
  it("renders the empty-state placeholder when no report exists", () => {
    render(<TestPanel report={null} />);
    expect(screen.getByText(/no test report yet/i)).toBeInTheDocument();
  });

  it.each([
    [
      {
        passed: true,
        total: 2,
        failures: 0,
        details: [{ test: "structure", passed: true }],
        error: null,
      },
      "All tests passed",
    ],
    [
      {
        passed: false,
        total: 3,
        failures: 1,
        details: [{ test: "runtime", passed: false }],
        error: null,
      },
      "1/3 failed",
    ],
    [
      {
        passed: false,
        total: 0,
        failures: 0,
        details: [],
        error: "Sandbox failed",
      },
      "0/0 failed",
    ],
    [
      {
        passed: false,
        total: 2,
        failures: 2,
        details: [{ passed: false }, { test: "type-check", passed: false }],
        error: "Compilation failed",
      },
      "2/2 failed",
    ],
  ])("renders test report state %j", (report, label) => {
    render(<TestPanel report={report as TestReport} />);
    expect(screen.getByText(label)).toBeInTheDocument();
    if (report.error) {
      expect(screen.getByText(report.error)).toBeInTheDocument();
    }
    for (const detail of report.details) {
      const testName = String(detail.test ?? "Test 1");
      expect(screen.getByText(testName)).toBeInTheDocument();
    }
  });
});
