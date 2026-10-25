import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";

import { ApiError } from "@/api/caliberApi";
import type { WorkflowRun } from "@/api/workflowTypes";
import {
  approvalCapabilityMessage,
  calibrationRunEvalSummary,
  checkpointWaitUntilDisplay,
  defaultSelectedRunNodeId,
  emptyRunHistoryMessage,
  emptyRunLogsMessage,
  filteredRunHistoryMessage,
  formatResumeEventPayload,
  normalizeRunStep,
  normalizeWorkflowDetailTab,
  parseJsonObjectText,
  readNumber,
  readString,
  runActionErrorDetail,
  triggerServiceOpenApiDownload,
  TriggerBadge,
  runArtifactFilterMatches,
  runArtifactFilterQueryValue,
  runSearchMatches,
  savedVersionRunGraphFallbackMessage,
  syntheticRunGraphFallbackMessage,
  waitUntilDisplay,
  workflowRunGraphUnavailableMessage,
  workflowRunResumeByEventFailureMessage,
  workflowRunResumeByEventIssue,
  workflowRunApprovalActionFailureMessage,
  workflowRunCheckpointIdentityIssue,
  workflowRunResumeCorrelationMatches,
  workflowRunResumeCorrelationValueLabel,
  workflowRunResumeEventNameIssue,
  workflowRunResumeFailureMessage,
  workflowRunRetryFailureMessage,
} from "@/pages/WorkflowDetail";
import { render, screen } from "@/test/utils";

const run = (status = "running", overrides: Record<string, unknown> = {}): WorkflowRun => ({
  workflow_run_id: "run-1",
  workflow_id: "wf-1",
  project_id: null,
  tenant_id: null,
  workflow_version_id: "v1",
  deployment_alias: "prod",
  mlflow_run_id: null,
  trace_id: "trace-1",
  session_id: null,
  status,
  source: "editor",
  priority: 0,
  queued_at: "2026-01-01T00:00:00Z",
  started_at: "2026-01-01T00:00:01Z",
  completed_at: null,
  current_node_id: "agent",
  summary: null,
  ...overrides,
});

const api = (message: string) => new ApiError(409, message, null);

