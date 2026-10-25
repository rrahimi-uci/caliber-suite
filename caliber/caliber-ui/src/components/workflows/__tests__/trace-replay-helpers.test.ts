import { describe, expect, it } from "vitest";

import type { WorkflowRun, WorkflowRunEvent } from "@/api/workflowTypes";
import {
  buildTraceEventSteps,
  buildTracePathEntries,
  checkpointResumeDetail,
  compactNodeType,
  formatTraceDuration,
  initialLifecycleEvents,
  normalizeTraceStatus,
  recoveryMarkerTone,
  stepRecoveryEvents,
} from "@/components/workflows/TraceReplayGraph";

const event = (overrides: Partial<WorkflowRunEvent> = {}): WorkflowRunEvent => ({
  event_id: 1,
  workflow_run_id: "run-1",
  project_id: null,
  sequence: 1,
  event_type: "workflow.run.started",
  node_id: null,
  payload: null,
  created_at: "2026-01-01T00:00:00Z",
  ...overrides,
});

describe("TraceReplayGraph helpers", () => {
  it("maps path entries with and without a recorded node path", () => {
    const run = {
      summary: {
        node_path: [],
        steps: [
          { node_id: "a", status: "ok" },
          { node_id: "b", status: "error" },
        ],
      },
    } as unknown as WorkflowRun;
    expect(buildTracePathEntries(run).map((entry) => entry.nodeId)).toEqual(["a", "b"]);
    expect(
      buildTracePathEntries({
        summary: {
          node_path: ["a", "missing", "a"],
          steps: [{ node_id: "a", status: "ok" }],
        },
      } as unknown as WorkflowRun),
    ).toMatchObject([
      { nodeId: "a", stepIndex: 0 },
      { nodeId: "missing", step: null, stepIndex: null },
      { nodeId: "a", step: null, stepIndex: null },
    ]);
  });

  it("normalizes status, duration, node labels, and marker tones", () => {
    expect(normalizeTraceStatus({ status: "custom" } as never, { runStatus: "failed", index: 0, total: 1 })).toBe("custom");
    expect(normalizeTraceStatus(null, { runStatus: "completed", index: 0, total: 1 })).toBe("ok");
    expect(normalizeTraceStatus(null, { runStatus: "blocked", index: 0, total: 1 })).toBe("blocked");
    expect(normalizeTraceStatus(null, { runStatus: "failed", index: 0, total: 1 })).toBe("error");
    expect(normalizeTraceStatus(null, { runStatus: "running", index: 0, total: 1 })).toBe("ok");
    expect(formatTraceDuration(undefined)).toBeNull();
    expect(formatTraceDuration(-1)).toBeNull();
    expect(formatTraceDuration(12.4)).toBe("12 ms");
    expect(formatTraceDuration(1500)).toBe("1.5 s");
    expect(formatTraceDuration(12000)).toBe("12 s");
    expect(compactNodeType("wait_for_event")).toBeTruthy();
    expect(compactNodeType("custom_node")).toBe("custom node");
    for (const kind of ["warn", "success", "info", "danger", "neutral"] as const) {
      expect(recoveryMarkerTone(kind)).toContain("border-");
    }
  });

  it("extracts step events and recovery windows without crossing neighboring steps", () => {
    const events = [
      event({ sequence: 1, event_type: "workflow.run.queued" }),
      event({ sequence: 2, event_type: "workflow.run.step", payload: { step: { node_id: "a" } } }),
      event({ sequence: 3, event_type: "workflow.run.waiting_event", node_id: "a" }),
      event({ sequence: 4, event_type: "workflow.run.step", payload: { step: { node_id: "b" } } }),
      event({ sequence: 5, event_type: "workflow.run.completed" }),
      event({ sequence: 6, event_type: "workflow.run.step", payload: { malformed: true } }),
    ];
    const steps = buildTraceEventSteps(events);
    expect(steps).toEqual([
      { nodeId: "a", stepIndex: 0, sequence: 2 },
      { nodeId: "b", stepIndex: 1, sequence: 4 },
    ]);
    expect(initialLifecycleEvents({ pathIndex: 0, eventSteps: steps, events })).toHaveLength(1);
    expect(initialLifecycleEvents({ pathIndex: 1, eventSteps: steps, events })).toEqual([]);
    expect(stepRecoveryEvents({ stepIndex: 0, nodeId: "a", eventSteps: steps, events })).toHaveLength(1);
    expect(stepRecoveryEvents({ stepIndex: null, nodeId: "a", eventSteps: steps, events })).toHaveLength(2);
    expect(stepRecoveryEvents({ stepIndex: 4, nodeId: "a", eventSteps: steps, events })).toHaveLength(2);
  });

  it("formats checkpoint resume targets with optional timezone details", () => {
    expect(checkpointResumeDetail(null)).toBeNull();
    expect(checkpointResumeDetail({ state_blob: { wait_until: "2026-01-01" } } as never)).toBe("2026-01-01");
    expect(
      checkpointResumeDetail({ state_blob: { resume_at: "2026-01-01", timezone: "UTC" } } as never),
    ).toBe("2026-01-01 (UTC)");
    expect(checkpointResumeDetail({ state_blob: { kind: "approval" } } as never)).toBeNull();
  });
});
