import { describe, expect, it } from "vitest";

import type { WorkflowRun } from "@/api/workflowTypes";
import {
  cardClasses,
  emptyLineageMessage,
  formatTimestamp,
  isActiveRunStatus,
  isStoppedRunStatus,
  lineageLoadErrorMessage,
  missingParentLineageMessage,
  resolveFallbackLineage,
  sortLineageRuns,
  truncatedLineageMessage,
} from "@/components/workflows/WorkflowRunLineagePanel";
import { render } from "@/test/utils";

const run = (
  id: string,
  status = "running",
  overrides: Partial<WorkflowRun> = {},
): WorkflowRun => ({
  workflow_run_id: id,
  workflow_id: "wf-1",
  project_id: null,
  tenant_id: null,
  workflow_version_id: null,
  deployment_alias: null,
  mlflow_run_id: null,
  trace_id: null,
  session_id: null,
  status,
  source: null,
  priority: null,
  queued_at: "2026-01-01T00:00:00Z",
  started_at: null,
  completed_at: null,
  attempt_number: 1,
  parent_run_id: null,
  summary: null,
  ...overrides,
});

describe("WorkflowRunLineagePanel helpers", () => {
  it("classifies lifecycle statuses and renders card variants", () => {
    for (const status of ["queued", "running", "resuming", "cancel_requested"]) {
      expect(isActiveRunStatus(status)).toBe(true);
    }
    for (const status of ["failed", "cancelled", "rejected", "expired", "blocked"]) {
      expect(isStoppedRunStatus(status)).toBe(true);
    }
    expect(isActiveRunStatus("completed")).toBe(false);
    expect(isStoppedRunStatus("waiting_event")).toBe(false);

    expect(cardClasses(true, true)).toContain("border-sky");
    expect(cardClasses(false, true)).toContain("hover:border");
    expect(cardClasses(true, false)).not.toContain("hover:border");
    expect(cardClasses(false, false)).toContain("border-slate");
  });

  it("formats timestamps and sorts attempts by attempt, time, and id", () => {
    expect(formatTimestamp(run("none", "running", {
      queued_at: null,
      started_at: null,
      completed_at: null,
    }))).toBe("n/a");
    expect(formatTimestamp(run("invalid", "running", {
      queued_at: "not-a-date",
    }))).toBe("not-a-date");
    expect(formatTimestamp(run("valid", "completed", {
      completed_at: "2026-01-01T12:00:00Z",
    }))).not.toBe("n/a");

    const sorted = sortLineageRuns([
      run("same-b", "running", { attempt_number: 2, queued_at: "2026-01-02" }),
      run("attempt-one", "running", { attempt_number: 0, queued_at: "2026-01-03" }),
      run("same-a", "running", { attempt_number: 2, queued_at: "2026-01-02" }),
      run("completed-time", "completed", {
        attempt_number: 3,
        queued_at: null,
        started_at: null,
        completed_at: "2026-01-04",
      }),
      run("empty-time", "completed", {
        attempt_number: 4,
        queued_at: null,
        started_at: null,
        completed_at: null,
      }),
    ]);
    expect(sorted.map((item) => item.workflow_run_id)).toEqual([
      "attempt-one",
      "same-a",
      "same-b",
      "completed-time",
      "empty-time",
    ]);
  });

  it("reconstructs connected fallback lineage, missing parents, and cycles", () => {
    const root = run("root", "completed", { attempt_number: 1 });
    const current = run("current", "failed", {
      attempt_number: 2,
      parent_run_id: "root",
    });
    const child = run("child", "queued", {
      attempt_number: 3,
      parent_run_id: "current",
    });
    const resolved = resolveFallbackLineage(current, [root, child]);
    expect(resolved).toMatchObject({
      rootRunId: "root",
      totalAttempts: 3,
      parentCount: 1,
      childCount: 1,
      missingParentId: null,
      truncated: false,
    });
    expect(resolved.lineageRuns.map((item) => item.workflow_run_id)).toEqual([
      "root",
      "current",
      "child",
    ]);

    const missing = resolveFallbackLineage(
      run("orphan", "running", { parent_run_id: "outside-history" }),
      [],
    );
    expect(missing).toMatchObject({
      rootRunId: "orphan",
      parentCount: 0,
      missingParentId: "outside-history",
      totalAttempts: 1,
    });

    const cycleA = run("cycle-a", "running", { parent_run_id: "cycle-b" });
    const cycleB = run("cycle-b", "running", { parent_run_id: "cycle-a" });
    const cyclic = resolveFallbackLineage(cycleA, [cycleB]);
    expect(cyclic.rootRunId).toBe("cycle-b");
    expect(cyclic.lineageRuns).toHaveLength(2);
  });

  it("covers empty-lineage messaging for every run lifecycle family", () => {
    const statuses = [
      "queued",
      "running",
      "resuming",
      "cancel_requested",
      "waiting_approval",
      "waiting_event",
      "failed",
      "cancelled",
      "rejected",
      "expired",
      "blocked",
      "completed",
      "unknown",
    ];
    for (const status of statuses) {
      expect(emptyLineageMessage(run(status, status))).toContain("No retries");
      expect(missingParentLineageMessage(run(`missing-${status}`, status), "parent-1")).toContain(
        "partial",
      );
      expect(truncatedLineageMessage(run(`truncated-${status}`, status))).toContain(
        "longer than the current panel limit",
      );
    }
  });

  it("maps lineage load errors to active, paused, completed, stopped, and fallback copy", () => {
    const cases = [
      ["running", "still active"],
      ["waiting_approval", "paused run"],
      ["completed", "completed run"],
      ["failed", "stopped run"],
      ["unknown", "for this run"],
      [null, "for this run"],
    ] as const;
    for (const [status, expected] of cases) {
      const message = render(
        <>{lineageLoadErrorMessage(status, status === "unknown" ? " " : "backend unavailable")}</>,
      );
      expect(message.container).toHaveTextContent(expected);
      expect(message.container).toHaveTextContent(
        status === "unknown" ? "Unknown error" : "backend unavailable",
      );
    }
  });
});
