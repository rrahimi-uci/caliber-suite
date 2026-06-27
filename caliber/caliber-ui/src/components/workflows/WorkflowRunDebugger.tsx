/**
 * Rich debugger for persisted workflow runs.
 *
 * Pairs summary steps with stored run events so operators can inspect a run
 * node-by-node, see upstream context, and open the raw event payloads that
 * explain how a step executed.
 */

import { useEffect, useMemo, useState } from "react";

import type {
  WorkflowManifest,
  WorkflowRun,
  WorkflowRunCheckpoint,
  WorkflowRunEvent,
  WorkflowRunStep,
} from "@/api/workflowTypes";
import {
  StepPreview,
  ageSeedStrategyLabel,
  extractKnowledgeBuildDiagnostics,
  extractErrorBoundaryDiagnostics,
  extractForEachDiagnostics,
  extractJoinDiagnostics,
  extractKnowledgeQueryDiagnostics,
  extractStepTelemetry,
  extractSubworkflowDiagnostics,
  extractToolNodeDiagnostics,
  formatUsdEstimate,
  knowledgeBuildActivationStatusLabel,
  knowledgeBuildStatusLabel,
  knowledgeBuildWaitStatusLabel,
  knowledgeGraphTargetLabel,
  retrievalModeLabel,
  subworkflowStatusLabel,
  subworkflowStatusTone,
  toolArgumentSummary,
  toolBindingTargetLabel,
  toolBindingTypeLabel,
  toolSideEffectLabel,
  workflowNodeTypeLabel,
  type UpstreamOutput,
} from "@/components/workflows/StepPreview";
import {
  approvalCheckpointKind,
  workflowRunCheckpointMarkerLabel,
  workflowRunLifecycleLabel,
  workflowRunLifecycleSummary,
} from "@/lib/workflowRunLabels";

interface WorkflowRunDebuggerProps {
  manifest: WorkflowManifest;
  run: WorkflowRun;
  events?: WorkflowRunEvent[];
  checkpoints?: WorkflowRunCheckpoint[];
  focusedNodeId?: string | null;
  onSelectNodeId?: (nodeId: string) => void;
}

interface DebugStep extends WorkflowRunStep {
  stepIndex: number;
  selectionKey: string;
}

interface DebugEventStep {
  event: WorkflowRunEvent;
  step: DebugStep;
}

interface DebugStepMarker {
  label: string;
  tone: string;
}

const PRE_STEP_LIFECYCLE_EVENT_TYPES = new Set([
  "workflow.run.queued",
  "workflow.run.started",
]);

const POST_STEP_LIFECYCLE_EVENT_TYPES = new Set([
  "workflow.run.recovered",
  "workflow.run.approval.approved",
  "workflow.run.approval.rejected",
  "workflow.run.waiting_approval",
  "workflow.run.waiting_event",
  "workflow.run.resumed",
  "workflow.run.cancel_requested",
  "workflow.run.cancelled",
  "workflow.run.completed",
  "workflow.run.expired",
  "workflow.run.failed",
  "workflow.run.retried",
]);

interface PortDiff {
  added: string[];
  removed: string[];
  changed: string[];
  unchanged: number;
}

function isActiveRunStatus(runStatus: string): boolean {
  return (
    runStatus === "queued"
    || runStatus === "running"
    || runStatus === "resuming"
    || runStatus === "cancel_requested"
  );
}

function isStoppedRunStatus(runStatus: string): boolean {
  return (
    runStatus === "failed"
    || runStatus === "cancelled"
    || runStatus === "rejected"
    || runStatus === "expired"
    || runStatus === "blocked"
  );
}

export function runEventsLoadErrorMessage(
  runStatus: string | null | undefined,
  errorMessage: string,
): JSX.Element {
  const detail = errorMessage.trim() || "Unknown error";
  if (
    runStatus === "queued"
    || runStatus === "running"
    || runStatus === "resuming"
    || runStatus === "cancel_requested"
    || runStatus === "waiting_approval"
    || runStatus === "waiting_event"
  ) {
    return (
      <>
        Persisted run events could not be loaded while this run is still active. Trace replay and
        manifest-aware debugging are temporarily unavailable, so use the recovery, checkpoint, and
        lineage panels to confirm live execution state until event history catches up.
        <span className="mt-2 block text-red-700/80">Latest event error: {detail}</span>
      </>
    );
  }
  if (runStatus === "completed") {
    return (
      <>
        Persisted run events could not be loaded for this completed run. Trace replay and
        manifest-aware debugging are unavailable until event history is restored, so inspect the
        recovery panel, final outputs, and generated artifacts to reconstruct how execution
        finished.
        <span className="mt-2 block text-red-700/80">Latest event error: {detail}</span>
      </>
    );
  }
  if (
    runStatus === "failed"
    || runStatus === "cancelled"
    || runStatus === "rejected"
    || runStatus === "expired"
    || runStatus === "blocked"
  ) {
    return (
      <>
        Persisted run events could not be loaded for this stopped run. Trace replay and
        manifest-aware debugging are unavailable, so inspect the recovery, checkpoint, and lineage
        panels to trace where execution failed or was interrupted until event history is restored.
        <span className="mt-2 block text-red-700/80">Latest event error: {detail}</span>
      </>
    );
  }
  return (
    <>
      Persisted run events could not be loaded for this run. Trace replay and manifest-aware
      debugging are unavailable until event history is restored, so use the recovery, checkpoint,
      and lineage panels to follow the remaining execution evidence.
      <span className="mt-2 block text-red-700/80">Latest event error: {detail}</span>
    </>
  );
}

function emptyDebuggerMessage(
  events: WorkflowRunEvent[],
  checkpoints: WorkflowRunCheckpoint[],
  runStatus: string,
): string {
  const eventCount = events.length;
  const checkpointCount = checkpoints.length;
  const activeRun = isActiveRunStatus(runStatus);
  const stoppedRun = isStoppedRunStatus(runStatus);
  const completedRun = runStatus === "completed";
  if (eventCount > 0 || checkpointCount > 0) {
    const evidence = [
      eventCount > 0
        ? `${eventCount} lifecycle event${eventCount === 1 ? "" : "s"}`
        : null,
      checkpointCount > 0
        ? `${checkpointCount} checkpoint${checkpointCount === 1 ? "" : "s"}`
        : null,
    ]
      .filter((value): value is string => Boolean(value))
      .join(" and ");
    if (activeRun) {
      return `No recorded step details yet. This run may still be executing or step persistence may still be catching up. Inspect the ${evidence} in the recovery and checkpoint panels while execution continues.`;
    }
    if (completedRun) {
      return `No recorded step details yet. This execution completed with only lightweight persisted evidence. Inspect the ${evidence}, final outputs, and generated artifacts to reconstruct how it finished.`;
    }
    if (stoppedRun) {
      return `No recorded step details yet. This run stopped before richer step telemetry was persisted. Inspect the ${evidence} in the recovery and checkpoint panels to trace where execution stopped.`;
    }
    return `No recorded step details yet. This run may only have lifecycle events or a lightweight summary. Inspect the ${evidence} in the recovery and checkpoint panels to keep tracing execution until richer step telemetry is available.`;
  }
  if (activeRun) {
    return "No recorded step details yet. This run may still be executing or step persistence may still be catching up. Check the recovery timeline and checkpoint panel while execution continues.";
  }
  if (completedRun) {
    return "No recorded step details yet. This execution completed with only lightweight persisted evidence. Check the recovery timeline, final outputs, and generated artifacts to reconstruct how it finished.";
  }
  if (stoppedRun) {
    return "No recorded step details yet. This run stopped before richer step telemetry was persisted. Check the recovery timeline and checkpoint panel to trace where execution stopped.";
  }
  return "No recorded step details yet. This run may only have lifecycle events or a lightweight summary. Check the recovery timeline and checkpoint panel for any persisted run evidence until richer step telemetry is available.";
}

function emptyEventTimelineMessage({
  hasSelectedStep,
  hasCheckpoint,
  runStatus,
}: {
  hasSelectedStep: boolean;
  hasCheckpoint: boolean;
  runStatus: string;
}): string {
  const activeRun = isActiveRunStatus(runStatus);
  const stoppedRun = isStoppedRunStatus(runStatus);
  const completedRun = runStatus === "completed";

  if (hasSelectedStep && hasCheckpoint) {
    if (activeRun) {
      return "No persisted run events were found for this workflow run yet. This run may still be executing or event persistence may still be catching up, so use the selected step snapshot, port-level context, and stored checkpoint details while execution continues.";
    }
    if (completedRun) {
      return "No persisted run events were found for this workflow run. This execution completed without stored event history, so use the selected step snapshot, port-level context, stored checkpoint details, and final outputs to reconstruct how it finished.";
    }
    if (stoppedRun) {
      return "No persisted run events were found for this workflow run. Use the selected step snapshot, port-level context, stored checkpoint details, and recovery diagnostics to trace where execution stopped.";
    }
    return "No persisted run events were found for this workflow run. Use the selected step snapshot, port-level context, and stored checkpoint details to keep tracing execution until event persistence is available.";
  }
  if (hasSelectedStep) {
    if (activeRun) {
      return "No persisted run events were found for this workflow run yet. This run may still be executing or event persistence may still be catching up, so use the selected step snapshot and port-level context while execution continues.";
    }
    if (completedRun) {
      return "No persisted run events were found for this workflow run. This execution completed without stored event history, so use the selected step snapshot, port-level context, and final outputs to reconstruct how it finished.";
    }
    if (stoppedRun) {
      return "No persisted run events were found for this workflow run. Use the selected step snapshot, port-level context, and recovery diagnostics to trace where execution stopped.";
    }
    return "No persisted run events were found for this workflow run. Use the selected step snapshot and port-level context to keep tracing execution until event persistence is available.";
  }
  if (hasCheckpoint) {
    if (activeRun) {
      return "No persisted run events were found for this workflow run yet. This run may still be executing or event persistence may still be catching up, so use the stored checkpoint details and recovery panel while execution continues.";
    }
    if (completedRun) {
      return "No persisted run events were found for this workflow run. This execution completed without stored event history, so use the stored checkpoint details, recovery panel, and final outputs to reconstruct how it finished.";
    }
    if (stoppedRun) {
      return "No persisted run events were found for this workflow run. Use the stored checkpoint details and recovery panel to trace where execution stopped.";
    }
    return "No persisted run events were found for this workflow run. Use the stored checkpoint details and recovery panel to keep tracing execution until event persistence is available.";
  }
  if (activeRun) {
    return "No persisted run events were found for this workflow run yet. This run may still be executing or event persistence may still be catching up, so use the run summary and recovery diagnostics while execution continues.";
  }
  if (completedRun) {
    return "No persisted run events were found for this workflow run. This execution completed without stored event history, so use the run summary, final outputs, and recovery diagnostics to reconstruct how it finished.";
  }
  if (stoppedRun) {
    return "No persisted run events were found for this workflow run. Use the run summary and recovery diagnostics to trace where execution stopped.";
  }
  return "No persisted run events were found for this workflow run. Use the run summary and recovery diagnostics to confirm whether this execution emitted any persisted runtime history.";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function checkpointState(
  checkpoint: WorkflowRunCheckpoint | null,
): Record<string, unknown> | null {
  return isRecord(checkpoint?.state_blob) ? checkpoint.state_blob : null;
}

function stepSelectionKey(step: WorkflowRunStep, stepIndex: number): string {
  return `${step.node_id}:${stepIndex}`;
}

function coerceStep(value: unknown, stepIndex: number): DebugStep | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Partial<WorkflowRunStep>;
  if (typeof raw.node_id !== "string" || typeof raw.node_type !== "string")
    return null;
  const inputByPort = isRecord(raw.input_by_port) ? raw.input_by_port : null;
  const outputByPort = isRecord(raw.output_by_port) ? raw.output_by_port : null;
  const step: WorkflowRunStep = {
    node_id: raw.node_id,
    node_type: raw.node_type,
    status: typeof raw.status === "string" ? raw.status : "ok",
    output: typeof raw.output === "string" ? raw.output : "",
    tokens: readNumber(raw.tokens) ?? undefined,
    prompt_tokens: readNumber(raw.prompt_tokens) ?? undefined,
    completion_tokens: readNumber(raw.completion_tokens) ?? undefined,
    cached_prompt_tokens: readNumber(raw.cached_prompt_tokens) ?? undefined,
    cost_usd: readNumber(raw.cost_usd) ?? undefined,
    model: typeof raw.model === "string" ? raw.model : null,
    prompt_version:
      typeof raw.prompt_version === "string" ? raw.prompt_version : null,
    tool_calls: Array.isArray(raw.tool_calls) ? raw.tool_calls : [],
    handoff_target:
      typeof raw.handoff_target === "string" ? raw.handoff_target : null,
    detail: typeof raw.detail === "string" ? raw.detail : "",
    duration_ms: typeof raw.duration_ms === "number" ? raw.duration_ms : 0,
    input_by_port: inputByPort,
    output_by_port: outputByPort,
  };
  return {
    ...step,
    stepIndex,
    selectionKey: stepSelectionKey(step, stepIndex),
  };
}

