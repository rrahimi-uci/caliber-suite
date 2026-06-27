/**
 * Trace replay view — n8n-inspired execution visualization.
 *
 * Renders a workflow run's path over the graph (highlighting executed nodes),
 * lists per-node steps + tool calls + prompt versions, and offers a
 * "Create Verification Item" button for the CALIBER feedback loop.
 */

import type {
  PreviewStep,
  WorkflowManifest,
  WorkflowRun,
  WorkflowRunCheckpoint,
  WorkflowRunEvent,
} from "@/api/workflowTypes";
import { Canvas } from "@/components/workflows/Canvas";
import {
  ageSeedStrategyLabel,
  describeStepChange,
  extractForEachDiagnostics,
  extractKnowledgeBuildDiagnostics,
  extractKnowledgeQueryDiagnostics,
  extractSubworkflowDiagnostics,
  extractToolNodeDiagnostics,
  knowledgeBuildActivationStatusLabel,
  knowledgeBuildStatusLabel,
  knowledgeBuildWaitStatusLabel,
  knowledgeGraphTargetLabel,
  retrievalModeLabel,
  stepStatusStyle,
  subworkflowStatusLabel,
  subworkflowStatusTone,
  toolArgumentSummary,
  toolBindingTargetLabel,
  toolBindingTypeLabel,
  toolSideEffectLabel,
  workflowNodeTypeLabel,
} from "@/components/workflows/StepPreview";
import {
  approvalCheckpointKind,
  workflowRunCheckpointMarkerLabel,
  workflowRunLifecycleDetail,
  workflowRunLifecycleLabel,
} from "@/lib/workflowRunLabels";
import { buildNodeExecutionBadgeMap, nodeLabel } from "@/lib/workflowGraph";

interface TraceReplayGraphProps {
  manifest: WorkflowManifest;
  run: WorkflowRun;
  events?: WorkflowRunEvent[];
  checkpoints?: WorkflowRunCheckpoint[];
  selectedNodeId?: string | null;
  onSelectNodeId?: (nodeId: string | null) => void;
  onCreateVerification?: (run: WorkflowRun) => void;
}

interface TracePathEntry {
  nodeId: string;
  step: PreviewStep | null;
  stepIndex: number | null;
}

interface IndexedTraceStep {
  step: PreviewStep;
  stepIndex: number;
}

interface TraceEventStep {
  nodeId: string;
  stepIndex: number;
  sequence: number;
}

interface TraceRecoveryMarker {
  label: string;
  tone: string;
}

function buildTracePathEntries(run: WorkflowRun): TracePathEntry[] {
  const path = Array.isArray(run.summary?.node_path)
    ? run.summary.node_path
    : [];
  const steps: IndexedTraceStep[] = (
    Array.isArray(run.summary?.steps) ? run.summary.steps : []
  ).map((step, stepIndex) => ({
    step,
    stepIndex,
  }));
  if (path.length === 0) {
    return steps.map(({ step, stepIndex }) => ({
      nodeId: step.node_id,
      step,
      stepIndex,
    }));
  }

  const stepQueue = new Map<string, IndexedTraceStep[]>();
  for (const step of steps) {
    const bucket = stepQueue.get(step.step.node_id) ?? [];
    bucket.push(step);
    stepQueue.set(step.step.node_id, bucket);
  }

  return path.map((nodeId) => {
    const queue = stepQueue.get(nodeId) ?? [];
    const [entry, ...rest] = queue;
    stepQueue.set(nodeId, rest);
    return {
      nodeId,
      step: entry?.step ?? null,
      stepIndex: entry?.stepIndex ?? null,
    };
  });
}

function normalizeTraceStatus(
  step: PreviewStep | null,
  options: {
    runStatus: string;
    index: number;
    total: number;
  },
): string {
  const { runStatus, index, total } = options;
  if (step?.status) return step.status;
  if (runStatus === "completed") return "ok";
  if (runStatus === "blocked" && index === total - 1) return "blocked";
  if ((runStatus === "error" || runStatus === "failed") && index === total - 1)
    return "error";
  return "ok";
}

function formatTraceDuration(durationMs: number | undefined): string | null {
  if (
    typeof durationMs !== "number" ||
    !Number.isFinite(durationMs) ||
    durationMs < 0
  ) {
    return null;
  }
  if (durationMs >= 1000) {
    return `${(durationMs / 1000).toFixed(durationMs >= 10_000 ? 0 : 1)} s`;
  }
  return `${Math.round(durationMs)} ms`;
}

function compactNodeType(nodeType: string): string {
  return workflowNodeTypeLabel(nodeType) ?? nodeType.replace(/_/g, " ");
}

const TRACE_PRE_STEP_EVENT_TYPES = new Set([
  "workflow.run.queued",
  "workflow.run.started",
]);

