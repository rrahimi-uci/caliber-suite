import { describe, expect, it } from "vitest";

import type { WorkflowRun } from "@/api/workflowTypes";
import {
  checkpointIntegrityWarning,
  checkpointLoadErrorMessage,
  emptyCheckpointMessage,
  formatDeadline,
  formatJson,
  formatTimestamp,
  formatValue,
  inheritedCheckpointUnavailableMessage,
  isActiveRunStatus,
  isRecord,
  isStoppedRunStatus,
  portSummary,
  readPositiveNumber,
  readString,
  selectedCardClasses,
  stateBlob,
  summaryCheckpointId,
  summaryCheckpointRunId,
  summaryRetryMode,
} from "@/components/workflows/WorkflowRunCheckpointPanel";
import { render } from "@/test/utils";

const run = (status: string, overrides: Record<string, unknown> = {}) => ({
  workflow_run_id: "run-1",
  status,
  parent_run_id: null,
  summary: null,
  ...overrides,
}) as unknown as WorkflowRun;

describe("WorkflowRunCheckpointPanel helpers", () => {
  it("normalizes checkpoint values and summary references", () => {
    expect(isActiveRunStatus("running")).toBe(true);
    expect(isActiveRunStatus("completed")).toBe(false);
    expect(isStoppedRunStatus("failed")).toBe(true);
    expect(isStoppedRunStatus("running")).toBe(false);
    expect(formatTimestamp(null)).toBe("n/a");
    expect(formatTimestamp("invalid")).toBe("invalid");
    expect(formatTimestamp("2026-01-01T00:00:00Z")).not.toBe("n/a");
    expect(formatJson({ ok: true })).toContain('"ok": true');
    expect(isRecord({ ok: true })).toBe(true);
    expect(isRecord([])).toBe(false);
    expect(readString("x")).toBe("x");
    expect(readString(" ")).toBeNull();
    expect(readPositiveNumber(2)).toBe(2);
    expect(readPositiveNumber("3")).toBe(3);
    expect(readPositiveNumber(0)).toBeNull();
    expect(formatValue(false)).toBe("false");
    expect(formatValue(null)).toBeNull();
    expect(formatDeadline(null, 1)).toBeNull();
    expect(formatDeadline("invalid", 1)).toBeNull();
    expect(formatDeadline("2026-01-01T00:00:00Z", 1)).not.toBeNull();
    expect(portSummary(null)).toBeNull();
    expect(portSummary({})).toBe("0 ports");
    expect(portSummary({ a: 1 })).toBe("1 port: a");
    expect(stateBlob(null)).toBeNull();
    expect(stateBlob({ state_blob: { kind: "wait_for_event" } } as never)).toEqual({ kind: "wait_for_event" });
    expect(summaryCheckpointId(run("running", { summary: { resume_checkpoint_id: "CP-1" } }))).toBe("CP-1");
    expect(summaryCheckpointRunId(run("running", { summary: { resume_checkpoint_run_id: "run-0" } }))).toBe("run-0");
    expect(summaryRetryMode(run("running", { summary: { retry_mode: "checkpoint" } }))).toBe("checkpoint");
    expect(selectedCardClasses(true)).toContain("sky");
    expect(selectedCardClasses(false)).toContain("white");
  });

  it("reports identity conflicts and every checkpoint-empty lifecycle family", () => {
    expect(checkpointIntegrityWarning({
      checkpointId: "CP-1",
      checkpointNodeId: "a",
      stateNodeId: "b",
      currentNodeId: "c",
      active: false,
    })).toBeNull();
    expect(checkpointIntegrityWarning({
      checkpointId: "CP-1",
      checkpointNodeId: "a",
      stateNodeId: "b",
      currentNodeId: "c",
      active: true,
    })).toContain("inconsistent node identity");
    for (const status of ["queued", "running", "waiting_approval", "waiting_event", "completed", "failed", "unknown"]) {
      expect(emptyCheckpointMessage(run(status))).toContain("No persisted checkpoints");
      expect(emptyCheckpointMessage(run(status, { parent_run_id: "run-0" }))).toContain("No persisted checkpoints");
      expect(
        render(<>{checkpointLoadErrorMessage(status, " ")}</>).container,
      ).toHaveTextContent("checkpoint error");
      expect(
        render(<>{inheritedCheckpointUnavailableMessage({
          checkpointId: "CP-1",
          checkpointRunId: "run-0",
          retryMode: status === "queued" ? "checkpoint" : null,
          runStatus: status,
        })}</>).container,
      ).toHaveTextContent("inherited checkpoint");
    }
  });
});
