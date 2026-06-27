import { describe, expect, it } from "vitest";

import { workflowRunPath, workflowRunUrl } from "@/lib/workflowRunLinks";

describe("workflowRunLinks", () => {
  it("builds the canonical workflow run path", () => {
    expect(workflowRunPath("WR-123")).toBe("/workflow-runs/WR-123");
    expect(workflowRunPath(" WR-hello world ")).toBe(
      "/workflow-runs/WR-hello%20world",
    );
  });

  it("builds an absolute workflow run URL when an origin is provided", () => {
    expect(workflowRunUrl("WR-123", "https://caliber.example")).toBe(
      "https://caliber.example/workflow-runs/WR-123",
    );
  });

  it("falls back to the relative path when the origin is invalid", () => {
    expect(workflowRunUrl("WR-123", "not a valid origin")).toBe(
      "/workflow-runs/WR-123",
    );
  });
});
