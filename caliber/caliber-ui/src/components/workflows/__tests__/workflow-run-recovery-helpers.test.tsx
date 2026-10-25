import { describe, expect, it } from "vitest";

import {
  approvalLoadErrorMessage,
  approvalPolicy,
  approvalRecordsEmptyMessage,
  artifactStatusLabel,
  artifactStatusTone,
  checkpointNodeIntegrityWarning,
  checkpointState,
  formatDeadline,
  formatTimestamp,
  formatValue,
  isRecord,
  noteTone,
  readPositiveNumber,
  readString,
  recoveryTimelineEmptyMessage,
  recoveryTimelineLoadErrorMessage,
  statusTone,
} from "@/components/workflows/WorkflowRunRecoveryPanel";
import { render } from "@/test/utils";

describe("WorkflowRunRecoveryPanel helpers", () => {
  it("normalizes scalar values, timestamps, deadlines, and checkpoint state", () => {
    expect(formatTimestamp(null)).toBe("n/a");
    expect(formatTimestamp("invalid")).toBe("invalid");
    expect(formatTimestamp("2026-01-01T00:00:00Z")).not.toBe("n/a");
    expect(isRecord({ ok: true })).toBe(true);
    expect(isRecord([])).toBe(false);
    expect(readString(" value ")).toBe(" value ");
    expect(readString(" ")).toBeNull();
    expect(readPositiveNumber(2)).toBe(2);
    expect(readPositiveNumber(" 3 ")).toBe(3);
    expect(readPositiveNumber(0)).toBeNull();
    expect(readPositiveNumber("bad")).toBeNull();
    expect(formatValue(null)).toBeNull();
    expect(formatValue(false)).toBe("false");
    expect(formatValue({ key: "value" })).toBe('{"key":"value"}');
    expect(formatDeadline(null, 10)).toBeNull();
    expect(formatDeadline("invalid", 10)).toBeNull();
    expect(formatDeadline("2026-01-01T00:00:00Z", 10)).not.toBeNull();
    expect(checkpointState(null)).toBeNull();
    expect(checkpointState({ state_blob: { kind: "wait_for_event" } } as never)).toEqual({
      kind: "wait_for_event",
    });
    expect(checkpointState({ state_blob: [] } as never)).toBeNull();
  });

  it("reports checkpoint identity conflicts and maps presentation tones", () => {
    expect(
      checkpointNodeIntegrityWarning({
        checkpointId: "CP-1",
        checkpointNodeId: "node-a",
        stateNodeId: "node-b",
        currentNodeId: "node-c",
      }),
    ).toContain("inconsistent node identity");
    expect(
      checkpointNodeIntegrityWarning({
        checkpointId: null,
        checkpointNodeId: null,
        stateNodeId: null,
        currentNodeId: null,
      }),
    ).toBeNull();
    for (const status of ["waiting_approval", "waiting_event", "running", "queued", "completed", "failed", "rejected", "other"]) {
      expect(statusTone(status)).toContain("bg-");
      expect(artifactStatusTone(status)).toContain("bg-");
      expect(artifactStatusLabel(status)).toBe(status === "persisted" ? "Persisted" : status === "failed" ? "Failed" : status);
    }
    expect(noteTone("info")).toContain("sky");
    expect(noteTone("warn")).toContain("amber");
    expect(noteTone("danger")).toContain("red");
    expect(approvalPolicy(null)).toEqual({
      requiredRole: null,
      approvalCount: null,
      timeoutBehavior: null,
    });
    expect(
      approvalPolicy({
        policy_snapshot: {
          required_role: "manager",
          approval_count: 0,
          timeout_behavior: "reject",
        },
      } as never),
    ).toEqual({ requiredRole: "manager", approvalCount: 1, timeoutBehavior: "reject" });
  });

  it("covers every lifecycle family for recovery and approval empty states", () => {
    for (const status of ["queued", "running", "resuming", "cancel_requested", "completed", "failed", "cancelled", "unknown"]) {
      for (const hasApprovals of [false, true]) {
        for (const hasCheckpoint of [false, true]) {
          expect(
            recoveryTimelineEmptyMessage({ status, hasApprovals, hasCheckpoint }),
          ).toContain("No recovery-specific lifecycle events");
          const empty = approvalRecordsEmptyMessage({
            status,
            approvalKind: null,
            hasCheckpoint,
            hasHistoricalApprovals: hasApprovals,
          });
          expect(empty).toContain("No runtime approval records");
        }
      }
      expect(
        render(
          <>{recoveryTimelineLoadErrorMessage({
            status,
            hasApprovals: true,
            hasCheckpoint: true,
            errorMessage: status === "unknown" ? " " : "storage unavailable",
          })}</>,
        ).container,
      ).toHaveTextContent("recovery event error");
      expect(
        render(<>{approvalLoadErrorMessage(status, " ")}</>).container,
      ).toHaveTextContent("approval error");
    }
  });
});