const TRACE_WINDOW_EVENT_TYPES = new Set([
  "workflow.run.recovered",
  "workflow.run.approval.approved",
  "workflow.run.approval.rejected",
  "workflow.run.waiting_approval",
  "workflow.run.waiting_event",
  "workflow.run.resumed",
  "workflow.run.retried",
  "workflow.run.cancel_requested",
  "workflow.run.cancelled",
  "workflow.run.completed",
  "workflow.run.expired",
  "workflow.run.failed",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function checkpointState(
  checkpoint: WorkflowRunCheckpoint | null,
): Record<string, unknown> | null {
  return isRecord(checkpoint?.state_blob) ? checkpoint.state_blob : null;
}

function recoveryMarkerTone(
  kind: "info" | "warn" | "success" | "neutral" | "danger",
): string {
  switch (kind) {
    case "warn":
      return "border-amber-200 bg-amber-50 text-amber-700";
    case "success":
      return "border-emerald-200 bg-emerald-50 text-emerald-700";
    case "info":
      return "border-sky-200 bg-sky-50 text-sky-700";
    case "danger":
      return "border-red-200 bg-red-50 text-red-700";
    default:
      return "border-zinc-200 bg-white text-zinc-600";
  }
}

function appendRecoveryMarker(
  markers: TraceRecoveryMarker[],
  seen: Set<string>,
  label: string,
  tone: string,
): void {
  if (!label || seen.has(label)) return;
  markers.push({ label, tone });
  seen.add(label);
}

function appendRecoveryDetail(details: string[], value: string | null): void {
  if (!value || details.includes(value)) return;
  details.push(value);
}

function buildTraceEventSteps(events: WorkflowRunEvent[]): TraceEventStep[] {
  const derived: TraceEventStep[] = [];
  let stepIndex = 0;
  for (const event of events) {
    if (event.event_type !== "workflow.run.step") continue;
    const payload = isRecord(event.payload) ? event.payload.step : null;
    if (!isRecord(payload)) continue;
    const nodeId = readString(payload.node_id);
    if (!nodeId) continue;
    derived.push({
      nodeId,
      stepIndex,
      sequence: event.sequence,
    });
    stepIndex += 1;
  }
  return derived;
}

function initialLifecycleEvents({
  pathIndex,
  eventSteps,
  events,
}: {
  pathIndex: number;
  eventSteps: TraceEventStep[];
  events: WorkflowRunEvent[];
}): WorkflowRunEvent[] {
  if (pathIndex !== 0) return [];
  const firstStepSequence = eventSteps[0]?.sequence ?? null;
  return events.filter(
    (event) =>
      TRACE_PRE_STEP_EVENT_TYPES.has(event.event_type) &&
      (firstStepSequence === null || event.sequence < firstStepSequence),
  );
}

function stepRecoveryEvents({
  stepIndex,
  nodeId,
  eventSteps,
  events,
}: {
  stepIndex: number | null;
  nodeId: string;
  eventSteps: TraceEventStep[];
  events: WorkflowRunEvent[];
}): WorkflowRunEvent[] {
  if (stepIndex === null) {
    return events.filter(
      (event) =>
        TRACE_WINDOW_EVENT_TYPES.has(event.event_type) &&
        (event.node_id === nodeId || event.node_id === null),
    );
  }

  const currentSequence =
    eventSteps.find((entry) => entry.stepIndex === stepIndex)?.sequence ?? null;
  const nextSequence =
    eventSteps.find((entry) => entry.stepIndex === stepIndex + 1)?.sequence ??
    null;
  if (currentSequence === null) {
    return events.filter(
      (event) =>
        TRACE_WINDOW_EVENT_TYPES.has(event.event_type) &&
        (event.node_id === nodeId || event.node_id === null),
    );
  }

  return events.filter((event) => {
    if (!TRACE_WINDOW_EVENT_TYPES.has(event.event_type)) return false;
    if (event.sequence <= currentSequence) return false;
    if (nextSequence !== null && event.sequence >= nextSequence) return false;
    return event.node_id === nodeId || event.node_id === null;
  });
}

function checkpointResumeDetail(
  checkpoint: WorkflowRunCheckpoint | null,
): string | null {
  const state = checkpointState(checkpoint);
  const waitUntil =
    readString(state?.wait_until) ?? readString(state?.resume_at);
  if (!waitUntil) return null;
  const timezoneName = readString(state?.timezone);
  return timezoneName ? `${waitUntil} (${timezoneName})` : waitUntil;
}

function buildTraceRecoveryContext({
  nodeId,
  stepIndex,
  pathIndex,
  totalEntries,
  run,
  events,
  eventSteps,
  orderedCheckpoints,
  activeCheckpoint,
}: {
  nodeId: string;
  stepIndex: number | null;
  pathIndex: number;
  totalEntries: number;
  run: WorkflowRun;
  events: WorkflowRunEvent[];
  eventSteps: TraceEventStep[];
  orderedCheckpoints: WorkflowRunCheckpoint[];
  activeCheckpoint: WorkflowRunCheckpoint | null;
}): {
  markers: TraceRecoveryMarker[];
  details: string[];
} {
  const checkpointForNode =
    orderedCheckpoints.find((checkpoint) => checkpoint.node_id === nodeId) ??
    null;
  const checkpointForNodeState = checkpointState(checkpointForNode);
  const checkpointKind = readString(checkpointForNodeState?.kind);
  const inheritedCheckpoint =
    checkpointForNode?.workflow_run_id &&
    checkpointForNode.workflow_run_id !== run.workflow_run_id;
  const initialEvents = initialLifecycleEvents({
    pathIndex,
    eventSteps,
    events,
  });
  const recoveryEvents = stepRecoveryEvents({
    stepIndex,
    nodeId,
    eventSteps,
    events,
  });
  const markers: TraceRecoveryMarker[] = [];
  const seenMarkers = new Set<string>();
  const details: string[] = [];

  if (checkpointForNodeState) {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      workflowRunCheckpointMarkerLabel(checkpointKind),
      recoveryMarkerTone("neutral"),
    );
    if (inheritedCheckpoint) {
      appendRecoveryMarker(
        markers,
        seenMarkers,
        "Inherited checkpoint",
        recoveryMarkerTone("info"),
      );
    }
    appendRecoveryDetail(
      details,
      inheritedCheckpoint
        ? `Checkpoint #${checkpointForNode?.sequence ?? "?"} from ${checkpointForNode?.workflow_run_id}`
        : `Checkpoint #${checkpointForNode?.sequence ?? "?"}`,
    );
  }

  if (
    initialEvents.some((event) => event.event_type === "workflow.run.queued") ||
    (pathIndex === 0 && run.queued_at)
  ) {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      "Queued",
      recoveryMarkerTone("neutral"),
    );
  }

  if (
    initialEvents.some(
      (event) => event.event_type === "workflow.run.started",
    ) ||
    (pathIndex === 0 && run.started_at)
  ) {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      workflowRunLifecycleLabel("workflow.run.started", { mode: "marker" }),
      recoveryMarkerTone("info"),
    );
  }

  const expectedEventName = readString(
    checkpointForNodeState?.expected_event_name,
  );
  if (expectedEventName) {
    appendRecoveryDetail(details, `Waiting for ${expectedEventName}`);
  }

  const resumeDetail = checkpointResumeDetail(checkpointForNode);
  if (resumeDetail) {
    appendRecoveryDetail(details, `Resume ${resumeDetail}`);
  }

  if (
    activeCheckpoint &&
    checkpointForNode &&
    activeCheckpoint.checkpoint_id === checkpointForNode.checkpoint_id &&
    run.current_node_id === nodeId &&
    ["waiting_event", "waiting_approval"].includes(run.status)
  ) {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      "Resume target",
      recoveryMarkerTone("info"),
    );
  }

  if (run.current_node_id === nodeId && run.status === "waiting_approval") {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      workflowRunLifecycleLabel("workflow.run.waiting_approval", {
        approvalKind: approvalCheckpointKind(checkpointKind),
        mode: "marker",
      }),
      recoveryMarkerTone("warn"),
    );
  } else if (run.current_node_id === nodeId && run.status === "waiting_event") {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      workflowRunLifecycleLabel("workflow.run.waiting_event", {
        mode: "marker",
      }),
      recoveryMarkerTone("info"),
    );
  }

  if (
    recoveryEvents.some((event) => event.event_type === "workflow.run.recovered")
  ) {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      workflowRunLifecycleLabel("workflow.run.recovered", {
        mode: "marker",
      }),
      recoveryMarkerTone("warn"),
    );
  }
  if (
    recoveryEvents.some(
      (event) => event.event_type === "workflow.run.approval.approved",
    )
  ) {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      workflowRunLifecycleLabel("workflow.run.approval.approved", {
        mode: "marker",
      }),
      recoveryMarkerTone("success"),
    );
  }
  if (
    recoveryEvents.some(
      (event) => event.event_type === "workflow.run.approval.rejected",
    )
  ) {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      workflowRunLifecycleLabel("workflow.run.approval.rejected", {
        mode: "marker",
      }),
      recoveryMarkerTone("danger"),
    );
  }
  if (
    recoveryEvents.some(
      (event) => event.event_type === "workflow.run.waiting_approval",
    )
  ) {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      workflowRunLifecycleLabel("workflow.run.waiting_approval", {
        approvalKind: approvalCheckpointKind(checkpointKind),
        mode: "marker",
      }),
      recoveryMarkerTone("warn"),
    );
  } else if (
    recoveryEvents.some(
      (event) => event.event_type === "workflow.run.waiting_event",
    )
  ) {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      workflowRunLifecycleLabel("workflow.run.waiting_event", {
        mode: "marker",
      }),
      recoveryMarkerTone("info"),
    );
  }

  if (
    recoveryEvents.some((event) => event.event_type === "workflow.run.resumed")
  ) {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      workflowRunLifecycleLabel("workflow.run.resumed", { mode: "marker" }),
      recoveryMarkerTone("success"),
    );
  }

  if (
    recoveryEvents.some(
      (event) => event.event_type === "workflow.run.cancel_requested",
    ) ||
    (pathIndex === totalEntries - 1 && Boolean(run.cancel_requested_at))
  ) {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      workflowRunLifecycleLabel("workflow.run.cancel_requested", {
        mode: "marker",
      }),
      recoveryMarkerTone("warn"),
    );
  }

  if (
    recoveryEvents.some(
      (event) => event.event_type === "workflow.run.cancelled",
    ) ||
    (pathIndex === totalEntries - 1 && run.status === "cancelled")
  ) {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      workflowRunLifecycleLabel("workflow.run.cancelled", { mode: "marker" }),
      recoveryMarkerTone("warn"),
    );
  }

  if (
    recoveryEvents.some(
      (event) => event.event_type === "workflow.run.completed",
    ) ||
    (pathIndex === totalEntries - 1 &&
      run.status === "completed" &&
      Boolean(run.completed_at))
  ) {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      workflowRunLifecycleLabel("workflow.run.completed", { mode: "marker" }),
      recoveryMarkerTone("success"),
    );
  }
  if (
    recoveryEvents.some(
      (event) => event.event_type === "workflow.run.expired",
    ) ||
    (pathIndex === totalEntries - 1 &&
      run.status === "expired" &&
      Boolean(run.completed_at || run.lease_expires_at))
  ) {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      workflowRunLifecycleLabel("workflow.run.expired", { mode: "marker" }),
      recoveryMarkerTone("danger"),
    );
  }

  if (
    recoveryEvents.some(
      (event) => event.event_type === "workflow.run.failed",
    ) ||
    (pathIndex === totalEntries - 1 &&
      run.status === "failed" &&
      Boolean(run.completed_at))
  ) {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      workflowRunLifecycleLabel("workflow.run.failed", { mode: "marker" }),
      recoveryMarkerTone("danger"),
    );
  }

  if (
    recoveryEvents.some((event) => event.event_type === "workflow.run.retried")
  ) {
    appendRecoveryMarker(
      markers,
      seenMarkers,
      workflowRunLifecycleLabel("workflow.run.retried", { mode: "marker" }),
      recoveryMarkerTone("info"),
    );
  }

  const resumedEvent =
    recoveryEvents.find(
      (event) => event.event_type === "workflow.run.resumed",
    ) ?? null;
  const approvalApprovedEvent =
    recoveryEvents.find(
      (event) => event.event_type === "workflow.run.approval.approved",
    ) ?? null;
  const recoveredEvent =
    recoveryEvents.find(
      (event) => event.event_type === "workflow.run.recovered",
    ) ?? null;
  const recoveredDetail = workflowRunLifecycleDetail(
    "workflow.run.recovered",
    recoveredEvent?.payload ?? null,
  );
  if (recoveredDetail) appendRecoveryDetail(details, recoveredDetail);

  const approvalApprovedDetail = workflowRunLifecycleDetail(
    "workflow.run.approval.approved",
    approvalApprovedEvent?.payload ?? null,
  );
  if (approvalApprovedDetail) appendRecoveryDetail(details, approvalApprovedDetail);

  const approvalRejectedEvent =
    recoveryEvents.find(
      (event) => event.event_type === "workflow.run.approval.rejected",
    ) ?? null;
  const approvalRejectedDetail = workflowRunLifecycleDetail(
    "workflow.run.approval.rejected",
    approvalRejectedEvent?.payload ?? null,
  );
  if (approvalRejectedDetail) appendRecoveryDetail(details, approvalRejectedDetail);

  const resumedDetail = workflowRunLifecycleDetail(
    "workflow.run.resumed",
    resumedEvent?.payload ?? null,
  );
  if (resumedDetail) appendRecoveryDetail(details, resumedDetail);

  const retriedEvent =
    recoveryEvents.find(
      (event) => event.event_type === "workflow.run.retried",
    ) ?? null;
  const retriedDetail = workflowRunLifecycleDetail(
    "workflow.run.retried",
    retriedEvent?.payload ?? null,
  );
  if (retriedDetail) appendRecoveryDetail(details, retriedDetail);

  const cancelRequestedEvent =
    recoveryEvents.find(
      (event) => event.event_type === "workflow.run.cancel_requested",
    ) ?? null;
  const cancelRequestedDetail = workflowRunLifecycleDetail(
    "workflow.run.cancel_requested",
    cancelRequestedEvent?.payload ?? null,
    { cancelReason: run.cancel_reason ?? null },
  );
  if (cancelRequestedDetail) appendRecoveryDetail(details, cancelRequestedDetail);

  const cancelledEvent =
    recoveryEvents.find(
      (event) => event.event_type === "workflow.run.cancelled",
    ) ?? null;
  const cancelledDetail = workflowRunLifecycleDetail(
    "workflow.run.cancelled",
    cancelledEvent?.payload ?? null,
    { cancelReason: run.cancel_reason ?? null },
  );
  if (cancelledDetail) appendRecoveryDetail(details, cancelledDetail);

  const failedEvent =
    recoveryEvents.find(
      (event) => event.event_type === "workflow.run.failed",
    ) ?? null;
  const failedDetail = workflowRunLifecycleDetail(
    "workflow.run.failed",
    failedEvent?.payload ?? null,
    { failureDetail: run.error_summary ?? null },
  );
  if (failedDetail) appendRecoveryDetail(details, failedDetail);

  const expiredEvent =
    recoveryEvents.find(
      (event) => event.event_type === "workflow.run.expired",
    ) ?? null;
  const expiredDetail = workflowRunLifecycleDetail(
    "workflow.run.expired",
    expiredEvent?.payload ?? null,
    { failureDetail: run.error_summary ?? null },
  );
  if (expiredDetail) appendRecoveryDetail(details, expiredDetail);

  return { markers, details };
}