function deriveSummarySteps(run: WorkflowRun): DebugStep[] {
  const rawSteps = Array.isArray(run.summary?.steps) ? run.summary.steps : [];
  return rawSteps
    .map((step, index) => coerceStep(step, index))
    .filter((step): step is DebugStep => step !== null);
}

function deriveEventSteps(events: WorkflowRunEvent[]): DebugEventStep[] {
  let stepIndex = 0;
  const derived: DebugEventStep[] = [];
  for (const event of events) {
    if (event.event_type !== "workflow.run.step") continue;
    const payload =
      event.payload && typeof event.payload === "object"
        ? event.payload.step
        : null;
    const step = coerceStep(payload, stepIndex);
    if (!step) continue;
    derived.push({ event, step });
    stepIndex += 1;
  }
  return derived;
}

function formatJson(value: unknown): string {
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return String(value);
  }
}

function summarizeEvent(event: WorkflowRunEvent): string {
  const payload = isRecord(event.payload) ? event.payload : null;
  if (event.event_type === "workflow.run.step") {
    const step = coerceStep(payload?.step, 0);
    if (step?.detail) return step.detail;
    if (step?.handoff_target) return `handoff -> ${step.handoff_target}`;
    if (step?.tool_calls.length) {
      return `${step.tool_calls.length} tool call${step.tool_calls.length === 1 ? "" : "s"}`;
    }
    return step?.status ?? "step recorded";
  }
  if (event.event_type === "workflow.run.waiting_event") {
    return workflowRunLifecycleSummary(event.event_type, payload);
  }
  if (event.event_type.startsWith("workflow.run.")) {
    return workflowRunLifecycleSummary(event.event_type, payload);
  }
  return event.event_type.replace(/^workflow\.run\./, "").replaceAll(".", " ");
}

function checkpointKindForEvent(
  event: WorkflowRunEvent,
  checkpoints: WorkflowRunCheckpoint[],
): string | null {
  if (event.event_type !== "workflow.run.waiting_approval") return null;
  const nodeId = readString(event.node_id);
  if (!nodeId) return null;
  const checkpoint = [...checkpoints]
    .filter((item) => item.node_id === nodeId)
    .sort((left, right) => right.sequence - left.sequence)[0] ?? null;
  return readString(checkpointState(checkpoint)?.kind);
}

function summarizeLifecycleEvent(
  event: WorkflowRunEvent,
  checkpoints: WorkflowRunCheckpoint[],
): string {
  if (event.event_type === "workflow.run.waiting_approval") {
    return workflowRunLifecycleSummary(
      event.event_type,
      event.payload,
      {
        approvalKind: approvalCheckpointKind(
          checkpointKindForEvent(event, checkpoints),
        ),
      },
    );
  }
  return summarizeEvent(event);
}

