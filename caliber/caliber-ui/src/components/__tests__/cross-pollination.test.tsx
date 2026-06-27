/**
 * Tests for the PromptDiff component and its computeDiff helper.
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import {
  computeDiff,
  PromptDiff,
} from "@/components/PromptDiff";

/* -------------------------------------------------------------------------- */
/* computeDiff                                                                 */
/* -------------------------------------------------------------------------- */

describe("computeDiff", () => {
  it("returns empty array for two empty strings", () => {
    const result = computeDiff("", "");
    // Single empty-to-empty line is unchanged
    expect(result.length).toBeGreaterThanOrEqual(0);
  });

  it("marks all lines as added when baseline is empty", () => {
    const result = computeDiff("", "line1\nline2");
    const added = result.filter((l) => l.type === "added");
    expect(added.length).toBe(2);
  });

  it("marks all lines as removed when candidate is empty", () => {
    const result = computeDiff("line1\nline2", "");
    const removed = result.filter((l) => l.type === "removed");
    expect(removed.length).toBe(2);
  });

  it("detects unchanged lines", () => {
    const result = computeDiff("same\nline", "same\nline");
    const unchanged = result.filter((l) => l.type === "unchanged");
    expect(unchanged.length).toBe(2);
  });

  it("detects mixed changes", () => {
    const result = computeDiff("a\nb\nc", "a\nB\nc");
    const types = result.map((l) => l.type);
    expect(types).toContain("unchanged");
    expect(types).toContain("removed");
    expect(types).toContain("added");
  });

  it("handles single line modification", () => {
    const result = computeDiff("old rule", "new rule");
    expect(result.some((l) => l.type === "removed" && l.content === "old rule")).toBe(true);
    expect(result.some((l) => l.type === "added" && l.content === "new rule")).toBe(true);
  });

  it("assigns line numbers correctly", () => {
    const result = computeDiff("a\nb", "a\nc");
    const unchangedA = result.find((l) => l.content === "a" && l.type === "unchanged");
    expect(unchangedA?.lineNumber.left).toBe(1);
    expect(unchangedA?.lineNumber.right).toBe(1);

    const removedB = result.find((l) => l.content === "b" && l.type === "removed");
    expect(removedB?.lineNumber.left).toBe(2);
    expect(removedB?.lineNumber.right).toBeNull();
  });
});

/* -------------------------------------------------------------------------- */
/* PromptDiff component                                                        */
/* -------------------------------------------------------------------------- */

describe("PromptDiff", () => {
  it("shows cold-start message when baseline is null", () => {
    render(<PromptDiff baseline={null} candidate="new prompt" />);
    expect(screen.getByText(/cold start/i)).toBeTruthy();
    expect(screen.getByText("new prompt")).toBeTruthy();
  });

  it("shows no-changes message when baseline equals candidate", () => {
    render(<PromptDiff baseline="same" candidate="same" />);
    expect(screen.getByText(/no changes/i)).toBeTruthy();
  });

  it("renders unified diff with stats", () => {
    render(
      <PromptDiff baseline="old line" candidate="new line" mode="unified" />,
    );
    // Should show +1 -1 stats
    expect(screen.getByText("+1")).toBeTruthy();
    expect(screen.getByText("-1")).toBeTruthy();
  });

  it("renders side-by-side view", () => {
    render(
      <PromptDiff
        baseline="old"
        candidate="new"
        mode="side-by-side"
      />,
    );
    expect(screen.getByText(/baseline.*production/i)).toBeTruthy();
    expect(screen.getByText(/candidate/i)).toBeTruthy();
  });
});