function TraceRecoveryBadges({
  markers,
}: {
  markers: TraceRecoveryMarker[];
}): JSX.Element | null {
  if (markers.length === 0) return null;
  return (
    <>
      {markers.map((marker) => (
        <span
          key={`${marker.label}-${marker.tone}`}
          className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] ${marker.tone}`}
        >
          {marker.label}
        </span>
      ))}
    </>
  );
}

function TraceRecoveryMeta({
  details,
}: {
  details: string[];
}): JSX.Element | null {
  if (details.length === 0) return null;
  return (
    <div className="mt-2 text-[11px] leading-relaxed text-zinc-500">
      {details.join(" · ")}
    </div>
  );
}

function toolBindingTone(bindingType: string | null): string {
  if (bindingType === "mcp_tool") {
    return "border-amber-200 bg-amber-50 text-amber-700";
  }
  return "border-violet-200 bg-violet-50 text-violet-700";
}

function TraceToolBadges({
  localName,
  bindingType,
  registryRef,
  requiresApproval,
  callCount,
}: {
  localName: string | null;
  bindingType: string | null;
  registryRef: string | null;
  requiresApproval: boolean;
  callCount: number;
}): JSX.Element {
  const bindingLabel = toolBindingTypeLabel(bindingType);

  return (
    <>
      {localName && (
        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
          {localName}
        </span>
      )}
      {bindingLabel && (
        <span
          className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${toolBindingTone(bindingType)}`}
        >
          {bindingLabel}
        </span>
      )}
      {registryRef && (
        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 font-mono text-[10px] font-semibold text-zinc-600">
          {registryRef}
        </span>
      )}
      {callCount > 1 && (
        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
          {callCount} calls
        </span>
      )}
      {requiresApproval && (
        <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">
          Approval required
        </span>
      )}
    </>
  );
}

