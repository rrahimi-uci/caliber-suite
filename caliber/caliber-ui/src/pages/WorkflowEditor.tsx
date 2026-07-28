/**
 * Workflow Editor — n8n-inspired three-panel surface.
 *
 * Clean monochromatic layout: a left rail (component palette + node outline),
 * the React Flow canvas in the center, and the inspector on the right; a
 * problems panel runs along the bottom. The toolbar drives Validate / Preview /
 * Save / Publish with compact workflow-builder button variants.
 *
 * Supports drag-and-drop from the palette onto the canvas and quick-add "+"
 * buttons on nodes for rapid wiring.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ApiError, caliberApi } from "@/api/caliberApi";
import type {
  ManifestEdge,
  ManifestNode,
  PlatformCapabilities,
  PortSpec,
  PreviewResult,
  PreviewStep,
  Workflow,
  WorkflowRun,
  WorkflowRunManifest,
  WorkflowRunCheckpoint,
  WorkflowRunEvent,
  WorkflowRunLineage,
  WorkflowRuntimeApproval,
  WorkflowSessionMemoryEntry,
  WorkflowRunStep,
  ToolDefinition,
  ValidationReport,
  WorkflowComponent,
  WorkflowManifest,
} from "@/api/workflowTypes";
import { CollapsiblePanel } from "@/components/CollapsiblePanel";
import { Canvas } from "@/components/workflows/Canvas";
import { CodeView } from "@/components/workflows/CodeView";
import { NodeCodeModal } from "@/components/workflows/NodeCodeModal";
import { WorkflowCopilot } from "@/components/workflows/WorkflowCopilot";
import { WorkflowPlanPanel } from "@/components/workflows/WorkflowPlanPanel";
import { ConnectMapPopover } from "@/components/workflows/ConnectMapPopover";
import { Inspector } from "@/components/workflows/Inspector";
import { NodePalette } from "@/components/workflows/NodePalette";
import { NodeIcon } from "@/components/workflows/NodeIcon";
import { ProblemsPanel } from "@/components/workflows/ProblemsPanel";
import { PublishDrawer } from "@/components/workflows/PublishDrawer";
import { RunFilePanel } from "@/components/workflows/RunFilePanel";
import { TraceReplayGraph } from "@/components/workflows/TraceReplayGraph";
import { WorkflowRunArtifactPersistenceBadge } from "@/components/workflows/WorkflowRunArtifactPersistenceBadge";
import { WorkflowRunLineagePanel } from "@/components/workflows/WorkflowRunLineagePanel";
import { WorkflowRunRecoveryPanel } from "@/components/workflows/WorkflowRunRecoveryPanel";
import { StepPreview } from "@/components/workflows/StepPreview";
import {
  checkpointLoadErrorMessage,
  WorkflowRunCheckpointPanel,
} from "@/components/workflows/WorkflowRunCheckpointPanel";
import {
  runEventsLoadErrorMessage,
  WorkflowRunDebugger,
} from "@/components/workflows/WorkflowRunDebugger";
import {
  sessionMemoryLoadErrorMessage,
  WorkflowSessionMemoryPanel,
} from "@/components/workflows/WorkflowSessionMemoryPanel";
import { useEventStream, type CaliberEvent } from "@/hooks/useEventStream";
import { useApiMutation, useApiQuery, useInvalidate } from "@/hooks/useApiQuery";
import {
  buildNodePalette,
  buildNodeExecutionBadgeMap,
  canConnectNodes,
  deriveEdgeMap,
  ensureAgentToolBindings,
  type FlowNodePosition,
  isWorkflowNodeType,
  makeEdgeId,
  nodeInputs,
  nodeLabel,
  nodeOutputs,
  type NodePaletteItem,
  nodeColor,
} from "@/lib/workflowGraph";
import { showToast } from "@/lib/toast";
import {
  approvalCheckpointKind,
  workflowRunApprovalNoun,
  workflowRunApprovalSubject,
  workflowRunLifecycleMessage,
  workflowRunStatusBorderClass,
  workflowRunStatusFromEventType,
  workflowRunStatusFromStep,
  workflowRunStatusLabel,
  workflowRunStatusMessage,
  workflowRunStatusPhrase,
  workflowRunStatusVerbPhrase,
} from "@/lib/workflowRunLabels";
import { workflowRunPath, workflowRunUrl } from "@/lib/workflowRunLinks";
import {
  buildSyntheticWorkflowRunManifest,
  mergeWorkflowRunCheckpoints,
  resolveWorkflowRunActiveCheckpoint,
  workflowRunHasInheritedResumeCheckpoint,
  workflowRunResumeCheckpointId,
  workflowRunResumeCheckpointRunId,
} from "@/lib/workflowRunSummary";

/* ─── Quick-add popup menu (n8n-style) ─── */

interface QuickAddState {
  sourceId: string;
  screenX: number;
  screenY: number;
}

type ScreenPoint = { x: number; y: number };
type NodeLayoutMap = Record<string, FlowNodePosition>;

const RUN_ACTIVE_STATUSES = new Set([
  "queued",
  "running",
  "waiting_approval",
  "waiting_event",
  "resuming",
]);

const RUN_TERMINAL_STATUSES = new Set([
  "completed",
  "failed",
  "cancelled",
  "rejected",
  "expired",
]);
const RUN_MONITOR_REFRESH_INTERVAL_MS = 2000;
const EDITOR_RUN_HISTORY_PAGE_SIZE = 12;
const WORKFLOW_RUN_EVENTS = [
  "workflow.deleted",
  "workflow.paused",
  "workflow.resumed",
  "workflow.updated",
  "workflow.run.queued",
  "workflow.run.recovered",
  "workflow.run.retried",
  "workflow.run.started",
  "workflow.run.node_started",
  "workflow.run.step",
  "workflow.run.approval.approved",
  "workflow.run.approval.rejected",
  "workflow.run.cancel_requested",
  "workflow.run.waiting_approval",
  "workflow.run.waiting_event",
  "workflow.run.resumed",
  "workflow.run.cancelled",
  "workflow.run.completed",
  "workflow.run.expired",
  "workflow.run.failed",
];
const STREAM_RUN_EVENT_META_KEYS = new Set([
  "type",
  "workflow_id",
  "workflow_version_id",
  "workflow_run_id",
  "event_id",
  "sequence",
  "created_at",
  "node_id",
]);

interface RunMonitorSnapshot {
  run: WorkflowRun;
  events: WorkflowRunEvent[];
  eventsError: string | null;
  checkpoints: WorkflowRunCheckpoint[];
  checkpointsError: string | null;
  approvals: WorkflowRuntimeApproval[];
  approvalsReady: boolean;
  approvalsError: string | null;
}

function waitForEventOutputPorts(): Record<string, PortSpec> {
  return {
    output: { type: "string" },
    event_payload: { type: "structured" },
    event_name: { type: "string" },
  };
}

const STARTER_NODE_ID_MARKER = "__CALIBER_NODE_ID__";
const STARTER_NODE_NOW_PLUS_60S_ISO_MARKER = "__CALIBER_NOW_PLUS_60S_ISO__";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function materializeStarterNodeValue(value: unknown, id: string): unknown {
  if (value === STARTER_NODE_ID_MARKER) return id;
  if (value === STARTER_NODE_NOW_PLUS_60S_ISO_MARKER) {
    return new Date(Date.now() + 60_000).toISOString();
  }
  if (Array.isArray(value)) {
    return value.map((item) => materializeStarterNodeValue(item, id));
  }
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.entries(value).map(([key, nested]) => [key, materializeStarterNodeValue(nested, id)]),
    );
  }
  return value;
}

function starterNodeFromComponent(
  type: string,
  id: string,
  componentSpec?: WorkflowComponent | null,
): ManifestNode | null {
  if (!componentSpec?.starter_node || componentSpec.type !== type) return null;
  const materialized = materializeStarterNodeValue(componentSpec.starter_node, id);
  if (!isRecord(materialized)) return null;
  return {
    ...(materialized as Partial<ManifestNode>),
    id,
    type: type as ManifestNode["type"],
  } as ManifestNode;
}

function hasStructuredAgentOutputPort(outputs?: Record<string, PortSpec>): boolean {
  if (!outputs) return false;
  return Object.entries(outputs).some(
    ([port, spec]) =>
      spec?.type === "structured" && port !== "history" && port !== "tool_calls",
  );
}

function normalizeWorkflowManifest(manifest: WorkflowManifest): WorkflowManifest {
  let changed = false;
  const nodes = Object.fromEntries(
    Object.entries(manifest.nodes).map(([nodeId, node]) => {
      let nextNode = node;
      let nodeChanged = false;
      if (node.type === "wait_for_event") {
        const outputs = { ...(node.outputs ?? {}) };
        if (!outputs.output) {
          outputs.output = { type: "string" };
          nodeChanged = true;
        }
        if (!outputs.event_payload) {
          outputs.event_payload = { type: "structured" };
          nodeChanged = true;
        }
        if (!outputs.event_name) {
          outputs.event_name = { type: "string" };
          nodeChanged = true;
        }
        if (nodeChanged) nextNode = { ...nextNode, outputs };
      }
      if (
        nextNode.type === "agent" &&
        nextNode.output_type &&
        !hasStructuredAgentOutputPort(nextNode.outputs)
      ) {
        nextNode = {
          ...nextNode,
          outputs: {
            ...(nextNode.outputs ?? {}),
            structured_output: { type: "structured" },
          },
        };
        nodeChanged = true;
      }
      if (!nodeChanged) return [nodeId, node];
      changed = true;
      return [nodeId, nextNode];
    }),
  ) as WorkflowManifest["nodes"];
  return changed ? { ...manifest, nodes } : manifest;
}

