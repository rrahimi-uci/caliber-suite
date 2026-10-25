import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PromptTestCaseRow } from "@/pages/Prompts";

const testCase = {
  id: "tc-1",
  input: "How do I update my address?",
  expectedBehavior: "Ask for verification before changing it.",
  tags: ["account", "verification"],
};

describe("PromptTestCaseRow", () => {
  it("renders an unscored case, tags, and removal action", () => {
    const onRemove = vi.fn();
    render(
      <table>
        <tbody>
          <PromptTestCaseRow
            index={1}
            testCase={testCase}
            hasResults={false}
            onRemove={onRemove}
          />
        </tbody>
      </table>,
    );
    expect(screen.getByText("account")).toBeInTheDocument();
    expect(screen.getByText("verification")).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("Remove test case"));
    expect(onRemove).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Judge Reasoning")).not.toBeInTheDocument();
  });

  it.each([
    ["pass", "bg-emerald-100"],
    ["partial", "bg-amber-100"],
    ["fail", "bg-red-100"],
  ] as const)("renders and expands a %s judged case", (verdict, color) => {
    const onRemove = vi.fn();
    render(
      <table>
        <tbody>
          <PromptTestCaseRow
            index={2}
            testCase={{ ...testCase, tags: [] }}
            result={{
              testCaseId: "tc-1",
              input: testCase.input,
              expectedBehavior: testCase.expectedBehavior,
              actualResponse: verdict === "fail" ? "" : "Verified response",
              verdict,
              score: verdict === "pass" ? 1 : verdict === "partial" ? 0.5 : 0,
              reasoning: `Judge marked this ${verdict}.`,
            }}
            hasResults
            onRemove={onRemove}
          />
        </tbody>
      </table>,
    );
    expect(screen.getByText(verdict)).toHaveClass(color);
    expect(screen.getByText(verdict === "pass" ? "100%" : verdict === "partial" ? "50%" : "0%")).toBeInTheDocument();
    fireEvent.click(screen.getByText(testCase.input));
    expect(screen.getByText("Judge Reasoning")).toBeInTheDocument();
    expect(screen.getByText(`Judge marked this ${verdict}.`)).toBeInTheDocument();
    if (verdict === "fail") {
      expect(screen.getByText("(empty response)")).toBeInTheDocument();
    } else {
      expect(screen.getByText("Verified response")).toBeInTheDocument();
    }
    fireEvent.click(screen.getByTitle("Remove test case"));
    expect(onRemove).toHaveBeenCalledTimes(1);
  });
});