function TraceToolMeta({
  bindingTarget,
  argumentSummary,
  sideEffectLabel,
}: {
  bindingTarget: string | null;
  argumentSummary: string | null;
  sideEffectLabel: string | null;
}): JSX.Element | null {
  if (!bindingTarget && !argumentSummary && !sideEffectLabel) {
    return null;
  }
  return (
    <div className="mt-2 text-[11px] leading-relaxed text-zinc-500">
      {bindingTarget && <span>Binding {bindingTarget}</span>}
      {bindingTarget && (argumentSummary || sideEffectLabel) && " · "}
      {argumentSummary && <span>{argumentSummary}</span>}
      {argumentSummary && sideEffectLabel && " · "}
      {sideEffectLabel && <span>Effect {sideEffectLabel}</span>}
    </div>
  );
}

function traceOrchestrationTargetLabel(
  nodeId: string | null,
  nodeType: string | null,
): string | null {
  const parts = [nodeId, workflowNodeTypeLabel(nodeType)].filter(Boolean);
  return parts.length > 0 ? parts.join(" · ") : null;
}

function TraceForEachBadges({
  forEachNode,
}: {
  forEachNode: NonNullable<ReturnType<typeof extractForEachDiagnostics>>;
}): JSX.Element {
  const targetLabel = traceOrchestrationTargetLabel(
    forEachNode.targetNodeId,
    forEachNode.targetNodeType,
  );
  return (
    <>
      <span className="rounded-full border border-teal-200 bg-teal-50 px-2 py-0.5 text-[10px] font-semibold text-teal-700">
        {forEachNode.count} item{forEachNode.count === 1 ? "" : "s"}
      </span>
      {targetLabel && (
        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
          Target {targetLabel}
        </span>
      )}
      {forEachNode.failed > 0 && (
        <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">
          {forEachNode.failed} failed
        </span>
      )}
      {forEachNode.artifactCount > 0 && (
        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
          {forEachNode.artifactCount} artifact
          {forEachNode.artifactCount === 1 ? "" : "s"}
        </span>
      )}
    </>
  );
}