function parseJsonObjectText(raw: string): unknown {
  const trimmed = raw.trim();
  if (!trimmed) return undefined;
  return JSON.parse(trimmed);
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function formatResumeEventPayload(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function workflowRunResumeCorrelationMatches(actual: unknown, expected: unknown): boolean {
  if (Object.is(actual, expected)) return true;
  if (
    actual
    && expected
    && typeof actual === "object"
    && typeof expected === "object"
  ) {
    try {
      return JSON.stringify(actual) === JSON.stringify(expected);
    } catch {
      return false;
    }
  }
  return false;
}

function workflowRunResumeCorrelationValueLabel(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null) return "null";
  if (value === undefined) return "undefined";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function waitUntilDisplay(node: ManifestNode | null): string {
  if (!node || node.type !== "wait_until") return "the configured time";
  const raw = typeof node.wait_until === "string" && node.wait_until.trim() ? node.wait_until.trim() : "the configured time";
  const needsTimezone = !/[zZ]$/.test(raw) && !/[+-]\d{2}:\d{2}$/.test(raw);
  const timezoneName = typeof node.timezone === "string" && node.timezone.trim() ? node.timezone.trim() : "";
  return needsTimezone && timezoneName ? `${raw} (${timezoneName})` : raw;
}

function checkpointWaitUntilDisplay(state: Record<string, unknown> | null): string | null {
  if (!state) return null;
  const raw = readString(state.wait_until) ?? readString(state.resume_at);
  if (!raw) return null;
  const needsTimezone = !/[zZ]$/.test(raw) && !/[+-]\d{2}:\d{2}$/.test(raw);
  const timezoneName = readString(state.timezone);
  return needsTimezone && timezoneName ? `${raw} (${timezoneName})` : raw;
}

function workflowRunCheckpointIdentityIssue(
  run: Pick<WorkflowRun, "status" | "current_node_id"> | null | undefined,
  checkpoint: Pick<WorkflowRunCheckpoint, "checkpoint_id" | "node_id" | "state_blob"> | null | undefined,
): string | null {
  if (!run || !checkpoint) return null;
  if (run.status !== "waiting_event" && run.status !== "waiting_approval") return null;
  const currentNodeId = readString(run.current_node_id);
  const checkpointNodeId = readString(checkpoint.node_id);
  const state = isRecord(checkpoint.state_blob) ? checkpoint.state_blob : null;
  const stateNodeId = readString(state?.node_id);
  const issues: string[] = [];
  if (!state) issues.push("the checkpoint payload is corrupt");
  if (!currentNodeId) issues.push("the run has no active node recorded");
  if (!checkpointNodeId) issues.push("the checkpoint row has no node_id");
  if (state && !stateNodeId) issues.push("the checkpoint payload has no node_id");
  if (currentNodeId && checkpointNodeId && checkpointNodeId !== currentNodeId) {
    issues.push(`the checkpoint row points at ${checkpointNodeId} instead of ${currentNodeId}`);
  }
  if (currentNodeId && stateNodeId && stateNodeId !== currentNodeId) {
    issues.push(`the checkpoint payload points at ${stateNodeId} instead of ${currentNodeId}`);
  }
  if (checkpointNodeId && stateNodeId && checkpointNodeId !== stateNodeId) {
    issues.push(`the checkpoint row and payload disagree (${checkpointNodeId} vs ${stateNodeId})`);
  }
  if (run.status === "waiting_approval" && state) {
    if (state.kind !== "human_approval" && state.kind !== "runtime_approval") {
      issues.push("the approval checkpoint kind is invalid");
    }
    if (!isRecord(state.input_by_port)) {
      issues.push("the approval checkpoint has no input snapshot");
    }
  }
  if (run.status === "waiting_event" && state) {
    if (state.kind !== "wait_for_event" && state.kind !== "wait_until" && state.kind !== "wait_event") {
      issues.push("the wait checkpoint kind is invalid");
    }
    if (!isRecord(state.input_by_port)) {
      issues.push("the wait checkpoint has no input snapshot");
    }
    if (state.kind === "wait_for_event" && !readString(state.expected_event_name)) {
      issues.push("the wait-for-event checkpoint has no expected event name");
    }
  }
  if (issues.length === 0) return null;
  return `Resume is unavailable because the stored checkpoint no longer matches this run's active node: ${issues.join("; ")}. Inspect the checkpoint and recovery panels, then retry from a healthy checkpoint or start a new attempt instead of resuming this run.`;
}

function workflowRunResumeEventNameIssue({
  status,
  waitMode,
  expectedEventName,
  resumeEventName,
}: {
  status: string | null | undefined;
  waitMode: string | null | undefined;
  expectedEventName: string;
  resumeEventName: string;
}): string | null {
  if (status !== "waiting_event" || waitMode !== "wait_for_event") return null;
  const expected = expectedEventName.trim();
  const actual = resumeEventName.trim();
  if (!expected || !actual || actual === expected) return null;
  return `Resume is unavailable because this wait gate is configured for event ${expected}, but the current event name is ${actual}. Restore the configured event name before manually resuming or matching this event against waiting runs.`;
}

function workflowRunResumeByEventIssue({
  status,
  waitMode,
  correlationKey,
  correlationValue,
  resumeEventPayload,
}: {
  status: string | null | undefined;
  waitMode: string | null | undefined;
  correlationKey: string;
  correlationValue: unknown;
  resumeEventPayload: string;
}): string | null {
  if (status !== "waiting_event") return null;
  if (waitMode === "wait_event") {
    return "Event matching is unavailable because this paused checkpoint uses the legacy wait_event shape, which cannot be targeted by workflow-wide event matching. Resume this run directly by run_id instead.";
  }
  if (waitMode !== "wait_for_event") return null;
  const key = correlationKey.trim();
  if (!key) {
    return null;
  }
  if (correlationValue === null || correlationValue === undefined || correlationValue === "") {
    return `Event matching is unavailable because this wait gate requires correlation field ${key}, but the paused checkpoint did not capture a correlation value for this run. Resume this run directly by run_id or recreate the wait state with a populated correlation input before matching workflow-wide events.`;
  }
  let payload: unknown;
  try {
    payload = parseJsonObjectText(resumeEventPayload);
  } catch {
    return null;
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return `Event matching is unavailable because this wait gate requires correlation field ${key}=${workflowRunResumeCorrelationValueLabel(correlationValue)} in the event payload. Add that field/value before matching this event against waiting runs.`;
  }
  if (!workflowRunResumeCorrelationMatches((payload as Record<string, unknown>)[key], correlationValue)) {
    return `Event matching is unavailable because this wait gate requires correlation field ${key}=${workflowRunResumeCorrelationValueLabel(correlationValue)} in the event payload. Add that field/value before matching this event against waiting runs.`;
  }
  return null;
}

function readNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function savedWorkflowVersionLabel(
  workflowVersionId: string | null | undefined,
  workflowVersionNumber: number | null | undefined,
): string | null {
  const normalizedId =
    typeof workflowVersionId === "string" && workflowVersionId.trim()
      ? workflowVersionId.trim()
      : null;
  if (workflowVersionNumber != null) {
    return normalizedId
      ? `v${workflowVersionNumber} (${normalizedId})`
      : `v${workflowVersionNumber}`;
  }
  return normalizedId;
}

function runManifestContextLabel(
  run: Pick<WorkflowRun, "workflow_version_id"> | null,
  manifestMeta: Pick<WorkflowRunManifest, "manifest_mode"> | null,
  currentVersionId: string | undefined,
  savedVersionLabel: string | null,
): string {
  if (manifestMeta?.manifest_mode === "snapshot") {
    return "the queued draft snapshot captured for this run";
  }
  if (run?.workflow_version_id && run.workflow_version_id !== currentVersionId) {
    return `saved workflow version ${savedVersionLabel ?? run.workflow_version_id}`;
  }
  return "the saved workflow version used by this run";
}

function runMonitorManifestUnavailableMessage({
  manifestMode,
  hasSummarySteps,
  hasCurrentNode,
  hasCheckpoints,
}: {
  manifestMode: WorkflowRunManifest["manifest_mode"] | null;
  hasSummarySteps: boolean;
  hasCurrentNode: boolean;
  hasCheckpoints: boolean;
}): string {
  const recoverySuffix =
    hasCheckpoints || hasSummarySteps || hasCurrentNode
      ? " Inspect the recovery, checkpoint, and retry-lineage panels in this run monitor to keep tracing the persisted execution evidence until a richer graph can be restored."
      : " Inspect the recovery and retry-lineage panels in this run monitor to confirm whether any persisted execution evidence still remains for this run.";
  if (manifestMode === "snapshot") {
    return `The queued draft snapshot captured for this run is not available. Trace replay and manifest-aware debugging stay hidden until that snapshot can be restored.${recoverySuffix}`;
  }
  return `The saved workflow graph for this run could not be restored, and the recorded run history is too sparse to reconstruct it. Trace replay and manifest-aware debugging stay hidden until a persisted manifest, saved workflow version, or richer checkpoints are available.${recoverySuffix}`;
}

function runMonitorManifestLoadErrorMessage({
  run,
  manifestMode,
  versionReference,
  errorDetail,
}: {
  run: WorkflowRun;
  manifestMode: WorkflowRunManifest["manifest_mode"] | null;
  versionReference: string;
  errorDetail: string;
}): JSX.Element {
  const detail = errorDetail.trim() || "Unknown error";
  const base =
    manifestMode === "snapshot"
      ? "The queued draft snapshot captured for this run could not be restored for trace replay or manifest-aware debugging."
      : `The persisted run manifest and ${versionReference} could not be restored for trace replay or manifest-aware debugging.`;
  if (run.status === "waiting_approval" || run.status === "waiting_event") {
    return (
      <>
        {base} Use the recovery, checkpoint, and retry-lineage panels in this run monitor to
        trace the active gate until a restorable graph becomes available again.
        <span className="mt-2 block text-red-700/80">Latest graph error: {detail}</span>
      </>
    );
  }
  if (
    run.status === "queued"
    || run.status === "running"
    || run.status === "resuming"
    || run.status === "cancel_requested"
  ) {
    return (
      <>
        {base} Use the debugger, recovery, and checkpoint panels in this run monitor to confirm
        live execution state while the graph remains unavailable.
        <span className="mt-2 block text-red-700/80">Latest graph error: {detail}</span>
      </>
    );
  }
  if (run.status === "completed") {
    return (
      <>
        {base} Use the debugger, final outputs, generated artifacts, and retry lineage in this
        run monitor to reconstruct how execution finished until the graph can be restored.
        <span className="mt-2 block text-red-700/80">Latest graph error: {detail}</span>
      </>
    );
  }
  if (
    run.status === "failed"
    || run.status === "cancelled"
    || run.status === "rejected"
    || run.status === "expired"
    || run.status === "blocked"
  ) {
    return (
      <>
        {base} Use the debugger, recovery diagnostics, checkpoint trail, and retry lineage in
        this run monitor to trace where execution stopped.
        <span className="mt-2 block text-red-700/80">Latest graph error: {detail}</span>
      </>
    );
  }
  return (
    <>
      {base} Use the recovery, checkpoint, and retry-lineage panels in this run monitor to keep
      tracing persisted execution evidence until the graph can be restored.
      <span className="mt-2 block text-red-700/80">Latest graph error: {detail}</span>
    </>
  );
}

function syntheticRunMonitorGraphFallbackMessage(run: WorkflowRun): string {
  const base =
    "Replay and debugging are using a graph reconstructed from recorded run history and checkpoints because neither the persisted run manifest nor its saved workflow version could be restored.";
  if (run.status === "waiting_approval" || run.status === "waiting_event") {
    return `${base} Use this reconstructed graph to follow the active gate, but rely on the recovery and checkpoint panels in this run monitor for authoritative resume state until a full manifest can be restored.`;
  }
  if (
    run.status === "queued"
    || run.status === "running"
    || run.status === "resuming"
    || run.status === "cancel_requested"
  ) {
    return `${base} Use this reconstructed graph as a best-effort replay while execution is still in flight, and confirm live state with the debugger, recovery, and checkpoint panels in this run monitor.`;
  }
  if (run.status === "completed") {
    return `${base} Use this reconstructed graph as a best-effort replay, then confirm the terminal result with the debugger, final outputs, and generated artifacts in this run monitor.`;
  }
  if (
    run.status === "failed"
    || run.status === "cancelled"
    || run.status === "rejected"
    || run.status === "expired"
    || run.status === "blocked"
  ) {
    return `${base} Use this reconstructed graph as a best-effort replay, then rely on the debugger, recovery diagnostics, and retry lineage in this run monitor to trace where execution stopped.`;
  }
  return `${base} Use the recovery, checkpoint, and debugger panels in this run monitor to confirm any execution details that the reconstructed graph cannot prove on its own.`;
}

function savedVersionRunMonitorGraphFallbackMessage(
  run: WorkflowRun,
  versionReference: string,
): string {
  const base =
    `Replay and debugging are using ${versionReference} because the persisted run manifest could not be loaded separately.`;
  if (run.status === "waiting_approval" || run.status === "waiting_event") {
    return `${base} Use that restored version as the last known workflow graph, but rely on the recovery and checkpoint panels in this run monitor for authoritative resume state until the persisted manifest can be restored.`;
  }
  if (
    run.status === "queued"
    || run.status === "running"
    || run.status === "resuming"
    || run.status === "cancel_requested"
  ) {
    return `${base} Use that restored version as a best-effort replay while execution is still in flight, and confirm live state with the debugger, recovery, and checkpoint panels in this run monitor.`;
  }
  if (run.status === "completed") {
    return `${base} Use that restored version as the last known workflow graph, then confirm the terminal result with the debugger, final outputs, and generated artifacts in this run monitor.`;
  }
  if (
    run.status === "failed"
    || run.status === "cancelled"
    || run.status === "rejected"
    || run.status === "expired"
    || run.status === "blocked"
  ) {
    return `${base} Use that restored version as the last known workflow graph, then rely on the debugger, recovery diagnostics, and retry lineage in this run monitor to trace where execution stopped.`;
  }
  return `${base} Use the recovery, checkpoint, and debugger panels in this run monitor to confirm any execution details that may have diverged from the restored graph.`;
}

function snapshotRunMonitorContextNote(run: WorkflowRun): string {
  const base =
    "This run is pinned to the unsaved draft snapshot captured when you queued it. The canvas and inspector still show the draft currently open in the editor, which may differ from the running snapshot.";
  if (run.status === "waiting_approval" || run.status === "waiting_event") {
    return `${base} Use the recovery and checkpoint panels in this run monitor for authoritative resume state while the snapshot-backed run is paused.`;
  }
  if (
    run.status === "queued"
    || run.status === "running"
    || run.status === "resuming"
    || run.status === "cancel_requested"
  ) {
    return `${base} Treat the canvas as future edits only while this snapshot-backed run is still executing, and confirm live state with the debugger, recovery, and checkpoint panels in this run monitor.`;
  }
  if (run.status === "completed") {
    return `${base} Compare the debugger, final outputs, and generated artifacts in this run monitor before making follow-up edits to the current draft.`;
  }
  if (
    run.status === "failed"
    || run.status === "cancelled"
    || run.status === "rejected"
    || run.status === "expired"
    || run.status === "blocked"
  ) {
    return `${base} Use the debugger, recovery diagnostics, and retry lineage in this run monitor before editing the current draft to address the stopped execution.`;
  }
  return `${base} Use the debugger and recovery panels in this run monitor to confirm which graph the current attempt actually executed.`;
}

function historicalVersionRunMonitorContextNote(
  run: WorkflowRun,
  versionReference: string,
): string {
  const base =
    `This monitor is replaying ${versionReference}. The canvas and inspector still reflect the draft currently open in the editor.`;
  if (run.status === "waiting_approval" || run.status === "waiting_event") {
    return `${base} Use that restored version as last-known graph context, but rely on the recovery and checkpoint panels in this run monitor for authoritative resume state.`;
  }
  if (
    run.status === "queued"
    || run.status === "running"
    || run.status === "resuming"
    || run.status === "cancel_requested"
  ) {
    return `${base} Treat the canvas as future edits only while this run is still executing, and confirm live state with the debugger, recovery, and checkpoint panels in this run monitor.`;
  }
  if (run.status === "completed") {
    return `${base} Compare the debugger, final outputs, and generated artifacts in this run monitor before making follow-up edits to the current draft.`;
  }
  if (
    run.status === "failed"
    || run.status === "cancelled"
    || run.status === "rejected"
    || run.status === "expired"
    || run.status === "blocked"
  ) {
    return `${base} Use the debugger, recovery diagnostics, and retry lineage in this run monitor before editing the current draft to address the stopped execution.`;
  }
  return `${base} Use the debugger and recovery panels in this run monitor to confirm how the restored graph differs from the draft currently open in the editor.`;
}

function approvalCapabilityMessage(approvalSubject: string): string {
  return `This run is waiting on a ${approvalSubject}, but runtime approval controls are disabled for this deployment. Enable runtime approvals for this deployment, then approve or reject the pending request to continue.`;
}

function runActionErrorDetail(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

function workflowRunRetryFailureMessage(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  const detail = runActionErrorDetail(error);
  const normalized = detail.toLowerCase();
  if (
    normalized.includes("workflow run retry checkpoint")
    || normalized.includes("workflow run retry manifest is invalid")
    || normalized.includes("persisted manifest snapshot")
  ) {
    return `Retry failed because this run's stored checkpoint or manifest snapshot is no longer healthy. Inspect the recovery, checkpoint, lineage, and debugger panels in this run monitor before retrying from a different checkpoint or starting a new attempt. Latest backend detail: ${detail}`;
  }
  return null;
}

function workflowRunApprovalActionFailureMessage(
  action: "Approve" | "Reject",
  error: unknown,
): string | null {
  if (!(error instanceof ApiError)) return null;
  const detail = runActionErrorDetail(error);
  const normalized = detail.toLowerCase();
  if (
    normalized.includes("has no pending runtime approvals")
    || (
      normalized.includes("runtime approval")
      && normalized.includes("is not pending")
    )
  ) {
    return `${action} failed because no pending runtime approval is attached to this run anymore. Refresh approval history and inspect recovery diagnostics in this run monitor to confirm whether another operator already resolved it. Latest backend detail: ${detail}`;
  }
  if (normalized.includes("is not waiting for approval")) {
    return `${action} failed because this run is no longer paused for approval. Refresh the run history and recovery panels in this run monitor before trying again. Latest backend detail: ${detail}`;
  }
  if (
    normalized.includes("approval checkpoint")
    || (
      normalized.includes("runtime approval")
      && normalized.includes("not found for workflow run")
    )
  ) {
    return `${action} failed because this paused approval state is no longer healthy. Refresh approval history and inspect the recovery, checkpoint, and debugger panels in this run monitor before trying again. Latest backend detail: ${detail}`;
  }
  return null;
}

function workflowRunResumeFailureMessage(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  const detail = runActionErrorDetail(error);
  const normalized = detail.toLowerCase();
  if (
    normalized.includes("cannot resume while runtime approval decision is pending")
    || normalized.includes("cannot resume after runtime approval rejection")
    || normalized.includes("has no approved runtime approval decision to resume from")
  ) {
    return `Resume failed because the paused approval gate is no longer in a resumable state. Refresh approval history and inspect the recovery, checkpoint, and debugger panels in this run monitor before trying again. Latest backend detail: ${detail}`;
  }
  if (
    normalized.includes("has no resume checkpoint")
    || normalized.includes("resume checkpoint")
    || normalized.includes("approval checkpoint")
    || normalized.includes("is not resumable from")
  ) {
    return `Resume failed because this paused run is no longer resumable from its stored checkpoint. Inspect the recovery, checkpoint, lineage, and debugger panels in this run monitor before retrying from a healthy checkpoint or starting a new attempt. Latest backend detail: ${detail}`;
  }
  return null;
}

function workflowRunResumeByEventFailureMessage(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  const detail = runActionErrorDetail(error);
  const normalized = detail.toLowerCase();
  if (
    normalized.includes("no waiting workflow run matched event")
    || normalized.includes("matched multiple waiting workflow runs")
    || normalized.includes("corrupt resume checkpoints")
    || normalized.includes("missing correlation_value")
    || normalized.includes("legacy wait_event checkpoints")
  ) {
    return `External event resume failed because no safe waiting run could be selected for this event. Inspect the recovery, checkpoint, and lineage panels in this run monitor, then resume the target run directly or add the required event correlation before retrying. Latest backend detail: ${detail}`;
  }
  return null;
}

function runOutputEmptyMessage(run: WorkflowRun | null): string {
  if (!run) {
    return "Run a workflow to see output, then use the recovery, lineage, and debugger panels below to inspect how the execution was produced.";
  }
  switch (run.status) {
    case "waiting_approval":
    case "waiting_event":
      return `This run ${workflowRunStatusVerbPhrase(run.status)} and has not recorded a final output yet. Use the recovery diagnostics and checkpoint panels below to inspect the active gate.`;
    case "queued":
    case "running":
    case "resuming":
      return `This run ${workflowRunStatusVerbPhrase(run.status)} and has not recorded a final output yet. Use the debugger and recovery panels below to follow execution progress.`;
    case "failed":
    case "cancelled":
    case "rejected":
    case "expired":
    case "blocked":
    case "cancel_requested":
      return `This run ${workflowRunStatusVerbPhrase(run.status)} before a final output was recorded. Inspect the debugger, recovery diagnostics, and retry lineage below for the last persisted execution evidence.`;
    case "completed":
      return "This run completed without persisting a final output. Inspect the debugger, generated files, and step-level artifacts below for the recorded result.";
    default:
      return `This run ${workflowRunStatusVerbPhrase(run.status)} and has not recorded a final output yet. Use the debugger and recovery panels below to inspect the latest persisted execution evidence.`;
  }
}

function runMonitorIdleSectionMessage(
  section:
    | "recovery"
    | "lineage"
    | "trace_replay"
    | "debugger"
    | "files"
    | "checkpoints",
): string {
  switch (section) {
    case "recovery":
      return "Run a workflow or load a recent run to inspect blocked-run diagnostics, approval gates, and wait states.";
    case "lineage":
      return "Run a workflow or load a recent run to inspect retry lineage, ancestor attempts, and checkpoint retries.";
    case "trace_replay":
      return "Run a workflow or load a recent run to replay its executed node path and checkpoint flow.";
    case "debugger":
      return "Run a workflow or load a recent run to inspect step telemetry, tool calls, and persisted event history.";
    case "files":
      return "Run a workflow or load a recent run to inspect generated files, uploads, and node-level artifacts.";
    case "checkpoints":
      return "Run a workflow or load a recent run to inspect persisted checkpoints, resume gates, and inherited retry sources.";
    default:
      return "Run a workflow or load a recent run to inspect this execution surface.";
  }
}

function emptyEditorRunHistoryMessage({
  capabilitiesUnavailable,
  queueRunsEnabled,
  workflowStatus,
  versionStatus,
}: {
  capabilitiesUnavailable: boolean;
  queueRunsEnabled: boolean;
  workflowStatus: string | null;
  versionStatus: string | null;
}): string {
  if (capabilitiesUnavailable) {
    return "Workflow run capabilities could not be loaded for this deployment. Verify the CALIBER API and workflow-run settings, then refresh this editor before launching the first execution.";
  }
  if (!queueRunsEnabled) {
    return "This deployment has workflow execution disabled. Enable the run queue, then use Run to create the first editor execution history for this workflow.";
  }
  if (workflowStatus === "paused") {
    return "This workflow is paused, so new editor runs cannot start yet. Resume it first, then use Run to create the first execution history for this version.";
  }
  if (workflowStatus === "archived") {
    return "This workflow is archived, so new editor runs are disabled. Restore it first if you want to create execution history from this editor.";
  }
  if (versionStatus !== "published") {
    return "No recent workflow runs exist for this draft yet. Use Run to create the first editor execution, or publish the version first if you need deployment-triggered history.";
  }
  return "No recent workflow runs exist for this published version yet. Use Run or a deployment-triggered launch to create the first execution, then refresh this panel to inspect checkpoints, lineage, and debugger state.";
}

function normalizeRunStep(value: unknown): WorkflowRunStep | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Partial<WorkflowRunStep>;
  if (typeof raw.node_id !== "string" || typeof raw.node_type !== "string") return null;
  const inputByPort =
    raw.input_by_port && typeof raw.input_by_port === "object" && !Array.isArray(raw.input_by_port)
      ? raw.input_by_port
      : null;
  const outputByPort =
    raw.output_by_port && typeof raw.output_by_port === "object" && !Array.isArray(raw.output_by_port)
      ? raw.output_by_port
      : null;
  return {
    node_id: raw.node_id,
    node_type: raw.node_type,
    status: typeof raw.status === "string" ? raw.status : "ok",
    output: typeof raw.output === "string" ? raw.output : "",
    tool_calls: Array.isArray(raw.tool_calls) ? raw.tool_calls : [],
    handoff_target: typeof raw.handoff_target === "string" ? raw.handoff_target : null,
    detail: typeof raw.detail === "string" ? raw.detail : "",
    duration_ms: typeof raw.duration_ms === "number" ? raw.duration_ms : 0,
    input_by_port: inputByPort,
    output_by_port: outputByPort,
  };
}

function compareRunEvents(left: WorkflowRunEvent, right: WorkflowRunEvent): number {
  if (left.sequence !== right.sequence) return left.sequence - right.sequence;
  if (left.event_id !== right.event_id) return left.event_id - right.event_id;
  return left.created_at.localeCompare(right.created_at);
}

function reconcileRunSnapshot(
  run: WorkflowRun,
  events: WorkflowRunEvent[],
): WorkflowRun {
  if (events.length === 0) return run;

  let nextStatus = run.status;
  let nextNodeId = run.current_node_id ?? null;
  let sawLifecycleEvent = false;

  for (const event of [...events].sort(compareRunEvents)) {
    const payload = event.payload && typeof event.payload === "object" ? event.payload : null;
    const step = normalizeRunStep(payload?.step);
    const eventNodeId = step?.node_id ?? event.node_id ?? null;
    if (eventNodeId) {
      nextNodeId = eventNodeId;
    }
    if (event.event_type === "workflow.run.step") {
      nextStatus = workflowRunStatusFromStep(step) ?? "running";
      continue;
    }
    const mappedStatus = workflowRunStatusFromEventType(event.event_type);
    if (mappedStatus) {
      nextStatus = mappedStatus;
      sawLifecycleEvent = true;
    }
  }

  if (
    !sawLifecycleEvent &&
    !RUN_ACTIVE_STATUSES.has(run.status) &&
    nextStatus !== run.status
  ) {
    nextStatus = run.status;
    nextNodeId = run.current_node_id ?? nextNodeId;
  }

  const currentSummaryStatus = run.summary?.status ?? null;
  const nextSummary =
    run.summary || nextStatus !== currentSummaryStatus
      ? { ...(run.summary ?? {}), status: nextStatus }
      : run.summary;

  if (
    nextStatus === run.status
    && nextNodeId === (run.current_node_id ?? null)
    && (nextSummary?.status ?? null) === currentSummaryStatus
  ) {
    return run;
  }

  return {
    ...run,
    status: nextStatus,
    current_node_id: nextNodeId,
    summary: nextSummary,
  };
}

function stringifyRunEventPayload(value: unknown): string {
  try {
    return JSON.stringify(value ?? null);
  } catch {
    return String(value);
  }
}

function runEventFingerprint(event: WorkflowRunEvent): string {
  return [
    event.event_type,
    event.node_id ?? "",
    String(event.sequence),
    stringifyRunEventPayload(event.payload),
  ].join("|");
}

function mergeRunEvents(
  existing: WorkflowRunEvent[],
  incoming: WorkflowRunEvent[],
): WorkflowRunEvent[] {
  if (incoming.length === 0) return existing;
  if (existing.length === 0) return [...incoming].sort(compareRunEvents);

  const merged: WorkflowRunEvent[] = [];
  const seenIds = new Set<number>();
  const seenFingerprints = new Set<string>();

  for (const event of [...incoming, ...existing]) {
    const fingerprint = runEventFingerprint(event);
    if (event.event_id > 0 && seenIds.has(event.event_id)) continue;
    if (seenFingerprints.has(fingerprint)) continue;
    if (event.event_id > 0) seenIds.add(event.event_id);
    seenFingerprints.add(fingerprint);
    merged.push(event);
  }

  return merged.sort(compareRunEvents);
}

function workflowRunEventNodeId(event: CaliberEvent): string | null {
  if (typeof event.node_id === "string" && event.node_id.trim()) return event.node_id;
  const step = normalizeRunStep(event.step);
  return step?.node_id ?? null;
}

function workflowRunEventPayload(
  event: CaliberEvent,
  nodeId: string | null,
): Record<string, unknown> | null {
  const step = normalizeRunStep(event.step);
  switch (event.type) {
    case "workflow.run.step":
      return step ? { step } : null;
    case "workflow.run.node_started":
      return {
        ...(nodeId ? { node_id: nodeId } : {}),
        ...(typeof event.node_type === "string" ? { node_type: event.node_type } : {}),
      };
    case "workflow.run.approval.approved":
    case "workflow.run.approval.rejected":
      return {
        ...(nodeId ? { node_id: nodeId } : {}),
        ...(typeof event.runtime_approval_id === "string"
          ? { runtime_approval_id: event.runtime_approval_id }
          : {}),
        ...(typeof event.reason === "string" ? { reason: event.reason } : {}),
      };
    case "workflow.run.retried":
      return {
        ...(nodeId ? { node_id: nodeId } : {}),
        ...(typeof event.retried_run_id === "string"
          ? { retried_run_id: event.retried_run_id }
          : {}),
        ...(typeof event.checkpoint_id === "string"
          ? { checkpoint_id: event.checkpoint_id }
          : {}),
      };
    case "workflow.run.waiting_approval":
      return {
        ...(nodeId ? { node_id: nodeId } : {}),
        ...(typeof event.runtime_approval_id === "string"
          ? { runtime_approval_id: event.runtime_approval_id }
          : {}),
      };
    case "workflow.run.waiting_event":
      return nodeId ? { node_id: nodeId } : null;
    case "workflow.run.cancel_requested":
      return {
        ...(typeof event.reason === "string" ? { reason: event.reason } : {}),
        ...(typeof event.requested_by === "string"
          ? { requested_by: event.requested_by }
          : {}),
      };
    case "workflow.run.cancelled":
      return {
        ...(typeof event.status === "string" ? { status: event.status } : {}),
        ...(typeof event.reason === "string" ? { reason: event.reason } : {}),
        ...(typeof event.cancelled_by === "string"
          ? { cancelled_by: event.cancelled_by }
          : {}),
      };
    case "workflow.run.completed":
      return typeof event.status === "string" ? { status: event.status } : null;
    case "workflow.run.expired":
      return {
        ...(typeof event.status === "string" ? { status: event.status } : {}),
        ...(typeof event.reason === "string" ? { reason: event.reason } : {}),
        ...(typeof event.error === "string" ? { error: event.error } : {}),
      };
    case "workflow.run.failed":
      return {
        ...(typeof event.status === "string" ? { status: event.status } : {}),
        ...(typeof event.reason === "string" ? { reason: event.reason } : {}),
        ...(typeof event.error === "string" ? { error: event.error } : {}),
      };
    case "workflow.run.resumed":
      return {
        ...(typeof event.actor === "string" ? { actor: event.actor } : {}),
        ...(typeof event.event_name === "string" ? { event_name: event.event_name } : {}),
        ...(event.event_payload !== undefined ? { event_payload_supplied: true } : {}),
      };
    case "workflow.run.queued":
      return {
        ...(typeof event.workflow_id === "string" ? { workflow_id: event.workflow_id } : {}),
        ...(typeof event.workflow_version_id === "string"
          ? { workflow_version_id: event.workflow_version_id }
          : {}),
        ...(typeof event.alias === "string" ? { alias: event.alias } : {}),
      };
    default: {
      const payloadEntries = Object.entries(event).filter(
        ([key]) => !STREAM_RUN_EVENT_META_KEYS.has(key),
      );
      return payloadEntries.length > 0
        ? Object.fromEntries(payloadEntries)
        : null;
    }
  }
}

function workflowRunEventCursor(event: CaliberEvent): string | null {
  const eventType = typeof event.type === "string" ? event.type : "";
  const workflowId = typeof event.workflow_id === "string" ? event.workflow_id : "";
  const runId = typeof event.workflow_run_id === "string" ? event.workflow_run_id : "";
  const identity =
    typeof event.event_id === "number"
      ? `event:${event.event_id}`
      : typeof event.sequence === "number"
        ? `sequence:${event.sequence}`
        : typeof event.created_at === "string" && event.created_at
          ? `created:${event.created_at}`
          : `payload:${stringifyRunEventPayload(event)}`;
  if (!eventType || !workflowId || !identity) {
    return null;
  }
  return [workflowId, runId || "workflow", eventType, identity].join("|");
}

function runSummarySteps(run: WorkflowRun | null): WorkflowRunStep[] {
  const maybeSteps = run?.summary?.steps;
  if (!Array.isArray(maybeSteps)) return [];
  return maybeSteps
    .map((step) => normalizeRunStep(step))
    .filter((step): step is WorkflowRunStep => Boolean(step));
}

function defaultFocusedRunNode(run: WorkflowRun | null): string | null {
  if (!run) return null;
  if (typeof run.current_node_id === "string" && run.current_node_id.trim()) {
    return run.current_node_id;
  }
  const path = Array.isArray(run.summary?.node_path)
    ? run.summary.node_path.filter((nodeId): nodeId is string => typeof nodeId === "string")
    : [];
  if (path.length > 0) return path[path.length - 1] ?? null;
  const steps = runSummarySteps(run);
  return steps.length > 0 ? steps[steps.length - 1]?.node_id ?? null : null;
}

function layoutStorageKey(versionId: string): string {
  return `caliber.workflow.layout.${versionId}`;
}

function readLayout(versionId: string): NodeLayoutMap {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(layoutStorageKey(versionId));
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const next: NodeLayoutMap = {};
    for (const [nodeId, value] of Object.entries(parsed)) {
      if (!value || typeof value !== "object") continue;
      const x = Number((value as { x?: unknown }).x);
      const y = Number((value as { y?: unknown }).y);
      if (Number.isFinite(x) && Number.isFinite(y)) next[nodeId] = { x, y };
    }
    return next;
  } catch {
    return {};
  }
}

function persistLayout(versionId: string, layout: NodeLayoutMap): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(layoutStorageKey(versionId), JSON.stringify(layout));
  } catch {
    // Ignore storage failures: layout still works for this session.
  }
}

function clearLayout(versionId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(layoutStorageKey(versionId));
  } catch {
    // Ignore storage failures.
  }
}

function runSortKey(run: WorkflowRun): number {
  const candidate =
    run.started_at ??
    run.queued_at ??
    run.completed_at ??
    "";
  const parsed = Date.parse(candidate);
  return Number.isFinite(parsed) ? parsed : 0;
}

function runTimestamp(run: WorkflowRun): string {
  const raw = run.started_at ?? run.queued_at ?? run.completed_at;
  if (!raw) return "—";
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.getTime())) return "—";
  return parsed.toLocaleString();
}

function QuickAddMenu({
  state,
  items,
  onSelect,
  onClose,
}: {
  state: QuickAddState;
  items: NodePaletteItem[];
  onSelect: (sourceId: string, type: string) => void;
  onClose: () => void;
}): JSX.Element {
  const ref = useRef<HTMLDivElement>(null);

  // Close on outside click
  useEffect(() => {
    function handle(e: MouseEvent): void {
      if (ref.current && !ref.current.contains(e.target as HTMLElement)) onClose();
    }
    document.addEventListener("mousedown", handle);
    return () => document.removeEventListener("mousedown", handle);
  }, [onClose]);

  const quickAddItems = items.filter((item) => item.type !== "start");
  const width = 208;
  const viewWidth = typeof window !== "undefined" ? window.innerWidth : width + 24;
  const viewHeight = typeof window !== "undefined" ? window.innerHeight : 768;
  const left = Math.max(12, Math.min(state.screenX, viewWidth - width - 12));
  const top = Math.max(12, Math.min(state.screenY, viewHeight - 280));

  return (
    <div
      ref={ref}
      className="fixed z-50 w-52 rounded-xl border border-zinc-200 bg-white py-1.5 shadow-lg"
      style={{ left, top }}
    >
      <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
        Add & connect
      </div>
      {quickAddItems.map((item) => (
        <button
          key={item.type}
          type="button"
          className="flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-xs transition-colors hover:bg-zinc-50 active:scale-[0.97]"
          onClick={() => onSelect(state.sourceId, item.type)}
        >
          <span
            className="flex h-6 w-6 items-center justify-center rounded-md"
            style={{ backgroundColor: `${nodeColor(item.type)}12`, color: nodeColor(item.type) }}
          >
            <NodeIcon type={item.type} size={14} />
          </span>
          <span className="font-medium text-zinc-700">{item.label}</span>
        </button>
      ))}
    </div>
  );
}

