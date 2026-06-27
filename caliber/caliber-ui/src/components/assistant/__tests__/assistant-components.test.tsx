/**
 * Tests for assistant UI components.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen, userEvent } from "@/test/utils";
import { DraftStatusBadge } from "../DraftStatusBadge";
import { ArtifactTypeSelector } from "../ArtifactTypeSelector";
import { QuestionList } from "../QuestionList";
import { ValidationPanel } from "../ValidationPanel";
import { TestPanel } from "../TestPanel";
import type {
  ClarifyingQuestion,
  ValidationReport,
  TestReport,
} from "@/api/assistantTypes";

/* ================================================================== */
/* DraftStatusBadge                                                   */
/* ================================================================== */

describe("DraftStatusBadge", () => {
  it("renders the status text with underscores replaced by spaces", () => {
    render(<DraftStatusBadge status="validation_failed" />);
    expect(screen.getByText("validation failed")).toBeInTheDocument();
  });

  it("renders known statuses", () => {
    const { rerender } = render(<DraftStatusBadge status="draft" />);
    expect(screen.getByText("draft")).toBeInTheDocument();

    rerender(<DraftStatusBadge status="approved" />);
    expect(screen.getByText("approved")).toBeInTheDocument();

    rerender(<DraftStatusBadge status="published" />);
    expect(screen.getByText("published")).toBeInTheDocument();
  });

  it("handles unknown status gracefully", () => {
    render(<DraftStatusBadge status="unknown_status" />);
    expect(screen.getByText("unknown status")).toBeInTheDocument();
  });
});

/* ================================================================== */
/* ArtifactTypeSelector                                               */
/* ================================================================== */

describe("ArtifactTypeSelector", () => {
  it("renders all five artifact type buttons", () => {
    render(<ArtifactTypeSelector value={null} onChange={() => {}} />);
    expect(screen.getByText("Tool")).toBeInTheDocument();
    expect(screen.getByText("Skill")).toBeInTheDocument();
    expect(screen.getByText("Prompt")).toBeInTheDocument();
    expect(screen.getByText("Workflow")).toBeInTheDocument();
    expect(screen.getByText("MCP Server")).toBeInTheDocument();
  });

  it("calls onChange when a type is clicked", async () => {
    const onChange = vi.fn();
    render(<ArtifactTypeSelector value={null} onChange={onChange} />);
    await userEvent.click(screen.getByText("Skill"));
    expect(onChange).toHaveBeenCalledWith("skill");
  });

  it("highlights the selected type", () => {
    render(<ArtifactTypeSelector value="tool" onChange={() => {}} />);
    const toolBtn = screen.getByText("Tool").closest("button")!;
    expect(toolBtn.className).toContain("caliber");
  });
});

/* ================================================================== */
/* QuestionList                                                       */
/* ================================================================== */

describe("QuestionList", () => {
  it("returns null when no questions", () => {
    const { container } = render(<QuestionList questions={[]} onAnswer={() => {}} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders questions and option buttons", () => {
    const questions: ClarifyingQuestion[] = [
      { question: "What should the tool be named?", field: "name", options: ["greet", "hello"] },
    ];
    render(<QuestionList questions={questions} onAnswer={() => {}} />);
    expect(screen.getByText("What should the tool be named?")).toBeInTheDocument();
    expect(screen.getByText("greet")).toBeInTheDocument();
    expect(screen.getByText("hello")).toBeInTheDocument();
  });

  it("calls onAnswer when an option is clicked", async () => {
    const onAnswer = vi.fn();
    const questions: ClarifyingQuestion[] = [
      { question: "Pick a name", field: "name", options: ["greet"] },
    ];
    render(<QuestionList questions={questions} onAnswer={onAnswer} />);
    await userEvent.click(screen.getByText("greet"));
    expect(onAnswer).toHaveBeenCalledWith("greet");
  });

  it("renders questions without options (free-text)", () => {
    const questions: ClarifyingQuestion[] = [
      { question: "Describe the tool behaviour", field: "desc", options: [] },
    ];
    render(<QuestionList questions={questions} onAnswer={() => {}} />);
    expect(screen.getByText("Describe the tool behaviour")).toBeInTheDocument();
  });
});

/* ================================================================== */
/* ValidationPanel                                                    */
/* ================================================================== */

describe("ValidationPanel", () => {
  it("shows placeholder when report is null", () => {
    render(<ValidationPanel report={null} />);
    expect(screen.getByText(/no validation report/i)).toBeInTheDocument();
  });

  it("shows Valid when report is valid", () => {
    const report: ValidationReport = { valid: true, errors: [], warnings: [] };
    render(<ValidationPanel report={report} />);
    expect(screen.getByText("Valid")).toBeInTheDocument();
  });

  it("shows Invalid with errors", () => {
    const report: ValidationReport = {
      valid: false,
      errors: ["Missing name field", "Schema invalid"],
      warnings: [],
    };
    render(<ValidationPanel report={report} />);
    expect(screen.getByText("Invalid")).toBeInTheDocument();
    expect(screen.getByText("Missing name field")).toBeInTheDocument();
    expect(screen.getByText("Schema invalid")).toBeInTheDocument();
  });

  it("shows warnings", () => {
    const report: ValidationReport = {
      valid: true,
      errors: [],
      warnings: ["Consider adding a description"],
    };
    render(<ValidationPanel report={report} />);
    expect(screen.getByText("Consider adding a description")).toBeInTheDocument();
  });
});

/* ================================================================== */
/* TestPanel                                                          */
/* ================================================================== */

describe("TestPanel", () => {
  it("shows placeholder when report is null", () => {
    render(<TestPanel report={null} />);
    expect(screen.getByText(/no test report/i)).toBeInTheDocument();
  });

  it("shows passed when all tests pass", () => {
    const report: TestReport = {
      passed: true,
      total: 3,
      failures: 0,
      details: [
        { test: "structural", passed: true },
        { test: "type-check", passed: true },
        { test: "coverage", passed: true },
      ],
      error: null,
    };
    render(<TestPanel report={report} />);
    expect(screen.getByText("All tests passed")).toBeInTheDocument();
    expect(screen.getByText("structural")).toBeInTheDocument();
  });

  it("shows failure count", () => {
    const report: TestReport = {
      passed: false,
      total: 2,
      failures: 1,
      details: [
        { test: "structural", passed: true },
        { test: "runtime", passed: false },
      ],
      error: null,
    };
    render(<TestPanel report={report} />);
    expect(screen.getByText("1/2 failed")).toBeInTheDocument();
  });

  it("shows error message", () => {
    const report: TestReport = {
      passed: false,
      total: 0,
      failures: 0,
      details: [],
      error: "Engine crashed unexpectedly",
    };
    render(<TestPanel report={report} />);
    expect(screen.getByText("Engine crashed unexpectedly")).toBeInTheDocument();
  });
});