function TraceForEachMeta({
  forEachNode,
}: {
  forEachNode: NonNullable<ReturnType<typeof extractForEachDiagnostics>>;
}): JSX.Element | null {
  const preview = forEachNode.results.map((item) => item.itemLabel).join(", ");
  const failedItem = forEachNode.results.find((item) => item.error) ?? null;
  if (!preview && !failedItem) return null;
  return (
    <div className="mt-2 text-[11px] leading-relaxed text-zinc-500">
      {preview && <span>Preview {preview}</span>}
      {preview && failedItem && " · "}
      {failedItem && (
        <span>
          Failure {failedItem.itemLabel}: {failedItem.error}
        </span>
      )}
    </div>
  );
}

function TraceSubworkflowBadges({
  subworkflow,
}: {
  subworkflow: NonNullable<ReturnType<typeof extractSubworkflowDiagnostics>>;
}): JSX.Element {
  const childStatus = subworkflowStatusLabel(subworkflow.childStatus);
  return (
    <>
      {subworkflow.workflowId && (
        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
          {subworkflow.workflowId}
        </span>
      )}
      {subworkflow.alias && (
        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
          Alias {subworkflow.alias}
        </span>
      )}
      {childStatus && (
        <span
          className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${subworkflowStatusTone(subworkflow.childStatus)}`}
        >
          {childStatus}
        </span>
      )}
      {subworkflow.stepCount > 0 && (
        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
          {subworkflow.stepCount} child step
          {subworkflow.stepCount === 1 ? "" : "s"}
        </span>
      )}
      {subworkflow.tokens !== null && (
        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
          {subworkflow.tokens} tokens
        </span>
      )}
    </>
  );
}

function TraceSubworkflowMeta({
  subworkflow,
}: {
  subworkflow: NonNullable<ReturnType<typeof extractSubworkflowDiagnostics>>;
}): JSX.Element | null {
  const path =
    subworkflow.steps.length > 0
      ? `Path ${subworkflow.steps.join(" -> ")}`
      : null;
  const version = subworkflow.workflowVersionId
    ? `Version ${subworkflow.workflowVersionId}${subworkflow.workflowVersionNumber !== null ? ` · v${subworkflow.workflowVersionNumber}` : ""}`
    : subworkflow.workflowVersionNumber !== null
      ? `Version v${subworkflow.workflowVersionNumber}`
      : null;
  const output = subworkflow.outputPreview
    ? `Output ${subworkflow.outputPreview}`
    : null;
  const error = subworkflow.error ? `Failure ${subworkflow.error}` : null;
  const parts = [path, version, output, error].filter(Boolean);
  if (parts.length === 0) return null;
  return (
    <div className="mt-2 text-[11px] leading-relaxed text-zinc-500">
      {parts.join(" · ")}
    </div>
  );
}

function traceKnowledgeBuildTone(
  status: string | null,
  previewSkipped: boolean,
): string {
  if (previewSkipped || status === "preview_skipped") {
    return "border-zinc-200 bg-white text-zinc-600";
  }
  if (status === "completed") {
    return "border-emerald-200 bg-emerald-50 text-emerald-700";
  }
  if (status === "queued" || status === "processing") {
    return "border-sky-200 bg-sky-50 text-sky-700";
  }
  if (status === "failed") {
    return "border-rose-200 bg-rose-50 text-rose-700";
  }
  return "border-zinc-200 bg-white text-zinc-600";
}

function TraceKnowledgeBuildBadges({
  knowledgeBuild,
}: {
  knowledgeBuild: NonNullable<
    ReturnType<typeof extractKnowledgeBuildDiagnostics>
  >;
}): JSX.Element {
  const buildStatus = knowledgeBuildStatusLabel(
    knowledgeBuild.status,
    knowledgeBuild.previewSkipped,
  );
  const waitLabel = knowledgeBuildWaitStatusLabel(
    knowledgeBuild.waitStatus,
    knowledgeBuild.waitRequested,
  );
  const activationLabel = knowledgeBuildActivationStatusLabel(
    knowledgeBuild.activationStatus,
    knowledgeBuild.activationActiveVersionId,
    knowledgeBuild.activationRequested,
  );
  const graphTarget = knowledgeGraphTargetLabel(knowledgeBuild.graphTarget);
  const retrievalMode = retrievalModeLabel(
    knowledgeBuild.defaultRetrievalMode,
  );

  return (
    <>
      {buildStatus && (
        <span
          className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${traceKnowledgeBuildTone(
            knowledgeBuild.status,
            knowledgeBuild.previewSkipped,
          )}`}
        >
          {buildStatus}
        </span>
      )}
      {knowledgeBuild.knowledgeBaseId && (
        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
          KB {knowledgeBuild.knowledgeBaseId}
        </span>
      )}
      {knowledgeBuild.versionId ? (
        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 font-mono text-[10px] font-semibold text-zinc-600">
          {knowledgeBuild.versionId}
        </span>
      ) : knowledgeBuild.versionNumber !== null ? (
        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
          v{knowledgeBuild.versionNumber}
        </span>
      ) : null}
      {knowledgeBuild.runId && (
        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 font-mono text-[10px] font-semibold text-zinc-600">
          {knowledgeBuild.runId}
        </span>
      )}
      {graphTarget && (
        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
          {graphTarget}
        </span>
      )}
      {retrievalMode && (
        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
          {retrievalMode}
        </span>
      )}
      {waitLabel && (
        <span className="rounded-full border border-sky-200 bg-sky-50 px-2 py-0.5 text-[10px] font-semibold text-sky-700">
          {waitLabel}
        </span>
      )}
      {activationLabel && (
        <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10px] font-semibold text-emerald-700">
          {activationLabel}
        </span>
      )}
      {knowledgeBuild.ageSyncStatus && (
        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold uppercase text-zinc-600">
          AGE {knowledgeBuild.ageSyncStatus}
        </span>
      )}
    </>
  );
}