export function newNode(
  type: string,
  id: string,
  componentSpec?: WorkflowComponent | null,
): ManifestNode {
  if (!isWorkflowNodeType(type)) {
    throw new Error(
      `Unsupported workflow node type "${type}". Refresh the page or update the workflow component catalog before adding it.`,
    );
  }
  const starter = starterNodeFromComponent(type, id, componentSpec);
  if (starter) return starter;
  switch (type) {
    case "agent":
      return {
        id,
        type: "agent",
        name: id,
        model: "inherit",
        instructions: { type: "inline", text: "You are a helpful assistant." },
        tools: [],
        inputs: { input: { type: "string" }, history: { type: "structured" } },
        outputs: { final_output: { type: "string" }, history: { type: "structured" } },
      };
    case "guardrail":
      return {
        id,
        type: "guardrail",
        mode: "post_agent",
        inputs: { response: { type: "string" } },
        outputs: { passthrough: { type: "string" } },
        on_failure: "block",
        max_retries: 0,
        checks: [{ non_empty_output: {} }],
      };
    case "file_input":
      return {
        id,
        type: "file_input",
        path: "",
        encoding: "utf-8",
        max_bytes: 200000,
        inputs: { path: { type: "string" } },
        outputs: {
          text: { type: "string" },
          path: { type: "string" },
          file_ref: { type: "structured" },
          metadata: { type: "structured" },
        },
      };
    case "folder_input":
      return {
        id,
        type: "folder_input",
        path: "",
        pattern: "**/*",
        recursive: true,
        max_files: 50,
        max_bytes_per_file: 100000,
        encoding: "utf-8",
        inputs: { path: { type: "string" } },
        outputs: {
          text: { type: "string" },
          files: { type: "structured" },
          metadata: { type: "structured" },
        },
      };
    case "input_bucket":
      return {
        id,
        type: "input_bucket",
        bucket: "",
        prefix: "",
        recursive: true,
        max_files: 50,
        max_bytes_per_file: 100000,
        encoding: "utf-8",
        inputs: { prefix: { type: "string" } },
        outputs: {
          text: { type: "string" },
          files: { type: "structured" },
          metadata: { type: "structured" },
        },
      };
    case "output_bucket":
      return {
        id,
        type: "output_bucket",
        bucket: "",
        prefix: "",
        overwrite: true,
        inputs: { input: { type: "string" } },
        outputs: {
          keys: { type: "structured" },
          metadata: { type: "structured" },
        },
      };
    case "output_folder":
      return {
        id,
        type: "output_folder",
        path: "",
        overwrite: true,
        inputs: { input: { type: "string" } },
        outputs: {
          files: { type: "structured" },
          metadata: { type: "structured" },
        },
      };
    case "wait_until":
      return {
        id,
        type: "wait_until",
        wait_until: new Date(Date.now() + 60_000).toISOString(),
        timezone: "UTC",
        inputs: { input: { type: "string" } },
        outputs: { output: { type: "string" } },
      };
    case "wait_for_event":
      return {
        id,
        type: "wait_for_event",
        event_name: "resume_event",
        correlation_key: "",
        timeout_seconds: null,
        inputs: { input: { type: "string" } },
        outputs: waitForEventOutputPorts(),
      };
    case "parallel":
      return {
        id,
        type: "parallel",
        inputs: { input: { type: "string" } },
        outputs: { output: { type: "string" } },
      };
    case "join":
      return {
        id,
        type: "join",
        mode: "all",
        inputs: {},
        outputs: {
          output: { type: "string" },
          merged: { type: "structured" },
        },
      };
    case "for_each":
      return {
        id,
        type: "for_each",
        target_node_id: null,
        item_input_port: "items",
        max_items: 100,
        inputs: { items: { type: "structured" } },
        outputs: {
          results: { type: "structured" },
          text: { type: "string" },
          metadata: { type: "structured" },
        },
      };
    case "loop":
      return {
        id,
        type: "loop",
        target_node_id: null,
        max_iterations: 10,
        stop_condition: "",
        inputs: {
          input: { type: "string" },
          state: { type: "structured" },
        },
        outputs: {
          output: { type: "string" },
          result: { type: "structured" },
          iterations: { type: "structured" },
          metadata: { type: "structured" },
        },
      };
    case "error_boundary":
      return {
        id,
        type: "error_boundary",
        target_node_id: null,
        fallback_text: "",
        compensate_with: null,
        inputs: { input: { type: "string" } },
        outputs: {
          output: { type: "string" },
          error: { type: "structured" },
        },
      };
    case "subworkflow":
      return {
        id,
        type: "subworkflow",
        workflow_id: "",
        alias: "prod",
        timeout_seconds: 120,
        inputs: { input: { type: "string" } },
        outputs: {
          output: { type: "string" },
          result: { type: "structured" },
        },
      };
    case "tool":
      return {
        id,
        type: "tool",
        tool_name: "",
        inputs: {
          input: { type: "string" },
          arguments: { type: "structured" },
        },
        outputs: {
          text: { type: "string" },
          result: { type: "structured" },
          metadata: { type: "structured" },
        },
      };
    case "mcp_resource":
      return {
        id,
        type: "mcp_resource",
        server_id: "",
        tool_name: "",
        timeout_seconds: 45,
        inputs: { input: { type: "string" } },
        outputs: {
          text: { type: "string" },
          result: { type: "structured" },
          metadata: { type: "structured" },
        },
      };
    case "webhook":
      return {
        id,
        type: "webhook",
        url: "",
        method: "POST",
        headers: {},
        timeout_seconds: 30,
        inputs: {
          payload: { type: "structured" },
          input: { type: "string" },
        },
        outputs: {
          text: { type: "string" },
          response: { type: "structured" },
          metadata: { type: "structured" },
        },
      };
    case "api_request":
      return {
        id,
        type: "api_request",
        mode: "url",
        url: "",
        method: "GET",
        curl: "",
        headers: {},
        body: "",
        timeout_seconds: 30,
        inputs: {
          payload: { type: "structured" },
          input: { type: "string" },
        },
        outputs: {
          text: { type: "string" },
          response: { type: "structured" },
          metadata: { type: "structured" },
        },
      };
    case "knowledge_query":
      return {
        id,
        type: "knowledge_query",
        knowledge_base_id: "",
        version_ids: [],
        retrieval_modes: [],
        top_k: 6,
        chat_model: null,
        graph_overrides: null,
        inputs: {
          question: { type: "string" },
          history: { type: "structured" },
          retrieval_modes: { type: "structured" },
          version_ids: { type: "structured" },
          graph_overrides: { type: "structured" },
        },
        outputs: {
          text: { type: "string" },
          answer: { type: "string" },
          result: { type: "structured" },
          citations: { type: "structured" },
          chunks: { type: "structured" },
          graph_context: { type: "structured" },
        },
      };
    case "knowledge_build":
      return {
        id,
        type: "knowledge_build",
        knowledge_base_id: "",
        chunking_strategy: "",
        embedding_model: "",
        chunking_config: {},
        graph_config: null,
        activate_when_complete: false,
        wait_for_completion: false,
        wait_timeout_seconds: 300,
        inputs: {
          input: { type: "string" },
          sources: { type: "structured" },
          chunking_strategy: { type: "string" },
          embedding_model: { type: "string" },
          chunking_config: { type: "structured" },
          graph_config: { type: "structured" },
        },
        outputs: {
          text: { type: "string" },
          result: { type: "structured" },
          knowledge_base: { type: "structured" },
          version: { type: "structured" },
          run: { type: "structured" },
          status: { type: "string" },
          version_id: { type: "string" },
          run_id: { type: "string" },
        },
      };
    case "template":
      return {
        id,
        type: "template",
        template: "{{input}}",
        output_format: "text",
        missing_variable_mode: "preserve",
        inputs: {
          input: { type: "string" },
          variables: { type: "structured" },
        },
        outputs: {
          text: { type: "string" },
          result: { type: "structured" },
          metadata: { type: "structured" },
        },
      };
    case "external_app":
      return {
        id,
        type: "external_app",
        entrypoint: "",
        inputs: {
          input: { type: "string" },
          context: { type: "structured" },
        },
        outputs: {
          text: { type: "string" },
          result: { type: "structured" },
          metadata: { type: "structured" },
        },
      };
    case "python_code":
      return {
        id,
        type: "python_code",
        code: 'return {"text": input or run_input, "result": {"ok": True}}',
        timeout_seconds: 5,
        inputs: {
          input: { type: "string" },
          context: { type: "structured" },
        },
        outputs: {
          text: { type: "string" },
          result: { type: "structured" },
          metadata: { type: "structured" },
        },
      };
    case "router":
      return { id, type: "router", inputs: { decision: { type: "string" } }, outputs: {}, branches: [] };
    case "human_approval":
      return {
        id,
        type: "human_approval",
        required_role: "caliber.approver",
        approval_count: 1,
        timeout_behavior: "block",
        inputs: { request: { type: "string" } },
        outputs: { request: { type: "string" } },
      };
    case "output":
      return { id, type: "output", inputs: { response: { type: "string" } } };
    case "start":
      return { id, type: "start", outputs: { user_message: { type: "string" } } };
    case "note":
      return { id, type: "note", text: "" };
  }
}

/** Cap on the in-editor undo/redo history depth (bounds memory for big graphs). */
const MAX_HISTORY = 50;

/** Debounce before a dirty draft is autosaved (ms of edit inactivity). */
const AUTOSAVE_DELAY_MS = 1500;