describe("WorkflowDetail decision helpers", () => {
  it("normalizes tabs, values, and artifact triage filters", () => {
    for (const tab of ["graph", "versions", "deployments", "runs", "service", "promotions", "patches"]) {
      expect(normalizeWorkflowDetailTab(tab)).toBe(tab);
    }
    expect(normalizeWorkflowDetailTab(null)).toBeNull();
    expect(normalizeWorkflowDetailTab("unknown")).toBeNull();
    expect(readNumber(2)).toBe(2);
    expect(readNumber(" 3 ")).toBe(3);
    expect(readNumber("bad")).toBeNull();
    expect(readString("x")).toBe("x");
    expect(readString(" ")).toBeNull();
    expect(parseJsonObjectText(" ")).toBeUndefined();
    expect(parseJsonObjectText('{"x":1}')).toEqual({ x: 1 });
    expect(formatResumeEventPayload(null)).toBe("");
    expect(formatResumeEventPayload({ x: 1 })).toContain('"x": 1');
    expect(waitUntilDisplay(null)).toBe("the configured time");
    expect(waitUntilDisplay({ type: "wait_until", wait_until: "2026-01-01", timezone: "UTC" } as never)).toContain("UTC");
    expect(waitUntilDisplay({ type: "wait_until", wait_until: "2026-01-01+02:00", timezone: "UTC" } as never)).toBe("2026-01-01+02:00");
    expect(waitUntilDisplay({ type: "wait_until", wait_until: " ", timezone: " " } as never)).toBe("the configured time");
    expect(checkpointWaitUntilDisplay({ resume_at: "2026-01-01", timezone: "UTC" })).toContain("UTC");
    expect(checkpointWaitUntilDisplay({ wait_until: "2026-01-01Z", timezone: "UTC" })).toBe("2026-01-01Z");
    expect(checkpointWaitUntilDisplay({ wait_until: " ", resume_at: " " })).toBeNull();
    const failed = run("failed", { summary: { artifact_persistence: { status: "failed", bucket: "b", object_count: 0, artifact_names: ["log.txt"] } } });
    const stored = run("completed", { summary: { artifact_persistence: { status: "persisted", bucket: "b", object_count: 1, artifact_names: ["out.json"] } } });
    expect(runArtifactFilterMatches(failed, "upload_failed")).toBe(true);
    expect(runArtifactFilterMatches(stored, "artifacts_stored")).toBe(true);
    expect(runArtifactFilterMatches(run(), "all")).toBe(true);
    expect(runArtifactFilterQueryValue("upload_failed")).toBe("failed");
    expect(runArtifactFilterQueryValue("artifacts_stored")).toBe("persisted");
    expect(runArtifactFilterQueryValue("all")).toBeUndefined();
    expect(runSearchMatches(run(), "")).toBe(true);
    expect(runSearchMatches(failed, "artifact upload failed")).toBe(true);
    expect(runSearchMatches(stored, "out.json")).toBe(true);
    expect(runSearchMatches(run(), "missing")).toBe(false);
    expect(filteredRunHistoryMessage({ search: "x", filter: "upload_failed" })).toContain("artifact upload failed view");
    expect(filteredRunHistoryMessage({ search: "x", filter: "artifacts_stored" })).toContain("stored artifacts view");
    expect(filteredRunHistoryMessage({ search: "x", filter: "all" })).toContain("No runs match");
    expect(filteredRunHistoryMessage({ search: "", filter: "upload_failed" })).toContain("upload failures");
    expect(filteredRunHistoryMessage({ search: "", filter: "artifacts_stored" })).toContain("persisted artifacts");
    expect(filteredRunHistoryMessage({ search: "", filter: "all" })).toContain("triage view");
  });

  it("covers failure guidance and graph fallback states", () => {
    expect(approvalCapabilityMessage("runtime approval")).toContain("disabled");
    expect(runActionErrorDetail(api("boom"))).toBe("boom");
    expect(runActionErrorDetail("plain")).toBe("plain");
    expect(workflowRunRetryFailureMessage(api("persisted manifest snapshot is invalid"))).toContain("Retry failed");
    expect(workflowRunRetryFailureMessage(api("other"))).toBeNull();
    expect(workflowRunResumeFailureMessage(api("cannot resume after runtime approval rejection"))).toContain("Resume failed");
    expect(workflowRunResumeFailureMessage(api("approval checkpoint is missing"))).toContain("Resume failed");
    expect(workflowRunResumeFailureMessage(api("other"))).toBeNull();
    expect(workflowRunResumeByEventFailureMessage(api("legacy wait_event checkpoints"))).toContain("External event resume failed");
    expect(workflowRunResumeByEventFailureMessage(api("other"))).toBeNull();
    for (const text of ["has no pending runtime approvals", "is not waiting for approval", "approval checkpoint is invalid", "runtime approval not found for workflow run", "other"]) {
      const result = workflowRunApprovalActionFailureMessage("Reject", api(text));
      expect(result === null || result.includes("failed")).toBe(true);
    }
    expect(workflowRunGraphUnavailableMessage({ manifestMode: "snapshot", versionReference: "v1", versionLoadFailed: false, hasSummarySteps: false, hasCurrentNode: false, hasCheckpoints: false })).toContain("draft snapshot");
    expect(workflowRunGraphUnavailableMessage({ manifestMode: "saved_version", versionReference: "v1", versionLoadFailed: true, hasSummarySteps: true, hasCurrentNode: false, hasCheckpoints: false })).toContain("could not be loaded");
    expect(workflowRunGraphUnavailableMessage({ manifestMode: "saved_version", versionReference: "v1", versionLoadFailed: false, hasSummarySteps: false, hasCurrentNode: false, hasCheckpoints: false })).toContain("not available");
    for (const status of ["waiting_event", "running", "completed", "failed", "unknown"]) {
      expect(syntheticRunGraphFallbackMessage(run(status))).toContain("Replay");
      expect(savedVersionRunGraphFallbackMessage(run(status), "v1")).toContain("Replay");
      expect(emptyRunLogsMessage(run(status))).toContain("run");
    }
    expect(emptyRunHistoryMessage({ capabilitiesUnavailable: true, queueRunsEnabled: true, workflowStatus: null, runAlias: "manual", runVersionId: null })).toContain("could not be loaded");
    expect(emptyRunHistoryMessage({ capabilitiesUnavailable: false, queueRunsEnabled: false, workflowStatus: null, runAlias: "manual", runVersionId: null })).toContain("disabled");
    expect(emptyRunHistoryMessage({ capabilitiesUnavailable: false, queueRunsEnabled: true, workflowStatus: "paused", runAlias: "manual", runVersionId: null })).toContain("paused");
    expect(emptyRunHistoryMessage({ capabilitiesUnavailable: false, queueRunsEnabled: true, workflowStatus: "archived", runAlias: "manual", runVersionId: null })).toContain("archived");
    expect(emptyRunHistoryMessage({ capabilitiesUnavailable: false, queueRunsEnabled: true, workflowStatus: null, runAlias: "manual", runVersionId: null })).toContain("Publish");
    expect(emptyRunHistoryMessage({ capabilitiesUnavailable: false, queueRunsEnabled: true, workflowStatus: null, runAlias: "prod", runVersionId: null })).toContain("Alias prod");
    expect(emptyRunHistoryMessage({ capabilitiesUnavailable: false, queueRunsEnabled: true, workflowStatus: null, runAlias: "manual", runVersionId: "v1" })).toContain("Queue a run");
  });

  it("selects a valid run node and normalizes step payloads", () => {
    const manifest = { nodes: { start: {}, agent: {}, done: {} } } as never;
    expect(defaultSelectedRunNodeId(null, manifest)).toBeNull();
    expect(defaultSelectedRunNodeId(run(), null)).toBeNull();
    expect(defaultSelectedRunNodeId(run(), manifest)).toBe("agent");
    expect(defaultSelectedRunNodeId(run("running", { current_node_id: "missing", summary: { steps: [{ node_id: "agent", node_type: "tool" }], node_path: ["done"] } }), manifest)).toBe("agent");
    expect(defaultSelectedRunNodeId(run("running", { current_node_id: "missing", summary: { steps: [], node_path: ["missing"] } }), manifest)).toBeNull();
    expect(normalizeRunStep(null)).toBeNull();
    expect(normalizeRunStep({ node_id: "agent" })).toBeNull();
    expect(normalizeRunStep({ node_id: "agent", node_type: "tool", input_by_port: [], output_by_port: {}, tool_calls: ["call"], status: "ok", output: "out", detail: "detail", duration_ms: 2 })).toMatchObject({ input_by_port: null, output_by_port: {}, tool_calls: ["call"], duration_ms: 2 });
    expect(normalizeRunStep({ node_id: "minimal", node_type: "tool" })).toMatchObject({
      status: "ok",
      output: "",
      tool_calls: [],
      handoff_target: null,
      detail: "",
      duration_ms: 0,
      input_by_port: null,
      output_by_port: null,
    });
    expect(calibrationRunEvalSummary({ eval_results: { aggregate_score: 0.875 } } as never)).toContain("0.875");
    expect(calibrationRunEvalSummary({
      artifact_type: "workflow_manifest",
      workflow_id: "wf-1",
      calibration_spec: {},
      eval_results: {
        candidate: { content: "new", overall: 0.9 },
        baseline: { content: "old", overall: 0.8 },
        gate: { passed: true },
        n_examples: 3,
      },
    } as never)).toContain("gate passed");
    expect(calibrationRunEvalSummary({
      artifact_type: "workflow_manifest",
      workflow_id: "wf-1",
      calibration_spec: {},
      eval_results: {
        candidate: { content: "new", overall: 0.7 },
        baseline: { content: "old", overall: 0.8 },
        gate: { passed: false },
        n_examples: 2,
      },
    } as never)).toContain("gate blocked");
    expect(calibrationRunEvalSummary({ eval_results: {} } as never)).toBeNull();
    expect(workflowRunResumeCorrelationMatches({ a: 1 }, { a: 1 })).toBe(true);
    expect(workflowRunResumeCorrelationMatches({ a: 1 }, { a: 2 })).toBe(false);
    expect(workflowRunResumeCorrelationValueLabel(null)).toBe("null");
    expect(workflowRunResumeCorrelationValueLabel(undefined)).toBe("undefined");
    expect(workflowRunResumeEventNameIssue({ status: "completed", waitMode: "wait_for_event", expectedEventName: "a", resumeEventName: "b" })).toBeNull();
    expect(workflowRunCheckpointIdentityIssue(run("completed"), null)).toBeNull();
    expect(workflowRunCheckpointIdentityIssue(
      run("waiting_approval"),
      { node_id: "agent", state_blob: { node_id: "agent", kind: "human_approval", input_by_port: {} } } as never,
    )).toBeNull();
    expect(workflowRunCheckpointIdentityIssue(
      run("waiting_event"),
      { node_id: "agent", state_blob: { node_id: "agent", kind: "wait_until", input_by_port: {} } } as never,
    )).toBeNull();
    expect(workflowRunCheckpointIdentityIssue(
      run("waiting_event"),
      { node_id: "agent", state_blob: { node_id: "agent", kind: "wait_for_event", expected_event_name: "ticket.updated", input_by_port: {} } } as never,
    )).toBeNull();
    expect(workflowRunResumeByEventIssue({
      status: "completed", waitMode: "wait_for_event", correlationKey: "id", correlationValue: "1", resumeEventPayload: "{}",
    })).toBeNull();
    expect(workflowRunResumeByEventIssue({
      status: "waiting_event", waitMode: "other", correlationKey: "id", correlationValue: "1", resumeEventPayload: "{}",
    })).toBeNull();
    expect(triggerServiceOpenApiDownload(new Blob(["{}"], { type: "application/json" }), "wf-1")).toBe(true);
  });

  it("covers every run status family and optional search payload branch", () => {
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
      expect(syntheticRunGraphFallbackMessage(run(status))).toContain("Replay");
      expect(savedVersionRunGraphFallbackMessage(run(status), "v3")).toContain("Replay");
      expect(emptyRunLogsMessage(run(status))).toContain("run");
    }

    const searchable = run("completed", {
      trace_id: "trace-7",
      workflow_version_id: "version-7",
      deployment_alias: "staging",
      summary: { error: "summary failure" },
      error_summary: "run failure",
    });
    expect(runSearchMatches(searchable, "trace-7")).toBe(true);
    expect(runSearchMatches(searchable, "summary failure")).toBe(true);
    expect(runSearchMatches(searchable, "run failure")).toBe(true);
    expect(runSearchMatches(run("completed", {
      summary: {
        artifact_persistence: {
          status: "persisted",
          bucket: "results",
          object_count: 1,
          artifact_names: ["answer.json"],
        },
      },
    }), "results")).toBe(true);
    expect(runSearchMatches(run("completed", {
      summary: {
        artifact_persistence: {
          status: "failed",
          bucket: "results",
          object_count: 0,
          artifact_names: [],
          error: "upload failed",
        },
      },
    }), "artifact upload failed")).toBe(true);
    expect(runSearchMatches(run(), "  ")).toBe(false);
  });

  it("covers checkpoint identity, correlation serialization, and graph fallback branches", () => {
    expect(workflowRunCheckpointIdentityIssue(
      run("waiting_event"),
      { node_id: "agent", state_blob: { node_id: "agent", kind: "wait_until", input_by_port: {} } } as never,
    )).toBeNull();
    expect(workflowRunCheckpointIdentityIssue(
      run("waiting_approval"),
      { node_id: "agent", state_blob: { node_id: "agent", kind: "human_approval", input_by_port: {} } } as never,
    )).toBeNull();
    expect(workflowRunCheckpointIdentityIssue(
      run("waiting_event"),
      { node_id: "agent", state_blob: { node_id: "other", kind: "wait_for_event", input_by_port: {}, expected_event_name: "ready" } } as never,
    )).toContain("payload points at other");
    expect(workflowRunResumeByEventIssue({
      status: "completed",
      waitMode: "wait_for_event",
      correlationKey: "id",
      correlationValue: "1",
      resumeEventPayload: "{}",
    })).toBeNull();
    expect(workflowRunResumeByEventIssue({
      status: "waiting_event",
      waitMode: "other",
      correlationKey: "id",
      correlationValue: "1",
      resumeEventPayload: "{}",
    })).toBeNull();

    const cyclicA: Record<string, unknown> = {};
    const cyclicB: Record<string, unknown> = {};
    cyclicA.self = cyclicA;
    cyclicB.self = cyclicB;
    expect(workflowRunResumeCorrelationMatches(cyclicA, cyclicB)).toBe(false);
    expect(formatResumeEventPayload(cyclicA)).toBe("[object Object]");
    expect(workflowRunResumeCorrelationValueLabel(cyclicA)).toBe("[object Object]");
    expect(waitUntilDisplay({ type: "wait_until", wait_until: " ", timezone: " " } as never)).toBe("the configured time");
    expect(checkpointWaitUntilDisplay({ wait_until: " ", resume_at: " ", timezone: "UTC" })).toBeNull();

    for (const hasEvidence of [false, true]) {
      expect(workflowRunGraphUnavailableMessage({
        manifestMode: "snapshot",
        versionReference: "v1",
        versionLoadFailed: false,
        hasSummarySteps: hasEvidence,
        hasCurrentNode: false,
        hasCheckpoints: false,
      })).toContain("snapshot");
      expect(workflowRunGraphUnavailableMessage({
        manifestMode: "saved_version",
        versionReference: "v1",
        versionLoadFailed: true,
        hasSummarySteps: hasEvidence,
        hasCurrentNode: false,
        hasCheckpoints: false,
      })).toContain("v1");
      expect(workflowRunGraphUnavailableMessage({
        manifestMode: "saved_version",
        versionReference: "v1",
        versionLoadFailed: false,
        hasSummarySteps: hasEvidence,
        hasCurrentNode: false,
        hasCheckpoints: false,
      })).toContain("not available");
    }
  });

  it("covers trigger badge variants and malformed checkpoint diagnostics", () => {
    const manual = render(createElement(TriggerBadge, { trigger: null }));
    expect(manual.getByText(/Manual/)).toBeInTheDocument();
    manual.unmount();

    const event = render(createElement(TriggerBadge, { trigger: { mode: "event", event_name: "ready", enabled: true } as never }));
    expect(event.getByTestId("workflow-trigger-badge")).toHaveTextContent("Event: ready");
    event.unmount();

    const unnamedEvent = render(createElement(TriggerBadge, { trigger: { mode: "event", enabled: true } as never }));
    expect(unnamedEvent.getByTestId("workflow-trigger-badge")).toHaveTextContent("Event");
    unnamedEvent.unmount();

    const unnamedCron = render(createElement(TriggerBadge, { trigger: { mode: "cron", enabled: true } as never }));
    expect(unnamedCron.getByTestId("workflow-trigger-badge")).toHaveTextContent("Cron:");
    unnamedCron.unmount();

    const disabledCron = render(createElement(TriggerBadge, { trigger: { mode: "cron", cron: "0 * * * *", enabled: false } as never }));
    expect(disabledCron.getByTestId("workflow-trigger-badge")).toHaveTextContent("Cron: 0 * * * * (off)");
    expect(disabledCron.getByTestId("workflow-trigger-badge")).toHaveAttribute("title", "Trigger is disabled");
    disabledCron.unmount();

    expect(checkpointWaitUntilDisplay(null)).toBeNull();
    expect(checkpointWaitUntilDisplay({ wait_until: "2026-01-01T00:00:00Z" })).toBe("2026-01-01T00:00:00Z");
    expect(checkpointWaitUntilDisplay({ resume_at: "2026-01-01T00:00:00", timezone: "UTC" })).toContain("UTC");
    expect(workflowRunCheckpointIdentityIssue(run("waiting_event", { current_node_id: null }), {
      checkpoint_id: "cp-1",
      node_id: null,
      state_blob: null,
    } as never)).toContain("checkpoint payload is corrupt");
    expect(workflowRunCheckpointIdentityIssue(run("waiting_approval"), {
      checkpoint_id: "cp-2",
      node_id: "agent",
      state_blob: { node_id: "agent", kind: "other", input_by_port: [] },
    } as never)).toContain("approval checkpoint kind is invalid");
    expect(workflowRunCheckpointIdentityIssue(run("waiting_event"), {
      checkpoint_id: "cp-3",
      node_id: "agent",
      state_blob: { node_id: "agent", kind: "wait_for_event", input_by_port: {}, expected_event_name: " " },
    } as never)).toContain("expected event name");
    expect(workflowRunResumeEventNameIssue({
      status: "waiting_event",
      waitMode: "wait_for_event",
      expectedEventName: " ",
      resumeEventName: "ready",
    })).toBeNull();
    expect(workflowRunResumeEventNameIssue({
      status: "waiting_event",
      waitMode: "wait_for_event",
      expectedEventName: "ready",
      resumeEventName: " ",
    })).toBeNull();
  });

  it("covers checkpoint identity agreement and every missing checkpoint field", () => {
    expect(workflowRunCheckpointIdentityIssue(
      run("waiting_approval"),
      {
        checkpoint_id: "cp-runtime",
        node_id: "agent",
        state_blob: {
          node_id: "agent",
          kind: "runtime_approval",
          input_by_port: { request: { id: "R-1" } },
        },
      } as never,
    )).toBeNull();

    const missingFields = workflowRunCheckpointIdentityIssue(
      run("waiting_event", { current_node_id: "agent" }),
      {
        checkpoint_id: "cp-missing",
        node_id: null,
        state_blob: { kind: "wait_for_event", input_by_port: null },
      } as never,
    );
    expect(missingFields).toContain("checkpoint row has no node_id");
    expect(missingFields).toContain("checkpoint payload has no node_id");
    expect(missingFields).toContain("wait checkpoint has no input snapshot");
    expect(missingFields).toContain("expected event name");

    const disagreement = workflowRunCheckpointIdentityIssue(
      run("waiting_event", { current_node_id: "agent" }),
      {
        checkpoint_id: "cp-disagree",
        node_id: "other",
        state_blob: {
          node_id: "third",
          kind: "wait_until",
          input_by_port: {},
        },
      } as never,
    );
    expect(disagreement).toContain("row points at other");
    expect(disagreement).toContain("payload points at third");
    expect(disagreement).toContain("row and payload disagree");
  });

  it("returns a false download result when browser object-URL support is unavailable", () => {
    vi.stubGlobal("URL", { createObjectURL: undefined });
    expect(triggerServiceOpenApiDownload(new Blob(["{}"]))
      ).toBe(false);
    vi.unstubAllGlobals();
  });

  it("covers empty history guidance and API error phrase families", () => {
    for (const workflowStatus of ["paused", "archived", null]) {
      for (const queueRunsEnabled of [false, true]) {
        expect(emptyRunHistoryMessage({
          capabilitiesUnavailable: false,
          queueRunsEnabled,
          workflowStatus,
          runAlias: "prod",
          runVersionId: null,
        })).toContain("first");
      }
    }
    expect(emptyRunHistoryMessage({
      capabilitiesUnavailable: false,
      queueRunsEnabled: true,
      workflowStatus: null,
      runAlias: "manual",
      runVersionId: "v1",
    })).toContain("Queue a run");

    for (const text of [
      "workflow run retry manifest is invalid",
      "persisted manifest snapshot is invalid",
      "cannot resume while runtime approval decision is pending",
      "has no approved runtime approval decision to resume from",
      "resume checkpoint is corrupt",
      "no waiting workflow run matched event",
      "corrupt resume checkpoints",
      "missing correlation_value",
      "legacy wait_event checkpoints",
    ]) {
      const error = api(text);
      expect(
        workflowRunRetryFailureMessage(error)
        || workflowRunResumeFailureMessage(error)
        || workflowRunResumeByEventFailureMessage(error),
      ).toContain("failed");
    }
  });

  it("covers the remaining recovery, filtering, and payload decision table", () => {
    for (const text of [
      "workflow run retry checkpoint is corrupt",
      "workflow run retry manifest is invalid",
      "persisted manifest snapshot is missing",
    ]) {
      expect(workflowRunRetryFailureMessage(api(text))).toContain("Retry failed");
    }
    expect(workflowRunRetryFailureMessage(new Error("retry"))).toBeNull();

    for (const text of [
      "has no pending runtime approvals",
      "runtime approval is not pending",
      "is not waiting for approval",
      "approval checkpoint is corrupt",
      "runtime approval not found for workflow run",
    ]) {
      expect(workflowRunApprovalActionFailureMessage("Approve", api(text))).toContain("Approve failed");
    }
    expect(workflowRunApprovalActionFailureMessage("Reject", new Error("approval"))).toBeNull();

    for (const text of [
      "cannot resume while runtime approval decision is pending",
      "cannot resume after runtime approval rejection",
      "has no approved runtime approval decision to resume from",
      "has no resume checkpoint",
      "resume checkpoint is corrupt",
      "approval checkpoint is missing",
      "is not resumable from checkpoint",
    ]) {
      expect(workflowRunResumeFailureMessage(api(text))).toContain("Resume failed");
    }
    expect(workflowRunResumeFailureMessage(new Error("resume"))).toBeNull();

    for (const text of [
      "no waiting workflow run matched event",
      "matched multiple waiting workflow runs",
      "corrupt resume checkpoints",
      "missing correlation_value",
      "legacy wait_event checkpoints",
    ]) {
      expect(workflowRunResumeByEventFailureMessage(api(text))).toContain("External event resume failed");
    }
    expect(workflowRunResumeByEventFailureMessage(new Error("event"))).toBeNull();

    const noArtifacts = run("completed", { summary: { artifact_persistence: null } });
    const failed = run("failed", {
      summary: {
        artifact_persistence: {
          status: "failed",
          bucket: "failed-bucket",
          object_count: 0,
          error: "write denied",
          artifact_names: ["trace.json"],
        },
      },
    });
    const stored = run("completed", {
      summary: {
        artifact_persistence: {
          status: "persisted",
          bucket: "stored-bucket",
          object_count: 1,
          artifact_names: ["answer.json"],
        },
      },
    });
    expect(runArtifactFilterMatches(noArtifacts, "upload_failed")).toBe(false);
    expect(runArtifactFilterMatches(noArtifacts, "artifacts_stored")).toBe(false);
    expect(runArtifactFilterMatches(failed, "artifacts_stored")).toBe(false);
    expect(runArtifactFilterMatches(stored, "upload_failed")).toBe(false);
    for (const query of [
      ["run-1", true],
      ["version", true],
      ["staging", true],
      ["failed", true],
      ["write denied", true],
      ["failed-bucket", true],
      ["trace.json", true],
    ] as const) {
      expect(runSearchMatches({ ...failed, deployment_alias: "staging", workflow_version_id: "version" }, query[0])).toBe(query[1]);
    }
    expect(runSearchMatches(stored, "answer.json")).toBe(true);
    expect(runSearchMatches(stored, "stored-bucket")).toBe(true);
    expect(runSearchMatches({ ...run(), summary: [] }, "missing")).toBe(false);
    expect(runSearchMatches({ ...run(), summary: { error: 42 } }, "42")).toBe(false);

    expect(readNumber(null)).toBeNull();
    expect(readNumber(Number.POSITIVE_INFINITY)).toBeNull();
    expect(readNumber({})).toBeNull();
    expect(() => parseJsonObjectText("not json")).toThrow();
    expect(formatResumeEventPayload("already formatted")).toBe("already formatted");
    expect(formatResumeEventPayload(42)).toBe("42");
    expect(workflowRunResumeCorrelationMatches("1", "1")).toBe(true);
    expect(workflowRunResumeCorrelationMatches("1", 1)).toBe(false);
    expect(workflowRunResumeCorrelationMatches([1], [1])).toBe(true);
    expect(workflowRunResumeCorrelationValueLabel({ id: 1 })).toBe('{"id":1}');

    expect(waitUntilDisplay({ type: "agent" } as never)).toBe("the configured time");
    expect(waitUntilDisplay({ type: "wait_until", wait_until: "2026-01-01Z", timezone: "UTC" } as never)).toBe("2026-01-01Z");
    expect(waitUntilDisplay({ type: "wait_until", wait_until: "2026-01-01+02:00", timezone: "UTC" } as never)).toBe("2026-01-01+02:00");
    expect(checkpointWaitUntilDisplay({ wait_until: "2026-01-01+02:00", timezone: "UTC" })).toBe("2026-01-01+02:00");

    expect(workflowRunCheckpointIdentityIssue(run("waiting_event"), {
      node_id: "other",
      state_blob: { node_id: "third", kind: "other", input_by_port: [] },
    } as never)).toContain("instead of agent");
    expect(workflowRunCheckpointIdentityIssue(run("waiting_approval"), {
      node_id: "agent",
      state_blob: { node_id: "agent", kind: "runtime_approval", input_by_port: {} },
    } as never)).toBeNull();
    expect(workflowRunCheckpointIdentityIssue(run("waiting_event"), {
      node_id: "agent",
      state_blob: { node_id: "agent", kind: "wait_event", input_by_port: {} },
    } as never)).toBeNull();
    expect(workflowRunCheckpointIdentityIssue(run("waiting_event"), {
      node_id: "agent",
      state_blob: { node_id: "agent", kind: "wait_for_event", input_by_port: {} },
    } as never)).toContain("expected event name");
    expect(workflowRunCheckpointIdentityIssue(run("waiting_event"), {
      node_id: "agent",
      state_blob: { node_id: "agent", kind: "wait_until", input_by_port: [] },
    } as never)).toContain("no input snapshot");

    expect(workflowRunResumeEventNameIssue({
      status: "waiting_event",
      waitMode: "wait_for_event",
      expectedEventName: " ready ",
      resumeEventName: " ready ",
    })).toBeNull();
    expect(workflowRunResumeByEventIssue({
      status: "waiting_event",
      waitMode: "wait_event",
      correlationKey: "id",
      correlationValue: "1",
      resumeEventPayload: "{}",
    })).toContain("legacy wait_event");
    expect(workflowRunResumeByEventIssue({
      status: "waiting_event",
      waitMode: "wait_for_event",
      correlationKey: " ",
      correlationValue: "1",
      resumeEventPayload: "{}",
    })).toBeNull();
    expect(workflowRunResumeByEventIssue({
      status: "waiting_event",
      waitMode: "wait_for_event",
      correlationKey: "id",
      correlationValue: null,
      resumeEventPayload: "{}",
    })).toContain("did not capture");
    expect(workflowRunResumeByEventIssue({
      status: "waiting_event",
      waitMode: "wait_for_event",
      correlationKey: "id",
      correlationValue: "1",
      resumeEventPayload: "invalid",
    })).toBeNull();
    expect(workflowRunResumeByEventIssue({
      status: "waiting_event",
      waitMode: "wait_for_event",
      correlationKey: "id",
      correlationValue: { value: 1 },
      resumeEventPayload: JSON.stringify({ id: { value: 2 } }),
    })).toContain("id={\"value\":1}");
    expect(workflowRunResumeByEventIssue({
      status: "waiting_event",
      waitMode: "wait_for_event",
      correlationKey: "id",
      correlationValue: { value: 1 },
      resumeEventPayload: JSON.stringify({ id: { value: 1 } }),
    })).toBeNull();
  });

  it("covers trigger badge variants, sparse steps, and search payload fallbacks", () => {
    const { rerender } = render(createElement(TriggerBadge, { trigger: null }));
    expect(screen.getByText(/Manual/)).toBeInTheDocument();
    rerender(createElement(TriggerBadge, { trigger: { mode: "event", event_name: "ticket.updated", enabled: true } as never }));
    expect(screen.getByTestId("workflow-trigger-badge")).toHaveTextContent("Event: ticket.updated");
    rerender(createElement(TriggerBadge, { trigger: { mode: "event", enabled: false } as never }));
    expect(screen.getByTestId("workflow-trigger-badge")).toHaveTextContent("(off)");
    rerender(createElement(TriggerBadge, { trigger: { mode: "cron", cron: "0 * * * *" } as never }));
    expect(screen.getByTestId("workflow-trigger-badge")).toHaveTextContent("Cron: 0 * * * *");

    expect(normalizeRunStep({
      node_id: "tool", node_type: "tool", input_by_port: [], output_by_port: [],
      tool_calls: "not-an-array", handoff_target: 3, detail: 3, duration_ms: "3",
    })).toMatchObject({ input_by_port: null, output_by_port: null, tool_calls: [], handoff_target: null, detail: "", duration_ms: 0 });
    const manifest = { nodes: { agent: {}, step: {} } } as never;
    expect(defaultSelectedRunNodeId(run("running", {
      current_node_id: "missing",
      summary: { steps: [{ node_id: 3 }, { node_type: "tool" }], node_path: ["also-missing", "step"] },
    }), manifest)).toBe("step");
    expect(defaultSelectedRunNodeId(run("running", {
      current_node_id: "missing", summary: { steps: [{ node_type: "tool" }], node_path: [] },
    }), manifest)).toBeNull();

    expect(runSearchMatches(run("completed", { summary: ["not", "an", "object"] }), "not")).toBe(false);
    expect(runSearchMatches(run("completed", {
      summary: { artifact_persistence: { status: "failed", bucket: "results", object_count: 0, error: "upload timeout", artifact_names: ["trace.json"] } },
    }), "upload timeout")).toBe(true);
    expect(runSearchMatches(run("completed", {
      summary: { artifact_persistence: { status: "persisted", bucket: "results", object_count: 1, artifact_names: ["trace.json"] } },
    }), "artifacts stored")).toBe(true);
    expect(runArtifactFilterMatches(run(), "upload_failed")).toBe(false);
    expect(runArtifactFilterMatches(run(), "artifacts_stored")).toBe(false);
    expect(filteredRunHistoryMessage({ search: "x", filter: "all" })).toContain("No runs match");
  });
});