function TraceKnowledgeBuildMeta({
  knowledgeBuild,
}: {
  knowledgeBuild: NonNullable<
    ReturnType<typeof extractKnowledgeBuildDiagnostics>
  >;
}): JSX.Element | null {
  const buildProfile =
    knowledgeBuild.chunkingStrategy || knowledgeBuild.embeddingModel
      ? [
          knowledgeBuild.chunkingStrategy,
          knowledgeBuild.embeddingModel,
        ]
          .filter(Boolean)
          .join(" · ")
      : null;
  const graphProfile =
    knowledgeBuild.graphExtractor ||
    knowledgeBuild.graphTarget ||
    knowledgeBuild.defaultRetrievalMode ||
    knowledgeBuild.retrievalStrength
      ? [
          knowledgeBuild.graphExtractor,
          knowledgeGraphTargetLabel(knowledgeBuild.graphTarget),
          retrievalModeLabel(knowledgeBuild.defaultRetrievalMode),
          knowledgeBuild.retrievalStrength,
        ]
          .filter(Boolean)
          .join(" · ")
      : null;
  const waitPolicy =
    knowledgeBuild.waitRequested || knowledgeBuild.waitTimeoutSeconds !== null
      ? [
          knowledgeBuildWaitStatusLabel(
            knowledgeBuild.waitStatus,
            knowledgeBuild.waitRequested,
          ),
          knowledgeBuild.waitTimeoutSeconds !== null
            ? `${knowledgeBuild.waitTimeoutSeconds}s timeout`
            : null,
        ]
          .filter(Boolean)
          .join(" · ")
      : null;
  const activation =
    knowledgeBuild.activationActiveVersionId || knowledgeBuild.activeVersionId
      ? `Active ${
          knowledgeBuild.activationActiveVersionId ??
          knowledgeBuild.activeVersionId
        }`
      : null;
  const parts = [
    buildProfile ? `Build ${buildProfile}` : null,
    graphProfile ? `Graph ${graphProfile}` : null,
    waitPolicy ? `Wait ${waitPolicy}` : null,
    activation,
  ].filter(Boolean);
  if (parts.length === 0) return null;
  return (
    <div className="mt-2 text-[11px] leading-relaxed text-zinc-500">
      {parts.join(" · ")}
    </div>
  );
}