export function WorkflowEditor(): JSX.Element {
  const { workflowId, versionId } = useParams<{ workflowId: string; versionId: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const invalidate = useInvalidate();
  const queryClient = useQueryClient();
  const workflowRunEvent = useEventStream(WORKFLOW_RUN_EVENTS);
  const activeRunIdRef = useRef<string | null>(null);
  const processedWorkflowRunEventRef = useRef<string | null>(null);
  const [manifest, setManifest] = useState<WorkflowManifest | null>(null);
  const [manifestVersionId, setManifestVersionId] = useState<string | null>(null);
  const [hash, setHash] = useState<string>("");
  const [selected, setSelected] = useState<string | null>(null);
  // Node whose `<>` code editor (manifest JSON) is open, or null.
  const [codeNodeId, setCodeNodeId] = useState<string | null>(null);
  // The full multi-selection set (marquee / shift-click / select-all). The
  // single-node `selected` above stays the inspector's primary; `selectedIds`
  // drives bulk delete / duplicate / copy and is kept = [selected] for a single.
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  // In-memory clipboard for copy/paste (nodes + the edges internal to the copied
  // set + their relative layout). Survives re-renders; cleared on version load.
  const clipboardRef = useRef<{
    nodes: ManifestNode[];
    edges: ManifestEdge[];
    layout: Record<string, FlowNodePosition>;
  } | null>(null);
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [previewInput, setPreviewInput] = useState(
    searchParams.get("sampleInput") || "What is your refund policy?",
  );
  const [sessionIdInput, setSessionIdInput] = useState(searchParams.get("sessionId") || "");
  const [showPreview, setShowPreview] = useState(Boolean(searchParams.get("sampleInput")));
  const [dirty, setDirty] = useState(false);
  // Manifest-edit history for undo/redo. Each entry is a full manifest snapshot
  // (node positions live in localStorage and are deliberately not part of this).
  const [undoStack, setUndoStack] = useState<WorkflowManifest[]>([]);
  const [redoStack, setRedoStack] = useState<WorkflowManifest[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [showPublish, setShowPublish] = useState(false);
  const [viewMode, setViewMode] = useState<"visual" | "code" | "plan">("visual");
  const [copilotOpen, setCopilotOpen] = useState(false);
  // The edge a drag-to-connect just created, awaiting map confirmation.
  const [pendingEdgeId, setPendingEdgeId] = useState<string | null>(null);
  const [pendingEdgeAnchor, setPendingEdgeAnchor] = useState<ScreenPoint | null>(null);
  // Quick-add popup state (shows node type picker connected to a source node).
  const [quickAdd, setQuickAdd] = useState<QuickAddState | null>(null);
  const [outlineQuery, setOutlineQuery] = useState("");
  const [nodeLayout, setNodeLayout] = useState<NodeLayoutMap>({});
  const [showRunMonitor, setShowRunMonitor] = useState(false);
  const [runInput, setRunInput] = useState(
    searchParams.get("sampleInput") || "What is your refund policy?",
  );
  const [runMessage, setRunMessage] = useState<string | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [activeRun, setActiveRun] = useState<WorkflowRun | null>(null);
  const [runFocusedNodeId, setRunFocusedNodeId] = useState<string | null>(null);
  const [runEvents, setRunEvents] = useState<WorkflowRunEvent[]>([]);
  const [runEventsError, setRunEventsError] = useState<string | null>(null);
  const [runCheckpoints, setRunCheckpoints] = useState<WorkflowRunCheckpoint[]>([]);
  const [runCheckpointsError, setRunCheckpointsError] = useState<string | null>(null);
  const [runApprovals, setRunApprovals] = useState<WorkflowRuntimeApproval[]>([]);
  const [runApprovalsReady, setRunApprovalsReady] = useState(false);
  const [runApprovalsError, setRunApprovalsError] = useState<string | null>(null);
  const [runMonitorHydrated, setRunMonitorHydrated] = useState(false);
  const [resumeEventName, setResumeEventName] = useState("");
  const [resumeEventPayload, setResumeEventPayload] = useState("");
  const [workflowStatusOverride, setWorkflowStatusOverride] = useState<Workflow["status"] | null>(
    null,
  );
  const [inspectorExpandSignal, setInspectorExpandSignal] = useState(0);
  const [inspectorFieldTarget, setInspectorFieldTarget] = useState<string | null>(null);
  const [inspectorFieldFocusSignal, setInspectorFieldFocusSignal] = useState(0);

  const capabilitiesQuery = useApiQuery<PlatformCapabilities>(
    ["capabilities"],
    (signal) => caliberApi.getCapabilities(signal),
  );
  const versionQuery = useApiQuery(
    ["workflow-version", versionId],
    (signal) => caliberApi.getWorkflowVersion(versionId!, signal),
    { enabled: Boolean(versionId) },
  );
  const workflowQuery = useApiQuery<Workflow>(
    ["workflow", workflowId],
    (signal) => caliberApi.getWorkflow(workflowId!, signal),
    { enabled: Boolean(workflowId) },
  );
  const editorBootstrapReady = Boolean(versionQuery.data);
  const monitoredRunManifestQuery = useApiQuery<WorkflowRunManifest>(
    ["workflow-run", activeRun?.workflow_run_id ?? null, "manifest"],
    (signal) => caliberApi.getWorkflowRunManifest(activeRun!.workflow_run_id, signal),
    {
      enabled: Boolean(showRunMonitor && activeRun?.workflow_run_id),
    },
  );
  const activeRunManifestMode =
    monitoredRunManifestQuery.data?.manifest_mode
    ?? activeRun?.summary?.manifest_mode
    ?? null;
  const monitoredRunCanUseSavedVersionFallback = Boolean(
    activeRun
    && activeRunManifestMode !== "snapshot"
    && (activeRunManifestMode === "saved_version" || monitoredRunManifestQuery.error),
  );
  const monitoredHistoricalVersionQuery = useApiQuery(
    ["workflow-version", activeRun?.workflow_version_id ?? null],
    (signal) => caliberApi.getWorkflowVersion(activeRun!.workflow_version_id!, signal),
    {
      enabled: Boolean(
        showRunMonitor
        && activeRun?.workflow_version_id
        && activeRun.workflow_version_id !== versionId
        && monitoredRunCanUseSavedVersionFallback,
      ),
    },
  );
  const runHistoryQuery = useInfiniteQuery({
    queryKey: ["workflow-runs", workflowId, "editor-history"],
    queryFn: ({ pageParam, signal }) =>
      caliberApi.listWorkflowRunsPage(
        workflowId!,
        {
          limit: EDITOR_RUN_HISTORY_PAGE_SIZE,
          cursor: typeof pageParam === "string" ? pageParam : null,
        },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: Boolean(workflowId) && showRunMonitor,
  });
  const runHistoryFallbackQuery = useApiQuery(
    ["workflow-runs", workflowId, "editor-history-fallback"],
    (signal) => caliberApi.listWorkflowRuns(workflowId!, signal),
    {
      enabled: Boolean(workflowId) && showRunMonitor && runHistoryQuery.isError,
    },
  );
  const refreshRunHistoryQueries = useCallback((): void => {
    for (const queryKey of [
      ["workflow-runs", workflowId, "editor-history"],
      ["workflow-runs", workflowId, "editor-history-fallback"],
    ] as const) {
      void invalidate(queryKey);
      void queryClient.refetchQueries({ queryKey, type: "active" });
    }
  }, [invalidate, queryClient, workflowId]);
  const toolsQuery = useApiQuery(
    ["tools", "active"],
    (signal) => caliberApi.listTools("active", signal),
    { enabled: editorBootstrapReady },
  );
  const promptsQuery = useApiQuery(
    ["prompts"],
    (signal) => caliberApi.listPrompts(signal),
    { enabled: editorBootstrapReady },
  );
  const skillsQuery = useApiQuery(
    ["skills"],
    (signal) => caliberApi.listSkills(undefined, signal),
    { enabled: editorBootstrapReady },
  );
  const evalDatasetsQuery = useApiQuery(
    ["eval-datasets", "active"],
    (signal) => caliberApi.listEvalDatasets({ status: "active" }, signal),
    { enabled: editorBootstrapReady },
  );
  const mcpServersQuery = useApiQuery(
    ["mcp-servers"],
    (signal) => caliberApi.listMcpServers(undefined, signal),
    { enabled: editorBootstrapReady },
  );
  const workflowComponentsQuery = useApiQuery(
    ["workflow-components"],
    (signal) => caliberApi.listWorkflowComponents(signal),
    { enabled: editorBootstrapReady },
  );
  const activeRunSessionMemoryQuery = useApiQuery<WorkflowSessionMemoryEntry[]>(
    ["workflow-session-memory", workflowId, activeRun?.session_id ?? null],
    (signal) => caliberApi.listWorkflowSessionMemory(workflowId!, activeRun!.session_id!, {}, signal),
    {
      enabled: Boolean(workflowId && activeRun?.session_id && showRunMonitor),
      refetchInterval:
        showRunMonitor &&
        activeRun?.session_id &&
        !RUN_TERMINAL_STATUSES.has(activeRun.status)
          ? RUN_MONITOR_REFRESH_INTERVAL_MS
          : false,
    },
  );
  const activeRunLineageQuery = useApiQuery<WorkflowRunLineage>(
    ["workflow-run-lineage", activeRun?.workflow_run_id ?? null],
    (signal) => caliberApi.getWorkflowRunLineage(activeRun!.workflow_run_id, signal),
    {
      enabled: Boolean(showRunMonitor && activeRun?.workflow_run_id),
      refetchInterval:
        showRunMonitor &&
        activeRun?.workflow_run_id &&
        !RUN_TERMINAL_STATUSES.has(activeRun.status)
          ? RUN_MONITOR_REFRESH_INTERVAL_MS
          : false,
    },
  );
  const activeRunResumeCheckpointId = workflowRunResumeCheckpointId(activeRun);
  const activeRunResumeSourceRunId = workflowRunResumeCheckpointRunId(activeRun);
  const activeRunHasInheritedResumeSource = workflowRunHasInheritedResumeCheckpoint(activeRun);
  const activeRunResumeSourceCheckpointsQuery = useApiQuery<WorkflowRunCheckpoint[]>(
    ["workflow-run-source-checkpoints", activeRun?.workflow_run_id ?? null, activeRunResumeSourceRunId],
    (signal) =>
      caliberApi.listWorkflowRunCheckpoints(
        activeRunResumeSourceRunId!,
        { limit: 1000 },
        signal,
      ),
    {
      enabled: Boolean(
        showRunMonitor && activeRunHasInheritedResumeSource && activeRunResumeSourceRunId,
      ),
    },
  );
  const activeRunResumeSourceCheckpoint = useMemo(() => {
    if (!activeRunResumeCheckpointId) return null;
    return (
      activeRunResumeSourceCheckpointsQuery.data?.find(
        (checkpoint) => checkpoint.checkpoint_id === activeRunResumeCheckpointId,
      ) ?? null
    );
  }, [activeRunResumeCheckpointId, activeRunResumeSourceCheckpointsQuery.data]);
  const activeRunEffectiveCheckpoints = useMemo(
    () =>
      mergeWorkflowRunCheckpoints(
        runCheckpoints,
        activeRunResumeSourceCheckpoint,
      ),
    [activeRunResumeSourceCheckpoint, runCheckpoints],
  );

  const fetchRunMonitorSnapshot = useCallback(
    async (runId: string): Promise<RunMonitorSnapshot> => {
      let approvalsReady = true;
      let approvalsError: string | null = null;
      let eventsError: string | null = null;
      let checkpointsError: string | null = null;
      const [run, events, checkpoints, approvals] = await Promise.all([
        caliberApi.getWorkflowRun(runId),
        caliberApi.listWorkflowRunEvents(runId, { limit: 200 }).catch((error) => {
          eventsError = error instanceof Error ? error.message : String(error);
          return [] as WorkflowRunEvent[];
        }),
        caliberApi.listWorkflowRunCheckpoints(runId, { limit: 100 }).catch((error) => {
          checkpointsError = error instanceof Error ? error.message : String(error);
          return [] as WorkflowRunCheckpoint[];
        }),
        caliberApi
          .listWorkflowRunApprovals(runId)
          .catch((error) => {
            approvalsReady = false;
            approvalsError = error instanceof Error ? error.message : String(error);
            return [] as WorkflowRuntimeApproval[];
          }),
      ]);
      return {
        run,
        events,
        eventsError,
        checkpoints,
        checkpointsError,
        approvals,
        approvalsReady,
        approvalsError,
      };
    },
    [],
  );

  const applyRunMonitorSnapshot = useCallback((snapshot: RunMonitorSnapshot): void => {
    setActiveRun(reconcileRunSnapshot(snapshot.run, snapshot.events));
    setRunEvents((current) => mergeRunEvents(current, snapshot.events));
    setRunEventsError(snapshot.eventsError);
    setRunCheckpoints(snapshot.checkpoints);
    setRunCheckpointsError(snapshot.checkpointsError);
    setRunApprovals(snapshot.approvals);
    setRunApprovalsReady(snapshot.approvalsReady);
    setRunApprovalsError(snapshot.approvalsError);
    setRunMonitorHydrated(true);
  }, []);

  const refreshRunMonitorNow = useCallback(
    async (runId: string): Promise<void> => {
      try {
        const snapshot = await fetchRunMonitorSnapshot(runId);
        if (activeRunIdRef.current !== runId) return;
        applyRunMonitorSnapshot(snapshot);
      } catch (err) {
        if (activeRunIdRef.current !== runId) return;
        const detail = err instanceof Error ? err.message : String(err);
        setRunMessage(`Run monitor refresh failed: ${detail}`);
      }
    },
    [applyRunMonitorSnapshot, fetchRunMonitorSnapshot],
  );

  useEffect(() => {
    activeRunIdRef.current = activeRunId;
  }, [activeRunId]);
  const copyWorkflowRunLink = useCallback((runId: string): void => {
    if (!navigator.clipboard?.writeText) {
      showToast.error("Clipboard access is unavailable in this browser.");
      return;
    }
    void navigator.clipboard.writeText(workflowRunUrl(runId)).then(
      () => showToast.success(`Copied run link for ${runId}.`),
      () => showToast.error(`Failed to copy run link for ${runId}.`),
    );
  }, []);

  useEffect(() => {
    if (versionQuery.data) {
      setManifest(normalizeWorkflowManifest(versionQuery.data.manifest));
      setManifestVersionId(versionId ?? null);
      setHash(versionQuery.data.manifest_hash);
      // A freshly loaded version starts a clean history (no cross-version undo)
      // and a clean selection/clipboard.
      setUndoStack([]);
      setRedoStack([]);
      setSelectedIds([]);
      clipboardRef.current = null;
    }
  }, [versionId, versionQuery.data]);

  const persistedVersionManifest = useMemo(
    () => (versionQuery.data ? normalizeWorkflowManifest(versionQuery.data.manifest) : null),
    [versionQuery.data],
  );
  const workingManifest =
    manifestVersionId === versionId && manifest
      ? manifest
      : persistedVersionManifest;
  const workingHash =
    manifestVersionId === versionId
      ? hash
      : (versionQuery.data?.manifest_hash ?? hash);

  useEffect(() => {
    if (!versionId) return;
    setNodeLayout(readLayout(versionId));
  }, [versionId]);

  useEffect(() => {
    if (!versionId || !workingManifest) return;
    setNodeLayout((prev) => {
      const next: NodeLayoutMap = {};
      for (const nodeId of Object.keys(workingManifest.nodes)) {
        const pos = prev[nodeId];
        if (pos) next[nodeId] = pos;
      }
      const prevKeys = Object.keys(prev).sort().join("|");
      const nextKeys = Object.keys(next).sort().join("|");
      const changed = prevKeys !== nextKeys;
      if (changed) persistLayout(versionId, next);
      return changed ? next : prev;
    });
  }, [versionId, workingManifest]);

  const tools: ToolDefinition[] = toolsQuery.data ?? [];
  const published = versionQuery.data?.status !== "draft";

  const saveMut = useApiMutation(
    () => caliberApi.updateWorkflowVersion(versionId!, workingManifest!, workingHash),
    {
      onSuccess: (v) => {
        setHash(v.manifest_hash);
        setDirty(false);
        setMessage("Saved draft.");
      },
      onError: (err) => setMessage(`Save failed: ${err.message}`),
    },
  );
  const validateMut = useApiMutation(() => caliberApi.validateWorkflowVersion(versionId!), {
    onSuccess: (r) => {
      setReport(r);
      setMessage(r.valid ? "Valid." : `${r.errors.length} error(s).`);
    },
  });
  const previewMut = useApiMutation(
    () =>
      caliberApi.previewWorkflowVersion(
        versionId!,
        previewInput,
        sessionIdInput.trim() || undefined,
        dirty && manifest ? manifest : undefined,
      ),
    {
      onSuccess: (r) => setPreview(r),
    },
  );
  const publishMut = useApiMutation(() => caliberApi.publishWorkflowVersion(versionId!), {
    onSuccess: () => setMessage("Published."),
    onError: (err) => setMessage(`Publish failed: ${err.message}`),
  });

  const loadMonitoredRun = useCallback((run: WorkflowRun, message?: string): void => {
    setShowRunMonitor(true);
    setActiveRunId(run.workflow_run_id);
    setActiveRun(run);
    setRunFocusedNodeId(defaultFocusedRunNode(run));
    setRunEvents([]);
    setRunEventsError(null);
    setRunCheckpoints([]);
    setRunCheckpointsError(null);
    setRunApprovals([]);
    setRunApprovalsReady(false);
    setRunApprovalsError(null);
    setRunMonitorHydrated(false);
    if (message) setRunMessage(message);
    void refreshRunMonitorNow(run.workflow_run_id);
  }, [refreshRunMonitorNow]);

  const runMut = useApiMutation(
    () =>
      caliberApi.createWorkflowRun({
        workflow_version_id: versionId!,
        workflow_id: workflowId,
        alias: "manual",
        input: runInput,
        session_id: sessionIdInput.trim() || undefined,
        source: "editor",
        manifest: dirty && manifest ? manifest : undefined,
      }),
    {
      onSuccess: (run) => {
        refreshRunHistoryQueries();
        loadMonitoredRun(run, workflowRunStatusMessage(run.workflow_run_id, run.status));
      },
      onError: (err) => setRunMessage(`Run failed: ${err.message}`),
    },
  );
  const cancelRunMut = useApiMutation(
    (runId: string) => caliberApi.cancelWorkflowRun(runId),
    {
      onSuccess: (run) => {
        refreshRunHistoryQueries();
        loadMonitoredRun(
          run,
          `Pause requested for ${run.workflow_run_id}. Current state: ${workflowRunStatusPhrase(run.status)}.`,
        );
      },
      onError: (err) => setRunMessage(`Pause failed: ${err.message}`),
    },
  );
  const retryRunMut = useApiMutation(
    (payload: { runId: string; checkpointId?: string }) =>
      caliberApi.retryWorkflowRun(payload.runId, {
        checkpoint_id: payload.checkpointId,
      }),
    {
      onSuccess: (run, variables) => {
        const scope = variables.checkpointId
          ? ` from checkpoint ${variables.checkpointId}`
          : "";
        refreshRunHistoryQueries();
        loadMonitoredRun(run, `Retry${scope} queued as ${run.workflow_run_id}.`);
      },
      onError: (err) =>
        setRunMessage(
          workflowRunRetryFailureMessage(err) ?? `Retry failed: ${runActionErrorDetail(err)}`,
        ),
    },
  );
  const resumeRunMut = useApiMutation(
    (payload: { runId: string; event_name?: string; event_payload?: unknown }) =>
      caliberApi.resumeWorkflowRun(payload.runId, {
        event_name: payload.event_name,
        event_payload: payload.event_payload,
      }),
    {
      onSuccess: (run) => {
        refreshRunHistoryQueries();
        loadMonitoredRun(
          run,
          `Run ${run.workflow_run_id} resumed and ${workflowRunStatusVerbPhrase(run.status)}.`,
        );
      },
      onError: (err) =>
        setRunMessage(
          workflowRunResumeFailureMessage(err) ?? `Resume failed: ${runActionErrorDetail(err)}`,
        ),
    },
  );
  const resumeRunByEventMut = useApiMutation(
    (payload: { event_name: string; event_payload?: unknown }) =>
      caliberApi.resumeWorkflowRunByEvent({
        workflow_id: workflowId,
        event_name: payload.event_name,
        event_payload: payload.event_payload,
      }),
    {
      onSuccess: (run, variables) => {
        refreshRunHistoryQueries();
        loadMonitoredRun(
          run,
          `Matched event ${variables.event_name} to run ${run.workflow_run_id} and re-queued it.`,
        );
      },
      onError: (err) =>
        setRunMessage(
          workflowRunResumeByEventFailureMessage(err)
          ?? `External event resume failed: ${runActionErrorDetail(err)}`,
        ),
    },
  );
  const approveRunMut = useApiMutation(
    (payload: { runId: string; runtimeApprovalId?: string }) =>
      caliberApi.approveWorkflowRunApproval(payload.runId, {
        runtime_approval_id: payload.runtimeApprovalId,
      }),
    {
      onSuccess: (run) => {
        refreshRunHistoryQueries();
        loadMonitoredRun(run, `Approval recorded for ${run.workflow_run_id}.`);
      },
      onError: (err) =>
        setRunMessage(
          workflowRunApprovalActionFailureMessage("Approve", err)
          ?? `Approve failed: ${runActionErrorDetail(err)}`,
        ),
    },
  );
  const rejectRunMut = useApiMutation(
    (payload: { runId: string; runtimeApprovalId?: string }) =>
      caliberApi.rejectWorkflowRunApproval(payload.runId, {
        runtime_approval_id: payload.runtimeApprovalId,
      }),
    {
      onSuccess: (run) => {
        refreshRunHistoryQueries();
        loadMonitoredRun(run, workflowRunStatusMessage(run.workflow_run_id, run.status));
      },
      onError: (err) =>
        setRunMessage(
          workflowRunApprovalActionFailureMessage("Reject", err)
          ?? `Reject failed: ${runActionErrorDetail(err)}`,
        ),
    },
  );
  const clearSessionMemoryMut = useApiMutation(
    (payload: { sessionId: string; nodeId?: string }) =>
      caliberApi.clearWorkflowSessionMemory(workflowId!, payload.sessionId, {
        node_id: payload.nodeId,
      }),
    {
      onSuccess: async (result) => {
        await invalidate(["workflow-session-memory", workflowId, result.session_id]);
        const scope = result.node_id ? `node ${result.node_id}` : `session ${result.session_id}`;
        setRunMessage(`Cleared ${result.deleted_messages} message(s) from ${scope}.`);
      },
      onError: (err) => setRunMessage(`Session memory clear failed: ${err.message}`),
    },
  );

  useEffect(() => {
    setWorkflowStatusOverride(null);
  }, [workflowId]);

  useEffect(() => {
    if (workflowQuery.data?.status) {
      setWorkflowStatusOverride(workflowQuery.data.status);
    }
  }, [workflowQuery.data?.status]);

  useEffect(() => {
    if (!activeRunId) return undefined;
    let cancelled = false;
    let timer: number | null = null;

    const pollRun = async (): Promise<void> => {
      try {
        const snapshot = await fetchRunMonitorSnapshot(activeRunId);
        if (cancelled) return;
        applyRunMonitorSnapshot(snapshot);
        if (RUN_TERMINAL_STATUSES.has(snapshot.run.status)) {
          return;
        }
      } catch (err) {
        if (cancelled) return;
        const detail = err instanceof Error ? err.message : String(err);
        setRunMessage(`Run monitor refresh failed: ${detail}`);
      }

      if (!cancelled) {
        timer = window.setTimeout(() => {
          void pollRun();
        }, RUN_MONITOR_REFRESH_INTERVAL_MS);
      }
    };

    void pollRun();
    return () => {
      cancelled = true;
      if (timer !== null) {
        window.clearTimeout(timer);
      }
    };
  }, [activeRunId, applyRunMonitorSnapshot, fetchRunMonitorSnapshot]);

  useEffect(() => {
    if (!workflowRunEvent || String(workflowRunEvent.workflow_id ?? "") !== workflowId) return;
    const eventCursor = workflowRunEventCursor(workflowRunEvent);
    if (eventCursor && processedWorkflowRunEventRef.current === eventCursor) {
      return;
    }
    if (eventCursor) {
      processedWorkflowRunEventRef.current = eventCursor;
    }

    if (workflowRunEvent.type === "workflow.deleted") {
      const workflowLabel =
        typeof workflowRunEvent.name === "string" && workflowRunEvent.name.trim()
          ? workflowRunEvent.name
          : workflowQuery.data?.name ?? workflowId ?? "Workflow";
      showToast.info(`Workflow "${workflowLabel}" was deleted.`);
      navigate("/workflows", { replace: true });
      return;
    }

    if (
      workflowRunEvent.type === "workflow.paused" ||
      workflowRunEvent.type === "workflow.resumed" ||
      workflowRunEvent.type === "workflow.updated"
    ) {
      const status =
        workflowRunEvent.type === "workflow.paused"
          ? "paused"
          : workflowRunEvent.type === "workflow.resumed"
            ? "active"
            : typeof workflowRunEvent.status === "string"
              ? workflowRunEvent.status
              : null;
      if (status === "paused" || status === "active" || status === "archived") {
        setWorkflowStatusOverride(status);
      }
      if (status === "paused") {
        setMessage("Workflow paused. New runs are disabled until it is resumed.");
      } else if (status === "active") {
        setMessage("Workflow resumed. New runs can be started again.");
      } else if (status === "archived") {
        setMessage("Workflow archived. New runs are disabled until it is restored.");
      }
      void invalidate(["workflow", workflowId]);
      return;
    }

    const runId = String(workflowRunEvent.workflow_run_id ?? "");
    if (!runId) return;

    if (showRunMonitor) {
      refreshRunHistoryQueries();
    }

    const monitoredRunId = activeRunId ?? activeRun?.workflow_run_id ?? null;
    if (!monitoredRunId || runId !== monitoredRunId) {
      return;
    }

    const eventNodeId = workflowRunEventNodeId(workflowRunEvent);
    const eventPayload = workflowRunEventPayload(workflowRunEvent, eventNodeId);

    setRunEvents((current) => {
      const lastSequence = current.reduce(
        (max, event) => Math.max(max, event.sequence),
        0,
      );
      const lowestSyntheticEventId = current.reduce(
        (min, event) => (event.event_id < min ? event.event_id : min),
        0,
      );
      return mergeRunEvents(current, [
        {
          event_id:
            typeof workflowRunEvent.event_id === "number"
              ? workflowRunEvent.event_id
              : lowestSyntheticEventId - 1,
          workflow_run_id: runId,
          project_id: null,
          sequence:
            typeof workflowRunEvent.sequence === "number"
              ? workflowRunEvent.sequence
              : lastSequence + 1,
          event_type: workflowRunEvent.type,
          node_id: eventNodeId,
          payload: eventPayload,
          created_at:
            typeof workflowRunEvent.created_at === "string"
              ? workflowRunEvent.created_at
              : new Date().toISOString(),
        },
      ]);
    });

    if (workflowRunEvent.type === "workflow.run.queued") {
      setRunMessage(
        workflowRunLifecycleMessage(runId, workflowRunEvent.type, workflowRunEvent),
      );
      setActiveRun((current) =>
        current && current.workflow_run_id === runId
          ? {
              ...current,
              status:
                typeof workflowRunEvent.status === "string"
                  ? workflowRunEvent.status
                  : "queued",
              summary: {
                ...(current.summary ?? {}),
                status:
                  typeof workflowRunEvent.status === "string"
                    ? workflowRunEvent.status
                    : "queued",
              },
            }
          : current,
      );
      return;
    }

    if (workflowRunEvent.type === "workflow.run.started") {
      setRunMessage(
        workflowRunLifecycleMessage(runId, workflowRunEvent.type, workflowRunEvent),
      );
      setActiveRun((current) =>
        current && current.workflow_run_id === runId
          ? {
              ...current,
              status: "running",
              summary: {
                ...(current.summary ?? {}),
                status: "running",
              },
            }
          : current,
      );
      return;
    }

    if (workflowRunEvent.type === "workflow.run.node_started") {
      setActiveRun((current) =>
        current && current.workflow_run_id === runId
          ? {
              ...current,
              status: "running",
              current_node_id: eventNodeId ?? current.current_node_id,
              summary: {
                ...(current.summary ?? {}),
                status: "running",
              },
            }
          : current,
      );
      return;
    }

    if (workflowRunEvent.type === "workflow.run.step") {
      const step = normalizeRunStep(workflowRunEvent.step);
      if (!step) return;
      const nextStatus = workflowRunStatusFromStep(step) ?? "running";
      setActiveRun((current) => {
        if (!current || current.workflow_run_id !== runId) return current;
        const summary = current.summary ?? {};
        const priorSteps = Array.isArray(summary.steps) ? summary.steps : [];
        const nextSteps = [...priorSteps, step];
        const priorPath = Array.isArray(summary.node_path)
          ? summary.node_path.filter((item): item is string => typeof item === "string")
          : [];
        const nextPath =
          priorPath[priorPath.length - 1] === step.node_id
            ? priorPath
            : [...priorPath, step.node_id];
        return {
          ...current,
          status: nextStatus,
          current_node_id: step.node_id,
          summary: {
            ...summary,
            status: nextStatus,
            steps: nextSteps,
            node_path: nextPath,
          },
        };
      });
      if (activeRun?.session_id && step.node_type === "agent") {
        void invalidate([
          "workflow-session-memory",
          workflowId,
          activeRun.session_id,
        ]);
      }
      return;
    }

    if (
      workflowRunEvent.type === "workflow.run.approval.approved" ||
      workflowRunEvent.type === "workflow.run.approval.rejected"
    ) {
      setRunMessage(
        workflowRunLifecycleMessage(runId, workflowRunEvent.type, workflowRunEvent),
      );
      void refreshRunMonitorNow(runId);
      return;
    }

    if (workflowRunEvent.type === "workflow.run.recovered") {
      setRunMessage(
        workflowRunLifecycleMessage(runId, workflowRunEvent.type, workflowRunEvent),
      );
      void refreshRunMonitorNow(runId);
      return;
    }

    if (workflowRunEvent.type === "workflow.run.retried") {
      const retriedRunId =
        typeof workflowRunEvent.retried_run_id === "string"
          ? workflowRunEvent.retried_run_id
          : null;
      const message = workflowRunLifecycleMessage(
        runId,
        workflowRunEvent.type,
        eventPayload,
      );
      if (!retriedRunId) {
        setRunMessage(message);
        return;
      }
      refreshRunHistoryQueries();
      void caliberApi
        .getWorkflowRun(retriedRunId)
        .then((run) => {
          loadMonitoredRun(run, message);
        })
        .catch(() => {
          setShowRunMonitor(true);
          setActiveRunId(retriedRunId);
          setActiveRun(null);
          setRunFocusedNodeId(null);
          setRunEvents([]);
          setRunEventsError(null);
          setRunCheckpoints([]);
          setRunCheckpointsError(null);
          setRunApprovals([]);
          setRunApprovalsReady(false);
          setRunApprovalsError(null);
          setRunMonitorHydrated(false);
          setRunMessage(message);
        });
      return;
    }

    if (
      workflowRunEvent.type === "workflow.run.cancel_requested"
      || workflowRunEvent.type === "workflow.run.waiting_approval"
      || workflowRunEvent.type === "workflow.run.waiting_event"
      || workflowRunEvent.type === "workflow.run.resumed"
      || workflowRunEvent.type === "workflow.run.cancelled"
      || workflowRunEvent.type === "workflow.run.completed"
      || workflowRunEvent.type === "workflow.run.expired"
      || workflowRunEvent.type === "workflow.run.failed"
    ) {
      const eventPayload = workflowRunEventPayload(workflowRunEvent, eventNodeId);
      const nextStatus =
        typeof workflowRunEvent.status === "string"
          ? workflowRunEvent.status
          : workflowRunStatusFromEventType(workflowRunEvent.type)
            ?? activeRun?.status
            ?? "running";
      setActiveRun((current) =>
        current && current.workflow_run_id === runId
          ? {
              ...current,
              status: nextStatus,
              current_node_id: eventNodeId ?? current.current_node_id,
              summary: {
                ...(current.summary ?? {}),
                status: nextStatus,
              },
            }
          : current,
      );
      setRunMessage(
        workflowRunLifecycleMessage(runId, workflowRunEvent.type, eventPayload),
      );
      if (activeRun?.session_id) {
        void invalidate([
          "workflow-session-memory",
          workflowId,
          activeRun.session_id,
        ]);
      }
      void refreshRunMonitorNow(runId);
    }
  }, [
    activeRun?.session_id,
    activeRun?.status,
    activeRun?.workflow_run_id,
    activeRunId,
    invalidate,
    loadMonitoredRun,
    navigate,
    refreshRunMonitorNow,
    refreshRunHistoryQueries,
    showRunMonitor,
    workflowQuery.data?.name,
    workflowId,
    workflowRunEvent,
  ]);

  // Snapshot the current manifest onto the undo stack (and clear redo) before an
  // edit. Computed outside the setState updater so a StrictMode double-invoke
  // can't double-push.
  const pushHistory = useCallback((snapshot: WorkflowManifest | null): void => {
    if (!snapshot) return;
    setUndoStack((prev) => [...prev, structuredClone(snapshot)].slice(-MAX_HISTORY));
    setRedoStack([]);
  }, []);

  const patchManifest = useCallback((updater: (m: WorkflowManifest) => WorkflowManifest): void => {
    if (!workingManifest) return;
    pushHistory(workingManifest);
    setManifestVersionId(versionId ?? null);
    setManifest((prev) => updater(structuredClone(prev ?? workingManifest)));
    setDirty(true);
  }, [versionId, workingManifest, pushHistory]);

  const undo = useCallback((): void => {
    const previous = undoStack[undoStack.length - 1];
    if (!previous || !workingManifest) return;
    setRedoStack((r) => [...r, structuredClone(workingManifest)].slice(-MAX_HISTORY));
    setUndoStack((u) => u.slice(0, -1));
    setManifest(structuredClone(previous));
    setManifestVersionId(versionId ?? null);
    setDirty(true);
    setSelected(null);
  }, [undoStack, workingManifest, versionId]);

  const redo = useCallback((): void => {
    const next = redoStack[redoStack.length - 1];
    if (!next || !workingManifest) return;
    setUndoStack((u) => [...u, structuredClone(workingManifest)].slice(-MAX_HISTORY));
    setRedoStack((r) => r.slice(0, -1));
    setManifest(structuredClone(next));
    setManifestVersionId(versionId ?? null);
    setDirty(true);
    setSelected(null);
  }, [redoStack, workingManifest, versionId]);

  function addNode(type: string, initialPosition?: FlowNodePosition): void {
    if (!workingManifest) return;
    const existing = Object.keys(workingManifest.nodes).filter((id) => id.startsWith(type)).length;
    const id = existing === 0 ? type : `${type}_${existing + 1}`;
    const componentSpec =
      workflowComponentMap.get(type as WorkflowComponent["type"]) ?? null;
    let created: ManifestNode;
    try {
      created = newNode(type, id, componentSpec);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      setMessage(detail);
      showToast.error(detail);
      return;
    }
    patchManifest((m) => {
      m.nodes[id] = created;
      return m;
    });
    if (initialPosition && versionId) {
      const normalized = {
        x: Math.round(initialPosition.x),
        y: Math.round(initialPosition.y),
      };
      setNodeLayout((prev) => {
        const next = { ...prev, [id]: normalized };
        persistLayout(versionId, next);
        return next;
      });
    }
    openInspectorForNode(id);
  }

  function changeNode(nodeId: string, patch: Partial<ManifestNode>): void {
    patchManifest((m) => {
      m.nodes[nodeId] = { ...m.nodes[nodeId], ...patch } as ManifestNode;
      if (
        Array.isArray(patch.tools)
        || (m.nodes[nodeId]?.type === "tool" && typeof patch.tool_name === "string")
      ) {
        return ensureAgentToolBindings(m, tools, mcpServersQuery.data ?? []);
      }
      return m;
    });
  }

  function changeWorkflow(patch: Partial<WorkflowManifest>): void {
    patchManifest((m) => ({ ...m, ...patch }));
  }

  const selectNode = useCallback((nodeId: string | null, fieldKey: string | null = null) => {
    setSelected(nodeId);
    setSelectedIds(nodeId ? [nodeId] : []);
    setInspectorFieldTarget(nodeId ? fieldKey : null);
    if (nodeId && fieldKey) {
      setInspectorFieldFocusSignal((prev) => prev + 1);
    }
  }, []);

  // Canvas selection changes (marquee / shift-click). 0–1 nodes behave like a
  // single select (primary drives the inspector); 2+ enters multi-select where
  // the inspector yields to a bulk-actions panel.
  const handleSelectionChange = useCallback((ids: string[]) => {
    setSelectedIds((prev) => {
      const same = prev.length === ids.length && prev.every((id, i) => id === ids[i]);
      return same ? prev : ids;
    });
    setSelected(ids.length === 1 ? (ids[0] ?? null) : null);
  }, []);

  const selectAll = useCallback(() => {
    if (!workingManifest) return;
    const ids = Object.keys(workingManifest.nodes);
    setSelectedIds(ids);
    setSelected(ids.length === 1 ? (ids[0] ?? null) : null);
  }, [workingManifest]);

  const openInspectorForNode = useCallback((
    nodeId: string | null,
    options?: { fieldKey?: string | null },
  ) => {
    selectNode(nodeId, options?.fieldKey ?? null);
    if (nodeId) {
      setInspectorExpandSignal((prev) => prev + 1);
    }
  }, [selectNode]);

  const focusRunNode = useCallback((nodeId: string | null) => {
    setRunFocusedNodeId(nodeId);
    if (!nodeId) return;
    if (workingManifest?.nodes[nodeId]) {
      openInspectorForNode(nodeId);
      return;
    }
    const versionContext = runManifestContextLabel(
      activeRun,
      activeRunManifestMode === "saved_version" || activeRunManifestMode === "snapshot"
        ? { manifest_mode: activeRunManifestMode }
        : null,
      versionId,
      savedWorkflowVersionLabel(
        activeRun?.workflow_version_id,
        activeRun?.workflow_version_id && activeRun.workflow_version_id !== versionId
          ? readNumber(activeRun.summary?.workflow_version_number)
          : versionQuery.data?.version_number ?? readNumber(activeRun?.summary?.workflow_version_number),
      ),
    );
    setRunMessage(
      `Focused run node ${nodeId}. It is available in ${versionContext}, but not in the draft currently open in the inspector.`,
    );
  }, [activeRun, activeRunManifestMode, openInspectorForNode, versionId, versionQuery.data?.version_number, workingManifest]);

  const deleteNode = useCallback((nodeId: string): void => {
    patchManifest((m) => {
      delete m.nodes[nodeId];
      m.edges = m.edges.filter((e) => e.from !== nodeId && e.to !== nodeId);
      return m;
    });
    selectNode(null);
  }, [patchManifest, selectNode]);

  // Duplicate a node: deep-clone it under a fresh id, offset its position, and
  // select the copy. Edges are intentionally not copied (the clone starts
  // unconnected, like a fresh add). Start/output are unique and not duplicable.
  const duplicateNode = useCallback((nodeId: string): void => {
    if (!workingManifest) return;
    const original = workingManifest.nodes[nodeId];
    if (!original || original.type === "start" || original.type === "output") return;
    const baseType = original.type;
    const existing = Object.keys(workingManifest.nodes).filter((id) =>
      id.startsWith(baseType),
    ).length;
    const newId = `${baseType}_${existing + 1}`;
    const clone = structuredClone(original);
    clone.id = newId;
    patchManifest((m) => {
      m.nodes[newId] = clone;
      return m;
    });
    if (versionId) {
      const pos = nodeLayout[nodeId];
      if (pos) {
        const offset = { x: Math.round(pos.x + 48), y: Math.round(pos.y + 48) };
        setNodeLayout((prev) => {
          const nextLayout = { ...prev, [newId]: offset };
          persistLayout(versionId, nextLayout);
          return nextLayout;
        });
      }
    }
    openInspectorForNode(newId);
  }, [workingManifest, versionId, nodeLayout, patchManifest, openInspectorForNode]);

  // Remove every node in the set (never the unique start) + their incident edges.
  const deleteNodes = useCallback((ids: string[]): void => {
    if (!workingManifest) return;
    const removable = ids.filter((id) => workingManifest.nodes[id]?.type !== "start");
    if (removable.length === 0) return;
    const removeSet = new Set(removable);
    patchManifest((m) => {
      for (const id of removable) delete m.nodes[id];
      m.edges = m.edges.filter((e) => !removeSet.has(e.from) && !removeSet.has(e.to));
      return m;
    });
    selectNode(null);
  }, [workingManifest, patchManifest, selectNode]);

  // Duplicate a set of nodes together: clone each under a fresh id, rewire the
  // edges *internal* to the set onto the clones, offset positions, and select
  // the copies. Start/output are unique and skipped.
  const duplicateNodes = useCallback((ids: string[]): void => {
    if (!workingManifest) return;
    const dupable = ids.filter((id) => {
      const t = workingManifest.nodes[id]?.type;
      return Boolean(t) && t !== "start" && t !== "output";
    });
    if (dupable.length === 0) return;
    const existingIds = new Set(Object.keys(workingManifest.nodes));
    const idMap = new Map<string, string>();
    for (const id of dupable) {
      const baseType = workingManifest.nodes[id]!.type;
      let n =
        Object.keys(workingManifest.nodes).filter((k) => k.startsWith(baseType)).length + 1;
      let candidate = `${baseType}_${n}`;
      while (existingIds.has(candidate)) {
        n += 1;
        candidate = `${baseType}_${n}`;
      }
      existingIds.add(candidate);
      idMap.set(id, candidate);
    }
    const internalEdges = workingManifest.edges.filter(
      (e) => idMap.has(e.from) && idMap.has(e.to),
    );
    patchManifest((m) => {
      for (const [oldId, newId] of idMap) {
        const clone = structuredClone(m.nodes[oldId]!);
        clone.id = newId;
        m.nodes[newId] = clone;
      }
      const edgeIds = new Set(m.edges.map((e) => e.id));
      for (const edge of internalEdges) {
        const from = idMap.get(edge.from)!;
        const to = idMap.get(edge.to)!;
        const eid = makeEdgeId(from, to, edgeIds);
        edgeIds.add(eid);
        m.edges.push({ id: eid, from, to, map: { ...edge.map } });
      }
      return m;
    });
    if (versionId) {
      setNodeLayout((prev) => {
        const next = { ...prev };
        for (const [oldId, newId] of idMap) {
          const pos = prev[oldId];
          if (pos) next[newId] = { x: Math.round(pos.x + 48), y: Math.round(pos.y + 48) };
        }
        persistLayout(versionId, next);
        return next;
      });
    }
    const newIds = [...idMap.values()];
    setSelectedIds(newIds);
    setSelected(newIds.length === 1 ? newIds[0]! : null);
  }, [workingManifest, versionId, patchManifest]);

  // Copy the selected nodes + the edges internal to them + their layout. The
  // unique start/output nodes are skipped (pasting a second one is invalid).
  const copyNodes = useCallback((ids: string[]): void => {
    if (!workingManifest || ids.length === 0) return;
    const copyable = ids.filter((id) => {
      const t = workingManifest.nodes[id]?.type;
      return Boolean(t) && t !== "start" && t !== "output";
    });
    if (copyable.length === 0) {
      clipboardRef.current = null;
      return;
    }
    const idSet = new Set(copyable);
    clipboardRef.current = {
      nodes: copyable
        .map((id) => workingManifest.nodes[id])
        .filter((n): n is ManifestNode => Boolean(n))
        .map((n) => structuredClone(n)),
      edges: workingManifest.edges
        .filter((e) => idSet.has(e.from) && idSet.has(e.to))
        .map((e) => structuredClone(e)),
      layout: Object.fromEntries(
        copyable.flatMap((id) => (nodeLayout[id] ? [[id, nodeLayout[id]] as const] : [])),
      ),
    };
    setMessage(`Copied ${clipboardRef.current.nodes.length} node(s).`);
  }, [workingManifest, nodeLayout]);

  // Paste the clipboard: fresh ids, offset positions, internal edges rewired.
  const pasteNodes = useCallback((): void => {
    const clip = clipboardRef.current;
    if (!clip || !workingManifest || clip.nodes.length === 0) return;
    const existingIds = new Set(Object.keys(workingManifest.nodes));
    const idMap = new Map<string, string>();
    for (const node of clip.nodes) {
      const baseType = node.type;
      let n =
        Object.keys(workingManifest.nodes).filter((k) => k.startsWith(baseType)).length + 1;
      let candidate = `${baseType}_${n}`;
      while (existingIds.has(candidate)) {
        n += 1;
        candidate = `${baseType}_${n}`;
      }
      existingIds.add(candidate);
      idMap.set(node.id, candidate);
    }
    patchManifest((m) => {
      for (const node of clip.nodes) {
        const newId = idMap.get(node.id)!;
        const clone = structuredClone(node);
        clone.id = newId;
        m.nodes[newId] = clone;
      }
      const edgeIds = new Set(m.edges.map((e) => e.id));
      for (const edge of clip.edges) {
        const from = idMap.get(edge.from);
        const to = idMap.get(edge.to);
        if (!from || !to) continue;
        const eid = makeEdgeId(from, to, edgeIds);
        edgeIds.add(eid);
        m.edges.push({ id: eid, from, to, map: { ...edge.map } });
      }
      return m;
    });
    if (versionId) {
      setNodeLayout((prev) => {
        const next = { ...prev };
        for (const [oldId, newId] of idMap) {
          const pos = clip.layout[oldId];
          if (pos) next[newId] = { x: Math.round(pos.x + 48), y: Math.round(pos.y + 48) };
        }
        persistLayout(versionId, next);
        return next;
      });
    }
    const newIds = [...idMap.values()];
    setSelectedIds(newIds);
    setSelected(newIds.length === 1 ? newIds[0]! : null);
  }, [workingManifest, versionId, patchManifest]);

  // Editor keyboard shortcuts (visual mode only; ignored while typing in a field).
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent): void {
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName;
      if (
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        tag === "SELECT" ||
        target?.isContentEditable
      ) {
        return;
      }
      if (viewMode !== "visual") return;
      const mod = e.metaKey || e.ctrlKey;
      if (mod && (e.key === "z" || e.key === "Z")) {
        e.preventDefault();
        if (e.shiftKey) redo();
        else undo();
        return;
      }
      if (mod && (e.key === "y" || e.key === "Y")) {
        e.preventDefault();
        redo();
        return;
      }
      if (mod && (e.key === "a" || e.key === "A")) {
        e.preventDefault();
        selectAll();
        return;
      }
      if (mod && (e.key === "d" || e.key === "D")) {
        e.preventDefault();
        if (selectedIds.length > 0) duplicateNodes(selectedIds);
        return;
      }
      if (mod && (e.key === "c" || e.key === "C")) {
        if (selectedIds.length > 0) {
          e.preventDefault();
          copyNodes(selectedIds);
        }
        return;
      }
      if (mod && (e.key === "v" || e.key === "V")) {
        if (clipboardRef.current) {
          e.preventDefault();
          pasteNodes();
        }
        return;
      }
      if (mod && (e.key === "s" || e.key === "S")) {
        e.preventDefault();
        if (!published && !saveMut.isPending) saveMut.mutate(undefined);
        return;
      }
      if ((e.key === "Delete" || e.key === "Backspace") && selectedIds.length > 0) {
        // deleteNodes skips the unique start node internally.
        e.preventDefault();
        deleteNodes(selectedIds);
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [
    viewMode,
    selectedIds,
    published,
    saveMut,
    undo,
    redo,
    selectAll,
    duplicateNodes,
    copyNodes,
    pasteNodes,
    deleteNodes,
  ]);

  // Hard-navigation backstop (refresh/close/URL-change). With autosave below,
  // the unsaved window is sub-second, but this still guards a mid-debounce reload.
  useEffect(() => {
    if (!dirty) return undefined;
    function onBeforeUnload(e: BeforeUnloadEvent): void {
      e.preventDefault();
      e.returnValue = "";
    }
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [dirty]);

  // --- Autosave: debounced persistence of dirty drafts ----------------------
  // A single save is ever in flight (savingRef gates re-entry). The snapshot ref
  // holds the latest values so the flush-on-unmount can read them after teardown.
  const savingRef = useRef(false);
  savingRef.current = saveMut.isPending;
  const autosaveSnapshotRef = useRef<{
    dirty: boolean;
    published: boolean;
    versionId: string | undefined;
    manifest: WorkflowManifest | null;
    hash: string;
  }>({ dirty, published, versionId, manifest: workingManifest, hash: workingHash });
  autosaveSnapshotRef.current = {
    dirty,
    published,
    versionId,
    manifest: workingManifest,
    hash: workingHash,
  };
  const runAutosaveRef = useRef<() => void>(() => undefined);
  runAutosaveRef.current = () => {
    const s = autosaveSnapshotRef.current;
    if (!savingRef.current && s.dirty && !s.published && s.versionId && s.manifest) {
      saveMut.mutate(undefined);
    }
  };

  // Debounce: reschedules on every manifest edit; clears once saved (dirty false).
  useEffect(() => {
    if (!dirty || published || !versionId || !workingManifest) return undefined;
    const timer = window.setTimeout(() => runAutosaveRef.current(), AUTOSAVE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [dirty, published, versionId, workingManifest]);

  // Flush a still-dirty draft when leaving the editor (SPA navigation / unmount).
  // beforeunload doesn't fire on in-app route changes, so this is what actually
  // closes the "clicked a sidebar link mid-edit" data-loss gap. Fire-and-forget.
  useEffect(
    () => () => {
      const s = autosaveSnapshotRef.current;
      if (s.dirty && !s.published && s.versionId && s.manifest) {
        void caliberApi
          .updateWorkflowVersion(s.versionId, s.manifest, s.hash)
          .catch(() => undefined);
      }
    },
    [],
  );

  function handleConnect({ source, target }: { source: string; target: string }): void {
    if (!workingManifest || source === target) return;
    const sourceNode = workingManifest.nodes[source];
    const targetNode = workingManifest.nodes[target];
    if (!sourceNode || !targetNode) return;
    if (!canConnectNodes(sourceNode, targetNode)) {
      setMessage(
        `Cannot connect ${source} → ${target} because their declared ports are incompatible.`,
      );
      return;
    }
    const initialMap = deriveEdgeMap(sourceNode, targetNode);
    const existing = new Set(workingManifest.edges.map((e) => e.id));
    const edgeId = makeEdgeId(source, target, existing);
    patchManifest((m) => {
      m.edges = [
        ...m.edges,
        { id: edgeId, from: source, to: target, map: initialMap },
      ];
      return m;
    });
    if (
      Object.keys(initialMap).length === 0
      && nodeOutputs(sourceNode).length > 0
      && nodeInputs(targetNode).length > 0
    ) {
      setMessage(
        `Connected ${source} → ${target}. No compatible ports were auto-mapped; review the edge contract before publishing.`,
      );
    }
    setPendingEdgeId(edgeId);
    setPendingEdgeAnchor(null);
  }

  const workflowComponentMap = useMemo(
    () =>
      new Map<WorkflowComponent["type"], WorkflowComponent>(
        (workflowComponentsQuery.data?.components ?? []).map((component) => [
          component.type,
          component,
        ]),
      ),
    [workflowComponentsQuery.data],
  );

  /** Quick-add: clicking "+" on a node opens the type picker popup. */
  const handleQuickAdd = useCallback(
    (sourceId: string) => {
      // Find the node's position on screen via its DOM element.
      const el = document.querySelector(`[data-testid="wf-node-${sourceId}"]`);
      if (el) {
        const rect = el.getBoundingClientRect();
        setQuickAdd({ sourceId, screenX: rect.right + 12, screenY: rect.top });
      } else {
        setQuickAdd({ sourceId, screenX: 400, screenY: 200 });
      }
    },
    [],
  );

  /** User picked a type from the quick-add popup — create + connect. */
  const handleQuickAddSelect = useCallback(
    (sourceId: string, type: string) => {
      if (!workingManifest) return;
      const existing = Object.keys(workingManifest.nodes).filter((id) => id.startsWith(type)).length;
      const newId = existing === 0 ? type : `${type}_${existing + 1}`;
      const componentSpec =
        workflowComponentMap.get(type as WorkflowComponent["type"]) ?? null;
      let created: ManifestNode;
      try {
        created = newNode(type, newId, componentSpec);
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        setMessage(detail);
        showToast.error(detail);
        setQuickAdd(null);
        return;
      }
      const sourceNode = workingManifest.nodes[sourceId];
      if (!sourceNode) return;
      const shouldConnect = canConnectNodes(sourceNode, created);
      const initialMap = shouldConnect ? deriveEdgeMap(sourceNode, created) : {};

      patchManifest((m) => {
        m.nodes[newId] = created;
        if (shouldConnect) {
          const edgeIds = new Set(m.edges.map((e) => e.id));
          const edgeId = makeEdgeId(sourceId, newId, edgeIds);
          m.edges = [
            ...m.edges,
            { id: edgeId, from: sourceId, to: newId, map: initialMap },
          ];
        }
        return m;
      });
      if (!shouldConnect && nodeOutputs(sourceNode).length > 0 && nodeInputs(created).length > 0) {
        setMessage(
          `Added ${newId}, but it was not connected because ${sourceId} has no compatible output for it yet.`,
        );
      }
      openInspectorForNode(newId);
      setQuickAdd(null);
    },
    [openInspectorForNode, patchManifest, workflowComponentMap, workingManifest],
  );

  /** Connection dropped on empty canvas space → open quick-add at pointer. */
  const handleConnectionDrop = useCallback(
    (
      sourceId: string,
      flowPos: { x: number; y: number },
      screenPos?: { x: number; y: number },
    ) => {
      const screenX = screenPos?.x ?? flowPos.x;
      const screenY = screenPos?.y ?? flowPos.y;
      setQuickAdd({ sourceId, screenX: screenX + 12, screenY: screenY + 12 });
    },
    [],
  );

  /** Handle drag-and-drop from the palette. */
  function handleDropNode(type: string, position: { x: number; y: number }): void {
    addNode(type, position);
  }

  function updateEdgeMap(edgeId: string, map: Record<string, string>): void {
    patchManifest((m) => {
      m.edges = m.edges.map((e) => (e.id === edgeId ? { ...e, map } : e));
      return m;
    });
  }

  function removeEdge(edgeId: string): void {
    patchManifest((m) => {
      m.edges = m.edges.filter((e) => e.id !== edgeId);
      return m;
    });
    setPendingEdgeId(null);
    setPendingEdgeAnchor(null);
  }

  const handleNodePositionChange = useCallback(
    (nodeId: string, position: FlowNodePosition) => {
      if (!versionId) return;
      const normalized = { x: Math.round(position.x), y: Math.round(position.y) };
      setNodeLayout((prev) => {
        const current = prev[nodeId];
        if (current && current.x === normalized.x && current.y === normalized.y) {
          return prev;
        }
        const next = { ...prev, [nodeId]: normalized };
        persistLayout(versionId, next);
        return next;
      });
    },
    [versionId],
  );

  const resetCanvasLayout = useCallback(() => {
    if (versionId) clearLayout(versionId);
    setNodeLayout({});
    setMessage("Canvas layout reset to auto.");
  }, [versionId]);

  const handleEdgeClick = useCallback(
    (edgeId: string, screenPosition?: ScreenPoint) => {
      if (!workingManifest) return;
      const edge = workingManifest.edges.find((item) => item.id === edgeId);
      if (edge) {
        setPendingEdgeId(edge.id);
        setPendingEdgeAnchor(screenPosition ?? null);
      }
    },
    [workingManifest],
  );

  const liveSteps = useMemo(() => runSummarySteps(activeRun), [activeRun]);
  const livePath = useMemo(
    () =>
      activeRun?.summary?.node_path?.filter(
        (item): item is string => typeof item === "string",
      ) ?? [],
    [activeRun],
  );
  const execPath = useMemo(() => {
    if (livePath.length > 0) return livePath;
    if (liveSteps.length > 0) return liveSteps.map((step) => step.node_id);
    return preview ? preview.steps.map((step) => step.node_id) : undefined;
  }, [livePath, liveSteps, preview]);

  // Per-step preview (Lakeflow "see what changed at every step"): index the
  // latest preview-run's steps by node so the canvas can badge each node and
  // the preview panel can show the selected node's output / what-changed.
  const stepByNode = useMemo<Record<string, PreviewStep>>(
    () => Object.fromEntries((preview?.steps ?? []).map((s) => [s.node_id, s])),
    [preview],
  );
  const monitoredRunUsesHistoricalVersion = Boolean(
    activeRun?.workflow_version_id &&
    activeRun.workflow_version_id !== versionId &&
    activeRunManifestMode !== "snapshot",
  );
  const activeRunVersionNumber = useMemo(() => {
    if (!activeRun) return null;
    const summaryVersionNumber = readNumber(activeRun.summary?.workflow_version_number);
    if (!activeRun.workflow_version_id || activeRun.workflow_version_id === versionId) {
      return versionQuery.data?.version_number ?? summaryVersionNumber ?? null;
    }
    return monitoredHistoricalVersionQuery.data?.version_number ?? summaryVersionNumber ?? null;
  }, [
    activeRun,
    monitoredHistoricalVersionQuery.data?.version_number,
    versionId,
    versionQuery.data?.version_number,
  ]);
  const activeRunSavedVersionLabel = useMemo(
    () => savedWorkflowVersionLabel(activeRun?.workflow_version_id, activeRunVersionNumber),
    [activeRun?.workflow_version_id, activeRunVersionNumber],
  );
  const activeRunSavedVersionReference = useMemo(() => {
    if (activeRunManifestMode === "snapshot") {
      return "the queued draft snapshot captured for this run";
    }
    return activeRunSavedVersionLabel
      ? `saved workflow version ${activeRunSavedVersionLabel}`
      : activeRun?.workflow_version_id
        ? `saved workflow version ${activeRun.workflow_version_id}`
        : "the saved workflow version used by this run";
  }, [activeRun?.workflow_version_id, activeRunManifestMode, activeRunSavedVersionLabel]);
  const monitoredHistoricalVersionManifest = useMemo(
    () => (
      monitoredHistoricalVersionQuery.data
        ? normalizeWorkflowManifest(monitoredHistoricalVersionQuery.data.manifest)
        : null
    ),
    [monitoredHistoricalVersionQuery.data],
  );
  const monitoredRunFallbackManifest = useMemo(() => {
    if (!activeRun || !monitoredRunCanUseSavedVersionFallback) return null;
    if (!activeRun.workflow_version_id || activeRun.workflow_version_id === versionId) {
      return persistedVersionManifest;
    }
    return monitoredHistoricalVersionManifest;
  }, [
    activeRun,
    monitoredRunCanUseSavedVersionFallback,
    monitoredHistoricalVersionManifest,
    persistedVersionManifest,
    versionId,
  ]);
  const monitoredRunSyntheticManifest = useMemo(
    () => buildSyntheticWorkflowRunManifest(activeRun, activeRunEffectiveCheckpoints),
    [activeRun, activeRunEffectiveCheckpoints],
  );
  const monitoredRunSavedVersionFallbackLoading = Boolean(
    activeRun &&
      monitoredRunUsesHistoricalVersion &&
      monitoredRunCanUseSavedVersionFallback &&
      monitoredHistoricalVersionQuery.isLoading,
  );
  const monitoredRunManifest = useMemo(() => {
    if (!activeRun) return persistedVersionManifest;
    if (monitoredRunManifestQuery.data) {
      return normalizeWorkflowManifest(monitoredRunManifestQuery.data.manifest);
    }
    if (monitoredRunFallbackManifest) {
      return monitoredRunFallbackManifest;
    }
    if (monitoredRunManifestQuery.isLoading || monitoredRunSavedVersionFallbackLoading) {
      return null;
    }
    return monitoredRunSyntheticManifest;
  }, [
    activeRun,
    monitoredRunFallbackManifest,
    monitoredRunManifestQuery.data,
    monitoredRunManifestQuery.isLoading,
    monitoredRunSavedVersionFallbackLoading,
    monitoredRunSyntheticManifest,
    persistedVersionManifest,
  ]);
  const monitoredRunManifestLoading = Boolean(
    activeRun && (
      monitoredRunManifestQuery.isLoading
      || (
        monitoredRunManifest == null
        && monitoredRunSavedVersionFallbackLoading
      )
    ),
  );
  const monitoredRunManifestError = useMemo(() => {
    if (!activeRun || monitoredRunManifest) return null;
    const manifestError = monitoredRunManifestQuery.error?.message ?? null;
    const fallbackError = monitoredHistoricalVersionQuery.error?.message ?? null;
    if (manifestError && fallbackError) {
      return `${manifestError} (saved-version fallback failed: ${fallbackError})`;
    }
    return manifestError ?? fallbackError;
  }, [
    activeRun,
    monitoredHistoricalVersionQuery.error,
    monitoredRunManifest,
    monitoredRunManifestQuery.error,
  ]);
  const monitoredRunManifestLoadErrorMessage = useMemo(() => {
    if (!activeRun || !monitoredRunManifestError || monitoredRunManifest) return null;
    return runMonitorManifestLoadErrorMessage({
      run: activeRun,
      manifestMode: activeRunManifestMode,
      versionReference:
        activeRun.workflow_version_id && activeRun.workflow_version_id !== versionId
          ? activeRunSavedVersionReference
          : "the saved workflow version already loaded in the editor",
      errorDetail: monitoredRunManifestError,
    });
  }, [
    activeRun,
    activeRunManifestMode,
    activeRunSavedVersionReference,
    monitoredRunManifest,
    monitoredRunManifestError,
    versionId,
  ]);
  const runMonitorManifestNote = useMemo(() => {
    const notes: string[] = [];
    if (activeRun && activeRunManifestMode === "snapshot") {
      notes.push(snapshotRunMonitorContextNote(activeRun));
    }
    if (activeRun && activeRunManifestMode !== "snapshot") {
      if (monitoredRunUsesHistoricalVersion) {
        notes.push(
          historicalVersionRunMonitorContextNote(
            activeRun,
            activeRunSavedVersionReference,
          ),
        );
      } else if (dirty) {
        notes.push(
          "This run is pinned to the last saved workflow version. New previews and runs from this page capture the current draft without saving a new version.",
        );
      }
    } else if (dirty) {
      notes.push(
        "Unsaved changes stay local to the editor until you preview or run. New previews and runs from this page capture the current draft without saving a new version.",
      );
    }
    return notes.length > 0 ? notes.join(" ") : null;
  }, [
    activeRun,
    activeRunManifestMode,
    activeRunSavedVersionReference,
    dirty,
    monitoredRunUsesHistoricalVersion,
  ]);
  const runMonitorManifestFallbackNotice = useMemo(() => {
    if (
      activeRun
      && monitoredRunManifest
      && monitoredRunManifestQuery.error
      && activeRunManifestMode !== "snapshot"
      && monitoredRunFallbackManifest
    ) {
      return savedVersionRunMonitorGraphFallbackMessage(
        activeRun,
        activeRun.workflow_version_id && activeRun.workflow_version_id !== versionId
          ? activeRunSavedVersionReference
          : "the saved workflow version already loaded in the editor",
      );
    }
    if (
      activeRun
      && monitoredRunManifest
      && !monitoredRunManifestQuery.data
      && !monitoredRunFallbackManifest
      && monitoredRunSyntheticManifest
    ) {
      return syntheticRunMonitorGraphFallbackMessage(activeRun);
    }
    return null;
  }, [
    activeRun,
    activeRunManifestMode,
    activeRunSavedVersionReference,
    monitoredRunFallbackManifest,
    monitoredRunManifest,
    monitoredRunManifestQuery.error,
    monitoredRunManifestQuery.data,
    monitoredRunSyntheticManifest,
    versionId,
  ]);
  const monitoredRunManifestUnavailableNotice = useMemo(
    () => (
      activeRun
      && !monitoredRunManifest
      && !monitoredRunManifestLoading
      && !monitoredRunManifestError
        ? runMonitorManifestUnavailableMessage({
            manifestMode: activeRunManifestMode,
            hasSummarySteps:
              Array.isArray(activeRun.summary?.steps) && activeRun.summary.steps.length > 0,
            hasCurrentNode: Boolean(
              typeof activeRun.current_node_id === "string" && activeRun.current_node_id.trim(),
            ),
            hasCheckpoints: activeRunEffectiveCheckpoints.length > 0,
          })
        : null
    ),
    [
      activeRun,
      activeRunManifestMode,
      activeRunEffectiveCheckpoints,
      monitoredRunManifest,
      monitoredRunManifestError,
      monitoredRunManifestLoading,
    ],
  );
  const nodeExecutionByNode = useMemo(
    () =>
      activeRun
        ? buildNodeExecutionBadgeMap({
            runSteps: liveSteps,
            runStatus: activeRun.status,
            currentNodeId: activeRun.current_node_id ?? null,
          })
        : buildNodeExecutionBadgeMap({
            previewSteps: preview?.steps ?? [],
          }),
    [activeRun, liveSteps, preview],
  );
  const upstreamFor = useCallback(
    (nodeId: string) =>
      (workingManifest?.edges ?? [])
        .filter((e) => e.to === nodeId && stepByNode[e.from])
        .map((e) => ({ nodeId: e.from, output: stepByNode[e.from]!.output })),
    [stepByNode, workingManifest],
  );
  const activeWaitNode = useMemo(() => {
    if (!activeRun || activeRun.status !== "waiting_event" || !activeRun.current_node_id) {
      return null;
    }
    const manifestNode = monitoredRunManifest?.nodes[activeRun.current_node_id];
    if (manifestNode?.type === "wait_for_event" || manifestNode?.type === "wait_until") {
      return manifestNode;
    }
    const syntheticNode = monitoredRunSyntheticManifest?.nodes[activeRun.current_node_id];
    return syntheticNode?.type === "wait_for_event" || syntheticNode?.type === "wait_until"
      ? syntheticNode
      : null;
  }, [activeRun, monitoredRunManifest, monitoredRunSyntheticManifest]);
  const activeRunActiveCheckpoint = useMemo(
    () => resolveWorkflowRunActiveCheckpoint(activeRun, activeRunEffectiveCheckpoints),
    [activeRun, activeRunEffectiveCheckpoints],
  );
  const activeRunCheckpointStateReady =
    runMonitorHydrated && !activeRunResumeSourceCheckpointsQuery.isLoading;
  const activeRunActiveCheckpointState = isRecord(activeRunActiveCheckpoint?.state_blob)
    ? activeRunActiveCheckpoint.state_blob
    : null;
  const activeRunApprovalCheckpointKind =
    activeRun?.status === "waiting_approval"
      ? approvalCheckpointKind(activeRunActiveCheckpointState)
      : null;
  const activeRunMissingResumeCheckpointIssue = activeRunCheckpointStateReady && activeRun
    ? activeRun.status === "waiting_event"
      ? !activeRunActiveCheckpoint
        ? "Manual and event-match resume are unavailable because this paused run no longer has a stored checkpoint. Inspect the recovery, checkpoint, and lineage panels before retrying from a healthy state or starting a new attempt."
        : null
      : activeRun.status === "waiting_approval"
        ? !activeRunActiveCheckpoint
          ? "Manual resume is unavailable because this paused run no longer has a stored checkpoint. Inspect the recovery, checkpoint, and lineage panels before retrying from a healthy state or starting a new attempt."
          : null
        : null
    : null;
  const activeRunCheckpointIdentityIssue = workflowRunCheckpointIdentityIssue(
    activeRun,
    activeRunActiveCheckpoint,
  );
  const activeWaitMode =
    activeWaitNode?.type === "wait_until"
      ? "wait_until"
      : activeWaitNode?.type === "wait_for_event"
        ? "wait_for_event"
        : readString(activeRunActiveCheckpointState?.kind);
  const activeWaitEventName =
    activeWaitNode?.type === "wait_for_event" && typeof activeWaitNode.event_name === "string"
      ? activeWaitNode.event_name
      : readString(activeRunActiveCheckpointState?.expected_event_name)
        ?? readString(activeRunActiveCheckpointState?.event_name)
        ?? "";
  const activeWaitCorrelationKey = readString(activeRunActiveCheckpointState?.correlation_key)
    ?? "";
  const activeWaitCorrelationValue = activeRunActiveCheckpointState?.correlation_value;
  const activeRunResumeEventNameIssue = workflowRunResumeEventNameIssue({
    status: activeRun?.status,
    waitMode: activeWaitMode,
    expectedEventName: activeWaitEventName,
    resumeEventName,
  });
  const activeRunResumeByEventIssue = workflowRunResumeByEventIssue({
    status: activeRun?.status,
    waitMode: activeWaitMode,
    correlationKey: activeWaitCorrelationKey,
    correlationValue: activeWaitCorrelationValue,
    resumeEventPayload,
  });
  const activeWaitUntilText =
    checkpointWaitUntilDisplay(activeRunActiveCheckpointState)
    ?? (
      activeWaitNode?.type === "wait_until"
        ? waitUntilDisplay(activeWaitNode)
        : null
    )
    ?? "the configured time";
  const activeRunApprovalLabel = workflowRunApprovalNoun(activeRunApprovalCheckpointKind);
  const activeRunApprovalSubject = workflowRunApprovalSubject(activeRunApprovalCheckpointKind);
  const relevantRunApprovals = useMemo(() => {
    if (!activeRun?.current_node_id) {
      return runApprovals;
    }
    return runApprovals.filter((approval) => approval.node_id === activeRun.current_node_id);
  }, [activeRun?.current_node_id, runApprovals]);
  const pendingApproval =
    relevantRunApprovals.find((approval) => approval.status === "pending") ?? null;
  const approvedApproval =
    relevantRunApprovals.find((approval) => approval.status === "approved") ?? null;
  const rejectedApproval =
    relevantRunApprovals.find((approval) => approval.status === "rejected") ?? null;
  const runCapabilities = capabilitiesQuery.data?.workflow_runs ?? null;
  const queueRunsEnabled = Boolean(runCapabilities?.queue_enabled);
  const runtimeApprovalsEnabled = Boolean(runCapabilities?.runtime_approvals_enabled);
  const checkpointingEnabled = Boolean(runCapabilities?.checkpointing_enabled);
  const manualResumeEnabled = Boolean(runCapabilities?.supports_resume);
  const workflowStatus = workflowStatusOverride ?? workflowQuery.data?.status ?? null;
  const currentVersionStatus = versionQuery.data?.status ?? null;
  const runExecuteDisabledReason = capabilitiesQuery.isLoading
    ? "Loading workflow run capabilities"
    : capabilitiesQuery.isError
      ? "Workflow run capabilities could not be loaded. Refresh the page or verify deployment settings/API health."
      : workflowStatus === "archived"
        ? "Archived workflows cannot be run"
        : workflowStatus === "paused"
          ? "Resume this workflow before running it"
      : !queueRunsEnabled
        ? "Enable the run queue to execute workflows"
        : null;
  const runBusy =
    runMut.isPending ||
    cancelRunMut.isPending ||
    retryRunMut.isPending ||
    resumeRunMut.isPending ||
    resumeRunByEventMut.isPending ||
    approveRunMut.isPending ||
    rejectRunMut.isPending;
  const pagedRunHistory = useMemo(() => {
    const runs = new Map<string, WorkflowRun>();
    for (const page of runHistoryQuery.data?.pages ?? []) {
      for (const run of page.data ?? []) {
        runs.set(run.workflow_run_id, run);
      }
    }
    return [...runs.values()];
  }, [runHistoryQuery.data]);
  const editorRunHistory = useMemo(() => {
    if (pagedRunHistory.length > 0) return pagedRunHistory;
    if (runHistoryQuery.isError) return runHistoryFallbackQuery.data ?? [];
    return [];
  }, [pagedRunHistory, runHistoryFallbackQuery.data, runHistoryQuery.isError]);
  const runHistoryLoading =
    runHistoryQuery.isLoading || (runHistoryQuery.isError && runHistoryFallbackQuery.isLoading);
  const runHistoryRefreshing =
    runHistoryQuery.isFetching || runHistoryFallbackQuery.isFetching;
  const canPauseRun = Boolean(
    queueRunsEnabled &&
    runCapabilities?.supports_cancel &&
    activeRun &&
    RUN_ACTIVE_STATUSES.has(activeRun.status) &&
    activeRun.status !== "waiting_approval" &&
    activeRun.status !== "waiting_event",
  );
  const canRetryRun = Boolean(
    queueRunsEnabled &&
    runCapabilities?.supports_retry &&
    activeRun &&
    ["failed", "cancelled", "expired"].includes(activeRun.status),
  );
  const canResumeRun = Boolean(
    manualResumeEnabled &&
    queueRunsEnabled &&
    checkpointingEnabled &&
    !activeRunMissingResumeCheckpointIssue &&
    !activeRunCheckpointIdentityIssue &&
    !activeRunResumeEventNameIssue &&
    activeRun
    && (
      activeRun.status === "waiting_event"
      || (
        activeRun.status === "waiting_approval"
        && runApprovalsReady
        && approvedApproval !== null
        && pendingApproval === null
        && rejectedApproval === null
      )
    ),
  );
  const canApproveRun = Boolean(
    queueRunsEnabled &&
    runtimeApprovalsEnabled &&
    activeRun?.status === "waiting_approval" &&
    pendingApproval,
  );
  const canRejectRun = canApproveRun;
  const showQueueDisabledNote = Boolean(
    capabilitiesQuery.data && !queueRunsEnabled,
  );
  const showCapabilitiesUnavailableNote = capabilitiesQuery.isError;
  const showWorkflowPausedNote = workflowStatus === "paused";
  const showWorkflowArchivedNote = workflowStatus === "archived";
  const activeRunApprovalQueueIssue = Boolean(
    activeRun?.status === "waiting_approval" && !queueRunsEnabled,
  )
    ? "Approval actions are unavailable until the workflow run queue is enabled for this deployment. Re-enable the queue before approving or rejecting this paused run."
    : null;
  const activeRunApprovalRecordsActionIssue =
    activeRun?.status === "waiting_approval"
      ? runApprovalsError
        ? activeRunApprovalCheckpointKind === "runtime_approval"
          ? "Approval actions are unavailable because runtime approval records could not be loaded. Refresh approval history or inspect recovery diagnostics before continuing this run."
          : "Approval actions are unavailable because approval records could not be loaded. Refresh approval history or inspect recovery diagnostics before continuing this run."
        : !runApprovalsReady
          ? activeRunApprovalCheckpointKind === "runtime_approval"
            ? "Approval actions are unavailable until runtime approval records finish loading."
            : "Approval actions are unavailable until approval records finish loading."
          : null
      : null;
  const showApprovalCapabilityNote = Boolean(
    activeRun?.status === "waiting_approval"
    && (
      Boolean(activeRunApprovalQueueIssue)
      || Boolean(activeRunApprovalRecordsActionIssue)
      || !runtimeApprovalsEnabled
    ),
  );
  const activeRunResumeQueueIssue = !queueRunsEnabled && activeRun
    ? activeRun.status === "waiting_event"
      ? "Manual and event-match resume are unavailable until the workflow run queue is enabled for this deployment. Re-enable the queue before continuing this paused event gate."
      : activeRun.status === "waiting_approval"
        ? "Manual resume is unavailable until the workflow run queue is enabled for this deployment. Re-enable the queue before continuing this paused approval run."
        : null
    : null;
  const activeRunResumeCheckpointingIssue = Boolean(
    (activeRun?.status === "waiting_event" || activeRun?.status === "waiting_approval")
    && !checkpointingEnabled,
  )
    ? activeRun?.status === "waiting_event"
      ? "Manual and event-match resume are unavailable until checkpoint persistence is enabled for workflow runs. Re-enable checkpointing for this deployment before trying to continue this paused event gate."
      : "Manual resume is unavailable until checkpoint persistence is enabled for workflow runs. Re-enable checkpointing for this deployment before continuing this paused approval run."
    : null;
  const activeRunApprovalRecordsIssue =
    activeRun?.status === "waiting_approval"
      ? runApprovalsError
        ? activeRunApprovalCheckpointKind === "runtime_approval"
          ? "Manual resume is unavailable because runtime approval records could not be loaded. Refresh approval history or inspect recovery diagnostics before continuing this run."
          : "Manual resume is unavailable because approval records could not be loaded. Refresh approval history or inspect recovery diagnostics before continuing this run."
        : !runApprovalsReady
          ? activeRunApprovalCheckpointKind === "runtime_approval"
            ? "Manual resume is unavailable until runtime approval records finish loading."
            : "Manual resume is unavailable until approval records finish loading."
          : null
      : null;
  const showResumeCapabilityNote = Boolean(
    activeRun
    && (
      activeRunApprovalRecordsIssue ||
      activeRunMissingResumeCheckpointIssue
      || activeRunCheckpointIdentityIssue
      || activeRunResumeEventNameIssue
      || activeRunResumeQueueIssue
      || activeRunResumeCheckpointingIssue
      || (
        !manualResumeEnabled &&
        (
          activeRun.status === "waiting_event"
          || activeRun.status === "waiting_approval"
        )
      )
    ),
  );
  const resumeCapabilityNote = activeRunResumeQueueIssue
    ?? activeRunResumeCheckpointingIssue
    ?? activeRunApprovalRecordsIssue
    ?? activeRunMissingResumeCheckpointIssue
    ?? activeRunCheckpointIdentityIssue
    ?? activeRunResumeEventNameIssue
    ?? (
      !showResumeCapabilityNote
        ? null
        : activeRun?.status === "waiting_event"
          ? activeWaitMode === "wait_until"
            ? "Manual resume override is unavailable until checkpoint persistence is enabled for workflow runs. This scheduled wait will resume automatically when its deadline arrives."
            : "Manual resume is unavailable until checkpoint persistence is enabled for workflow runs. Use the event-match controls below to resume this event gate."
          : "Manual resume is unavailable until checkpoint persistence is enabled for workflow runs. Record the required approval to continue this run."
    );
  const activeWaitUntilCapabilityNote = activeRun?.status === "waiting_event"
    && activeWaitMode === "wait_until"
    ? activeRunResumeQueueIssue
      ? "Automatic and manual resume are unavailable until the workflow run queue is enabled for this deployment."
      : activeRunResumeCheckpointingIssue
        ? "Manual resume is unavailable until checkpoint persistence is enabled for workflow runs. The worker will still re-queue this run automatically when that time arrives."
        : activeRunMissingResumeCheckpointIssue
          ? "Automatic and manual resume are unavailable because this paused run no longer has a stored checkpoint. Inspect the recovery, checkpoint, and lineage panels before retrying from a healthy state or starting a new attempt."
          : activeRunCheckpointIdentityIssue
            ? "Automatic and manual resume are unavailable until the stored checkpoint and active node agree again. Inspect the checkpoint and recovery panels before trying to continue this scheduled wait."
            : !manualResumeEnabled
              ? "The worker will resume this run automatically when that time arrives, but manual Resume override is unavailable for this deployment."
              : "The worker will resume this run automatically when that time arrives, and Resume remains available as a manual override."
    : null;

  useEffect(() => {
    if (activeRun?.status !== "waiting_event") {
      setResumeEventName("");
      setResumeEventPayload("");
      return;
    }
    if (activeWaitMode === "wait_until") {
      setResumeEventName("");
      setResumeEventPayload("");
      return;
    }
    setResumeEventName(activeWaitEventName);
    setResumeEventPayload((current) => {
      const basePayload: Record<string, unknown> = {
        source: "manual_resume",
        node_id: activeRun.current_node_id ?? undefined,
      };
      const basePayloadText = formatResumeEventPayload(basePayload);
      const payload: Record<string, unknown> = { ...basePayload };
      if (
        activeWaitCorrelationKey
        && activeWaitCorrelationValue !== null
        && activeWaitCorrelationValue !== undefined
        && activeWaitCorrelationValue !== ""
      ) {
        payload[activeWaitCorrelationKey] = activeWaitCorrelationValue;
      }
      const upgradedPayloadText = formatResumeEventPayload(payload);
      if (!current.trim() || current === basePayloadText || current === upgradedPayloadText) {
        return upgradedPayloadText;
      }
      return current;
    });
  }, [
    activeRun?.workflow_run_id,
    activeRun?.status,
    activeRun?.current_node_id,
    activeWaitCorrelationKey,
    activeWaitCorrelationValue,
    activeWaitEventName,
    activeWaitMode,
  ]);

  const submitActiveRunResume = (): void => {
    if (!activeRun) return;
    if (activeRunMissingResumeCheckpointIssue) {
      setRunMessage(activeRunMissingResumeCheckpointIssue);
      return;
    }
    if (activeRunCheckpointIdentityIssue) {
      setRunMessage(activeRunCheckpointIdentityIssue);
      return;
    }
    if (activeRunResumeEventNameIssue) {
      setRunMessage(activeRunResumeEventNameIssue);
      return;
    }
    if (activeRun.status === "waiting_approval") {
      if (!runApprovalsReady) {
        setRunMessage(
          activeRunApprovalRecordsIssue
          ?? (
            activeRunApprovalCheckpointKind === "runtime_approval"
              ? "Resume unavailable until runtime approval records finish loading."
              : "Resume unavailable until approval records finish loading."
          ),
        );
        return;
      }
      if (pendingApproval) {
        setRunMessage(
          `Resume unavailable until the pending ${workflowRunApprovalNoun(
            activeRunApprovalCheckpointKind,
          )} is resolved.`,
        );
        return;
      }
      if (rejectedApproval) {
        setRunMessage(
          activeRunApprovalCheckpointKind === "runtime_approval"
            ? "Resume unavailable after runtime approval rejection."
            : "Resume unavailable after approval rejection.",
        );
        return;
      }
      if (!approvedApproval) {
        setRunMessage(
          activeRunApprovalCheckpointKind === "runtime_approval"
            ? "Resume unavailable until a runtime approval has been approved."
            : "Resume unavailable until an approval has been recorded.",
        );
        return;
      }
    }
    if (activeRun.status === "waiting_event") {
      if (activeWaitNode?.type === "wait_until") {
        resumeRunMut.mutate({ runId: activeRun.workflow_run_id });
        return;
      }
      let eventPayload: unknown;
      try {
        eventPayload = parseJsonObjectText(resumeEventPayload);
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        setRunMessage(`Resume failed: event payload must be valid JSON (${detail}).`);
        return;
      }
      resumeRunMut.mutate({
        runId: activeRun.workflow_run_id,
        event_name: resumeEventName.trim() || undefined,
        event_payload: eventPayload,
      });
      return;
    }
    resumeRunMut.mutate({ runId: activeRun.workflow_run_id });
  };

  const submitActiveRunResumeByEvent = (): void => {
    if (activeRunMissingResumeCheckpointIssue) {
      setRunMessage(activeRunMissingResumeCheckpointIssue);
      return;
    }
    if (activeRunCheckpointIdentityIssue) {
      setRunMessage(activeRunCheckpointIdentityIssue);
      return;
    }
    if (activeRunResumeByEventIssue) {
      setRunMessage(activeRunResumeByEventIssue);
      return;
    }
    if (activeRunResumeEventNameIssue) {
      setRunMessage(activeRunResumeEventNameIssue);
      return;
    }
    const eventName = resumeEventName.trim();
    if (!eventName) {
      setRunMessage("External event resume failed: event name is required.");
      return;
    }
    let eventPayload: unknown;
    try {
      eventPayload = parseJsonObjectText(resumeEventPayload);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      setRunMessage(
        `External event resume failed: event payload must be valid JSON (${detail}).`,
      );
      return;
    }
    resumeRunByEventMut.mutate({
      event_name: eventName,
      event_payload: eventPayload,
    });
  };

  const pendingEdge = useMemo(
    () =>
      pendingEdgeId && workingManifest
        ? (workingManifest.edges.find((e) => e.id === pendingEdgeId) ?? null)
        : null,
    [pendingEdgeId, workingManifest],
  );

  useEffect(() => {
    if (!pendingEdgeId) setPendingEdgeAnchor(null);
  }, [pendingEdgeId]);
  const outlineNodes = useMemo(() => {
    const allNodes = Object.values(workingManifest?.nodes ?? {});
    const query = outlineQuery.trim().toLowerCase();
    if (!query) return allNodes;
    return allNodes.filter((node) => {
      const label = nodeLabel(node).toLowerCase();
      return label.includes(query) || node.id.toLowerCase().includes(query);
    });
  }, [outlineQuery, workingManifest]);
  const recentWorkflowRuns = useMemo(() => {
    const runs = new Map<string, WorkflowRun>();
    for (const run of editorRunHistory) {
      runs.set(run.workflow_run_id, run);
    }
    if (activeRun?.workflow_run_id) {
      runs.set(activeRun.workflow_run_id, activeRun);
    }
    return [...runs.values()].sort((a, b) => runSortKey(b) - runSortKey(a));
  }, [activeRun, editorRunHistory]);
  const waitingEventRunCount = recentWorkflowRuns.filter(
    (run) => run.status === "waiting_event",
  ).length;
  const paletteItems = useMemo(
    () => buildNodePalette(workflowComponentsQuery.data?.components),
    [workflowComponentsQuery.data],
  );

  if (!workingManifest) {
    if (versionQuery.isError) {
      return (
        <div className="flex h-full items-center justify-center px-6">
          <div
            data-testid="workflow-editor-load-error"
            className="w-full max-w-md rounded-2xl border border-rose-200 bg-rose-50 px-5 py-4 text-sm text-rose-900 shadow-sm"
          >
            <div className="font-semibold">Workflow editor could not be loaded.</div>
            <p className="mt-1 text-xs leading-relaxed text-rose-800">
              {versionQuery.error.message}
            </p>
            <button
              type="button"
              data-testid="workflow-editor-retry"
              className="mt-3 rounded-lg border border-rose-300 bg-white px-3 py-1.5 text-xs font-semibold text-rose-900 transition hover:bg-rose-100"
              onClick={() => {
                void versionQuery.refetch();
              }}
            >
              Retry loading
            </button>
          </div>
        </div>
      );
    }
    return (
      <div
        data-testid="workflow-editor-loading"
        className="flex h-full items-center justify-center text-sm text-zinc-400"
      >
        Loading editor…
      </div>
    );
  }

  const editorManifest = workingManifest;
  const codeNode =
    codeNodeId && editorManifest ? (editorManifest.nodes[codeNodeId] ?? null) : null;

  return (
    <div className="flex h-[calc(100vh-7rem)] flex-col" data-testid="workflow-editor">
      {/* Toolbar — n8n-inspired clean bar */}
      <div className="flex items-center justify-between border-b border-zinc-200 bg-white px-4 py-2">
        <div className="flex items-center gap-3">
          <div className="text-sm font-semibold text-zinc-900">
            {editorManifest.name}
          </div>
          <span className="rounded-full bg-zinc-100 px-2 py-0.5 text-[10px] font-medium text-zinc-500">
            v{versionQuery.data?.version_number} · {versionQuery.data?.status}
          </span>
          {saveMut.isPending ? (
            <span
              data-testid="editor-save-status"
              className="rounded-full bg-zinc-100 px-2 py-0.5 text-[10px] font-medium text-zinc-500"
            >
              Saving…
            </span>
          ) : dirty ? (
            <span
              data-testid="editor-save-status"
              className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium text-amber-700"
            >
              Unsaved
            </span>
          ) : (
            <span
              data-testid="editor-save-status"
              className="rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-medium text-emerald-700"
            >
              Saved
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {message && (
            <span data-testid="editor-message" className="mr-2 rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-1 text-xs text-zinc-500">
              {message}
            </span>
          )}
          {runMessage && (
            <span data-testid="editor-run-message" className="mr-2 rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-1 text-xs text-zinc-500">
              {runMessage}
            </span>
          )}
          <button
            type="button"
            data-testid="editor-undo"
            title="Undo (⌘Z)"
            aria-label="Undo"
            disabled={published || undoStack.length === 0}
            onClick={undo}
            className="rounded-lg border border-zinc-200 px-2.5 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 disabled:opacity-40 active:scale-[0.97]"
          >
            ↶
          </button>
          <button
            type="button"
            data-testid="editor-redo"
            title="Redo (⇧⌘Z)"
            aria-label="Redo"
            disabled={published || redoStack.length === 0}
            onClick={redo}
            className="rounded-lg border border-zinc-200 px-2.5 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 disabled:opacity-40 active:scale-[0.97]"
          >
            ↷
          </button>
          <button
            type="button"
            data-testid="editor-save"
            disabled={published || saveMut.isPending}
            onClick={() => saveMut.mutate(undefined)}
            className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 disabled:opacity-40 active:scale-[0.97]"
          >
            💾 Save
          </button>
          <button
            type="button"
            data-testid="editor-validate"
            onClick={() => validateMut.mutate(undefined)}
            className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 active:scale-[0.97]"
          >
            ✓ Validate
          </button>
          <button
            type="button"
            data-testid="editor-preview"
            onClick={() => setShowPreview((v) => !v)}
            className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 active:scale-[0.97]"
          >
            ▶ Preview
          </button>
          <div
            role="tablist"
            aria-label="Editor view"
            className="flex items-center gap-0.5 rounded-lg border border-zinc-200 p-0.5"
          >
            {(
              [
                { mode: "visual", label: "◧ Visual" },
                { mode: "code", label: "</> Code" },
                { mode: "plan", label: "✦ Plan" },
              ] as const
            ).map(({ mode, label }) => (
              <button
                key={mode}
                type="button"
                role="tab"
                aria-selected={viewMode === mode ? "true" : "false"}
                data-testid={`editor-view-${mode}`}
                onClick={() => setViewMode(mode)}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors active:scale-[0.97] ${
                  viewMode === mode
                    ? "bg-zinc-900 text-white"
                    : "text-zinc-600 hover:bg-zinc-50"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <button
            type="button"
            data-testid="editor-copilot-toggle"
            onClick={() => setCopilotOpen((v) => !v)}
            className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors active:scale-[0.97] ${
              copilotOpen
                ? "border-violet-300 bg-violet-50 text-violet-700"
                : "border-zinc-200 text-zinc-700 hover:bg-zinc-50"
            }`}
          >
            ✨ Copilot
          </button>
          <button
            type="button"
            data-testid="editor-auto-layout"
            onClick={resetCanvasLayout}
            className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 active:scale-[0.97]"
          >
            ⌗ Auto Layout
          </button>
          <button
            type="button"
            data-testid="editor-run-monitor"
            onClick={() => setShowRunMonitor((v) => !v)}
            className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 active:scale-[0.97]"
          >
            ⏯ Run Monitor
          </button>
          <button
            type="button"
            data-testid="editor-publish"
            disabled={published}
            onClick={() => setShowPublish(true)}
            className="rounded-lg bg-zinc-900 px-4 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-zinc-800 disabled:opacity-40 active:scale-[0.97]"
          >
            ↑ Publish
          </button>
        </div>
      </div>

      <div className="flex flex-1 overflow-hidden">
        {/* Left rail — palette + outline */}
        <CollapsiblePanel
          id="wf-palette"
          side="left"
          title="Palette"
          widthClass="w-56"
          bodyClassName="bg-zinc-50/50 p-3"
        >
          <NodePalette
            onAddNode={addNode}
            components={workflowComponentsQuery.data?.components ?? null}
          />
          <div className="mt-5 space-y-1">
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-zinc-400">
              Outline
            </h3>
            <input
              type="text"
              data-testid="outline-search"
              className="mb-1 w-full rounded-lg border border-zinc-200 bg-white px-2.5 py-1.5 text-xs text-zinc-700 placeholder:text-zinc-400 transition-colors focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
              placeholder="Find node…"
              value={outlineQuery}
              onChange={(event) => setOutlineQuery(event.target.value)}
            />
            {outlineNodes.map((node) => (
              <button
                key={node.id}
                type="button"
                data-testid={`outline-${node.id}`}
                onClick={() => selectNode(node.id)}
                onDoubleClick={() => openInspectorForNode(node.id)}
                className={`flex w-full items-center gap-2 truncate rounded-lg px-2.5 py-1.5 text-left text-xs transition-colors ${
                  selected === node.id
                    ? "bg-caliber-50 font-semibold text-caliber-900 border border-caliber-200"
                    : "text-zinc-600 hover:bg-zinc-100"
                }`}
              >
                {nodeLabel(node)}
              </button>
            ))}
          </div>
        </CollapsiblePanel>

        {/* Canvas */}
        <div className="relative flex-1">
          <Canvas
            manifest={editorManifest}
            selectedNodeId={selected}
            selectedNodeIds={selectedIds}
            nodePositions={nodeLayout}
            validationReport={report}
            componentSpecs={workflowComponentMap}
            onSelectNode={selectNode}
            onSelectionChange={handleSelectionChange}
            onNodeDoubleClick={openInspectorForNode}
            onEdgeClick={handleEdgeClick}
            onConnect={handleConnect}
            onNodePositionChange={handleNodePositionChange}
            executionPath={execPath}
            onQuickAdd={handleQuickAdd}
            onDuplicate={duplicateNode}
            onViewCode={setCodeNodeId}
            nodeExecutionByNode={nodeExecutionByNode}
            onDropNode={handleDropNode}
            onConnectionDrop={handleConnectionDrop}
          />
          {codeNode && (
            <NodeCodeModal
              node={codeNode}
              onApply={(updated) =>
                patchManifest((m) => ({
                  ...m,
                  nodes: { ...m.nodes, [updated.id]: updated },
                }))
              }
              onClose={() => setCodeNodeId(null)}
            />
          )}
          {viewMode === "code" && (
            <div className="absolute inset-0 z-10" data-testid="code-overlay">
              <CodeView
                manifest={editorManifest}
                onApplyManifest={(m) => {
                  patchManifest(() => m);
                  setMessage("Applied manifest from code view");
                }}
                loadPython={() => caliberApi.exportWorkflowPython(versionId!)}
              />
            </div>
          )}
          {copilotOpen && viewMode === "visual" && (
            <div
              data-testid="copilot-panel"
              className="absolute right-0 top-0 bottom-0 z-20 w-[380px] max-w-full border-l border-zinc-200 shadow-xl"
            >
              <WorkflowCopilot
                versionId={versionId!}
                manifest={editorManifest}
                onApply={(m) => {
                  patchManifest(() => m);
                  setMessage("Copilot edit applied");
                }}
              />
            </div>
          )}
          {viewMode === "plan" && (
            <div className="absolute inset-0 z-10" data-testid="plan-overlay">
              <WorkflowPlanPanel
                versionId={versionId!}
                manifest={editorManifest}
                onApply={(m) => {
                  patchManifest(() => m);
                  setViewMode("visual");
                  setMessage("Workflow built from plan");
                }}
              />
            </div>
          )}
          {pendingEdge && (
            <ConnectMapPopover
              source={editorManifest.nodes[pendingEdge.from]!}
              target={editorManifest.nodes[pendingEdge.to]!}
              map={pendingEdge.map}
              anchor={pendingEdgeAnchor}
              onChange={(map) => updateEdgeMap(pendingEdge.id, map)}
              onDone={() => {
                setPendingEdgeId(null);
                setPendingEdgeAnchor(null);
              }}
              onRemove={() => removeEdge(pendingEdge.id)}
            />
          )}
          {showPreview && (
            <div
              data-testid="preview-panel"
              className="absolute inset-x-0 bottom-0 z-[15] flex max-h-[55%] flex-col rounded-t-2xl border-t border-zinc-200 bg-white shadow-[0_-6px_24px_rgba(0,0,0,0.08)]"
            >
              {/* Header */}
              <div className="flex items-center justify-between border-b border-zinc-100 px-4 py-2">
                <div className="flex items-center gap-2 text-sm font-semibold text-zinc-900">
                  <span>▶</span> Preview Run
                  <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700">
                    Tools sandboxed
                  </span>
                </div>
                <button
                  type="button"
                  data-testid="preview-close"
                  aria-label="Close preview"
                  onClick={() => setShowPreview(false)}
                  className="flex h-7 w-7 items-center justify-center rounded-lg text-zinc-400 transition-colors hover:bg-zinc-100 hover:text-zinc-700"
                >
                  ✕
                </button>
              </div>

              {/* Body: input column + results column */}
              <div className="flex min-h-0 flex-1 gap-4 overflow-hidden p-4">
                <div className="flex w-72 shrink-0 flex-col gap-2">
                  <input
                    data-testid="preview-session-id"
                    className="rounded-lg border border-zinc-200 bg-white px-3 py-2 text-xs transition-colors hover:border-zinc-300 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
                    placeholder="Session ID (optional)"
                    value={sessionIdInput}
                    onChange={(e) => setSessionIdInput(e.target.value)}
                  />
                  <textarea
                    data-testid="preview-input"
                    className="flex-1 resize-none rounded-lg border border-zinc-200 bg-white px-3 py-2 text-xs transition-colors hover:border-zinc-300 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
                    rows={4}
                    placeholder="Enter test input…"
                    value={previewInput}
                    onChange={(e) => setPreviewInput(e.target.value)}
                  />
                  <button
                    type="button"
                    data-testid="preview-run"
                    disabled={previewMut.isPending}
                    onClick={() => previewMut.mutate(undefined)}
                    className="rounded-lg bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-zinc-800 disabled:opacity-40 active:scale-[0.97]"
                  >
                    {previewMut.isPending ? "Running…" : "Run Preview ▶"}
                  </button>
                </div>

                <div className="min-w-0 flex-1 overflow-auto">
                  {preview ? (
                    <div data-testid="preview-result" className="space-y-2 text-xs">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span
                          className={`rounded-full px-2.5 py-0.5 font-semibold ${preview.status === "completed" ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}
                        >
                          {preview.status}
                        </span>
                        {preview.steps.map((s) => (
                          <button
                            type="button"
                            key={s.node_id}
                            data-testid={`preview-step-${s.node_id}`}
                            onClick={() => selectNode(s.node_id)}
                            className={`flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 transition-colors ${
                              selected === s.node_id
                                ? "border-caliber-300 bg-caliber-50"
                                : "border-zinc-200 bg-white hover:bg-zinc-50"
                            }`}
                          >
                            <span>{s.status === "ok" ? "✅" : s.status === "skipped" ? "⏭" : "❌"}</span>
                            <span className="truncate font-mono text-zinc-700">{s.node_id}</span>
                          </button>
                        ))}
                      </div>
                      {selected && stepByNode[selected] ? (
                        <div className="rounded-lg border border-zinc-200 bg-white p-2.5">
                          <StepPreview step={stepByNode[selected]!} upstream={upstreamFor(selected)} />
                        </div>
                      ) : (
                        <div className="whitespace-pre-wrap rounded-lg border border-zinc-200 bg-zinc-50 p-2.5 font-mono text-zinc-600">
                          {preview.output || "(no output)"}
                        </div>
                      )}
                      <div className="text-zinc-400">Click a step above to inspect its input/output.</div>
                    </div>
                  ) : (
                    <div className="flex h-full items-center justify-center rounded-lg border border-dashed border-zinc-200 text-xs text-zinc-400">
                      Enter an input and run a preview to see per-step output.
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
          {showRunMonitor && (
            <div
              data-testid="run-monitor-panel"
              className="absolute bottom-2 right-2 z-[5] flex max-h-[78vh] w-[42rem] max-w-[calc(100vw-1rem)] flex-col rounded-xl border border-zinc-200 bg-white p-4 shadow-lg"
            >
              <div className="mb-3 flex items-center justify-between">
                <div className="flex items-center gap-2 text-sm font-semibold text-zinc-900">
                  <span>⏯</span>
                  <span>Run Monitor</span>
                </div>
                {activeRun && (
                  <div className="flex items-center gap-2">
                    <span
                      data-testid="run-status-badge"
                      data-status={activeRun.status}
                      title={activeRun.status}
                      className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${workflowRunStatusBorderClass(activeRun.status)}`}
                    >
                      {workflowRunStatusLabel(activeRun.status)}
                    </span>
                    <WorkflowRunArtifactPersistenceBadge
                      run={activeRun}
                      compact
                      dataTestId="active-run-artifact-persistence"
                    />
                    <Link
                      to={workflowRunPath(activeRun.workflow_run_id)}
                      target="_blank"
                      rel="noreferrer"
                      data-testid="run-active-open-link"
                      className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-medium text-zinc-600 transition-colors hover:bg-zinc-50"
                    >
                      Open link
                    </Link>
                    <button
                      type="button"
                      data-testid="run-active-copy-link"
                      onClick={() => copyWorkflowRunLink(activeRun.workflow_run_id)}
                      className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-medium text-zinc-600 transition-colors hover:bg-zinc-50"
                    >
                      Copy link
                    </button>
                  </div>
                )}
              </div>

              <textarea
                data-testid="run-input"
                className="mb-2 w-full rounded-lg border border-zinc-200 bg-white px-3 py-2 text-xs transition-colors hover:border-zinc-300 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
                rows={3}
                placeholder="Enter runtime input…"
                value={runInput}
                onChange={(e) => setRunInput(e.target.value)}
              />
              <input
                data-testid="run-session-id"
                className="mb-2 w-full rounded-lg border border-zinc-200 bg-white px-3 py-2 text-xs transition-colors hover:border-zinc-300 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
                placeholder="Session ID (optional)"
                value={sessionIdInput}
                onChange={(e) => setSessionIdInput(e.target.value)}
              />
              <div className="mb-3 grid grid-cols-4 gap-2">
                <button
                  type="button"
                  data-testid="run-execute"
                  disabled={runMut.isPending || Boolean(runExecuteDisabledReason)}
                  title={runExecuteDisabledReason ?? undefined}
                  onClick={() => runMut.mutate(undefined)}
                  className="rounded-lg bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-zinc-800 disabled:opacity-40 active:scale-[0.97]"
                >
                  {runMut.isPending ? "Starting…" : "Run"}
                </button>
                <button
                  type="button"
                  data-testid="run-pause"
                  disabled={!canPauseRun || runBusy || !activeRun}
                  onClick={() => {
                    if (!activeRun) return;
                    cancelRunMut.mutate(activeRun.workflow_run_id);
                  }}
                  className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 disabled:opacity-40 active:scale-[0.97]"
                >
                  Pause
                </button>
                <button
                  type="button"
                  data-testid="run-resume"
                  disabled={!canResumeRun || runBusy || !activeRun}
                  onClick={() => {
                    if (!activeRun) return;
                    submitActiveRunResume();
                  }}
                  className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 disabled:opacity-40 active:scale-[0.97]"
                >
                  Resume
                </button>
                <button
                  type="button"
                  data-testid="run-retry"
                  disabled={!canRetryRun || runBusy || !activeRun}
                  onClick={() => {
                    if (!activeRun) return;
                    retryRunMut.mutate({ runId: activeRun.workflow_run_id });
                  }}
                  className="rounded-lg border border-violet-200 bg-violet-50 px-3 py-1.5 text-xs font-medium text-violet-700 transition-colors hover:bg-violet-100 disabled:opacity-40 active:scale-[0.97]"
                >
                  Retry
                </button>
              </div>

              {(showCapabilitiesUnavailableNote || showWorkflowPausedNote || showWorkflowArchivedNote || showQueueDisabledNote || showApprovalCapabilityNote || showResumeCapabilityNote) && (
                <div className="mb-3 space-y-2">
                  {showCapabilitiesUnavailableNote && (
                    <div
                      data-testid="run-capabilities-unavailable-note"
                      className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[11px] text-rose-800"
                    >
                      Workflow run capabilities could not be loaded for this deployment. Run
                      controls stay disabled until the capabilities check succeeds. Refresh the
                      page or verify the CALIBER API and workflow-run settings.
                    </div>
                  )}
                  {showWorkflowPausedNote && (
                    <div
                      data-testid="run-workflow-paused-note"
                      className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-800"
                    >
                      This workflow is paused. Resume it before launching a new editor run.
                    </div>
                  )}
                  {showWorkflowArchivedNote && (
                    <div
                      data-testid="run-workflow-archived-note"
                      className="rounded-lg border border-slate-300 bg-slate-100 px-3 py-2 text-[11px] text-slate-700"
                    >
                      This workflow is archived. Create or restore an active workflow version before running it.
                    </div>
                  )}
                  {showQueueDisabledNote && (
                    <div
                      data-testid="run-queue-capability-note"
                      className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-800"
                    >
                      This deployment has workflow execution disabled. Enable the run queue to start editor runs.
                    </div>
                  )}
                  {showApprovalCapabilityNote && (
                    <div
                      data-testid="run-approval-capability-note"
                      className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-800"
                    >
                      {activeRunApprovalQueueIssue
                        ?? activeRunApprovalRecordsActionIssue
                        ?? approvalCapabilityMessage(activeRunApprovalSubject)}
                    </div>
                  )}
                  {showResumeCapabilityNote && (
                    <div
                      data-testid="run-resume-capability-note"
                      className="rounded-lg border border-sky-200 bg-sky-50 px-3 py-2 text-[11px] text-sky-800"
                    >
                      {resumeCapabilityNote}
                    </div>
                  )}
                </div>
              )}

              {runMonitorManifestNote && (
                <div
                  data-testid="run-monitor-manifest-note"
                  className="mb-3 rounded-lg border border-sky-200 bg-sky-50 px-3 py-2 text-[11px] text-sky-800"
                >
                  {runMonitorManifestNote}
                </div>
              )}
              {runMonitorManifestFallbackNotice && (
                <div
                  data-testid="run-monitor-manifest-fallback-notice"
                  className="mb-3 rounded-lg border border-sky-200 bg-sky-50 px-3 py-2 text-[11px] text-sky-800"
                >
                  {runMonitorManifestFallbackNotice}
                </div>
              )}

              {activeRun && (
                <div data-testid="run-active-summary" className="mb-3 grid grid-cols-3 gap-2 text-[11px] text-zinc-500">
                  <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-2 py-1.5">
                    <div className="text-[10px] uppercase tracking-wider text-zinc-400">Run</div>
                    <div className="truncate font-mono text-zinc-700">{activeRun.workflow_run_id}</div>
                  </div>
                  <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-2 py-1.5">
                    <div className="text-[10px] uppercase tracking-wider text-zinc-400">Current Node</div>
                    <div className="truncate font-mono text-zinc-700">{activeRun.current_node_id ?? "—"}</div>
                  </div>
                  <div
                    className={`rounded-lg border px-2 py-1.5 ${
                      monitoredRunUsesHistoricalVersion
                        ? "border-sky-200 bg-sky-50"
                        : "border-zinc-200 bg-zinc-50"
                    }`}
                  >
                    <div className="text-[10px] uppercase tracking-wider text-zinc-400">Version</div>
                    <div className="truncate font-mono text-zinc-700">
                      {activeRunVersionNumber != null
                        ? `workflow v${activeRunVersionNumber}`
                        : activeRun.workflow_version_id ?? "—"}
                    </div>
                    {activeRunVersionNumber != null && activeRun.workflow_version_id && (
                      <div className="truncate text-[10px] text-zinc-400">
                        {activeRun.workflow_version_id}
                      </div>
                    )}
                  </div>
                </div>
              )}

              {activeRun?.status === "waiting_event" && (
                activeWaitMode === "wait_until" ? (
                  <div
                    data-testid="run-wait-until-config"
                    className="mb-3 rounded-lg border border-sky-200 bg-sky-50/70 p-3"
                  >
                    <div className="text-[11px] font-semibold uppercase tracking-wider text-sky-700">
                      Scheduled Resume
                    </div>
                    <div className="mt-1 text-xs text-sky-800">
                      This run is paused until {activeWaitUntilText}. {activeWaitUntilCapabilityNote}
                    </div>
                  </div>
                ) : (
                  <div
                    data-testid="run-waiting-event-config"
                    className="mb-3 rounded-lg border border-sky-200 bg-sky-50/70 p-3"
                  >
                    <div className="text-[11px] font-semibold uppercase tracking-wider text-sky-700">
                      Resume Event
                    </div>
                    <div className="mt-1 text-xs text-sky-800">
                      Inject the external event payload that should unblock this node and continue the workflow.
                    </div>
                    <label className="mt-3 block text-[11px] font-medium text-sky-900">
                      Event name
                      <input
                        data-testid="run-resume-event-name"
                        type="text"
                        value={resumeEventName}
                        onChange={(event) => setResumeEventName(event.target.value)}
                        className="mt-1 w-full rounded-lg border border-sky-200 bg-white px-3 py-1.5 text-xs text-zinc-800 focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500"
                        placeholder="ticket.approved"
                      />
                    </label>
                    <label className="mt-3 block text-[11px] font-medium text-sky-900">
                      Event payload JSON
                      <textarea
                        data-testid="run-resume-event-payload"
                        value={resumeEventPayload}
                        onChange={(event) => setResumeEventPayload(event.target.value)}
                        rows={5}
                        className="mt-1 w-full rounded-lg border border-sky-200 bg-white px-3 py-2 font-mono text-[11px] text-zinc-700 focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500"
                        placeholder='{"ticket_id":"T-42","approved":true}'
                      />
                    </label>
                    {activeRunResumeByEventIssue && (
                      <div
                        data-testid="run-resume-by-event-capability-note"
                        className="rounded-lg border border-sky-200/70 bg-white/80 px-3 py-2 text-[11px] leading-relaxed text-sky-900"
                      >
                        {activeRunResumeByEventIssue}
                      </div>
                    )}
                    <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-sky-200/70 bg-white/70 px-3 py-2">
                      <div className="text-[11px] leading-relaxed text-sky-900">
                        Match this event against waiting runs in workflow{" "}
                        <span className="font-mono">{workflowId}</span>.
                        {waitingEventRunCount > 0 && (
                          <>
                            {" "}
                            {waitingEventRunCount} waiting event run
                            {waitingEventRunCount === 1 ? "" : "s"} currently visible.
                          </>
                        )}{" "}
                        Include correlation fields in the payload when multiple runs share the same event name.
                      </div>
                      <button
                        type="button"
                        data-testid="run-resume-by-event"
                        disabled={
                          runBusy
                          || !queueRunsEnabled
                          || !checkpointingEnabled
                          || !resumeEventName.trim()
                          || Boolean(activeRunResumeByEventIssue)
                          || Boolean(activeRunResumeEventNameIssue)
                          || Boolean(activeRunMissingResumeCheckpointIssue)
                          || Boolean(activeRunCheckpointIdentityIssue)
                        }
                        onClick={() => submitActiveRunResumeByEvent()}
                        className="rounded-lg border border-sky-200 bg-white px-3 py-1.5 text-xs font-semibold text-sky-700 transition-colors hover:bg-sky-50 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {resumeRunByEventMut.isPending
                          ? "Matching event..."
                          : "Match by event in workflow"}
                      </button>
                    </div>
                  </div>
                )
              )}

              <section data-testid="run-history-list" className="mb-3">
                <div className="mb-1.5 flex items-center justify-between text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                  <span>Recent Runs</span>
                  <button
                    type="button"
                    data-testid="run-history-refresh"
                    disabled={runHistoryRefreshing}
                    onClick={() => {
                      void runHistoryQuery.refetch();
                      if (runHistoryQuery.isError) {
                        void runHistoryFallbackQuery.refetch();
                      }
                    }}
                    className="rounded border border-zinc-200 bg-white px-1.5 py-0.5 text-[10px] font-medium text-zinc-500 transition-colors hover:bg-zinc-50 disabled:opacity-40"
                  >
                    Refresh
                  </button>
                </div>
                {runHistoryQuery.isError ? (
                  <div
                    data-testid="run-history-query-fallback"
                    className="mb-2 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-2 text-[11px] text-amber-800"
                  >
                    Full run-history paging is temporarily unavailable. Showing the recent run index instead.
                  </div>
                ) : null}
                {runHistoryLoading ? (
                  <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-2 text-[11px] text-zinc-400">
                    Loading run history…
                  </div>
                ) : recentWorkflowRuns.length === 0 ? (
                  <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-2 text-[11px] text-zinc-400">
                    {emptyEditorRunHistoryMessage({
                      capabilitiesUnavailable: capabilitiesQuery.isError,
                      queueRunsEnabled,
                      workflowStatus,
                      versionStatus: currentVersionStatus,
                    })}
                  </div>
                ) : (
                  <div className="max-h-36 space-y-1 overflow-auto rounded-lg border border-zinc-200 bg-zinc-50 p-1.5">
                    {recentWorkflowRuns.map((run) => {
                      const selectedRun = run.workflow_run_id === activeRun?.workflow_run_id;
                      return (
                        <div key={run.workflow_run_id} className="flex items-stretch gap-1.5">
                          <button
                            type="button"
                            data-testid={`run-history-select-${run.workflow_run_id}`}
                            onClick={() => {
                              loadMonitoredRun(run, `Loaded run ${run.workflow_run_id}.`);
                            }}
                            className={`min-w-0 flex-1 rounded-lg border px-2 py-1.5 text-left text-[11px] transition-colors ${
                              selectedRun
                                ? "border-zinc-300 bg-white"
                                : "border-transparent bg-zinc-50 hover:border-zinc-200 hover:bg-white"
                            }`}
                          >
                            <div className="flex items-center justify-between gap-2">
                              <span className="truncate font-mono text-zinc-700">{run.workflow_run_id}</span>
                              <div className="flex flex-wrap items-center justify-end gap-1.5">
                                <span className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold ${workflowRunStatusBorderClass(run.status)}`}>
                                  {workflowRunStatusLabel(run.status)}
                                </span>
                                <WorkflowRunArtifactPersistenceBadge
                                  run={run}
                                  compact
                                  dataTestId={`run-history-artifact-persistence-${run.workflow_run_id}`}
                                />
                              </div>
                            </div>
                            <div className="mt-0.5 text-zinc-400">{runTimestamp(run)}</div>
                          </button>
                          <Link
                            to={workflowRunPath(run.workflow_run_id)}
                            target="_blank"
                            rel="noreferrer"
                            data-testid={`run-history-open-link-${run.workflow_run_id}`}
                            className="inline-flex shrink-0 items-center rounded-lg border border-zinc-200 bg-white px-2 py-1 text-[10px] font-medium text-zinc-500 transition-colors hover:bg-zinc-100"
                          >
                            Open
                          </Link>
                        </div>
                      );
                    })}
                  </div>
                )}
                {!runHistoryQuery.isError && runHistoryQuery.hasNextPage ? (
                  <button
                    type="button"
                    data-testid="run-history-load-more"
                    onClick={() => {
                      void runHistoryQuery.fetchNextPage();
                    }}
                    disabled={runHistoryQuery.isFetchingNextPage}
                    className="mt-2 rounded border border-zinc-200 bg-white px-2 py-1 text-[11px] font-medium text-zinc-600 transition-colors hover:bg-zinc-50 disabled:opacity-40"
                  >
                    {runHistoryQuery.isFetchingNextPage ? "Loading more history…" : "Load more history"}
                  </button>
                ) : null}
              </section>

              {canApproveRun && pendingApproval && activeRun && (
                <div
                  data-testid="run-approval-actions"
                  className="mb-3 rounded-lg border border-amber-200 bg-amber-50 p-2"
                >
                  <div className="mb-2 text-[11px] font-medium text-amber-800">
                    Awaiting {activeRunApprovalLabel} for node <span className="font-mono">{pendingApproval.node_id}</span>
                  </div>
                  <div className="mb-2 text-[11px] text-amber-700">
                    Approve to unlock Resume, or Reject to stop this attempt and retry after you adjust the workflow.
                  </div>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      data-testid="run-approve"
                      disabled={runBusy}
                      onClick={() =>
                        approveRunMut.mutate({
                          runId: activeRun.workflow_run_id,
                          runtimeApprovalId: pendingApproval.runtime_approval_id,
                        })
                      }
                      className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-1 text-xs font-medium text-emerald-700 transition-colors hover:bg-emerald-100 disabled:opacity-40 active:scale-[0.97]"
                    >
                      Approve
                    </button>
                    <button
                      type="button"
                      data-testid="run-reject"
                      disabled={!canRejectRun || runBusy}
                      onClick={() =>
                        rejectRunMut.mutate({
                          runId: activeRun.workflow_run_id,
                          runtimeApprovalId: pendingApproval.runtime_approval_id,
                        })
                      }
                      className="rounded-lg border border-red-200 bg-red-50 px-3 py-1 text-xs font-medium text-red-700 transition-colors hover:bg-red-100 disabled:opacity-40 active:scale-[0.97]"
                    >
                      Reject
                    </button>
                  </div>
                </div>
              )}

              <div className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
                <section data-testid="run-output">
                  <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                    Output
                  </div>
                  <div className="max-h-24 overflow-auto rounded-lg border border-zinc-200 bg-zinc-50 p-2 text-[11px] font-mono text-zinc-600">
                    {activeRun?.summary?.output ??
                      activeRun?.error_summary ??
                      runOutputEmptyMessage(activeRun)}
                  </div>
                </section>

                <section data-testid="run-recovery-section">
                  <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                    Recovery Diagnostics
                  </div>
                  {activeRun ? (
                    <WorkflowRunRecoveryPanel
                      run={activeRun}
                      approvals={runApprovals}
                      approvalsLoadError={runApprovalsError}
                      checkpoints={activeRunEffectiveCheckpoints}
                      events={runEvents}
                      eventsLoadError={runEventsError}
                      loading={!runMonitorHydrated}
                    />
                  ) : (
                    <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-2 text-[11px] text-zinc-400">
                      {runMonitorIdleSectionMessage("recovery")}
                    </div>
                  )}
                </section>

                <section data-testid="run-lineage-section">
                  <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                    Retry Lineage
                  </div>
                  {activeRun ? (
                    <WorkflowRunLineagePanel
                      run={activeRun}
                      lineage={activeRunLineageQuery.data ?? null}
                      loading={activeRunLineageQuery.isLoading}
                      loadError={
                        activeRunLineageQuery.isError
                          ? activeRunLineageQuery.error.message
                          : null
                      }
                      runs={[activeRun, ...recentWorkflowRuns]}
                      onSelectRun={(item) => loadMonitoredRun(item, `Loaded run ${item.workflow_run_id}.`)}
                    />
                  ) : (
                    <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-2 text-[11px] text-zinc-400">
                      {runMonitorIdleSectionMessage("lineage")}
                    </div>
                  )}
                </section>

                <section data-testid="run-trace-replay-section">
                  <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                    Trace Replay
                  </div>
                  {activeRun ? (
                    monitoredRunManifest ? (
                      runEventsError ? (
                        <div
                          data-testid="run-trace-replay-events-error"
                          className="rounded-lg border border-red-200 bg-red-50 px-2.5 py-2 text-[11px] leading-relaxed text-red-700"
                        >
                          {runEventsLoadErrorMessage(activeRun.status, runEventsError)}
                        </div>
                      ) : (
                      <TraceReplayGraph
                        manifest={monitoredRunManifest}
                        run={activeRun}
                        events={runEvents}
                        checkpoints={activeRunEffectiveCheckpoints}
                        selectedNodeId={runFocusedNodeId}
                        onSelectNodeId={focusRunNode}
                      />
                      )
                    ) : monitoredRunManifestLoading ? (
                      <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-2 text-[11px] text-zinc-400">
                        Loading the saved workflow version used by this run for trace replay.
                      </div>
                    ) : monitoredRunManifestLoadErrorMessage ? (
                      <div
                        data-testid="run-trace-replay-manifest-error"
                        className="rounded-lg border border-red-200 bg-red-50 px-2.5 py-2 text-[11px] leading-relaxed text-red-700"
                      >
                        {monitoredRunManifestLoadErrorMessage}
                      </div>
                    ) : (
                      <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-2 text-[11px] text-zinc-400">
                        {monitoredRunManifestUnavailableNotice}
                      </div>
                    )
                  ) : (
                    <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-2 text-[11px] text-zinc-400">
                      {runMonitorIdleSectionMessage("trace_replay")}
                    </div>
                  )}
                </section>

                <section data-testid="run-debugger-section">
                  <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                    Execution Debugger
                  </div>
                  {activeRun ? (
                    monitoredRunManifest ? (
                      runEventsError ? (
                        <div
                          data-testid="run-debugger-events-error"
                          className="rounded-lg border border-red-200 bg-red-50 px-2.5 py-2 text-[11px] leading-relaxed text-red-700"
                        >
                          {runEventsLoadErrorMessage(activeRun.status, runEventsError)}
                        </div>
                      ) : (
                      <WorkflowRunDebugger
                        manifest={monitoredRunManifest}
                        run={activeRun}
                        events={runEvents}
                        checkpoints={activeRunEffectiveCheckpoints}
                        focusedNodeId={runFocusedNodeId}
                        onSelectNodeId={focusRunNode}
                      />
                      )
                    ) : monitoredRunManifestLoading ? (
                      <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-2 text-[11px] text-zinc-400">
                        Loading the saved workflow version used by this run for manifest-aware debugging.
                      </div>
                    ) : monitoredRunManifestLoadErrorMessage ? (
                      <div
                        data-testid="run-debugger-manifest-error"
                        className="rounded-lg border border-red-200 bg-red-50 px-2.5 py-2 text-[11px] leading-relaxed text-red-700"
                      >
                        {monitoredRunManifestLoadErrorMessage}
                      </div>
                    ) : (
                      <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-2 text-[11px] text-zinc-400">
                        {monitoredRunManifestUnavailableNotice}
                      </div>
                    )
                  ) : (
                    <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-2 text-[11px] text-zinc-400">
                      {runMonitorIdleSectionMessage("debugger")}
                    </div>
                  )}
                </section>

                <section data-testid="run-files-section">
                  <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                    Files & Artifact Lineage
                  </div>
                  {activeRun ? (
                    <RunFilePanel
                      runId={activeRun.workflow_run_id}
                      runStatus={activeRun.status}
                      runSummary={activeRun.summary}
                      canUpload={false}
                      selectedNodeId={selected}
                      onSelectNodeId={openInspectorForNode}
                      compact
                    />
                  ) : (
                    <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-2 text-[11px] text-zinc-400">
                      {runMonitorIdleSectionMessage("files")}
                    </div>
                  )}
                </section>

                <section data-testid="run-checkpoints-section">
                  <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
                    Resume Checkpoints
                  </div>
                  {!checkpointingEnabled && (
                    <div
                      data-testid="run-checkpoint-capability-note"
                      className="mb-2 rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-[11px] text-zinc-600"
                    >
                      Persisted checkpoints are disabled for this deployment, so only live retry state is available here.
                    </div>
                  )}
                  {activeRun && runCheckpointsError ? (
                    <div
                      data-testid="workflow-run-checkpoints-error"
                      className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-[11px] leading-relaxed text-red-700"
                    >
                      {checkpointLoadErrorMessage(activeRun.status, runCheckpointsError)}
                    </div>
                  ) : activeRun ? (
                    <WorkflowRunCheckpointPanel
                      run={activeRun}
                      checkpoints={runCheckpoints}
                      loading={!runMonitorHydrated}
                      resumeSourceCheckpoint={activeRunResumeSourceCheckpoint}
                      resumeSourceCheckpointLoading={activeRunResumeSourceCheckpointsQuery.isLoading}
                      resumeSourceCheckpointError={
                        activeRunResumeSourceCheckpointsQuery.isError
                          ? activeRunResumeSourceCheckpointsQuery.error.message
                          : null
                      }
                      canRetryFromCheckpoint={canRetryRun}
                      retryingCheckpointId={
                        retryRunMut.isPending
                          ? retryRunMut.variables?.checkpointId ?? null
                          : null
                      }
                      onRetryFromCheckpoint={(checkpointId) =>
                        retryRunMut.mutate({
                          runId: activeRun.workflow_run_id,
                          checkpointId,
                        })}
                    />
                  ) : (
                    <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-2 text-[11px] text-zinc-400">
                      {runMonitorIdleSectionMessage("checkpoints")}
                    </div>
                  )}
                </section>

                <section data-testid="run-session-memory-section">
                  {!activeRun ? (
                    <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-2.5 py-2 text-[11px] text-zinc-400">
                      Run a workflow or load a recent run to inspect shared session memory, node conversation state, and reusable agent context.
                    </div>
                  ) : activeRunSessionMemoryQuery.isError ? (
                    <div
                      data-testid="workflow-session-memory-error"
                      className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-[11px] leading-relaxed text-red-700"
                    >
                      {sessionMemoryLoadErrorMessage(
                        activeRun?.status,
                        activeRunSessionMemoryQuery.error.message,
                      )}
                    </div>
                  ) : (
                    <WorkflowSessionMemoryPanel
                      sessionId={activeRun?.session_id}
                      runStatus={activeRun?.status}
                      entries={activeRunSessionMemoryQuery.data ?? []}
                      loading={activeRunSessionMemoryQuery.isLoading}
                      clearingSession={
                        clearSessionMemoryMut.isPending && !clearSessionMemoryMut.variables?.nodeId
                      }
                      clearingNodeId={clearSessionMemoryMut.variables?.nodeId ?? null}
                      onClearSession={
                        activeRun?.session_id
                          ? () => clearSessionMemoryMut.mutate({ sessionId: activeRun.session_id! })
                          : undefined
                      }
                      onClearNode={
                        activeRun?.session_id
                          ? (nodeId: string) =>
                              clearSessionMemoryMut.mutate({
                                sessionId: activeRun.session_id!,
                                nodeId,
                              })
                          : undefined
                      }
                    />
                  )}
                </section>
              </div>
            </div>
          )}
        </div>

        {/* Inspector panel */}
        <CollapsiblePanel
          id="wf-inspector"
          side="right"
          title="Inspector"
          resizable
          defaultWidth={300}
          minWidth={260}
          maxWidth={560}
          bodyClassName="bg-white p-3"
          expandSignal={inspectorExpandSignal}
        >
          {selectedIds.length > 1 ? (
            <div data-testid="wf-bulk-panel" className="space-y-3">
              <div className="text-sm font-semibold text-zinc-900">
                {selectedIds.length} nodes selected
              </div>
              <p className="text-xs text-zinc-500">
                Bulk actions apply to the whole selection. Shift-click or marquee-drag to
                change it; click a single node to edit it.
              </p>
              <div className="flex flex-col gap-2">
                <button
                  type="button"
                  data-testid="wf-bulk-duplicate"
                  onClick={() => duplicateNodes(selectedIds)}
                  className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50"
                >
                  📋 Duplicate selection
                </button>
                <button
                  type="button"
                  data-testid="wf-bulk-copy"
                  onClick={() => copyNodes(selectedIds)}
                  className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50"
                >
                  ⧉ Copy selection
                </button>
                <button
                  type="button"
                  data-testid="wf-bulk-delete"
                  onClick={() => deleteNodes(selectedIds)}
                  className="rounded-lg border border-red-200 px-3 py-1.5 text-xs font-medium text-red-600 transition-colors hover:bg-red-50"
                >
                  🗑 Delete selection
                </button>
              </div>
            </div>
          ) : (
            <Inspector
              manifest={editorManifest}
              projectId={workflowQuery.data?.project_id ?? null}
              selectedNodeId={selected}
              focusFieldKey={selected ? inspectorFieldTarget : null}
              focusFieldSignal={inspectorFieldFocusSignal}
              tools={tools}
              prompts={promptsQuery.data}
              skills={skillsQuery.data}
              evalDatasets={evalDatasetsQuery.data}
              mcpServers={mcpServersQuery.data}
              componentSpec={
                selected && editorManifest.nodes[selected]
                  ? workflowComponentMap.get(editorManifest.nodes[selected].type) ?? null
                  : null
              }
              validationReport={report}
              lastStep={selected ? stepByNode[selected] ?? null : null}
              onChangeNode={changeNode}
              onChangeWorkflow={changeWorkflow}
              onDeleteNode={deleteNode}
            />
          )}
        </CollapsiblePanel>
      </div>

      <ProblemsPanel
        report={report}
        onFocusIssue={(target) =>
          openInspectorForNode(target.nodeId, { fieldKey: target.fieldKey })
        }
        onFocusNode={openInspectorForNode}
      />

      {showPublish && (
        <PublishDrawer
          versionLabel={`v${versionQuery.data?.version_number ?? ""}`}
          report={report}
          changeSummary={[]}
          publishing={publishMut.isPending}
          onValidate={() => validateMut.mutate(undefined)}
          onPublish={() => {
            publishMut.mutate(undefined, { onSuccess: () => setShowPublish(false) });
          }}
          onClose={() => setShowPublish(false)}
        />
      )}

      {/* Quick-add node picker popup */}
      {quickAdd && (
        <QuickAddMenu
          state={quickAdd}
          items={paletteItems}
          onSelect={handleQuickAddSelect}
          onClose={() => setQuickAdd(null)}
        />
      )}
    </div>
  );
}