function formatEventTime(value: string | null | undefined): string {
  if (!value) return "n/a";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function portSummary(
  value: Record<string, unknown> | null | undefined,
  runStatus: string,
): string {
  if (!value) {
    if (isActiveRunStatus(runStatus)) return "Snapshot pending";
    if (runStatus === "completed" || isStoppedRunStatus(runStatus)) {
      return "Snapshot unavailable";
    }
    return "No snapshot recorded";
  }
  const keys = Object.keys(value);
  if (keys.length === 0) return "0 ports";
  return `${keys.length} port${keys.length === 1 ? "" : "s"} · ${keys.join(", ")}`;
}

function hasPortSnapshot(
  value: Record<string, unknown> | null | undefined,
): value is Record<string, unknown> {
  return value !== null && value !== undefined;
}

function emptyPortSnapshotMessage(
  snapshotLabel: string,
  runStatus: string,
): string {
  if (isActiveRunStatus(runStatus)) {
    return `${snapshotLabel} has not been persisted for this step yet. This run may still be executing or step persistence may still be catching up, so use the recovery and checkpoint panels while runtime evidence fills in.`;
  }
  if (runStatus === "completed") {
    return `${snapshotLabel} was not persisted for this completed run step. Use the event timeline, final outputs, and generated artifacts to reconstruct the missing context.`;
  }
  if (isStoppedRunStatus(runStatus)) {
    return `${snapshotLabel} was not persisted before this run stopped. Use the recovery timeline, checkpoint trail, and surrounding step evidence to trace the missing context.`;
  }
  return `${snapshotLabel} was not persisted for this step.`;
}

function transitionSubtitle(
  previousStep: DebugStep | null,
  displayStep: DebugStep,
  runStatus: string,
): string {
  if (previousStep) {
    return `Previous recorded step ${previousStep.node_id} -> ${displayStep.node_id} input`;
  }
  if (isActiveRunStatus(runStatus)) {
    return "Earliest persisted step so far";
  }
  if (runStatus === "completed") {
    return "Earliest persisted step in this completed run";
  }
  if (isStoppedRunStatus(runStatus)) {
    return "Earliest persisted step before execution stopped";
  }
  return "Earliest persisted step in this run";
}

function transitionEmptyMessage(
  previousStep: DebugStep | null,
  runStatus: string,
): string {
  if (previousStep) {
    if (isActiveRunStatus(runStatus)) {
      return "No port-level changes were recorded between these persisted steps yet. This run may still be executing or richer telemetry may still be catching up, so use the step detail, tool-call trace, and recovery timeline to confirm whether the node is still working from the same snapshot.";
    }
    if (runStatus === "completed") {
      return "No port-level changes were recorded between these persisted steps. This node appears to have received the same snapshot that the previous step emitted, so use the step detail, tool calls, final outputs, and generated artifacts to confirm what happened inside the node.";
    }
    if (isStoppedRunStatus(runStatus)) {
      return "No port-level changes were recorded between these persisted steps before the run stopped. Use the step detail, checkpoint trail, and recovery diagnostics to confirm whether execution stalled while carrying the same snapshot forward.";
    }
    return "No port-level changes were recorded between these persisted steps.";
  }
  if (isActiveRunStatus(runStatus)) {
    return "No previous recorded step is available yet. Use the input ports, checkpoint trail, and recovery timeline to trace how execution entered this node while earlier telemetry catches up.";
  }
  if (runStatus === "completed") {
    return "No previous recorded step was persisted for this completed run. Use the input ports, event timeline, and final outputs to reconstruct how execution entered this node.";
  }
  if (isStoppedRunStatus(runStatus)) {
    return "No previous recorded step was persisted before this run stopped. Use the input ports, checkpoint trail, and recovery diagnostics to trace where execution lost continuity.";
  }
  return "No previous recorded step is available for transition comparison.";
}

function comparableValue(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function diffPorts(
  before: Record<string, unknown> | null | undefined,
  after: Record<string, unknown> | null | undefined,
): PortDiff {
  const left = before ?? {};
  const right = after ?? {};
  const keys = Array.from(
    new Set([...Object.keys(left), ...Object.keys(right)]),
  ).sort();
  const diff: PortDiff = { added: [], removed: [], changed: [], unchanged: 0 };
  for (const key of keys) {
    const hasLeft = Object.prototype.hasOwnProperty.call(left, key);
    const hasRight = Object.prototype.hasOwnProperty.call(right, key);
    if (!hasLeft && hasRight) {
      diff.added.push(key);
      continue;
    }
    if (hasLeft && !hasRight) {
      diff.removed.push(key);
      continue;
    }
    if (comparableValue(left[key]) !== comparableValue(right[key])) {
      diff.changed.push(key);
      continue;
    }
    diff.unchanged += 1;
  }
  return diff;
}

function markerTone(
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
      return "border-slate-200 bg-white text-slate-600";
  }
}

function markerChips(markers: DebugStepMarker[]): JSX.Element | null {
  if (markers.length === 0) return null;
  return (
    <div
      className="mt-2 flex flex-wrap gap-1.5"
      data-testid="workflow-run-step-markers"
    >
      {markers.map((marker, index) => (
        <span
          key={`${marker.label}-${index}`}
          className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] ${marker.tone}`}
        >
          {marker.label}
        </span>
      ))}
    </div>
  );
}

function appendStepMarker(
  markers: DebugStepMarker[],
  seenLabels: Set<string>,
  label: string,
  tone: string,
): void {
  if (!label || seenLabels.has(label)) return;
  markers.push({ label, tone });
  seenLabels.add(label);
}

function initialLifecycleEvents(
  step: DebugStep,
  eventSteps: DebugEventStep[],
  events: WorkflowRunEvent[],
): WorkflowRunEvent[] {
  if (step.stepIndex !== 0) return [];
  const firstStepSequence = eventSteps[0]?.event.sequence ?? null;
  return events.filter(
    (event) =>
      PRE_STEP_LIFECYCLE_EVENT_TYPES.has(event.event_type) &&
      (firstStepSequence === null || event.sequence < firstStepSequence),
  );
}

function stepWindowEvents(
  step: DebugStep,
  {
    stepEvent,
    eventSteps,
    events,
    totalSteps,
  }: {
    stepEvent: DebugEventStep | null;
    eventSteps: DebugEventStep[];
    events: WorkflowRunEvent[];
    totalSteps: number;
  },
): WorkflowRunEvent[] {
  const currentSequence = stepEvent?.event.sequence ?? null;
  const nextSequence =
    eventSteps.find((entry) => entry.step.stepIndex === step.stepIndex + 1)
      ?.event.sequence ?? null;
  const isLastStep = step.stepIndex === totalSteps - 1;

  return events.filter((event) => {
    if (!POST_STEP_LIFECYCLE_EVENT_TYPES.has(event.event_type)) return false;
    if (currentSequence !== null) {
      if (event.sequence <= currentSequence) return false;
      if (nextSequence !== null && event.sequence >= nextSequence) return false;
      return event.node_id === step.node_id || event.node_id === null;
    }
    if (!isLastStep) return false;
    return event.node_id === step.node_id || event.node_id === null;
  });
}

function DiffBadge({
  label,
  items,
  tone,
}: {
  label: string;
  items: string[];
  tone: string;
}): JSX.Element | null {
  if (items.length === 0) return null;
  return (
    <div className={`rounded-xl border px-3 py-2 text-xs ${tone}`}>
      <div className="font-semibold uppercase tracking-[0.14em]">{label}</div>
      <div className="mt-1 font-mono">{items.join(", ")}</div>
    </div>
  );
}

function DiffCard({
  title,
  subtitle,
  diff,
  testId,
  emptyMessage = "No structural port changes were recorded in this comparison.",
}: {
  title: string;
  subtitle: string;
  diff: PortDiff;
  testId: string;
  emptyMessage?: string;
}): JSX.Element {
  return (
    <div
      data-testid={testId}
      className="rounded-2xl border border-slate-200/70 bg-white p-4 shadow-card"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
            {title}
          </div>
          <div className="mt-1 text-sm font-semibold text-slate-900">
            {subtitle}
          </div>
        </div>
        <div className="text-right text-[11px] text-slate-500">
          <div>{diff.added.length} added</div>
          <div>{diff.changed.length} changed</div>
          <div>{diff.removed.length} removed</div>
          <div>{diff.unchanged} unchanged</div>
        </div>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-3">
        <DiffBadge
          label="Added"
          items={diff.added}
          tone="border-emerald-200 bg-emerald-50 text-emerald-800"
        />
        <DiffBadge
          label="Changed"
          items={diff.changed}
          tone="border-sky-200 bg-sky-50 text-sky-800"
        />
        <DiffBadge
          label="Removed"
          items={diff.removed}
          tone="border-amber-200 bg-amber-50 text-amber-800"
        />
      </div>
      {diff.added.length === 0 &&
        diff.changed.length === 0 &&
        diff.removed.length === 0 && (
          <div className="mt-4 rounded-xl border border-dashed border-slate-200 bg-slate-50/70 px-3 py-3 text-xs text-slate-500">
            {emptyMessage}
          </div>
        )}
    </div>
  );
}

function deriveStepMarkers(
  step: DebugStep,
  {
    stepEvent,
    eventSteps,
    events,
    orderedCheckpoints,
    activeCheckpoint,
    currentNodeId,
    runStatus,
    run,
    totalSteps,
  }: {
    stepEvent: DebugEventStep | null;
    eventSteps: DebugEventStep[];
    events: WorkflowRunEvent[];
    orderedCheckpoints: WorkflowRunCheckpoint[];
    activeCheckpoint: WorkflowRunCheckpoint | null;
    currentNodeId: string | null | undefined;
    runStatus: string;
    run: WorkflowRun;
    totalSteps: number;
  },
): DebugStepMarker[] {
  const markers: DebugStepMarker[] = [];
  const seenLabels = new Set<string>();
  const checkpointForNode =
    orderedCheckpoints.find(
      (checkpoint) => checkpoint.node_id === step.node_id,
    ) ?? null;
  const checkpointKind = readString(checkpointState(checkpointForNode)?.kind);
  const inheritedCheckpoint =
    checkpointForNode?.workflow_run_id &&
    checkpointForNode.workflow_run_id !== run.workflow_run_id;
  const initialEvents = initialLifecycleEvents(step, eventSteps, events);
  if (checkpointForNode) {
    appendStepMarker(
      markers,
      seenLabels,
      workflowRunCheckpointMarkerLabel(checkpointKind),
      markerTone("neutral"),
    );
    if (inheritedCheckpoint) {
      appendStepMarker(
        markers,
        seenLabels,
        "Inherited checkpoint",
        markerTone("info"),
      );
    }
  }
  if (
    initialEvents.some((event) => event.event_type === "workflow.run.queued") ||
    (step.stepIndex === 0 && run.queued_at)
  ) {
    appendStepMarker(
      markers,
      seenLabels,
      workflowRunLifecycleLabel("workflow.run.queued", { mode: "marker" }),
      markerTone("neutral"),
    );
  }
  if (
    initialEvents.some(
      (event) => event.event_type === "workflow.run.started",
    ) ||
    (step.stepIndex === 0 && run.started_at)
  ) {
    appendStepMarker(
      markers,
      seenLabels,
      workflowRunLifecycleLabel("workflow.run.started", { mode: "marker" }),
      markerTone("info"),
    );
  }
  if (
    activeCheckpoint &&
    activeCheckpoint.node_id === step.node_id &&
    currentNodeId === step.node_id &&
    ["waiting_event", "waiting_approval"].includes(runStatus)
  ) {
    appendStepMarker(markers, seenLabels, "Resume target", markerTone("info"));
  }
  const windowEvents = stepWindowEvents(step, {
    stepEvent,
    eventSteps,
    events,
    totalSteps,
  });
  if (
    windowEvents.some((event) => event.event_type === "workflow.run.recovered")
  ) {
    appendStepMarker(
      markers,
      seenLabels,
      workflowRunLifecycleLabel("workflow.run.recovered", { mode: "marker" }),
      markerTone("warn"),
    );
  }
  if (
    windowEvents.some(
      (event) => event.event_type === "workflow.run.approval.approved",
    )
  ) {
    appendStepMarker(
      markers,
      seenLabels,
      workflowRunLifecycleLabel("workflow.run.approval.approved", { mode: "marker" }),
      markerTone("success"),
    );
  }
  if (
    windowEvents.some(
      (event) => event.event_type === "workflow.run.approval.rejected",
    )
  ) {
    appendStepMarker(
      markers,
      seenLabels,
      workflowRunLifecycleLabel("workflow.run.approval.rejected", { mode: "marker" }),
      markerTone("danger"),
    );
  }
  if (
    windowEvents.some(
      (event) => event.event_type === "workflow.run.waiting_approval",
    )
  ) {
    appendStepMarker(
      markers,
      seenLabels,
      workflowRunLifecycleLabel("workflow.run.waiting_approval", {
        approvalKind: approvalCheckpointKind(checkpointKind),
        mode: "marker",
      }),
      markerTone("warn"),
    );
  } else if (
    windowEvents.some(
      (event) => event.event_type === "workflow.run.waiting_event",
    )
  ) {
    appendStepMarker(
      markers,
      seenLabels,
      workflowRunLifecycleLabel("workflow.run.waiting_event", {
        mode: "marker",
      }),
      markerTone("info"),
    );
  }
  if (
    windowEvents.some((event) => event.event_type === "workflow.run.resumed")
  ) {
    appendStepMarker(
      markers,
      seenLabels,
      workflowRunLifecycleLabel("workflow.run.resumed", { mode: "marker" }),
      markerTone("success"),
    );
  }
  if (
    windowEvents.some(
      (event) => event.event_type === "workflow.run.cancel_requested",
    ) ||
    (step.stepIndex === totalSteps - 1 && Boolean(run.cancel_requested_at))
  ) {
    appendStepMarker(
      markers,
      seenLabels,
      workflowRunLifecycleLabel("workflow.run.cancel_requested", {
        mode: "marker",
      }),
      markerTone("warn"),
    );
  }
  if (
    windowEvents.some(
      (event) => event.event_type === "workflow.run.cancelled",
    ) ||
    (step.stepIndex === totalSteps - 1 && run.status === "cancelled")
  ) {
    appendStepMarker(
      markers,
      seenLabels,
      workflowRunLifecycleLabel("workflow.run.cancelled", { mode: "marker" }),
      markerTone("warn"),
    );
  }
  if (
    windowEvents.some(
      (event) => event.event_type === "workflow.run.completed",
    ) ||
    (step.stepIndex === totalSteps - 1 &&
      run.status === "completed" &&
      Boolean(run.completed_at))
  ) {
    appendStepMarker(
      markers,
      seenLabels,
      workflowRunLifecycleLabel("workflow.run.completed", { mode: "marker" }),
      markerTone("success"),
    );
  }
  if (
    windowEvents.some(
      (event) => event.event_type === "workflow.run.expired",
    ) ||
    (step.stepIndex === totalSteps - 1 &&
      run.status === "expired" &&
      Boolean(run.completed_at || run.lease_expires_at))
  ) {
    appendStepMarker(
      markers,
      seenLabels,
      workflowRunLifecycleLabel("workflow.run.expired", { mode: "marker" }),
      markerTone("danger"),
    );
  }
  if (
    windowEvents.some((event) => event.event_type === "workflow.run.failed") ||
    (step.stepIndex === totalSteps - 1 &&
      run.status === "failed" &&
      Boolean(run.completed_at))
  ) {
    appendStepMarker(
      markers,
      seenLabels,
      workflowRunLifecycleLabel("workflow.run.failed", { mode: "marker" }),
      markerTone("danger"),
    );
  }
  if (
    windowEvents.some((event) => event.event_type === "workflow.run.retried")
  ) {
    appendStepMarker(
      markers,
      seenLabels,
      workflowRunLifecycleLabel("workflow.run.retried", { mode: "marker" }),
      markerTone("info"),
    );
  }
  return markers;
}

function buildUpstreamOutputs(
  manifest: WorkflowManifest,
  steps: DebugStep[],
  selectedStep: DebugStep | null,
): UpstreamOutput[] {
  if (!selectedStep) return [];
  const upstream: UpstreamOutput[] = [];
  for (const edge of manifest.edges) {
    if (edge.to !== selectedStep.node_id) continue;
    for (let index = selectedStep.stepIndex - 1; index >= 0; index -= 1) {
      const candidate = steps[index];
      if (candidate?.node_id === edge.from) {
        upstream.push({
          nodeId: edge.from,
          output: candidate.output,
        });
        break;
      }
    }
  }
  return upstream;
}

function selectedStepCardClasses(selected: boolean): string {
  return selected
    ? "border-sky-300 bg-sky-50/70 shadow-sm"
    : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50/60";
}

function knowledgeModeTone(mode: string | null): string {
  if (mode === "age_graph") {
    return "border-emerald-200 bg-emerald-50 text-emerald-700";
  }
  if (mode === "graph_hybrid") {
    return "border-sky-200 bg-sky-50 text-sky-700";
  }
  return "border-slate-200 bg-slate-50 text-slate-600";
}

function KnowledgeQueryBadges({
  knowledgeQuery,
  testId,
}: {
  knowledgeQuery: NonNullable<
    ReturnType<typeof extractKnowledgeQueryDiagnostics>
  >;
  testId?: string;
}): JSX.Element {
  const knowledgeModeLabel = retrievalModeLabel(knowledgeQuery.retrievalMode);
  const knowledgeSeedLabel = ageSeedStrategyLabel(
    knowledgeQuery.ageSeedStrategy,
  );

  return (
    <div className="flex flex-wrap gap-1.5" data-testid={testId}>
      {knowledgeModeLabel && (
        <span
          className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${knowledgeModeTone(knowledgeQuery.retrievalMode)}`}
        >
          {knowledgeModeLabel}
        </span>
      )}
      {knowledgeQuery.ageGraphName && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          AGE {knowledgeQuery.ageGraphName}
        </span>
      )}
      {knowledgeQuery.citations.length > 0 && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          {knowledgeQuery.citations.length} citation
          {knowledgeQuery.citations.length === 1 ? "" : "s"}
        </span>
      )}
      {knowledgeQuery.chunks.length > 0 && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          {knowledgeQuery.chunks.length} chunk
          {knowledgeQuery.chunks.length === 1 ? "" : "s"}
        </span>
      )}
      {knowledgeSeedLabel && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          {knowledgeSeedLabel}
        </span>
      )}
      {knowledgeQuery.fallbackRetrievalMode && (
        <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">
          Fallback{" "}
          {retrievalModeLabel(knowledgeQuery.fallbackRetrievalMode) ??
            knowledgeQuery.fallbackRetrievalMode}
        </span>
      )}
      {knowledgeQuery.strictAgeRetrieval && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          strict AGE
        </span>
      )}
      {knowledgeQuery.queryOverrideActive && (
        <span className="rounded-full border border-sky-200 bg-sky-50 px-2 py-0.5 text-[10px] font-semibold text-sky-700">
          Query override
        </span>
      )}
    </div>
  );
}