export function TraceReplayGraph({
  manifest,
  run,
  events = [],
  checkpoints = [],
  selectedNodeId = null,
  onSelectNodeId,
  onCreateVerification,
}: TraceReplayGraphProps): JSX.Element {
  const path = Array.isArray(run.summary?.node_path)
    ? run.summary.node_path
    : [];
  const tags = run.summary?.tags ?? {};
  const promptVersion = tags["caliber.prompt_version"];
  const pathEntries = buildTracePathEntries(run);
  const eventSteps = buildTraceEventSteps(events);
  const orderedCheckpoints = [...checkpoints].sort(
    (left, right) => right.sequence - left.sequence,
  );
  const activeCheckpointId = readString(run.summary?.resume_checkpoint_id);
  const activeCheckpoint = activeCheckpointId
    ? (orderedCheckpoints.find(
        (checkpoint) => checkpoint.checkpoint_id === activeCheckpointId,
      ) ?? null)
    : null;
  const executionPath =
    path.length > 0 ? path : pathEntries.map((entry) => entry.nodeId);
  const executionByNode = buildNodeExecutionBadgeMap({
    runSteps: Array.isArray(run.summary?.steps) ? run.summary.steps : [],
    runStatus: run.status,
    currentNodeId: run.current_node_id ?? null,
  });

  return (
    <div data-testid="trace-replay" className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-sm">
          <span className="font-medium text-zinc-900">Trace replay</span>{" "}
          <span className="text-xs text-zinc-400">
            {run.trace_id ?? run.workflow_run_id} · {run.status}
          </span>
        </div>
        {onCreateVerification && (
          <button
            type="button"
            data-testid="trace-create-verification"
            onClick={() => onCreateVerification(run)}
            className="rounded-lg border border-zinc-200 px-2.5 py-1 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 active:scale-[0.97]"
          >
            Create Verification Item
          </button>
        )}
      </div>

      <div className="h-72 rounded-lg border border-zinc-200">
        <Canvas
          manifest={manifest}
          executionPath={executionPath}
          nodeExecutionByNode={executionByNode}
          selectedNodeId={selectedNodeId}
          onSelectNode={onSelectNodeId}
        />
      </div>

      <div data-testid="trace-steps" className="space-y-1 text-xs">
        <div className="text-[11px] font-semibold uppercase tracking-wider text-zinc-400">
          Execution path
        </div>
        {pathEntries.length === 0 ? (
          <div className="text-zinc-400">No recorded path.</div>
        ) : (
          pathEntries.map(({ nodeId, step, stepIndex }, index) => {
            const node = manifest.nodes[nodeId];
            const displayLabel = node ? nodeLabel(node) : nodeId;
            const nodeType = step?.node_type ?? node?.type ?? "node";
            const status = normalizeTraceStatus(step, {
              runStatus: run.status,
              index,
              total: pathEntries.length,
            });
            const detail = step
              ? describeStepChange(step)
              : "Executed during this run.";
            const durationLabel = formatTraceDuration(step?.duration_ms);
            const toolCallCount = step?.tool_calls?.length ?? 0;
            const knowledgeBuild = step
              ? extractKnowledgeBuildDiagnostics(step)
              : null;
            const knowledgeQuery = step
              ? extractKnowledgeQueryDiagnostics(step)
              : null;
            const toolNode = step ? extractToolNodeDiagnostics(step) : null;
            const knowledgeModeLabel = retrievalModeLabel(
              knowledgeQuery?.retrievalMode ?? null,
            );
            const seedStrategyLabel = ageSeedStrategyLabel(
              knowledgeQuery?.ageSeedStrategy ?? null,
            );
            const toolBindingTarget = toolBindingTargetLabel(toolNode);
            const toolEffectLabel = toolSideEffectLabel(
              toolNode?.sideEffectLevel ?? null,
            );
            const toolArguments = toolNode
              ? toolArgumentSummary(toolNode.argumentKeys)
              : null;
            const forEachNode = step ? extractForEachDiagnostics(step) : null;
            const subworkflowNode = step
              ? extractSubworkflowDiagnostics(step)
              : null;
            const recoveryContext = buildTraceRecoveryContext({
              nodeId,
              stepIndex,
              pathIndex: index,
              totalEntries: pathEntries.length,
              run,
              events,
              eventSteps,
              orderedCheckpoints,
              activeCheckpoint,
            });

            return (
              <button
                key={`${nodeId}-${index}`}
                type="button"
                data-testid={`trace-path-step-${index}`}
                onClick={() => onSelectNodeId?.(nodeId)}
                className={`w-full rounded-xl border px-3 py-2.5 text-left transition-colors ${
                  selectedNodeId === nodeId
                    ? "border-sky-200 bg-sky-50/80 text-sky-900 ring-1 ring-sky-200/80"
                    : "border-zinc-200 bg-zinc-50/80 text-zinc-700 hover:bg-zinc-100"
                }`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span
                        className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${stepStatusStyle(status)}`}
                      >
                        {status}
                      </span>
                      <span className="font-medium text-zinc-900">
                        {displayLabel}
                      </span>
                      <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-zinc-500">
                        {compactNodeType(nodeType)}
                      </span>
                      <span className="font-mono text-[11px] text-zinc-400">
                        {nodeId}
                      </span>
                    </div>
                    <div className="mt-1 text-[11px] leading-relaxed text-zinc-600">
                      {detail}
                    </div>
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {toolCallCount > 0 && (
                        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
                          {toolCallCount} tool{toolCallCount === 1 ? "" : "s"}
                        </span>
                      )}
                      {toolNode && (
                        <TraceToolBadges
                          localName={toolNode.localName}
                          bindingType={toolNode.bindingType}
                          registryRef={toolNode.registryRef}
                          requiresApproval={toolNode.requiresApproval}
                          callCount={toolNode.callCount}
                        />
                      )}
                      {forEachNode && (
                        <TraceForEachBadges forEachNode={forEachNode} />
                      )}
                      {subworkflowNode && (
                        <TraceSubworkflowBadges subworkflow={subworkflowNode} />
                      )}
                      {knowledgeBuild && (
                        <TraceKnowledgeBuildBadges
                          knowledgeBuild={knowledgeBuild}
                        />
                      )}
                      {knowledgeModeLabel && (
                        <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10px] font-semibold text-emerald-700">
                          {knowledgeModeLabel}
                        </span>
                      )}
                      {knowledgeQuery?.ageGraphName && (
                        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
                          AGE {knowledgeQuery.ageGraphName}
                        </span>
                      )}
                      {knowledgeQuery?.citations.length ? (
                        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
                          {knowledgeQuery.citations.length} citation
                          {knowledgeQuery.citations.length === 1 ? "" : "s"}
                        </span>
                      ) : null}
                      {knowledgeQuery?.chunks.length ? (
                        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
                          {knowledgeQuery.chunks.length} chunk
                          {knowledgeQuery.chunks.length === 1 ? "" : "s"}
                        </span>
                      ) : null}
                      {seedStrategyLabel && (
                        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
                          {seedStrategyLabel}
                        </span>
                      )}
                      {knowledgeQuery?.fallbackRetrievalMode && (
                        <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">
                          Fallback{" "}
                          {retrievalModeLabel(
                            knowledgeQuery.fallbackRetrievalMode,
                          ) ?? knowledgeQuery.fallbackRetrievalMode}
                        </span>
                      )}
                      {knowledgeQuery?.strictAgeRetrieval && (
                        <span className="rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-600">
                          strict AGE
                        </span>
                      )}
                      <TraceRecoveryBadges markers={recoveryContext.markers} />
                    </div>
                    {toolNode && (
                      <TraceToolMeta
                        bindingTarget={toolBindingTarget}
                        argumentSummary={toolArguments}
                        sideEffectLabel={toolEffectLabel}
                      />
                    )}
                    {forEachNode && (
                      <TraceForEachMeta forEachNode={forEachNode} />
                    )}
                    {subworkflowNode && (
                      <TraceSubworkflowMeta subworkflow={subworkflowNode} />
                    )}
                    {knowledgeBuild && (
                      <TraceKnowledgeBuildMeta
                        knowledgeBuild={knowledgeBuild}
                      />
                    )}
                    {knowledgeQuery?.matchedEntities.length ||
                    knowledgeQuery?.ageFallbackReason ? (
                      <div className="mt-2 text-[11px] leading-relaxed text-zinc-500">
                        {knowledgeQuery.matchedEntities.length > 0 && (
                          <span>
                            Matched{" "}
                            {knowledgeQuery.matchedEntities
                              .slice(0, 3)
                              .join(", ")}
                            {knowledgeQuery.matchedEntities.length > 3
                              ? "…"
                              : ""}
                          </span>
                        )}
                        {knowledgeQuery.matchedEntities.length > 0 &&
                          knowledgeQuery.ageFallbackReason &&
                          " · "}
                        {knowledgeQuery.ageFallbackReason && (
                          <span>
                            AGE fallback: {knowledgeQuery.ageFallbackReason}
                          </span>
                        )}
                      </div>
                    ) : null}
                    <TraceRecoveryMeta details={recoveryContext.details} />
                  </div>
                  {durationLabel && (
                    <span className="shrink-0 rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-zinc-500">
                      {durationLabel}
                    </span>
                  )}
                </div>
              </button>
            );
          })
        )}
        {promptVersion && (
          <div className="text-zinc-500">prompt version: {promptVersion}</div>
        )}
        {run.summary?.error && (
          <div className="text-red-600">error: {run.summary.error}</div>
        )}
      </div>
    </div>
  );
}
