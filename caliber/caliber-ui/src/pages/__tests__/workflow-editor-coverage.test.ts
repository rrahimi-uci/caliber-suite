import { describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/caliberApi";
import type { WorkflowRun, WorkflowRunEvent } from "@/api/workflowTypes";
import { NODE_PALETTE } from "@/lib/workflowGraph";
import {
  approvalCapabilityMessage,
  checkpointWaitUntilDisplay,
  clearLayout,
  compareRunEvents,
  defaultFocusedRunNode,
  dropSyntheticTwins,
  emptyEditorRunHistoryMessage,
  formatResumeEventPayload,
  mergeRunEvents,
  normalizeRunStep,
  normalizeWorkflowManifest,
  parseJsonObjectText,
  readNumber,
  readString,
  reconcileRunSnapshot,
  runManifestContextLabel,
  runMonitorIdleSectionMessage,
  runMonitorManifestLoadErrorMessage,
  runMonitorManifestUnavailableMessage,
  runOutputEmptyMessage,
  runActionErrorDetail,
  runEventFingerprint,
  runEventTwinKey,
  runSortKey,
  runSummarySteps,
  runTimestamp,
  readLayout,
  persistLayout,
  layoutStorageKey,
  savedVersionRunMonitorGraphFallbackMessage,
  savedWorkflowVersionLabel,
  starterNodeFromComponent,
  snapshotRunMonitorContextNote,
  historicalVersionRunMonitorContextNote,
  hasStructuredAgentOutputPort,
  materializeStarterNodeValue,
  newNode,
  syntheticRunMonitorGraphFallbackMessage,
  waitUntilDisplay,
  workflowRunApprovalActionFailureMessage,
  workflowRunCheckpointIdentityIssue,
  workflowRunEventNodeId,
  workflowRunEventPayload,
  workflowRunEventCursor,
  workflowRunResumeByEventFailureMessage,
  workflowRunResumeByEventIssue,
  workflowRunResumeEventNameIssue,
  workflowRunResumeFailureMessage,
  workflowRunResumeCorrelationMatches,
  workflowRunResumeCorrelationValueLabel,
  workflowRunRetryFailureMessage,
} from "@/pages/WorkflowEditor";

const baseRun = (status = "running"): WorkflowRun => ({
  workflow_run_id: "run-1",
  workflow_id: "wf-1",
  project_id: null,
  tenant_id: null,
  workflow_version_id: "version-1",
  deployment_alias: "manual",
  mlflow_run_id: null,
  trace_id: null,
  session_id: null,
  status,
  source: "editor",
  priority: 0,
  queued_at: "2026-01-01T00:00:00Z",
  started_at: "2026-01-01T00:00:01Z",
  completed_at: null,
  current_node_id: "agent",
  summary: null,
});

const event = (overrides: Partial<WorkflowRunEvent> = {}): WorkflowRunEvent => ({
  event_id: 1,
  workflow_run_id: "run-1",
  project_id: null,
  sequence: 1,
  event_type: "workflow.run.started",
  node_id: null,
  payload: null,
  created_at: "2026-01-01T00:00:01Z",
  ...overrides,
});

describe("WorkflowEditor decision helpers", () => {
  it("normalizes wait and structured-agent output ports without changing clean manifests", () => {
    const clean = {
      nodes: {
        agent: {
          id: "agent",
          type: "agent",
          output_type: "structured",
          outputs: { answer: { type: "structured" } },
        },
      },
    } as never;
    expect(normalizeWorkflowManifest(clean)).toBe(clean);

    const normalized = normalizeWorkflowManifest({
      nodes: {
        wait: { id: "wait", type: "wait_for_event", outputs: {} },
        agent: { id: "agent", type: "agent", output_type: "structured" },
      },
    } as never);
    expect(normalized.nodes.wait.outputs).toEqual({
      output: { type: "string" },
      event_payload: { type: "structured" },
      event_name: { type: "string" },
    });
    expect(normalized.nodes.agent.outputs).toEqual({
      structured_output: { type: "structured" },
    });
  });

  it("covers parsing, scalar formatting, and correlation comparisons", () => {
    expect(parseJsonObjectText("  {\"id\": 1} ")).toEqual({ id: 1 });
    expect(parseJsonObjectText("  ")).toBeUndefined();
    expect(() => parseJsonObjectText("not-json")).toThrow();
    expect(readString(" value ")).toBe(" value ");
    expect(readString(" ")).toBeNull();
    expect(readString(3)).toBeNull();
    expect(readNumber(3)).toBe(3);
    expect(readNumber(" 4.5 ")).toBe(4.5);
    expect(readNumber("nope")).toBeNull();
    expect(readNumber(null)).toBeNull();
    expect(formatResumeEventPayload(undefined)).toBe("");
    expect(formatResumeEventPayload("hello")).toBe("hello");
    expect(formatResumeEventPayload({ ok: true })).toContain('"ok": true');
    expect(workflowRunResumeCorrelationMatches(1, 1)).toBe(true);
    expect(workflowRunResumeCorrelationMatches({ a: 1 }, { a: 1 })).toBe(true);
    expect(workflowRunResumeCorrelationMatches({ a: 1 }, { a: 2 })).toBe(false);
    expect(workflowRunResumeCorrelationMatches(1, "1")).toBe(false);
    expect(workflowRunResumeCorrelationValueLabel("x")).toBe("x");
    expect(workflowRunResumeCorrelationValueLabel(null)).toBe("null");
    expect(workflowRunResumeCorrelationValueLabel(undefined)).toBe("undefined");
    expect(workflowRunResumeCorrelationValueLabel({ a: 1 })).toBe('{"a":1}');
  });

  it("reports every checkpoint and event-resume mismatch class", () => {
    expect(workflowRunCheckpointIdentityIssue(null, null)).toBeNull();
    expect(workflowRunCheckpointIdentityIssue(baseRun("completed"), null)).toBeNull();
    const approvalIssue = workflowRunCheckpointIdentityIssue(
      baseRun("waiting_approval"),
      { node_id: "other", state_blob: { kind: "bad" } } as never,
    );
    expect(approvalIssue).toContain("checkpoint payload has no node_id");
    expect(approvalIssue).toContain("approval checkpoint kind is invalid");
    expect(approvalIssue).toContain("no input snapshot");

    const eventIssue = workflowRunCheckpointIdentityIssue(
      baseRun("waiting_event"),
      { node_id: "other", state_blob: { kind: "wait_for_event", input_by_port: {} } } as never,
    );
    expect(eventIssue).toContain("checkpoint row points at other");
    expect(eventIssue).toContain("expected event name");
    expect(workflowRunCheckpointIdentityIssue(
      { ...baseRun("waiting_event"), current_node_id: null },
      { node_id: "agent", state_blob: null } as never,
    )).toContain("payload is corrupt");

    expect(workflowRunResumeEventNameIssue({
      status: "completed", waitMode: "wait_for_event", expectedEventName: "x", resumeEventName: "y",
    })).toBeNull();
    expect(workflowRunResumeEventNameIssue({
      status: "waiting_event", waitMode: "wait_for_event", expectedEventName: " ", resumeEventName: "y",
    })).toBeNull();
    expect(workflowRunResumeEventNameIssue({
      status: "waiting_event", waitMode: "wait_for_event", expectedEventName: "x", resumeEventName: "y",
    })).toContain("configured for event x");

    const args = {
      status: "waiting_event",
      waitMode: "wait_for_event",
      correlationKey: "ticket_id",
      correlationValue: "T-1",
    };
    expect(workflowRunResumeByEventIssue({ ...args, correlationKey: "", resumeEventPayload: "{}" })).toBeNull();
    expect(workflowRunResumeByEventIssue({ ...args, waitMode: "wait_event", resumeEventPayload: "{}" })).toContain("legacy wait_event");
    expect(workflowRunResumeByEventIssue({ ...args, correlationValue: null, resumeEventPayload: "{}" })).toContain("did not capture");
    expect(workflowRunResumeByEventIssue({ ...args, resumeEventPayload: "bad" })).toBeNull();
    expect(workflowRunResumeByEventIssue({ ...args, resumeEventPayload: "[]" })).toContain("requires correlation field");
    expect(workflowRunResumeByEventIssue({ ...args, resumeEventPayload: '{"ticket_id":"T-2"}' })).toContain("Add that field/value");
    expect(workflowRunResumeByEventIssue({ ...args, resumeEventPayload: '{"ticket_id":"T-1"}' })).toBeNull();
  });

  it("formats run-monitor context and empty-state messages for each state family", () => {
    expect(waitUntilDisplay(null)).toBe("the configured time");
    expect(waitUntilDisplay({ type: "agent" } as never)).toBe("the configured time");
    expect(waitUntilDisplay({ type: "wait_until", wait_until: "2026-01-01", timezone: "UTC" } as never)).toContain("UTC");
    expect(waitUntilDisplay({ type: "wait_until", wait_until: "2026-01-01Z" } as never)).toBe("2026-01-01Z");
    expect(checkpointWaitUntilDisplay(null)).toBeNull();
    expect(checkpointWaitUntilDisplay({ resume_at: "2026-01-01", timezone: "UTC" })).toContain("UTC");
    expect(checkpointWaitUntilDisplay({ wait_until: "2026-01-01Z" })).toBe("2026-01-01Z");
    expect(savedWorkflowVersionLabel(" v1 ", 2)).toBe("v2 (v1)");
    expect(savedWorkflowVersionLabel("", 2)).toBe("v2");
    expect(savedWorkflowVersionLabel("v1", null)).toBe("v1");
    expect(runManifestContextLabel(baseRun(), { manifest_mode: "snapshot" }, "version-1", null)).toContain("draft snapshot");
    expect(runManifestContextLabel(baseRun(), { manifest_mode: "saved_version" }, "other", null)).toContain("version-1");
    expect(runManifestContextLabel(baseRun(), { manifest_mode: "saved_version" }, "version-1", null)).toContain("saved workflow");

    for (const status of ["waiting_event", "running", "completed", "failed", "unknown"]) {
      expect(runMonitorManifestLoadErrorMessage({
        run: baseRun(status), manifestMode: "saved_version", versionReference: "v1", errorDetail: "  ",
      })).toBeTruthy();
      expect(syntheticRunMonitorGraphFallbackMessage(baseRun(status))).toContain("Replay");
      expect(savedVersionRunMonitorGraphFallbackMessage(baseRun(status), "v1")).toContain("Replay");
    }
    expect(runMonitorManifestUnavailableMessage({ manifestMode: "snapshot", hasSummarySteps: false, hasCurrentNode: false, hasCheckpoints: false })).toContain("draft snapshot");
    expect(runMonitorManifestUnavailableMessage({ manifestMode: "saved_version", hasSummarySteps: true, hasCurrentNode: false, hasCheckpoints: false })).toContain("recorded run history");
    expect(approvalCapabilityMessage("runtime approval")).toContain("disabled");
    expect(runOutputEmptyMessage(null)).toContain("Run a workflow");
    for (const status of ["waiting_event", "running", "failed", "completed", "unknown"]) {
      expect(runOutputEmptyMessage(baseRun(status))).toContain("run");
    }
    for (const section of ["recovery", "lineage", "trace_replay", "debugger", "files", "checkpoints"] as const) {
      expect(runMonitorIdleSectionMessage(section)).toContain("Run a workflow");
    }
    expect(emptyEditorRunHistoryMessage({ capabilitiesUnavailable: true, queueRunsEnabled: true, workflowStatus: null, versionStatus: null })).toContain("could not be loaded");
    expect(emptyEditorRunHistoryMessage({ capabilitiesUnavailable: false, queueRunsEnabled: false, workflowStatus: null, versionStatus: null })).toContain("disabled");
    expect(emptyEditorRunHistoryMessage({ capabilitiesUnavailable: false, queueRunsEnabled: true, workflowStatus: "paused", versionStatus: null })).toContain("paused");
    expect(emptyEditorRunHistoryMessage({ capabilitiesUnavailable: false, queueRunsEnabled: true, workflowStatus: "archived", versionStatus: null })).toContain("archived");
    expect(emptyEditorRunHistoryMessage({ capabilitiesUnavailable: false, queueRunsEnabled: true, workflowStatus: null, versionStatus: "draft" })).toContain("draft");
    expect(emptyEditorRunHistoryMessage({ capabilitiesUnavailable: false, queueRunsEnabled: true, workflowStatus: null, versionStatus: "published" })).toContain("published");
  });

  it("covers every run-monitor status family and optional-input fallback", () => {
    const statuses = [
      "waiting_approval",
      "waiting_event",
      "queued",
      "running",
      "resuming",
      "cancel_requested",
      "completed",
      "failed",
      "cancelled",
      "rejected",
      "expired",
      "blocked",
      "unknown",
    ];
    for (const status of statuses) {
      const run = baseRun(status);
      expect(syntheticRunMonitorGraphFallbackMessage(run)).toContain("Replay");
      expect(savedVersionRunMonitorGraphFallbackMessage(run, "v4")).toContain("v4");
      expect(snapshotRunMonitorContextNote(run)).toContain("snapshot");
      expect(historicalVersionRunMonitorContextNote(run, "v4")).toContain("v4");
      expect(runMonitorManifestLoadErrorMessage({
        run,
        manifestMode: status === "unknown" ? null : "snapshot",
        versionReference: "v4",
        errorDetail: status === "unknown" ? "  " : "backend detail",
      })).toBeTruthy();
      expect(runOutputEmptyMessage(run)).toContain("run");
    }
    expect(waitUntilDisplay({ type: "wait_until", wait_until: "2026-01-01", timezone: " America/Los_Angeles " } as never)).toContain("America/Los_Angeles");
    expect(waitUntilDisplay({ type: "wait_until", wait_until: "  ", timezone: " " } as never)).toBe("the configured time");
    expect(checkpointWaitUntilDisplay({ wait_until: "2026-01-01", timezone: "America/Los_Angeles" })).toContain("America/Los_Angeles");
    expect(checkpointWaitUntilDisplay({ wait_until: "  ", resume_at: "  " })).toBeNull();

    const validApproval = workflowRunCheckpointIdentityIssue(
      { status: "waiting_approval", current_node_id: "approval" },
      {
        checkpoint_id: "cp",
        node_id: "approval",
        state_blob: { node_id: "approval", kind: "runtime_approval", input_by_port: {} },
      } as never,
    );
    expect(validApproval).toBeNull();
    const validWait = workflowRunCheckpointIdentityIssue(
      { status: "waiting_event", current_node_id: "wait" },
      {
        checkpoint_id: "cp",
        node_id: "wait",
        state_blob: { node_id: "wait", kind: "wait_until", input_by_port: {} },
      } as never,
    );
    expect(validWait).toBeNull();
  });

  it("covers circular formatting and starter-node fallback branches", () => {
    const circular: Record<string, unknown> = {};
    circular.self = circular;
    expect(formatResumeEventPayload(circular)).toContain("[object Object]");
    expect(workflowRunResumeCorrelationMatches(circular, { different: true })).toBe(false);
    expect(workflowRunResumeCorrelationValueLabel(circular)).toContain("[object Object]");
    expect(starterNodeFromComponent("agent", "agent", null)).toBeNull();
    expect(starterNodeFromComponent("agent", "agent", {
      type: "tool",
      starter_node: { id: "__CALIBER_NODE_ID__", type: "tool" },
    } as never)).toBeNull();
    expect(starterNodeFromComponent("agent", "agent", {
      type: "agent",
      starter_node: "not-an-object",
    } as never)).toBeNull();
    expect(hasStructuredAgentOutputPort()).toBe(false);
    expect(hasStructuredAgentOutputPort({ history: { type: "structured" }, tool_calls: { type: "structured" } })).toBe(false);
    expect(hasStructuredAgentOutputPort({ answer: { type: "string" } })).toBe(false);
  });

  it("covers draft and historical snapshot context notes plus layout persistence", () => {
    for (const status of ["waiting_approval", "waiting_event", "running", "completed", "failed", "unknown"]) {
      expect(snapshotRunMonitorContextNote(baseRun(status))).toContain("snapshot");
      expect(historicalVersionRunMonitorContextNote(baseRun(status), "v3")).toContain("v3");
    }
    expect(layoutStorageKey("v1")).toContain("v1");
    window.localStorage.clear();
    expect(readLayout("v1")).toEqual({});
    persistLayout("v1", { node: { x: 10, y: 20 } });
    expect(readLayout("v1")).toEqual({ node: { x: 10, y: 20 } });
    window.localStorage.setItem(layoutStorageKey("v1"), JSON.stringify({
      good: { x: 1, y: 2 }, bad: { x: "x", y: 2 }, scalar: 3,
    }));
    expect(readLayout("v1")).toEqual({ good: { x: 1, y: 2 } });
    window.localStorage.setItem(layoutStorageKey("v1"), "not-json");
    expect(readLayout("v1")).toEqual({});
    clearLayout("v1");
    expect(readLayout("v1")).toEqual({});
  });

  it("maps API failures to safe operator messages", () => {
    const api = (message: string) => new ApiError(409, message, null);
    expect(runActionErrorDetail(api("boom"))).toBe("boom");
    expect(runActionErrorDetail("plain")).toBe("plain");
    expect(workflowRunRetryFailureMessage(api("workflow run retry checkpoint is corrupt"))).toContain("Retry failed");
    expect(workflowRunRetryFailureMessage(api("unrelated"))).toBeNull();
    for (const text of ["has no pending runtime approvals", "is not waiting for approval", "approval checkpoint is invalid", "unrelated"]) {
      const result = workflowRunApprovalActionFailureMessage("Approve", api(text));
      expect(result === null || result.includes("failed")).toBe(true);
    }
    expect(workflowRunApprovalActionFailureMessage("Reject", api("runtime approval not found for workflow run"))).toContain("Reject failed");
    expect(workflowRunResumeFailureMessage(api("cannot resume after runtime approval rejection"))).toContain("Resume failed");
    expect(workflowRunResumeFailureMessage(api("has no resume checkpoint"))).toContain("Resume failed");
    expect(workflowRunResumeFailureMessage(api("unrelated"))).toBeNull();
    expect(workflowRunResumeByEventFailureMessage(api("matched multiple waiting workflow runs"))).toContain("External event resume failed");
    expect(workflowRunResumeByEventFailureMessage(api("unrelated"))).toBeNull();
  });

  it("normalizes steps, reconciles events, and removes synthetic twins", () => {
    expect(normalizeRunStep(null)).toBeNull();
    expect(normalizeRunStep({ node_id: "x" })).toBeNull();
    expect(normalizeRunStep({ node_id: "x", node_type: "tool", input_by_port: [], output_by_port: { ok: true } })).toMatchObject({
      status: "ok", input_by_port: null, output_by_port: { ok: true }, tool_calls: [], duration_ms: 0,
    });
    const stored = event({ event_id: 10, event_type: "workflow.run.step", node_id: "agent" });
    const synthetic = event({ event_id: -1, event_type: "workflow.run.step", node_id: "agent" });
    expect(dropSyntheticTwins([synthetic, stored])).toEqual([stored]);
    expect(dropSyntheticTwins([synthetic])).toEqual([synthetic]);
    expect(mergeRunEvents([], [stored])[0]).toBe(stored);
    expect(mergeRunEvents([stored], [])).toEqual([stored]);
    expect(mergeRunEvents([stored], [stored])).toEqual([stored]);
    const next = reconcileRunSnapshot(baseRun("running"), [event({ event_type: "workflow.run.completed", node_id: "done" })]);
    expect(next.status).toBe("completed");
    expect(next.current_node_id).toBe("done");
    expect(reconcileRunSnapshot(baseRun(), [])).toBeInstanceOf(Object);
    expect(defaultFocusedRunNode(null)).toBeNull();
    expect(defaultFocusedRunNode({ ...baseRun(), current_node_id: "agent" })).toBe("agent");
    expect(defaultFocusedRunNode({ ...baseRun(), current_node_id: null, summary: { node_path: ["start", "done"] } })).toBe("done");
    expect(defaultFocusedRunNode({ ...baseRun(), current_node_id: null, summary: { node_path: [], steps: [{ node_id: "step", node_type: "tool" }] as never } })).toBe("step");
    expect(runSummarySteps(null)).toEqual([]);
    expect(runSummarySteps({ ...baseRun(), summary: { steps: [{ node_id: "valid", node_type: "tool" }, null] as never } })).toHaveLength(1);
    expect(compareRunEvents(event({ sequence: 1, event_id: 1 }), event({ sequence: 2, event_id: 1 }))).toBeLessThan(0);
    expect(compareRunEvents(event({ sequence: 1, event_id: 1, created_at: "a" }), event({ sequence: 1, event_id: 2, created_at: "a" }))).toBeLessThan(0);
    expect(compareRunEvents(event({ sequence: 1, event_id: 1, created_at: "a" }), event({ sequence: 1, event_id: 1, created_at: "b" }))).toBeLessThan(0);
    expect(runEventFingerprint(event({ payload: undefined }))).toContain("workflow.run.started");
    expect(runEventTwinKey(event({ node_id: null }))).toContain("workflow.run.started|");
    expect(runSortKey(baseRun())).toBeGreaterThan(0);
    expect(runSortKey({ ...baseRun(), started_at: "bad", queued_at: null, completed_at: null })).toBe(0);
    expect(runTimestamp({ ...baseRun(), started_at: null, queued_at: null, completed_at: null })).toBe("—");
    expect(runTimestamp({ ...baseRun(), started_at: "bad" })).toBe("—");
  });

  it("covers stream event payload branches and cursors", () => {
    expect(workflowRunEventNodeId({ type: "x", node_id: "node" })).toBe("node");
    expect(workflowRunEventNodeId({ type: "x", step: { node_id: "step", node_type: "tool" } })).toBe("step");
    const cases = [
      "workflow.run.step", "workflow.run.node_started", "workflow.run.approval.approved",
      "workflow.run.retried", "workflow.run.waiting_approval", "workflow.run.waiting_event",
      "workflow.run.cancel_requested", "workflow.run.cancelled", "workflow.run.completed",
      "workflow.run.expired", "workflow.run.failed", "workflow.run.resumed", "workflow.run.queued", "other",
    ];
    for (const type of cases) {
      expect(workflowRunEventPayload({
        type, node_id: "node", node_type: "tool", runtime_approval_id: "approval",
        reason: "reason", retried_run_id: "retry", checkpoint_id: "checkpoint", status: "ok",
        cancelled_by: "operator", error: "error", actor: "operator", event_name: "ready",
        event_payload: {}, workflow_id: "wf", workflow_version_id: "version", alias: "prod",
        requested_by: "operator", extra: true, step: { node_id: "node", node_type: "tool" },
      }, "node")).toBeTruthy();
    }
    expect(workflowRunEventCursor({ type: "x", workflow_id: "wf", event_id: 1 })).toContain("event:1");
    expect(workflowRunEventCursor({ type: "x", workflow_id: "wf", sequence: 2 })).toContain("sequence:2");
    expect(workflowRunEventCursor({ type: "x", workflow_id: "wf", created_at: "now" })).toContain("created:now");
    expect(workflowRunEventCursor({ type: "x", workflow_id: "wf" })).toContain("payload:");
    expect(workflowRunEventCursor({ type: "x" })).toBeNull();
    expect(workflowRunEventPayload({ type: "workflow.run.node_started" }, null)).toEqual({});
    expect(workflowRunEventPayload({ type: "workflow.run.approval.approved" }, null)).toEqual({});
    expect(workflowRunEventPayload({ type: "workflow.run.retried" }, null)).toEqual({});
    expect(workflowRunEventPayload({ type: "workflow.run.waiting_approval" }, null)).toEqual({});
    expect(workflowRunEventPayload({ type: "workflow.run.waiting_event" }, null)).toBeNull();
    expect(workflowRunEventPayload({ type: "workflow.run.cancel_requested" }, null)).toEqual({});
    expect(workflowRunEventPayload({ type: "workflow.run.cancelled" }, null)).toEqual({});
    expect(workflowRunEventPayload({ type: "workflow.run.completed" }, null)).toBeNull();
    expect(workflowRunEventPayload({ type: "workflow.run.expired" }, null)).toEqual({});
    expect(workflowRunEventPayload({ type: "workflow.run.failed" }, null)).toEqual({});
    expect(workflowRunEventPayload({ type: "workflow.run.resumed" }, null)).toEqual({});
    expect(workflowRunEventPayload({ type: "workflow.run.queued" }, null)).toEqual({});
    expect(workflowRunEventPayload({ type: "other", type_only: true }, null)).toEqual({ type_only: true });
    expect(workflowRunEventPayload({ type: "other" }, null)).toBeNull();
  });

  it("materializes component starters across valid and invalid shapes", () => {
    expect(starterNodeFromComponent("agent", "id", null)).toBeNull();
    expect(starterNodeFromComponent("agent", "id", { type: "tool", starter_node: {} } as never)).toBeNull();
    expect(starterNodeFromComponent("agent", "id", { type: "agent", starter_node: [] } as never)).toBeNull();
    expect(starterNodeFromComponent("agent", "id", {
      type: "agent", starter_node: { id: "old", type: "agent", name: "starter" },
    } as never)).toMatchObject({ id: "id", type: "agent", name: "starter" });
    expect(materializeStarterNodeValue("__CALIBER_NODE_ID__", "id")).toBe("id");
    expect(materializeStarterNodeValue("__CALIBER_NOW_PLUS_60S_ISO__", "id")).toMatch(/T/);
    expect(materializeStarterNodeValue(["__CALIBER_NODE_ID__", { nested: "x" }], "id")).toEqual(["id", { nested: "x" }]);
    expect(materializeStarterNodeValue(3, "id")).toBe(3);
    expect(hasStructuredAgentOutputPort()).toBe(false);
    expect(hasStructuredAgentOutputPort({ history: { type: "structured" }, tool_calls: { type: "structured" } })).toBe(false);
    expect(hasStructuredAgentOutputPort({ answer: { type: "structured" } })).toBe(true);
  });

  it("creates a valid starter manifest for every palette node type", () => {
    for (const item of NODE_PALETTE) {
      const node = newNode(item.type, `node-${item.type}`);
      expect(node.id).toBe(`node-${item.type}`);
      expect(node.type).toBe(item.type);
    }
    expect(() => newNode("not-a-node", "bad")).toThrow(/Unsupported workflow node type/);
  });

  it("covers remaining starter, checkpoint, and run-monitor status branches", () => {
    expect(materializeStarterNodeValue({
      id: "__CALIBER_NODE_ID__",
      nested: ["__CALIBER_NODE_ID__", { when: "__CALIBER_NOW_PLUS_60S_ISO__" }],
    }, "node-1")).toMatchObject({ id: "node-1", nested: ["node-1", { when: expect.any(String) }] });
    expect(starterNodeFromComponent("agent", "agent-1", {
      type: "agent",
      starter_node: null,
    } as never)).toBeNull();
    expect(normalizeWorkflowManifest({
      nodes: {
        wait: {
          id: "wait",
          type: "wait_for_event",
          outputs: {
            output: { type: "string" },
            event_payload: { type: "structured" },
            event_name: { type: "string" },
          },
        },
        agent: {
          id: "agent",
          type: "agent",
          output_type: { type: "object" },
          outputs: { history: { type: "structured" }, tool_calls: { type: "structured" } },
        },
      },
    } as never).nodes.agent.outputs).toHaveProperty("structured_output");

    expect(workflowRunCheckpointIdentityIssue(
      { status: "waiting_event", current_node_id: "wait" },
      { node_id: "wait", state_blob: { node_id: "wait", kind: "wait_for_event", input_by_port: {}, expected_event_name: "ready" } } as never,
    )).toBeNull();
    expect(workflowRunCheckpointIdentityIssue(
      { status: "waiting_approval", current_node_id: "approval" },
      { node_id: "approval", state_blob: { node_id: "approval", kind: "runtime_approval", input_by_port: {} } } as never,
    )).toBeNull();

    const cyclicA: Record<string, unknown> = {};
    const cyclicB: Record<string, unknown> = {};
    cyclicA.self = cyclicA;
    cyclicB.self = cyclicB;
    expect(workflowRunResumeCorrelationMatches(cyclicA, cyclicB)).toBe(false);
    expect(formatResumeEventPayload(cyclicA)).toBe("[object Object]");
    expect(workflowRunResumeCorrelationValueLabel(cyclicA)).toBe("[object Object]");
    expect(workflowRunResumeByEventIssue({
      status: "completed",
      waitMode: "wait_for_event",
      correlationKey: "id",
      correlationValue: "x",
      resumeEventPayload: "{}",
    })).toBeNull();

    for (const status of [
      "waiting_approval",
      "waiting_event",
      "queued",
      "running",
      "resuming",
      "cancel_requested",
      "completed",
      "failed",
      "cancelled",
      "rejected",
      "expired",
      "unknown",
    ]) {
      expect(runOutputEmptyMessage(baseRun(status))).toContain("run");
      expect(syntheticRunMonitorGraphFallbackMessage(baseRun(status))).toContain("Replay");
      expect(savedVersionRunMonitorGraphFallbackMessage(baseRun(status), "v3")).toContain("Replay");
      expect(snapshotRunMonitorContextNote(baseRun(status))).toContain("run");
      expect(historicalVersionRunMonitorContextNote(baseRun(status), "v3")).toContain("v3");
    }
  });

  it("covers full step defaults, terminal reconciliation, and serialized event fallbacks", () => {
    expect(normalizeRunStep({
      node_id: "tool",
      node_type: "tool",
      status: "completed",
      output: "ok",
      tool_calls: [{ name: "lookup" }],
      handoff_target: "agent",
      detail: "done",
      duration_ms: 12,
      input_by_port: { query: "x" },
      output_by_port: { result: "y" },
    })).toMatchObject({
      status: "completed",
      output: "ok",
      handoff_target: "agent",
      duration_ms: 12,
      input_by_port: { query: "x" },
      output_by_port: { result: "y" },
    });
    expect(normalizeRunStep({
      node_id: "tool",
      node_type: "tool",
      input_by_port: {},
      output_by_port: {},
      tool_calls: [],
    })).toMatchObject({ status: "ok", output: "", handoff_target: null, detail: "" });

    const terminal = reconcileRunSnapshot(
      baseRun("completed"),
      [event({
        event_type: "workflow.run.step",
        node_id: "other",
        payload: { step: { node_id: "other", node_type: "tool", status: "running" } },
      })],
    );
    expect(terminal.status).toBe("completed");
    expect(terminal.current_node_id).toBe("agent");
    const active = reconcileRunSnapshot(
      baseRun("running"),
      [event({
        event_type: "workflow.run.step",
        node_id: "other",
        payload: null,
      })],
    );
    expect(active.current_node_id).toBe("other");
    const circular: Record<string, unknown> = {};
    circular.self = circular;
    expect(runEventFingerprint(event({ payload: circular }))).toContain("workflow.run.started");
    expect(runEventTwinKey(event({ payload: circular }))).toContain("workflow.run.started");
  });

  it("covers reconciliation, storage failures, and every sparse event payload shape", () => {
    expect(workflowRunCheckpointIdentityIssue(
      { status: "waiting_approval", current_node_id: "approval" },
      { node_id: "approval", state_blob: null } as never,
    )).toContain("checkpoint payload is corrupt");
    expect(workflowRunCheckpointIdentityIssue(
      { status: "waiting_event", current_node_id: "wait" },
      { node_id: "wait", state_blob: { kind: "wait_for_event", input_by_port: {} } } as never,
    )).toContain("no expected event name");

    for (const detail of [
      "workflow run retry manifest is invalid",
      "persisted manifest snapshot is unavailable",
    ]) {
      expect(workflowRunRetryFailureMessage(new ApiError(409, detail, null))).toContain("Retry failed");
    }
    expect(workflowRunApprovalActionFailureMessage(
      "Approve",
      new ApiError(409, "runtime approval is not pending", null),
    )).toContain("Approve failed");
    expect(workflowRunApprovalActionFailureMessage(
      "Reject",
      new ApiError(409, "runtime approval not found for workflow run", null),
    )).toContain("Reject failed");
    expect(workflowRunResumeFailureMessage(
      new ApiError(409, "cannot resume while runtime approval decision is pending", null),
    )).toContain("Resume failed");
    expect(workflowRunResumeFailureMessage(
      new ApiError(409, "has no approved runtime approval decision to resume from", null),
    )).toContain("Resume failed");

    expect(reconcileRunSnapshot(baseRun("completed"), [
      event({ event_type: "workflow.run.step", node_id: "other", payload: null }),
    ])).toMatchObject({ status: "completed", current_node_id: "agent" });
    const same = { ...baseRun("running"), summary: { status: "running" } } as WorkflowRun;
    expect(reconcileRunSnapshot(same, [
      event({ event_type: "workflow.run.node_started", node_id: "agent" }),
    ])).toBe(same);
    expect(reconcileRunSnapshot(baseRun("running"), [
      event({ event_type: "workflow.run.step", node_id: "tool", payload: {
        step: { node_id: "tool", node_type: "tool", status: "completed" },
      } }),
      event({ event_type: "workflow.run.failed", node_id: "tool" }),
    ]).status).toBe("failed");
    expect(reconcileRunSnapshot(baseRun("failed"), [
      event({ event_type: "workflow.run.step", node_id: "tool", payload: {
        step: { node_id: "tool", node_type: "tool", status: "running" },
      } }),
    ])).toMatchObject({ status: "failed", current_node_id: "agent" });

    const storedAgent = event({ event_id: 10, event_type: "workflow.run.step", node_id: "agent" });
    const storedTool = event({ event_id: 11, event_type: "workflow.run.step", node_id: "tool" });
    const syntheticAgent = event({ event_id: -1, event_type: "workflow.run.step", node_id: "agent" });
    const syntheticTool = event({ event_id: -2, event_type: "workflow.run.step", node_id: "tool" });
    expect(dropSyntheticTwins([storedAgent, syntheticAgent, syntheticTool, storedTool])).toEqual([
      storedAgent,
      storedTool,
    ]);
    expect(dropSyntheticTwins([storedAgent, syntheticTool])).toEqual([storedAgent, syntheticTool]);
    expect(mergeRunEvents(
      [storedAgent],
      [event({ event_id: 10, event_type: "workflow.run.failed", node_id: "other" })],
    )).toHaveLength(1);

    window.localStorage.setItem(layoutStorageKey("storage-failure"), JSON.stringify({ old: { x: 1, y: 2 } }));
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementationOnce(() => {
      throw new Error("storage unavailable");
    });
    expect(readLayout("storage-failure")).toEqual({});
    getItem.mockRestore();
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementationOnce(() => {
      throw new Error("storage unavailable");
    });
    expect(() => persistLayout("storage-failure", { node: { x: 1, y: 2 } })).not.toThrow();
    setItem.mockRestore();
    const removeItem = vi.spyOn(Storage.prototype, "removeItem").mockImplementationOnce(() => {
      throw new Error("storage unavailable");
    });
    expect(() => clearLayout("storage-failure")).not.toThrow();
    removeItem.mockRestore();
  });

  it("covers event identity fallbacks and optional payload fields independently", () => {
    expect(workflowRunEventNodeId({ type: "x", node_id: "  " })).toBeNull();
    expect(workflowRunEventNodeId({ type: "x", step: { node_id: "  " } })).toBeNull();
    const sparsePayloads = [
      ["workflow.run.node_started", {}],
      ["workflow.run.approval.approved", {}],
      ["workflow.run.approval.rejected", {}],
      ["workflow.run.retried", {}],
      ["workflow.run.waiting_approval", {}],
      ["workflow.run.cancel_requested", {}],
      ["workflow.run.cancelled", {}],
      ["workflow.run.expired", {}],
      ["workflow.run.failed", {}],
      ["workflow.run.resumed", {}],
      ["workflow.run.queued", {}],
    ] as const;
    for (const [type, expected] of sparsePayloads) {
      expect(workflowRunEventPayload({ type }, null)).toEqual(expected);
    }
    expect(workflowRunEventPayload({ type: "workflow.run.step", step: { node_id: "x", node_type: "tool" } }, "x"))
      .toEqual({ step: expect.objectContaining({ node_id: "x" }) });
    expect(workflowRunEventPayload({ type: "workflow.run.waiting_event" }, "wait"))
      .toEqual({ node_id: "wait" });
    expect(workflowRunEventPayload({ type: "workflow.run.completed", status: "completed" }, null))
      .toEqual({ status: "completed" });
    expect(workflowRunEventPayload({ type: "workflow.run.resumed", event_payload: null }, null))
      .toEqual({ event_payload_supplied: true });
    expect(workflowRunEventPayload({ type: "other", custom: true }, null)).toEqual({ custom: true });

    expect(workflowRunEventCursor({ type: "x", workflow_id: "wf", event_id: 0, sequence: 2 }))
      .toContain("event:0");
    expect(workflowRunEventCursor({ type: "x", workflow_id: "wf", event_id: null, sequence: 0 }))
      .toContain("sequence:0");
    expect(workflowRunEventCursor({ type: "x", workflow_id: "wf", event_id: null, sequence: null, created_at: "" }))
      .toContain("payload:");
    expect(workflowRunEventCursor({ type: "x", workflow_id: "wf", workflow_run_id: "run" }))
      .toContain("|run|");
  });

  it("covers the remaining recovery error families and sparse run fallbacks", () => {
    expect(workflowRunCheckpointIdentityIssue({ status: "waiting_event", current_node_id: "wait" }, {
      node_id: "wait", state_blob: { node_id: "wait", kind: "unknown", input_by_port: [] },
    } as never)).toContain("wait checkpoint kind is invalid");
    expect(workflowRunCheckpointIdentityIssue({ status: "waiting_event", current_node_id: "wait" }, {
      node_id: "wait", state_blob: { node_id: "wait", kind: "wait_for_event", input_by_port: [] },
    } as never)).toContain("no input snapshot");
    expect(workflowRunRetryFailureMessage(new Error("retry"))).toBeNull();
    expect(workflowRunApprovalActionFailureMessage("Approve", new Error("approval"))).toBeNull();
    expect(workflowRunResumeFailureMessage(new Error("resume"))).toBeNull();
    expect(workflowRunResumeByEventFailureMessage(new Error("event"))).toBeNull();
    expect(workflowRunApprovalActionFailureMessage("Approve", new ApiError(409, "runtime approval is pending", null))).toBeNull();
    expect(workflowRunResumeFailureMessage(new ApiError(409, "is not resumable from this checkpoint", null))).toContain("Resume failed");
    expect(workflowRunResumeByEventFailureMessage(new ApiError(409, "missing correlation_value", null))).toContain("External event resume failed");
    expect(runMonitorIdleSectionMessage("not-a-section" as never)).toContain("execution surface");

    const terminal = reconcileRunSnapshot(baseRun("completed"), [
      event({ event_type: "workflow.run.started", node_id: "other" }),
    ]);
    expect(terminal).toMatchObject({ status: "running", current_node_id: "other" });
    expect(reconcileRunSnapshot(baseRun("running"), [event({ event_type: "other", node_id: null })])).toBeTruthy();
    expect(workflowRunEventPayload({ type: "workflow.run.step", step: null }, null)).toBeNull();
    expect(workflowRunEventCursor({ type: "", workflow_id: "wf", event_id: 1 })).toBeNull();
    expect(defaultFocusedRunNode({ ...baseRun(), current_node_id: " ", summary: { node_path: [1, null] } } as never)).toBeNull();
    expect(defaultFocusedRunNode({ ...baseRun(), current_node_id: " ", summary: { node_path: [], steps: [] } } as never)).toBeNull();
    expect(runSortKey({ ...baseRun(), started_at: null, queued_at: "2026-01-01T00:00:00Z" })).toBeGreaterThan(0);
    expect(runTimestamp({ ...baseRun(), started_at: null, queued_at: "2026-01-01T00:00:00Z" })).not.toBe("—");
  });
});