function KnowledgeQueryMeta({
  knowledgeQuery,
  className = "mt-2 text-[11px] text-slate-500",
}: {
  knowledgeQuery: NonNullable<
    ReturnType<typeof extractKnowledgeQueryDiagnostics>
  >;
  className?: string;
}): JSX.Element | null {
  if (
    knowledgeQuery.matchedEntities.length === 0 &&
    !knowledgeQuery.ageFallbackReason
  ) {
    return null;
  }
  return (
    <div className={className}>
      {knowledgeQuery.matchedEntities.length > 0 && (
        <span>
          Matched {knowledgeQuery.matchedEntities.slice(0, 3).join(", ")}
          {knowledgeQuery.matchedEntities.length > 3 ? "..." : ""}
        </span>
      )}
      {knowledgeQuery.matchedEntities.length > 0 &&
        knowledgeQuery.ageFallbackReason &&
        " · "}
      {knowledgeQuery.ageFallbackReason && (
        <span>AGE fallback: {knowledgeQuery.ageFallbackReason}</span>
      )}
    </div>
  );
}

function knowledgeBuildTone(status: string | null, previewSkipped: boolean): string {
  if (previewSkipped || status === "preview_skipped") {
    return "border-slate-200 bg-slate-50 text-slate-600";
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
  return "border-slate-200 bg-slate-50 text-slate-600";
}

function KnowledgeBuildBadges({
  knowledgeBuild,
  testId,
}: {
  knowledgeBuild: NonNullable<
    ReturnType<typeof extractKnowledgeBuildDiagnostics>
  >;
  testId?: string;
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
    <div className="flex flex-wrap gap-1.5" data-testid={testId}>
      {buildStatus && (
        <span
          className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${knowledgeBuildTone(
            knowledgeBuild.status,
            knowledgeBuild.previewSkipped,
          )}`}
        >
          {buildStatus}
        </span>
      )}
      {knowledgeBuild.knowledgeBaseId && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          KB {knowledgeBuild.knowledgeBaseId}
        </span>
      )}
      {knowledgeBuild.versionId && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 font-mono text-[10px] font-semibold text-slate-600">
          {knowledgeBuild.versionId}
        </span>
      )}
      {knowledgeBuild.runId && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 font-mono text-[10px] font-semibold text-slate-600">
          {knowledgeBuild.runId}
        </span>
      )}
      {graphTarget && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          {graphTarget}
        </span>
      )}
      {retrievalMode && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
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
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold uppercase text-slate-600">
          AGE {knowledgeBuild.ageSyncStatus}
        </span>
      )}
    </div>
  );
}

function KnowledgeBuildMeta({
  knowledgeBuild,
  className = "mt-2 text-[11px] text-slate-500",
}: {
  knowledgeBuild: NonNullable<
    ReturnType<typeof extractKnowledgeBuildDiagnostics>
  >;
  className?: string;
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
  const parts = [
    buildProfile ? `Build ${buildProfile}` : null,
    graphProfile ? `Graph ${graphProfile}` : null,
    waitPolicy ? `Wait ${waitPolicy}` : null,
  ].filter(Boolean);
  if (parts.length === 0) return null;
  return <div className={className}>{parts.join(" · ")}</div>;
}

function StepTelemetryBadges({
  telemetry,
  testId,
}: {
  telemetry: NonNullable<ReturnType<typeof extractStepTelemetry>>;
  testId?: string;
}): JSX.Element {
  return (
    <div className="flex flex-wrap gap-1.5" data-testid={testId}>
      {telemetry.model && (
        <span className="rounded-full border border-violet-200 bg-violet-50 px-2 py-0.5 text-[10px] font-semibold text-violet-700">
          {telemetry.model}
        </span>
      )}
      {telemetry.tokens !== null && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          {telemetry.tokens} tokens
        </span>
      )}
      {telemetry.promptVersion && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          Prompt {telemetry.promptVersion}
        </span>
      )}
      {telemetry.cachedPromptTokens !== null && (
        <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10px] font-semibold text-emerald-700">
          {telemetry.cachedPromptTokens} cached prompt
        </span>
      )}
      {telemetry.costUsd !== null && telemetry.costUsd > 0 && (
        <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">
          Est. {formatUsdEstimate(telemetry.costUsd)}
        </span>
      )}
    </div>
  );
}

function StepTelemetryMeta({
  telemetry,
  className = "mt-2 text-[11px] text-slate-500",
}: {
  telemetry: NonNullable<ReturnType<typeof extractStepTelemetry>>;
  className?: string;
}): JSX.Element | null {
  const parts = [
    telemetry.promptTokens !== null ? `${telemetry.promptTokens} prompt` : null,
    telemetry.completionTokens !== null
      ? `${telemetry.completionTokens} completion`
      : null,
    telemetry.cachedPromptTokens !== null
      ? `${telemetry.cachedPromptTokens} cached prompt`
      : null,
    telemetry.costUsd !== null && telemetry.costUsd > 0
      ? `Est. ${formatUsdEstimate(telemetry.costUsd)}`
      : null,
  ].filter(Boolean);
  if (parts.length === 0) return null;
  return <div className={className}>{parts.join(" · ")}</div>;
}

function toolBindingTone(bindingType: string | null): string {
  if (bindingType === "mcp_tool") {
    return "border-amber-200 bg-amber-50 text-amber-700";
  }
  return "border-violet-200 bg-violet-50 text-violet-700";
}

function ToolNodeBadges({
  toolNode,
  testId,
}: {
  toolNode: NonNullable<ReturnType<typeof extractToolNodeDiagnostics>>;
  testId?: string;
}): JSX.Element {
  const bindingLabel = toolBindingTypeLabel(toolNode.bindingType);
  const sideEffectLabel = toolSideEffectLabel(toolNode.sideEffectLevel);

  return (
    <div className="flex flex-wrap gap-1.5" data-testid={testId}>
      {toolNode.localName && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          {toolNode.localName}
        </span>
      )}
      {bindingLabel && (
        <span
          className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${toolBindingTone(toolNode.bindingType)}`}
        >
          {bindingLabel}
        </span>
      )}
      {toolNode.registryRef && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 font-mono text-[10px] font-semibold text-slate-600">
          {toolNode.registryRef}
        </span>
      )}
      {sideEffectLabel && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold capitalize text-slate-600">
          {sideEffectLabel}
        </span>
      )}
      {toolNode.requiresApproval && (
        <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">
          Approval required
        </span>
      )}
    </div>
  );
}

function ToolNodeMeta({
  toolNode,
  className = "mt-2 text-[11px] text-slate-500",
}: {
  toolNode: NonNullable<ReturnType<typeof extractToolNodeDiagnostics>>;
  className?: string;
}): JSX.Element | null {
  const bindingTarget = toolBindingTargetLabel(toolNode);
  const hasArguments = toolNode.argumentKeys.length > 0;
  if (!bindingTarget && !hasArguments) {
    return null;
  }
  return (
    <div className={className}>
      {bindingTarget && <span>Binding {bindingTarget}</span>}
      {bindingTarget && hasArguments && " · "}
      {hasArguments && (
        <span>{toolArgumentSummary(toolNode.argumentKeys)}</span>
      )}
    </div>
  );
}

function orchestrationTargetLabel(
  nodeId: string | null,
  nodeType: string | null,
): string | null {
  const parts = [nodeId, workflowNodeTypeLabel(nodeType)].filter(Boolean);
  return parts.length > 0 ? parts.join(" · ") : null;
}

function ForEachBadges({
  forEachNode,
  testId,
}: {
  forEachNode: NonNullable<ReturnType<typeof extractForEachDiagnostics>>;
  testId?: string;
}): JSX.Element {
  const targetLabel = orchestrationTargetLabel(
    forEachNode.targetNodeId,
    forEachNode.targetNodeType,
  );
  return (
    <div className="flex flex-wrap gap-1.5" data-testid={testId}>
      <span className="rounded-full border border-teal-200 bg-teal-50 px-2 py-0.5 text-[10px] font-semibold text-teal-700">
        {forEachNode.count} item{forEachNode.count === 1 ? "" : "s"}
      </span>
      {targetLabel && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          Target {targetLabel}
        </span>
      )}
      {forEachNode.failed > 0 && (
        <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">
          {forEachNode.failed} failed
        </span>
      )}
      {forEachNode.artifactCount > 0 && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          {forEachNode.artifactCount} artifacts
        </span>
      )}
    </div>
  );
}

