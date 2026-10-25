import { describe, expect, it } from "vitest";

import type { WorkflowRun, WorkflowRunEvent } from "@/api/workflowTypes";
import {
  checkpointKindForEvent,
  coerceStep,
  comparableValue,
  deriveEventSteps,
  deriveSummarySteps,
  diffPorts,
  emptyDebuggerMessage,
  emptyEventTimelineMessage,
  emptyPortSnapshotMessage,
  formatEventTime,
  formatJson,
  hasPortSnapshot,
  initialLifecycleEvents,
  markerTone,
  portSummary,
  readNumber,
  readString,
  stepWindowEvents,
  summarizeEvent,
  summarizeLifecycleEvent,
  transitionEmptyMessage,
  transitionSubtitle,
} from "@/components/workflows/WorkflowRunDebugger";

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

const step = (overrides: Record<string, unknown> = {}) => ({
  node_id: "node-a",
  node_type: "agent",
  status: "ok",
  output: "answer",
  ...overrides,
});

describe("WorkflowRunDebugger helpers", () => {
  it("covers empty-state guidance for evidence and lifecycle states", () => {
    for (const status of ["queued", "running", "completed", "failed", "unknown"]) {
      expect(emptyDebuggerMessage([], [], status)).toContain("No recorded step details");
      expect(emptyDebuggerMessage([event()], [], status)).toContain("lifecycle event");
      expect(emptyDebuggerMessage([], [{ checkpoint_id: "cp" } as never], status)).toContain("checkpoint");
      for (const hasSelectedStep of [false, true]) {
        for (const hasCheckpoint of [false, true]) {
          expect(
            emptyEventTimelineMessage({ hasSelectedStep, hasCheckpoint, runStatus: status }),
          ).toContain("No persisted run events");
        }
      }
      expect(emptyPortSnapshotMessage("Input ports", status)).toContain("persisted");
      expect(transitionSubtitle(null, { node_id: "a" } as never, status)).toContain("Earliest");
      expect(transitionEmptyMessage(null, status)).toContain("previous");
    }
    expect(
      transitionSubtitle({ node_id: "previous" } as never, { node_id: "current" } as never, "running"),
    ).toContain("previous");
    expect(
      transitionEmptyMessage({ node_id: "previous" } as never, "completed"),
    ).toContain("same snapshot");
  });

  it("coerces summary and event steps while preserving optional telemetry", () => {
    expect(coerceStep(null, 0)).toBeNull();
    expect(coerceStep({ node_id: "a" }, 0)).toBeNull();
    const full = coerceStep(
      step({
        status: null,
        tokens: "3",
        prompt_tokens: 2,
        completion_tokens: "bad",
        cached_prompt_tokens: 1,
        cost_usd: 0.2,
        model: "model",
        prompt_version: "v2",
        tool_calls: [{ name: "lookup" }],
        handoff_target: "next",
        detail: "done",
        duration_ms: 12,
        input_by_port: { input: "x" },
        output_by_port: { output: "y" },
      }),
      2,
    );
    expect(full).toMatchObject({
      node_id: "node-a",
      status: "ok",
      tokens: 3,
      completion_tokens: undefined,
      selectionKey: "node-a:2",
    });
    expect(deriveSummarySteps({ summary: { steps: [step(), { invalid: true }] } } as unknown as WorkflowRun)).toHaveLength(1);
    expect(
      deriveEventSteps([
        event({ event_type: "workflow.run.step", payload: { step: step() } }),
        event({ event_id: 2, event_type: "workflow.run.step", payload: { step: { invalid: true } } }),
      ]),
    ).toHaveLength(1);
  });

  it("formats and summarizes lifecycle, tool, and checkpoint events", () => {
    expect(readString(" x ")).toBe(" x ");
    expect(readString(" ")).toBeNull();
    expect(readNumber(2)).toBe(2);
    expect(readNumber("3")).toBe(3);
    expect(readNumber("bad")).toBeNull();
    expect(formatEventTime(null)).toBe("n/a");
    expect(formatEventTime("invalid")).toBe("invalid");
    expect(formatJson({ ok: true })).toContain('"ok": true');
    const circular: Record<string, unknown> = {};
    circular.self = circular;
    expect(formatJson(circular)).toContain("[object Object]");

    expect(summarizeEvent(event({ event_type: "workflow.run.step", payload: { step: step({ detail: "detail" }) } }))).toBe("detail");
    expect(summarizeEvent(event({ event_type: "workflow.run.step", payload: { step: step({ detail: "", handoff_target: "next" }) } }))).toContain("handoff");
    expect(summarizeEvent(event({ event_type: "workflow.run.step", payload: { step: step({ detail: "", handoff_target: null, tool_calls: [{}, {}] }) } }))).toContain("tool calls");
    expect(summarizeEvent(event({ event_type: "workflow.run.step", payload: { step: step({ detail: "", handoff_target: null, tool_calls: [] }) } }))).toBe("ok");
    expect(summarizeEvent(event({ event_type: "workflow.run.waiting_event", payload: {} }))).toBeTruthy();
    expect(summarizeEvent(event({ event_type: "custom.event" }))).toBe("custom event");

    const waiting = event({ event_type: "workflow.run.waiting_approval", node_id: "review" });
    const checkpoints = [{ node_id: "review", sequence: 2, state_blob: { kind: "human_approval" } }] as never;
    expect(checkpointKindForEvent(waiting, checkpoints)).toBe("human_approval");
    expect(checkpointKindForEvent(event({ event_type: "workflow.run.waiting_approval" }), checkpoints)).toBeNull();
    expect(summarizeLifecycleEvent(waiting, checkpoints)).toBeTruthy();
  });

  it("covers port snapshots, diff categories, marker tones, and event windows", () => {
    expect(portSummary(null, "running")).toBe("Snapshot pending");
    expect(portSummary(null, "completed")).toBe("Snapshot unavailable");
    expect(portSummary(null, "unknown")).toBe("No snapshot recorded");
    expect(portSummary({}, "running")).toBe("0 ports");
    expect(portSummary({ a: 1 }, "running")).toContain("1 port");
    expect(portSummary({ a: 1, b: 2 }, "running")).toContain("2 ports");
    expect(hasPortSnapshot(null)).toBe(false);
    expect(hasPortSnapshot({})).toBe(true);
    expect(comparableValue({ a: 1 })).toBe('{"a":1}');
    const before = { same: 1, changed: 1, removed: 1 };
    const after = { same: 1, changed: 2, added: 1 };
    expect(diffPorts(before, after)).toEqual({ added: ["added"], removed: ["removed"], changed: ["changed"], unchanged: 1 });
    for (const kind of ["warn", "success", "info", "danger", "neutral"] as const) {
      expect(markerTone(kind)).toContain("border-");
    }

    const steps = [
      { stepIndex: 0, selectionKey: "a:0", node_id: "a" },
      { stepIndex: 1, selectionKey: "b:1", node_id: "b" },
    ] as never;
    const events = [
      event({ sequence: 1, event_type: "workflow.run.queued" }),
      event({ sequence: 2, event_type: "workflow.run.step", node_id: "a", payload: { step: step() } }),
      event({ sequence: 3, event_type: "workflow.run.completed", node_id: "a" }),
      event({ sequence: 4, event_type: "workflow.run.step", node_id: "b", payload: { step: step({ node_id: "b" }) } }),
    ];
    expect(initialLifecycleEvents(steps[0]!, [{ step: steps[0], event: events[1] }] as never, events)).toHaveLength(1);
    expect(initialLifecycleEvents(steps[1]!, [], events)).toEqual([]);
    expect(stepWindowEvents(steps[0]!, {
      stepEvent: { step: steps[0], event: events[1] },
      eventSteps: [{ step: steps[0], event: events[1] }, { step: steps[1], event: events[3] }] as never,
      events,
      totalSteps: 2,
    })).toHaveLength(1);
    expect(stepWindowEvents(steps[1]!, { stepEvent: null, eventSteps: [], events, totalSteps: 2 })).toEqual([]);
  });
});