function ForEachMeta({
  forEachNode,
  className = "mt-2 text-[11px] text-slate-500",
}: {
  forEachNode: NonNullable<ReturnType<typeof extractForEachDiagnostics>>;
  className?: string;
}): JSX.Element | null {
  const preview = forEachNode.results.map((item) => item.itemLabel).join(", ");
  const failedItem = forEachNode.results.find((item) => item.error) ?? null;
  if (!preview && !failedItem) {
    return null;
  }
  return (
    <div className={className}>
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

function JoinBadges({
  joinNode,
  testId,
}: {
  joinNode: NonNullable<ReturnType<typeof extractJoinDiagnostics>>;
  testId?: string;
}): JSX.Element {
  return (
    <div className="flex flex-wrap gap-1.5" data-testid={testId}>
      <span className="rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[10px] font-semibold text-indigo-700">
        {joinNode.branchCount} merged port
        {joinNode.branchCount === 1 ? "" : "s"}
      </span>
      {joinNode.mergedKeys.slice(0, 3).map((key) => (
        <span
          key={key}
          className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600"
        >
          {key}
        </span>
      ))}
    </div>
  );
}

function JoinMeta({
  joinNode,
  className = "mt-2 text-[11px] text-slate-500",
}: {
  joinNode: NonNullable<ReturnType<typeof extractJoinDiagnostics>>;
  className?: string;
}): JSX.Element | null {
  if (joinNode.mergedKeys.length === 0 && !joinNode.outputPreview) {
    return null;
  }
  return (
    <div className={className}>
      {joinNode.mergedKeys.length > 0 && (
        <span>Keys {joinNode.mergedKeys.join(", ")}</span>
      )}
      {joinNode.mergedKeys.length > 0 && joinNode.outputPreview && " · "}
      {joinNode.outputPreview && <span>{joinNode.outputPreview}</span>}
    </div>
  );
}

function ErrorBoundaryBadges({
  errorBoundary,
  testId,
}: {
  errorBoundary: NonNullable<
    ReturnType<typeof extractErrorBoundaryDiagnostics>
  >;
  testId?: string;
}): JSX.Element {
  const protectedLabel = orchestrationTargetLabel(
    errorBoundary.targetNodeId,
    errorBoundary.targetNodeType,
  );
  const compensationLabel = orchestrationTargetLabel(
    errorBoundary.compensationNodeId,
    errorBoundary.compensationNodeType,
  );
  return (
    <div className="flex flex-wrap gap-1.5" data-testid={testId}>
      <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">
        Handled failure
      </span>
      {protectedLabel && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          Protected {protectedLabel}
        </span>
      )}
      {compensationLabel && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          Compensation {compensationLabel}
        </span>
      )}
      {errorBoundary.artifactCount > 0 && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          {errorBoundary.artifactCount} artifacts
        </span>
      )}
    </div>
  );
}

function ErrorBoundaryMeta({
  errorBoundary,
  className = "mt-2 text-[11px] text-slate-500",
}: {
  errorBoundary: NonNullable<
    ReturnType<typeof extractErrorBoundaryDiagnostics>
  >;
  className?: string;
}): JSX.Element | null {
  if (!errorBoundary.message && !errorBoundary.compensationOutputPreview) {
    return null;
  }
  return (
    <div className={className}>
      {errorBoundary.message && <span>{errorBoundary.message}</span>}
      {errorBoundary.message &&
        errorBoundary.compensationOutputPreview &&
        " · "}
      {errorBoundary.compensationOutputPreview && (
        <span>Recovery {errorBoundary.compensationOutputPreview}</span>
      )}
    </div>
  );
}

function SubworkflowBadges({
  subworkflow,
  testId,
}: {
  subworkflow: NonNullable<ReturnType<typeof extractSubworkflowDiagnostics>>;
  testId?: string;
}): JSX.Element {
  const childStatus = subworkflowStatusLabel(subworkflow.childStatus);
  return (
    <div className="flex flex-wrap gap-1.5" data-testid={testId}>
      {subworkflow.workflowId && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          {subworkflow.workflowId}
        </span>
      )}
      {subworkflow.alias && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
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
      {subworkflow.workflowVersionId && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 font-mono text-[10px] font-semibold text-slate-600">
          {subworkflow.workflowVersionId}
        </span>
      )}
      {subworkflow.stepCount > 0 && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          {subworkflow.stepCount} child step
          {subworkflow.stepCount === 1 ? "" : "s"}
        </span>
      )}
      {subworkflow.tokens !== null && (
        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[10px] font-semibold text-slate-600">
          {subworkflow.tokens} tokens
        </span>
      )}
    </div>
  );
}

function SubworkflowMeta({
  subworkflow,
  className = "mt-2 text-[11px] text-slate-500",
}: {
  subworkflow: NonNullable<ReturnType<typeof extractSubworkflowDiagnostics>>;
  className?: string;
}): JSX.Element | null {
  const path =
    subworkflow.steps.length > 0
      ? `Path ${subworkflow.steps.join(" -> ")}`
      : null;
  const version =
    subworkflow.workflowVersionNumber !== null
      ? `Version v${subworkflow.workflowVersionNumber}`
      : null;
  const output = subworkflow.outputPreview
    ? `Output ${subworkflow.outputPreview}`
    : null;
  const error = subworkflow.error ? `Failure ${subworkflow.error}` : null;
  const parts = [path, version, output, error].filter(Boolean);
  if (parts.length === 0) return null;
  return <div className={className}>{parts.join(" · ")}</div>;
}

export function WorkflowRunDebugger({
  manifest,
  run,
  events = [],
  checkpoints = [],
  focusedNodeId = null,
  onSelectNodeId,
}: WorkflowRunDebuggerProps): JSX.Element {
  const summarySteps = useMemo(() => deriveSummarySteps(run), [run]);
  const eventSteps = useMemo(() => deriveEventSteps(events), [events]);
  const steps =
    eventSteps.length > summarySteps.length
      ? eventSteps.map((entry) => entry.step)
      : summarySteps;
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

  useEffect(() => {
    if (steps.length === 0) {
      setSelectedKey(null);
      return;
    }
    const nextKey = steps[steps.length - 1]?.selectionKey ?? null;
    setSelectedKey((current) => {
      if (current && steps.some((step) => step.selectionKey === current))
        return current;
      return nextKey;
    });
  }, [steps]);

  useEffect(() => {
    if (!focusedNodeId) return;
    const matchingStep =
      [...steps].reverse().find((step) => step.node_id === focusedNodeId) ??
      null;
    if (!matchingStep) return;
    setSelectedKey((current) =>
      current === matchingStep.selectionKey
        ? current
        : matchingStep.selectionKey,
    );
  }, [focusedNodeId, steps]);

  const selectedStep = useMemo(
    () =>
      steps.find((step) => step.selectionKey === selectedKey) ??
      steps[steps.length - 1] ??
      null,
    [selectedKey, steps],
  );

  const selectedStepEvent = useMemo(
    () =>
      eventSteps.find(
        (entry) => entry.step.stepIndex === selectedStep?.stepIndex,
      ) ?? null,
    [eventSteps, selectedStep],
  );
  const displayStep = selectedStepEvent?.step ?? selectedStep;
  const orderedCheckpoints = useMemo(
    () =>
      [...checkpoints].sort((left, right) => right.sequence - left.sequence),
    [checkpoints],
  );
  const activeCheckpointId = readString(run.summary?.resume_checkpoint_id);
  const activeCheckpoint = useMemo(() => {
    if (!activeCheckpointId) return null;
    return (
      orderedCheckpoints.find(
        (checkpoint) => checkpoint.checkpoint_id === activeCheckpointId,
      ) ?? null
    );
  }, [activeCheckpointId, orderedCheckpoints]);
  const stepMarkersByKey = useMemo(() => {
    const markerMap = new Map<string, DebugStepMarker[]>();
    for (const step of steps) {
      markerMap.set(
        step.selectionKey,
        deriveStepMarkers(step, {
          stepEvent:
            eventSteps.find(
              (entry) => entry.step.stepIndex === step.stepIndex,
            ) ?? null,
          eventSteps,
          events,
          orderedCheckpoints,
          activeCheckpoint,
          currentNodeId: run.current_node_id,
          runStatus: run.status,
          run,
          totalSteps: steps.length,
        }),
      );
    }
    return markerMap;
  }, [
    activeCheckpoint,
    eventSteps,
    events,
    orderedCheckpoints,
    run,
    steps,
  ]);

  const upstreamOutputs = useMemo(
    () => buildUpstreamOutputs(manifest, steps, selectedStep),
    [manifest, selectedStep, steps],
  );
  const previousStep = useMemo(() => {
    if (!selectedStep || selectedStep.stepIndex <= 0) return null;
    return steps[selectedStep.stepIndex - 1] ?? null;
  }, [selectedStep, steps]);
  const previousSameNodeStep = useMemo(() => {
    if (!selectedStep) return null;
    for (let index = selectedStep.stepIndex - 1; index >= 0; index -= 1) {
      const candidate = steps[index];
      if (candidate?.node_id === selectedStep.node_id) {
        return candidate;
      }
    }
    return null;
  }, [selectedStep, steps]);

  const relatedEvents = useMemo(() => {
    if (events.length === 0) return [];
    if (!selectedStep)
      return [...events]
        .sort((left, right) => right.sequence - left.sequence)
        .slice(0, 8);

    const selectedSequence = selectedStepEvent?.event.sequence ?? null;
    const pool = new Map<number, WorkflowRunEvent>();
    for (const event of events) {
      const sameNode = event.node_id === selectedStep.node_id;
      const nearStep =
        selectedSequence !== null &&
        Math.abs(event.sequence - selectedSequence) <= 2;
      if (sameNode || nearStep) {
        pool.set(event.event_id, event);
      }
    }
    const collected = [...pool.values()].sort(
      (left, right) => right.sequence - left.sequence,
    );
    if (collected.length > 0) return collected.slice(0, 8);
    return [...events]
      .sort((left, right) => right.sequence - left.sequence)
      .slice(0, 8);
  }, [events, selectedStep, selectedStepEvent]);

  const selectedStepMarkers = useMemo(() => {
    if (!selectedStep) return [] as DebugStepMarker[];
    return stepMarkersByKey.get(selectedStep.selectionKey) ?? [];
  }, [selectedStep, stepMarkersByKey]);
  const displayKnowledgeBuild = useMemo(
    () => (displayStep ? extractKnowledgeBuildDiagnostics(displayStep) : null),
    [displayStep],
  );
  const displayKnowledgeQuery = useMemo(
    () => (displayStep ? extractKnowledgeQueryDiagnostics(displayStep) : null),
    [displayStep],
  );
  const displayTelemetry = useMemo(
    () => (displayStep ? extractStepTelemetry(displayStep) : null),
    [displayStep],
  );
  const displayToolNode = useMemo(
    () => (displayStep ? extractToolNodeDiagnostics(displayStep) : null),
    [displayStep],
  );
  const displayForEachNode = useMemo(
    () => (displayStep ? extractForEachDiagnostics(displayStep) : null),
    [displayStep],
  );
  const displayJoinNode = useMemo(
    () => (displayStep ? extractJoinDiagnostics(displayStep) : null),
    [displayStep],
  );
  const displayErrorBoundary = useMemo(
    () => (displayStep ? extractErrorBoundaryDiagnostics(displayStep) : null),
    [displayStep],
  );
  const displaySubworkflow = useMemo(
    () => (displayStep ? extractSubworkflowDiagnostics(displayStep) : null),
    [displayStep],
  );
  const displayKnowledgeBuildStatus = knowledgeBuildStatusLabel(
    displayKnowledgeBuild?.status ?? null,
    displayKnowledgeBuild?.previewSkipped ?? false,
  );
  const displayKnowledgeBuildWaitLabel = knowledgeBuildWaitStatusLabel(
    displayKnowledgeBuild?.waitStatus ?? null,
    displayKnowledgeBuild?.waitRequested ?? false,
  );
  const displayKnowledgeBuildWaitTimeoutLabel =
    displayKnowledgeBuild?.waitTimeoutSeconds != null
      ? `${displayKnowledgeBuild.waitTimeoutSeconds}s timeout`
      : null;
  const displayKnowledgeBuildActivationLabel =
    knowledgeBuildActivationStatusLabel(
      displayKnowledgeBuild?.activationStatus ?? null,
      displayKnowledgeBuild?.activationActiveVersionId ?? null,
      displayKnowledgeBuild?.activationRequested ?? false,
    );
  const displayKnowledgeBuildGraphTarget = knowledgeGraphTargetLabel(
    displayKnowledgeBuild?.graphTarget ?? null,
  );
  const displayKnowledgeBuildRetrievalMode = retrievalModeLabel(
    displayKnowledgeBuild?.defaultRetrievalMode ?? null,
  );
  const displayKnowledgeModeLabel = retrievalModeLabel(
    displayKnowledgeQuery?.retrievalMode ?? null,
  );
  const displayKnowledgeSeedLabel = ageSeedStrategyLabel(
    displayKnowledgeQuery?.ageSeedStrategy ?? null,
  );
  const displayToolBindingLabel = toolBindingTypeLabel(
    displayToolNode?.bindingType ?? null,
  );
  const displayToolBindingTarget = toolBindingTargetLabel(displayToolNode);
  const displayToolEffectLabel = toolSideEffectLabel(
    displayToolNode?.sideEffectLevel ?? null,
  );
  const displayForEachTarget = orchestrationTargetLabel(
    displayForEachNode?.targetNodeId ?? null,
    displayForEachNode?.targetNodeType ?? null,
  );
  const displayErrorTarget = orchestrationTargetLabel(
    displayErrorBoundary?.targetNodeId ?? null,
    displayErrorBoundary?.targetNodeType ?? null,
  );
  const displayCompensationTarget = orchestrationTargetLabel(
    displayErrorBoundary?.compensationNodeId ?? null,
    displayErrorBoundary?.compensationNodeType ?? null,
  );
  const displaySubworkflowStatus = subworkflowStatusLabel(
    displaySubworkflow?.childStatus ?? null,
  );

  const transformDiff = useMemo(
    () =>
      diffPorts(
        displayStep?.input_by_port ?? null,
        displayStep?.output_by_port ?? null,
      ),
    [displayStep],
  );
  const transitionDiff = useMemo(
    () =>
      diffPorts(
        previousStep?.output_by_port ?? null,
        displayStep?.input_by_port ?? null,
      ),
    [displayStep, previousStep],
  );
  const retryDiff = useMemo(
    () =>
      previousSameNodeStep && displayStep
        ? diffPorts(
            previousSameNodeStep.output_by_port ?? null,
            displayStep.output_by_port ?? null,
          )
        : null,
    [displayStep, previousSameNodeStep],
  );

  const totalDuration = steps.reduce(
    (sum, step) => sum + (step.duration_ms ?? 0),
    0,
  );
  const totalToolCalls = steps.reduce(
    (sum, step) => sum + step.tool_calls.length,
    0,
  );

  if (steps.length === 0) {
    return (
      <div
        data-testid="workflow-run-debugger-empty"
        className="rounded-2xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-8 text-sm text-slate-400"
      >
        {emptyDebuggerMessage(events, checkpoints, run.status)}
      </div>
    );
  }

  return (
    <div
      data-testid="workflow-run-debugger"
      className="grid grid-cols-1 gap-4 xl:grid-cols-[18rem_minmax(0,1fr)]"
    >
      <div className="rounded-2xl border border-slate-200/70 bg-slate-50/70 p-3">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div>
            <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
              Step Map
            </div>
            <div className="mt-1 text-sm font-semibold text-slate-900">
              {steps.length} recorded step{steps.length === 1 ? "" : "s"}
            </div>
          </div>
          <div className="text-right text-[11px] text-slate-500">
            <div>{totalToolCalls} tool calls</div>
            <div>{totalDuration} ms total</div>
          </div>
        </div>
        <div className="space-y-2">
          {steps.map((step) => {
            const selected = step.selectionKey === selectedStep?.selectionKey;
            const stepMarkers = stepMarkersByKey.get(step.selectionKey) ?? [];
            const knowledgeBuild = extractKnowledgeBuildDiagnostics(step);
            const knowledgeQuery = extractKnowledgeQueryDiagnostics(step);
            const stepTelemetry = extractStepTelemetry(step);
            const toolNode = extractToolNodeDiagnostics(step);
            const forEachNode = extractForEachDiagnostics(step);
            const joinNode = extractJoinDiagnostics(step);
            const errorBoundary = extractErrorBoundaryDiagnostics(step);
            const subworkflowNode = extractSubworkflowDiagnostics(step);
            return (
              <button
                key={step.selectionKey}
                type="button"
                data-testid={`workflow-run-step-button-${step.stepIndex}`}
                onClick={() => {
                  setSelectedKey(step.selectionKey);
                  onSelectNodeId?.(step.node_id);
                }}
                className={`w-full rounded-2xl border px-3 py-2 text-left transition-colors ${selectedStepCardClasses(selected)}`}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate font-mono text-[12px] font-semibold text-slate-800">
                      {step.node_id}
                    </div>
                    <div className="mt-0.5 text-[11px] text-slate-500">
                      {step.node_type} {step.detail ? `· ${step.detail}` : ""}
                    </div>
                    {stepMarkers.length > 0 && (
                      <div className="mt-2">{markerChips(stepMarkers)}</div>
                    )}
                    {stepTelemetry && (
                      <div className="mt-2">
                        <StepTelemetryBadges
                          telemetry={stepTelemetry}
                          testId={`workflow-run-step-telemetry-${step.stepIndex}`}
                        />
                        <StepTelemetryMeta telemetry={stepTelemetry} />
                      </div>
                    )}
                    {toolNode && (
                      <div className="mt-2">
                        <ToolNodeBadges
                          toolNode={toolNode}
                          testId={`workflow-run-step-tool-${step.stepIndex}`}
                        />
                        <ToolNodeMeta toolNode={toolNode} />
                      </div>
                    )}
                    {forEachNode && (
                      <div className="mt-2">
                        <ForEachBadges
                          forEachNode={forEachNode}
                          testId={`workflow-run-step-for-each-${step.stepIndex}`}
                        />
                        <ForEachMeta forEachNode={forEachNode} />
                      </div>
                    )}
                    {joinNode && (
                      <div className="mt-2">
                        <JoinBadges
                          joinNode={joinNode}
                          testId={`workflow-run-step-join-${step.stepIndex}`}
                        />
                        <JoinMeta joinNode={joinNode} />
                      </div>
                    )}
                    {errorBoundary && (
                      <div className="mt-2">
                        <ErrorBoundaryBadges
                          errorBoundary={errorBoundary}
                          testId={`workflow-run-step-error-boundary-${step.stepIndex}`}
                        />
                        <ErrorBoundaryMeta errorBoundary={errorBoundary} />
                      </div>
                    )}
                    {subworkflowNode && (
                      <div className="mt-2">
                        <SubworkflowBadges
                          subworkflow={subworkflowNode}
                          testId={`workflow-run-step-subworkflow-${step.stepIndex}`}
                        />
                        <SubworkflowMeta subworkflow={subworkflowNode} />
                      </div>
                    )}
                    {knowledgeBuild && (
                      <div className="mt-2">
                        <KnowledgeBuildBadges
                          knowledgeBuild={knowledgeBuild}
                          testId={`workflow-run-step-knowledge-build-${step.stepIndex}`}
                        />
                        <KnowledgeBuildMeta knowledgeBuild={knowledgeBuild} />
                      </div>
                    )}
                    {knowledgeQuery && (
                      <div className="mt-2">
                        <KnowledgeQueryBadges
                          knowledgeQuery={knowledgeQuery}
                          testId={`workflow-run-step-knowledge-${step.stepIndex}`}
                        />
                        <KnowledgeQueryMeta knowledgeQuery={knowledgeQuery} />
                      </div>
                    )}
                  </div>
                  <span className="rounded-full bg-white/90 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500 ring-1 ring-slate-200/80">
                    {step.status}
                  </span>
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-[0.14em] text-slate-400">
                  <span>Step {step.stepIndex + 1}</span>
                  <span>{step.duration_ms ?? 0} ms</span>
                  <span>{step.tool_calls.length} tool</span>
                </div>
              </button>
            );
          })}
        </div>
      </div>

      <div className="space-y-4">
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_18rem]">
          <div
            data-testid="workflow-run-step-detail"
            className="rounded-2xl border border-slate-200/70 bg-white p-4 shadow-card"
          >
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                  Selected Step
                </div>
                <div className="mt-1 text-sm font-semibold text-slate-900">
                  {displayStep?.node_id}
                </div>
                {markerChips(selectedStepMarkers)}
              </div>
              <div className="rounded-xl bg-slate-50 px-3 py-1.5 text-[11px] text-slate-500 ring-1 ring-slate-200/70">
                {displayStep?.node_type} · {displayStep?.status}
              </div>
            </div>
            {displayStep && (
              <StepPreview step={displayStep} upstream={upstreamOutputs} />
            )}
          </div>

          <div className="rounded-2xl border border-slate-200/70 bg-white p-4 shadow-card">
            <div data-testid="workflow-run-step-snapshot">
              <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                Snapshot
              </div>
              <dl className="mt-3 space-y-3 text-sm text-slate-600">
                <div>
                  <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                    Run
                  </dt>
                  <dd className="mt-1 font-mono text-[12px] text-slate-800">
                    {run.workflow_run_id}
                  </dd>
                </div>
                <div>
                  <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                    Sequence
                  </dt>
                  <dd className="mt-1 text-slate-800">
                    {selectedStepEvent
                      ? selectedStepEvent.event.sequence
                      : selectedStep
                        ? selectedStep.stepIndex + 1
                        : "n/a"}
                  </dd>
                </div>
                <div>
                  <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                    Recorded
                  </dt>
                  <dd className="mt-1 text-slate-800">
                    {selectedStepEvent
                      ? formatEventTime(selectedStepEvent.event.created_at)
                      : "n/a"}
                  </dd>
                </div>
                <div>
                  <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                    Related events
                  </dt>
                  <dd className="mt-1 text-slate-800">
                    {relatedEvents.length}
                  </dd>
                </div>
                <div>
                  <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                    Current node
                  </dt>
                  <dd className="mt-1 font-mono text-[12px] text-slate-800">
                    {run.current_node_id ?? selectedStep?.node_id ?? "n/a"}
                  </dd>
                </div>
                {displayTelemetry?.model && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Model
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayTelemetry.model}
                    </dd>
                  </div>
                )}
                {displayTelemetry &&
                  (displayTelemetry.tokens !== null ||
                    displayTelemetry.promptTokens !== null ||
                    displayTelemetry.completionTokens !== null ||
                    displayTelemetry.cachedPromptTokens !== null) && (
                    <div>
                      <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                        Token usage
                      </dt>
                      <dd className="mt-1 text-slate-800">
                        {displayTelemetry.tokens !== null
                          ? `${displayTelemetry.tokens} total`
                          : "n/a"}
                        {displayTelemetry.promptTokens !== null
                          ? ` · ${displayTelemetry.promptTokens} prompt`
                          : ""}
                        {displayTelemetry.completionTokens !== null
                          ? ` · ${displayTelemetry.completionTokens} completion`
                          : ""}
                        {displayTelemetry.cachedPromptTokens !== null
                          ? ` · ${displayTelemetry.cachedPromptTokens} cached prompt`
                          : ""}
                      </dd>
                    </div>
                  )}
                {displayTelemetry?.costUsd != null &&
                  displayTelemetry.costUsd > 0 && (
                    <div>
                      <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                        Estimated cost
                      </dt>
                      <dd className="mt-1 text-slate-800">
                        {formatUsdEstimate(displayTelemetry.costUsd)}
                      </dd>
                    </div>
                  )}
                {displayTelemetry?.promptVersion && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Prompt version
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayTelemetry.promptVersion}
                    </dd>
                  </div>
                )}
                {displaySubworkflow?.workflowId && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Child workflow
                    </dt>
                    <dd className="mt-1 font-mono text-[12px] text-slate-800">
                      {displaySubworkflow.workflowId}
                    </dd>
                  </div>
                )}
                {displaySubworkflow?.alias && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Child alias
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displaySubworkflow.alias}
                    </dd>
                  </div>
                )}
                {displaySubworkflowStatus && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Child status
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displaySubworkflowStatus}
                    </dd>
                  </div>
                )}
                {displaySubworkflow?.workflowVersionId && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Child version
                    </dt>
                    <dd className="mt-1 font-mono text-[12px] text-slate-800">
                      {displaySubworkflow.workflowVersionId}
                      {displaySubworkflow.workflowVersionNumber !== null
                        ? ` · v${displaySubworkflow.workflowVersionNumber}`
                        : ""}
                    </dd>
                  </div>
                )}
                {displaySubworkflow?.stepCount ? (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Child steps
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displaySubworkflow.stepCount}
                    </dd>
                  </div>
                ) : null}
                {displaySubworkflow && displaySubworkflow.tokens !== null && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Child tokens
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displaySubworkflow.tokens}
                    </dd>
                  </div>
                )}
                {displayForEachTarget && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Loop target
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayForEachTarget}
                    </dd>
                  </div>
                )}
                {displayForEachNode && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Loop status
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayForEachNode.count} item
                      {displayForEachNode.count === 1 ? "" : "s"}
                      {displayForEachNode.failed > 0
                        ? ` · ${displayForEachNode.failed} failed`
                        : ""}
                    </dd>
                  </div>
                )}
                {displayJoinNode && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Merged ports
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayJoinNode.branchCount}
                    </dd>
                  </div>
                )}
                {displayErrorTarget && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Protected node
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayErrorTarget}
                    </dd>
                  </div>
                )}
                {displayCompensationTarget && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Compensation
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayCompensationTarget}
                    </dd>
                  </div>
                )}
                {displayToolNode?.localName && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Tool
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayToolNode.localName}
                    </dd>
                  </div>
                )}
                {displayToolBindingLabel && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Binding type
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayToolBindingLabel}
                    </dd>
                  </div>
                )}
                {displayToolNode?.registryRef && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Registry ref
                    </dt>
                    <dd className="mt-1 font-mono text-[12px] text-slate-800">
                      {displayToolNode.registryRef}
                    </dd>
                  </div>
                )}
                {displayToolBindingTarget && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Binding target
                    </dt>
                    <dd className="mt-1 font-mono text-[12px] text-slate-800">
                      {displayToolBindingTarget}
                    </dd>
                  </div>
                )}
                {displayToolEffectLabel && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Side effect
                    </dt>
                    <dd className="mt-1 text-slate-800 capitalize">
                      {displayToolEffectLabel}
                    </dd>
                  </div>
                )}
                {displayToolNode && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Approval
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayToolNode.requiresApproval
                        ? "Required"
                        : "Not required"}
                    </dd>
                  </div>
                )}
                {displayToolNode && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Arguments
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {toolArgumentSummary(displayToolNode.argumentKeys)}
                    </dd>
                  </div>
                )}
                {displayKnowledgeBuild?.knowledgeBaseId && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Knowledge base
                    </dt>
                    <dd className="mt-1 font-mono text-[12px] text-slate-800">
                      {displayKnowledgeBuild.knowledgeBaseId}
                    </dd>
                  </div>
                )}
                {displayKnowledgeBuildStatus && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Build status
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayKnowledgeBuildStatus}
                    </dd>
                  </div>
                )}
                {displayKnowledgeBuild?.versionId && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      KB version
                    </dt>
                    <dd className="mt-1 font-mono text-[12px] text-slate-800">
                      {displayKnowledgeBuild.versionId}
                      {displayKnowledgeBuild.versionNumber !== null
                        ? ` · v${displayKnowledgeBuild.versionNumber}`
                        : ""}
                    </dd>
                  </div>
                )}
                {displayKnowledgeBuild?.runId && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Build run
                    </dt>
                    <dd className="mt-1 font-mono text-[12px] text-slate-800">
                      {displayKnowledgeBuild.runId}
                      {displayKnowledgeBuild.runStatus
                        ? ` · ${displayKnowledgeBuild.runStatus}`
                        : ""}
                    </dd>
                  </div>
                )}
                {displayKnowledgeBuildWaitLabel && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Wait policy
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayKnowledgeBuildWaitLabel}
                      {displayKnowledgeBuildWaitTimeoutLabel
                        ? ` · ${displayKnowledgeBuildWaitTimeoutLabel}`
                        : ""}
                    </dd>
                  </div>
                )}
                {displayKnowledgeBuildActivationLabel && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Activation
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayKnowledgeBuildActivationLabel}
                      {displayKnowledgeBuild?.activeVersionId
                        ? ` · Active ${displayKnowledgeBuild.activeVersionId}`
                        : ""}
                    </dd>
                  </div>
                )}
                {displayKnowledgeBuildGraphTarget && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Graph target
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayKnowledgeBuildGraphTarget}
                    </dd>
                  </div>
                )}
                {displayKnowledgeBuildRetrievalMode && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Default retrieval
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayKnowledgeBuildRetrievalMode}
                    </dd>
                  </div>
                )}
                {displayKnowledgeBuild?.ageSyncStatus && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      AGE sync
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayKnowledgeBuild.ageSyncStatus}
                    </dd>
                  </div>
                )}
                {displayKnowledgeModeLabel && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Retrieval
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayKnowledgeModeLabel}
                    </dd>
                  </div>
                )}
                {displayKnowledgeQuery?.ageGraphName && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      AGE graph
                    </dt>
                    <dd className="mt-1 font-mono text-[12px] text-slate-800">
                      {displayKnowledgeQuery.ageGraphName}
                    </dd>
                  </div>
                )}
                {displayKnowledgeSeedLabel && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Seed strategy
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {displayKnowledgeSeedLabel}
                    </dd>
                  </div>
                )}
                {displayKnowledgeQuery?.fallbackRetrievalMode && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Fallback mode
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      {retrievalModeLabel(
                        displayKnowledgeQuery.fallbackRetrievalMode,
                      ) ?? displayKnowledgeQuery.fallbackRetrievalMode}
                    </dd>
                  </div>
                )}
                {displayKnowledgeQuery?.queryOverrideActive && (
                  <div>
                    <dt className="text-[11px] uppercase tracking-[0.14em] text-slate-400">
                      Graph overrides
                    </dt>
                    <dd className="mt-1 text-slate-800">
                      Applied at query time
                    </dd>
                  </div>
                )}
              </dl>
              {displayKnowledgeBuild && (
                <div
                  data-testid="workflow-run-step-snapshot-knowledge-build"
                  className="mt-4 border-t border-slate-100 pt-3"
                >
                  <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                    Knowledge Build
                  </div>
                  <div className="mt-2">
                    <KnowledgeBuildBadges
                      knowledgeBuild={displayKnowledgeBuild}
                    />
                    <KnowledgeBuildMeta
                      knowledgeBuild={displayKnowledgeBuild!}
                      className="mt-2 text-[11px] text-slate-500"
                    />
                  </div>
                </div>
              )}
              {displayKnowledgeQuery && (
                <div className="mt-4 border-t border-slate-100 pt-3">
                  <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                    Knowledge Query
                  </div>
                  <div className="mt-2">
                    <KnowledgeQueryBadges
                      knowledgeQuery={displayKnowledgeQuery}
                      testId="workflow-run-step-snapshot-knowledge"
                    />
                    <KnowledgeQueryMeta
                      knowledgeQuery={displayKnowledgeQuery}
                      className="mt-2 text-[11px] text-slate-500"
                    />
                  </div>
                </div>
              )}
              {displayTelemetry && (
                <div
                  data-testid="workflow-run-step-snapshot-telemetry"
                  className="mt-4 border-t border-slate-100 pt-3"
                >
                  <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                    LLM Telemetry
                  </div>
                  <div className="mt-2">
                    <StepTelemetryBadges telemetry={displayTelemetry} />
                    <StepTelemetryMeta
                      telemetry={displayTelemetry}
                      className="mt-2 text-[11px] text-slate-500"
                    />
                  </div>
                </div>
              )}
              {displaySubworkflow && (
                <div
                  data-testid="workflow-run-step-snapshot-subworkflow"
                  className="mt-4 border-t border-slate-100 pt-3"
                >
                  <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                    Child Workflow
                  </div>
                  <div className="mt-2">
                    <SubworkflowBadges subworkflow={displaySubworkflow} />
                    <SubworkflowMeta
                      subworkflow={displaySubworkflow}
                      className="mt-2 text-[11px] text-slate-500"
                    />
                  </div>
                </div>
              )}
              {displayForEachNode && (
                <div className="mt-4 border-t border-slate-100 pt-3">
                  <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                    Loop Diagnostics
                  </div>
                  <div className="mt-2">
                    <ForEachBadges
                      forEachNode={displayForEachNode}
                      testId="workflow-run-step-snapshot-for-each"
                    />
                    <ForEachMeta
                      forEachNode={displayForEachNode}
                      className="mt-2 text-[11px] text-slate-500"
                    />
                  </div>
                </div>
              )}
              {displayJoinNode && (
                <div className="mt-4 border-t border-slate-100 pt-3">
                  <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                    Join Diagnostics
                  </div>
                  <div className="mt-2">
                    <JoinBadges
                      joinNode={displayJoinNode}
                      testId="workflow-run-step-snapshot-join"
                    />
                    <JoinMeta
                      joinNode={displayJoinNode}
                      className="mt-2 text-[11px] text-slate-500"
                    />
                  </div>
                </div>
              )}
              {displayErrorBoundary && (
                <div className="mt-4 border-t border-slate-100 pt-3">
                  <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                    Error Recovery
                  </div>
                  <div className="mt-2">
                    <ErrorBoundaryBadges
                      errorBoundary={displayErrorBoundary}
                      testId="workflow-run-step-snapshot-error-boundary"
                    />
                    <ErrorBoundaryMeta
                      errorBoundary={displayErrorBoundary}
                      className="mt-2 text-[11px] text-slate-500"
                    />
                  </div>
                </div>
              )}
              {displayToolNode && (
                <div className="mt-4 border-t border-slate-100 pt-3">
                  <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                    Tool Binding
                  </div>
                  <div className="mt-2">
                    <ToolNodeBadges
                      toolNode={displayToolNode}
                      testId="workflow-run-step-snapshot-tool"
                    />
                    <ToolNodeMeta
                      toolNode={displayToolNode}
                      className="mt-2 text-[11px] text-slate-500"
                    />
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>

        {displayStep && (
          <div
            data-testid="workflow-run-step-diffs"
            className="grid grid-cols-1 gap-4 xl:grid-cols-2"
          >
            <DiffCard
              testId="workflow-run-step-diff-transition"
              title="Step Transition"
              subtitle={transitionSubtitle(previousStep, displayStep, run.status)}
              diff={transitionDiff}
              emptyMessage={transitionEmptyMessage(previousStep, run.status)}
            />
            <DiffCard
              testId="workflow-run-step-diff-transform"
              title="Node Transformation"
              subtitle={`${displayStep.node_id} input -> output`}
              diff={transformDiff}
            />
            {retryDiff && previousSameNodeStep && (
              <div className="xl:col-span-2">
                <DiffCard
                  testId="workflow-run-step-diff-retry"
                  title="Attempt Delta"
                  subtitle={`Previous ${displayStep.node_id} attempt (step ${previousSameNodeStep.stepIndex + 1}) -> current output`}
                  diff={retryDiff}
                />
              </div>
            )}
          </div>
        )}

        {displayStep && (
          <div
            data-testid="workflow-run-step-ports"
            className="rounded-2xl border border-slate-200/70 bg-white p-4 shadow-card"
          >
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                  Ports & Context
                </div>
                <div className="mt-1 text-sm font-semibold text-slate-900">
                  Port-level execution snapshot
                </div>
              </div>
              <div className="flex flex-wrap gap-2 text-[11px] text-slate-500">
                <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 font-semibold">
                  In · {portSummary(displayStep.input_by_port, run.status)}
                </span>
                <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 font-semibold">
                  Out · {portSummary(displayStep.output_by_port, run.status)}
                </span>
              </div>
            </div>
            <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
              <div>
                <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                  Input Ports
                </div>
                {hasPortSnapshot(displayStep.input_by_port) ? (
                  <pre
                    data-testid="workflow-run-step-input-ports"
                    className="max-h-64 overflow-auto rounded-2xl bg-slate-950 px-3 py-3 font-mono text-[11px] leading-relaxed text-slate-100"
                  >
                    {formatJson(displayStep.input_by_port)}
                  </pre>
                ) : (
                  <div
                    data-testid="workflow-run-step-input-ports-empty"
                    className="rounded-2xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-4 text-xs leading-relaxed text-slate-500"
                  >
                    {emptyPortSnapshotMessage("Input port snapshot", run.status)}
                  </div>
                )}
              </div>
              <div>
                <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                  Output Ports
                </div>
                {hasPortSnapshot(displayStep.output_by_port) ? (
                  <pre
                    data-testid="workflow-run-step-output-ports"
                    className="max-h-64 overflow-auto rounded-2xl bg-slate-950 px-3 py-3 font-mono text-[11px] leading-relaxed text-slate-100"
                  >
                    {formatJson(displayStep.output_by_port)}
                  </pre>
                ) : (
                  <div
                    data-testid="workflow-run-step-output-ports-empty"
                    className="rounded-2xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-4 text-xs leading-relaxed text-slate-500"
                  >
                    {emptyPortSnapshotMessage("Output port snapshot", run.status)}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}

        {displayStep && displayStep.tool_calls.length > 0 && (
          <div className="rounded-2xl border border-slate-200/70 bg-white p-4 shadow-card">
            <div className="mb-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
              Tool Calls
            </div>
            <pre
              data-testid="workflow-run-step-tools"
              className="max-h-64 overflow-auto rounded-2xl bg-slate-950 px-3 py-3 font-mono text-[11px] leading-relaxed text-slate-100"
            >
              {formatJson(displayStep.tool_calls)}
            </pre>
          </div>
        )}

        <div
          data-testid="workflow-run-event-timeline"
          className="rounded-2xl border border-slate-200/70 bg-white p-4 shadow-card"
        >
          <div className="mb-3 flex items-center justify-between gap-3">
            <div>
              <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                Event Timeline
              </div>
              <div className="mt-1 text-sm font-semibold text-slate-900">
                Events around the selected step
              </div>
            </div>
            <div className="text-[11px] text-slate-500">
              {events.length} total event{events.length === 1 ? "" : "s"}
            </div>
          </div>

          {relatedEvents.length === 0 ? (
            <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-6 text-sm text-slate-400">
              {emptyEventTimelineMessage({
                hasSelectedStep: Boolean(displayStep),
                hasCheckpoint: checkpoints.length > 0,
                runStatus: run.status,
              })}
            </div>
          ) : (
            <div className="space-y-2">
              {relatedEvents.map((event) => {
                const selectedEvent =
                  event.event_id === selectedStepEvent?.event.event_id;
                const payload = isRecord(event.payload) ? event.payload : null;
                const eventStep =
                  event.event_type === "workflow.run.step"
                    ? coerceStep(payload?.step, Math.max(event.sequence - 1, 0))
                    : null;
                const eventForEachNode = eventStep
                  ? extractForEachDiagnostics(eventStep)
                  : null;
                const eventJoinNode = eventStep
                  ? extractJoinDiagnostics(eventStep)
                  : null;
                const eventErrorBoundary = eventStep
                  ? extractErrorBoundaryDiagnostics(eventStep)
                  : null;
                const eventKnowledgeBuild = eventStep
                  ? extractKnowledgeBuildDiagnostics(eventStep)
                  : null;
                const eventKnowledgeQuery = eventStep
                  ? extractKnowledgeQueryDiagnostics(eventStep)
                  : null;
                const eventTelemetry = eventStep
                  ? extractStepTelemetry(eventStep)
                  : null;
                const eventToolNode = eventStep
                  ? extractToolNodeDiagnostics(eventStep)
                  : null;
                const eventSubworkflow = eventStep
                  ? extractSubworkflowDiagnostics(eventStep)
                  : null;
                return (
                  <details
                    key={event.event_id}
                    open={selectedEvent}
                    className={`rounded-2xl border px-3 py-2 ${selectedEvent ? "border-sky-300 bg-sky-50/60" : "border-slate-200 bg-slate-50/70"}`}
                  >
                    <summary className="cursor-pointer list-none">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex min-w-0 flex-wrap items-center gap-2 text-sm text-slate-800">
                          <span className="rounded-full bg-white px-2 py-0.5 font-mono text-[11px] ring-1 ring-slate-200/80">
                            {event.sequence}
                          </span>
                          <span className="font-medium">
                            {event.event_type}
                          </span>
                          {event.node_id && (
                            <span className="truncate font-mono text-[12px] text-slate-500">
                              {event.node_id}
                            </span>
                          )}
                        </div>
                        <div className="text-[11px] text-slate-500">
                          {formatEventTime(event.created_at)}
                        </div>
                      </div>
                      <div className="mt-1 text-[11px] text-slate-500">
                        {summarizeLifecycleEvent(event, checkpoints)}
                      </div>
                      {eventTelemetry && (
                        <div className="mt-2">
                          <StepTelemetryBadges
                            telemetry={eventTelemetry}
                            testId={`workflow-run-event-telemetry-${event.sequence}`}
                          />
                          <StepTelemetryMeta
                            telemetry={eventTelemetry}
                            className="mt-2 text-[11px] text-slate-500"
                          />
                        </div>
                      )}
                      {eventKnowledgeBuild && (
                        <div className="mt-2">
                          <KnowledgeBuildBadges
                            knowledgeBuild={eventKnowledgeBuild}
                            testId={`workflow-run-event-knowledge-build-${event.sequence}`}
                          />
                          <KnowledgeBuildMeta
                            knowledgeBuild={eventKnowledgeBuild}
                            className="mt-2 text-[11px] text-slate-500"
                          />
                        </div>
                      )}
                      {eventKnowledgeQuery && (
                        <div className="mt-2">
                          <KnowledgeQueryBadges
                            knowledgeQuery={eventKnowledgeQuery}
                            testId={`workflow-run-event-knowledge-${event.sequence}`}
                          />
                          <KnowledgeQueryMeta
                            knowledgeQuery={eventKnowledgeQuery}
                            className="mt-2 text-[11px] text-slate-500"
                          />
                        </div>
                      )}
                      {eventToolNode && (
                        <div className="mt-2">
                          <ToolNodeBadges
                            toolNode={eventToolNode}
                            testId={`workflow-run-event-tool-${event.sequence}`}
                          />
                          <ToolNodeMeta
                            toolNode={eventToolNode}
                            className="mt-2 text-[11px] text-slate-500"
                          />
                        </div>
                      )}
                      {eventForEachNode && (
                        <div className="mt-2">
                          <ForEachBadges
                            forEachNode={eventForEachNode}
                            testId={`workflow-run-event-for-each-${event.sequence}`}
                          />
                          <ForEachMeta
                            forEachNode={eventForEachNode}
                            className="mt-2 text-[11px] text-slate-500"
                          />
                        </div>
                      )}
                      {eventJoinNode && (
                        <div className="mt-2">
                          <JoinBadges
                            joinNode={eventJoinNode}
                            testId={`workflow-run-event-join-${event.sequence}`}
                          />
                          <JoinMeta
                            joinNode={eventJoinNode}
                            className="mt-2 text-[11px] text-slate-500"
                          />
                        </div>
                      )}
                      {eventErrorBoundary && (
                        <div className="mt-2">
                          <ErrorBoundaryBadges
                            errorBoundary={eventErrorBoundary}
                            testId={`workflow-run-event-error-boundary-${event.sequence}`}
                          />
                          <ErrorBoundaryMeta
                            errorBoundary={eventErrorBoundary}
                            className="mt-2 text-[11px] text-slate-500"
                          />
                        </div>
                      )}
                      {eventSubworkflow && (
                        <div className="mt-2">
                          <SubworkflowBadges
                            subworkflow={eventSubworkflow}
                            testId={`workflow-run-event-subworkflow-${event.sequence}`}
                          />
                          <SubworkflowMeta
                            subworkflow={eventSubworkflow}
                            className="mt-2 text-[11px] text-slate-500"
                          />
                        </div>
                      )}
                    </summary>
                    <pre className="mt-2 max-h-64 overflow-auto rounded-2xl bg-slate-950 px-3 py-3 font-mono text-[11px] leading-relaxed text-slate-100">
                      {formatJson(event.payload ?? {})}
                    </pre>
                  </details>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
