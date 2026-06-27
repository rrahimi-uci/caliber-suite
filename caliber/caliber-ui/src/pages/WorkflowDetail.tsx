/**
 * Workflow Detail — professional tabs over Graph preview, Versions, Deployments,
 * Runs, Promotions, and CALIBER Patches for one workflow.
 */

import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ApiError, caliberApi } from "@/api/caliberApi";
import type {
  StartTriggerConfig,
  WorkflowCalibrationObjective,
  WorkflowCalibrationRunResult,
  WorkflowComponent,
  Workflow,
  WorkflowRunManifest,
  WorkflowRunCapabilities,
  WorkflowRun,
  WorkflowRunCheckpoint,
  WorkflowRunEvent,
  WorkflowRunHistoryStats,
  WorkflowRunLineage,
  WorkflowRuntimeApproval,
  WorkflowService,
  WorkflowSessionMemoryEntry,
  WorkflowManifest,
  WorkflowRunStep,
  WorkflowRunResult,
  WorkflowVersion,
} from "@/api/workflowTypes";
import { SearchInput } from "@/components/SearchInput";
import { Canvas } from "@/components/workflows/Canvas";
import { GraphDiff } from "@/components/workflows/GraphDiff";
import { NodeDetailPanel } from "@/components/workflows/NodeDetailPanel";
import { RunFilePanel } from "@/components/workflows/RunFilePanel";
import { WorkflowRunArtifactPersistenceBadge } from "@/components/workflows/WorkflowRunArtifactPersistenceBadge";
import { WorkflowRunLineagePanel } from "@/components/workflows/WorkflowRunLineagePanel";
import { WorkflowRunTracePanel } from "@/components/workflows/WorkflowRunTracePanel";
import { WorkflowRunRecoveryPanel } from "@/components/workflows/WorkflowRunRecoveryPanel";
import { TraceReplayGraph } from "@/components/workflows/TraceReplayGraph";
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
import { useApiMutation, useApiQuery, useInvalidate } from "@/hooks/useApiQuery";
import { useEventStream } from "@/hooks/useEventStream";
import { buildMlflowHref } from "@/lib/externalLinks";
import { SINGLE_ENVIRONMENT } from "@/lib/environment";
import { workflowCalibrationView } from "@/lib/workflowCalibration";
import { showToast } from "@/lib/toast";
import {
  approvalCheckpointKind,
  workflowRunApprovalNoun,
  workflowRunApprovalSubject,
  workflowRunLifecycleMessage,
  workflowRunPendingApprovalChipLabel,
  workflowRunStatusLabel,
  workflowRunStatusMessage,
  workflowRunStatusFromStep,
  workflowRunStatusPhrase,
  workflowRunStatusRingClass,
} from "@/lib/workflowRunLabels";
import { workflowRunPath, workflowRunUrl } from "@/lib/workflowRunLinks";
import {
  buildSyntheticWorkflowRunManifest,
  mergeWorkflowRunCheckpoints,
  resolveWorkflowRunActiveCheckpoint,
  workflowRunArtifactPersistence,
  workflowRunHasInheritedResumeCheckpoint,
  workflowRunResumeCheckpointId,
  workflowRunResumeCheckpointRunId,
} from "@/lib/workflowRunSummary";

type Tab = "graph" | "versions" | "deployments" | "runs" | "service" | "promotions" | "patches";
type RunArtifactTriageFilter = "all" | "upload_failed" | "artifacts_stored";

function normalizeWorkflowDetailTab(value: string | null): Tab | null {
  switch (value) {
    case "versions":
    case "deployments":
    case "runs":
    case "service":
    case "promotions":
    case "patches":
    case "graph":
      return value;
    default:
      return null;
  }
}

const ALL_TABS: Array<{ id: Tab; label: string; icon: string }> = [
  { id: "graph", label: "Graph", icon: "M4 5a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1V5zm10 10a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4zm-4-4l4 4" },
  { id: "versions", label: "Versions", icon: "M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" },
  { id: "deployments", label: "Deployments", icon: "M5 12h14M12 5l7 7-7 7" },
  { id: "runs", label: "Runs", icon: "M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664zM21 12a9 9 0 11-18 0 9 9 0 0118 0z" },
  { id: "service", label: "Service", icon: "M13 10V3L4 14h7v7l9-11h-7z" },
  { id: "promotions", label: "Promotions", icon: "M5 10l7-7m0 0l7 7m-7-7v18" },
  { id: "patches", label: "CALIBER Patches", icon: "M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" },
];

// In single-environment mode the dev→staging→prod ladder is hidden: the
// Deployments and Promotions tabs (alias rotation + promotion-approval queue)
// drop out of the nav. The tab content code stays so flipping SINGLE_ENVIRONMENT
// back to false restores them. ?tab=deployments URLs still resolve harmlessly.
const HIDDEN_TABS_SINGLE_ENV: ReadonlySet<Tab> = new Set(["deployments", "promotions"]);
const TABS: Array<{ id: Tab; label: string; icon: string }> = SINGLE_ENVIRONMENT
  ? ALL_TABS.filter((tab) => !HIDDEN_TABS_SINGLE_ENV.has(tab.id))
  : ALL_TABS;

const STATUS_BADGE: Record<string, string> = {
  active: "bg-emerald-50 text-emerald-700 ring-emerald-200/60",
  approved: "bg-emerald-50 text-emerald-700 ring-emerald-200/60",
  completed: "bg-emerald-50 text-emerald-700 ring-emerald-200/60",
  deprecated: "bg-slate-100 text-slate-500 ring-slate-200/60",
  draft: "bg-amber-50 text-amber-700 ring-amber-200/60",
  failed: "bg-red-50 text-red-600 ring-red-200/60",
  paused: "bg-amber-50 text-amber-700 ring-amber-200/60",
  pending: "bg-violet-50 text-violet-700 ring-violet-200/60",
  published: "bg-emerald-50 text-emerald-700 ring-emerald-200/60",
  rejected: "bg-red-50 text-red-600 ring-red-200/60",
  running: "bg-sky-50 text-sky-700 ring-sky-200/60",
};

const WORKFLOW_RUN_EVENTS = [
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
  "workflow.deleted",
  "workflow.paused",
  "workflow.resumed",
  "workflow.updated",
];

const RUN_TELEMETRY_REFRESH_INTERVAL_MS = 2000;
const RUN_HISTORY_PAGE_SIZE = 50;
const RUN_TERMINAL_STATUSES = new Set([
  "completed",
  "failed",
  "cancelled",
  "rejected",
  "expired",
]);

function EmptyState({ icon, title, desc }: { icon: string; title: string; desc: string }) {
  return (
    <div className="rounded-2xl border-2 border-dashed border-slate-200 bg-gradient-to-br from-slate-50/80 to-white px-8 py-14 text-center">
      <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-white shadow-card">
        <svg className="h-7 w-7 text-slate-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d={icon} />
        </svg>
      </div>
      <div className="text-sm font-semibold text-slate-600">{title}</div>
      <div className="mx-auto mt-1.5 max-w-xs text-xs leading-relaxed text-slate-400">{desc}</div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const cls = STATUS_BADGE[status] ?? "bg-slate-100 text-slate-500 ring-slate-200/60";
  return (
    <span className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[10px] font-semibold ring-1 ${cls}`}>
      {status}
    </span>
  );
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
    return `Retry failed because this run's stored checkpoint or manifest snapshot is no longer healthy. Inspect the recovery, checkpoint, lineage, and debugger panels before retrying from a different checkpoint or starting a new attempt. Latest backend detail: ${detail}`;
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
    || normalized.includes("runtime approval")
      && normalized.includes("is not pending")
  ) {
    return `${action} failed because no pending runtime approval is attached to this run anymore. Refresh approval history and inspect recovery diagnostics to confirm whether another operator already resolved it. Latest backend detail: ${detail}`;
  }
  if (normalized.includes("is not waiting for approval")) {
    return `${action} failed because this run is no longer paused for approval. Refresh the run history and recovery panels before trying again. Latest backend detail: ${detail}`;
  }
  if (
    normalized.includes("approval checkpoint")
    || normalized.includes("runtime approval")
      && normalized.includes("not found for workflow run")
  ) {
    return `${action} failed because this paused approval state is no longer healthy. Refresh approval history and inspect the recovery, checkpoint, and debugger panels before trying again. Latest backend detail: ${detail}`;
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
    return `Resume failed because the paused approval gate is no longer in a resumable state. Refresh approval history and inspect the recovery, checkpoint, and debugger panels before trying again. Latest backend detail: ${detail}`;
  }
  if (
    normalized.includes("has no resume checkpoint")
    || normalized.includes("resume checkpoint")
    || normalized.includes("approval checkpoint")
    || normalized.includes("is not resumable from")
  ) {
    return `Resume failed because this paused run is no longer resumable from its stored checkpoint. Inspect the recovery, checkpoint, lineage, and debugger panels before retrying from a healthy checkpoint or starting a new attempt. Latest backend detail: ${detail}`;
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
    return `External event resume failed because no safe waiting run could be selected for this event. Inspect the recovery, checkpoint, and lineage panels, then resume the target run directly or add the required event correlation before retrying. Latest backend detail: ${detail}`;
  }
  return null;
}

function RunStatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[10px] font-semibold ring-1 ${workflowRunStatusRingClass(status)}`}
    >
      {workflowRunStatusLabel(status)}
    </span>
  );
}

function runArtifactFilterMatches(
  run: WorkflowRun,
  filter: RunArtifactTriageFilter,
): boolean {
  const artifactPersistence = workflowRunArtifactPersistence(run);
  if (filter === "upload_failed") {
    return artifactPersistence?.status === "failed";
  }
  if (filter === "artifacts_stored") {
    return artifactPersistence?.status === "persisted";
  }
  return true;
}

function runArtifactFilterQueryValue(
  filter: RunArtifactTriageFilter,
): "failed" | "persisted" | undefined {
  if (filter === "upload_failed") return "failed";
  if (filter === "artifacts_stored") return "persisted";
  return undefined;
}

function runSearchMatches(run: WorkflowRun, query: string): boolean {
  if (!query) return true;
  const artifactPersistence = workflowRunArtifactPersistence(run);
  const summary =
    run.summary && typeof run.summary === "object" && !Array.isArray(run.summary)
      ? (run.summary as Record<string, unknown>)
      : null;
  const searchable = [
    run.workflow_run_id,
    run.trace_id,
    run.workflow_version_id,
    run.deployment_alias,
    run.status,
    run.error_summary,
    typeof summary?.error === "string" ? summary.error : null,
    artifactPersistence?.bucket ?? null,
    artifactPersistence?.status === "failed"
      ? "artifact upload failed"
      : artifactPersistence?.status === "persisted"
        ? "artifacts stored"
        : null,
    artifactPersistence?.error ?? null,
    ...(artifactPersistence?.artifact_names ?? []),
  ].filter((value): value is string => typeof value === "string" && value.trim().length > 0);
  return searchable.some((value) => value.toLowerCase().includes(query));
}

function filteredRunHistoryMessage({
  search,
  filter,
}: {
  search: string;
  filter: RunArtifactTriageFilter;
}): string {
  const quotedSearch = search ? `“${search}”` : null;
  if (search && filter === "upload_failed") {
    return `No runs match ${quotedSearch} within the artifact upload failed view. Clear the search or switch back to all runs to widen this history.`;
  }
  if (search && filter === "artifacts_stored") {
    return `No runs match ${quotedSearch} within the stored artifacts view. Clear the search or switch back to all runs to widen this history.`;
  }
  if (search) {
    return `No runs match ${quotedSearch}. Search by run id, trace id, status, version, bucket, or artifact name to find a recorded execution.`;
  }
  if (filter === "upload_failed") {
    return "No runs currently have recorded artifact upload failures. Failed post-run object-store uploads will appear here as they are recorded.";
  }
  if (filter === "artifacts_stored") {
    return "No runs currently report persisted artifacts in object storage. Runs that successfully write artifacts or logs back to the object store will appear here.";
  }
  return "No runs match the current triage view. Clear the filters to inspect the full run history again.";
}

function readNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function TriggerBadge({ trigger }: { trigger: StartTriggerConfig | null | undefined }) {
  const mode = trigger?.mode ?? "manual";
  if (mode === "manual") {
    return (
      <span className="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[10px] font-semibold ring-1 bg-slate-100 text-slate-500 ring-slate-200/60">
        ⏷ Manual
      </span>
    );
  }
  const cls =
    mode === "event"
      ? "bg-violet-50 text-caliber-purple ring-violet-200/70"
      : "bg-sky-50 text-sky-700 ring-sky-200/70";
  const label =
    mode === "event"
      ? `Event${trigger?.event_name ? `: ${trigger.event_name}` : ""}`
      : `Cron: ${trigger?.cron ?? ""}`;
  const disabled = trigger?.enabled === false;
  return (
    <span
      data-testid="workflow-trigger-badge"
      title={disabled ? "Trigger is disabled" : undefined}
      className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[10px] font-semibold ring-1 ${cls} ${disabled ? "opacity-50" : ""}`}
    >
      {mode === "event" ? "⚡" : "🕑"} {label}
      {disabled ? " (off)" : ""}
    </span>
  );
}

function parseJsonObjectText(raw: string): unknown {
  const trimmed = raw.trim();
  if (!trimmed) return undefined;
  return JSON.parse(trimmed);
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

function waitUntilDisplay(node: WorkflowVersion["manifest"]["nodes"][string] | null): string {
  if (!node || node.type !== "wait_until") return "the configured time";
  const raw = typeof node.wait_until === "string" && node.wait_until.trim() ? node.wait_until.trim() : "the configured time";
  const needsTimezone = !/[zZ]$/.test(raw) && !/[+-]\d{2}:\d{2}$/.test(raw);
  const timezoneName = typeof node.timezone === "string" && node.timezone.trim() ? node.timezone.trim() : "";
  return needsTimezone && timezoneName ? `${raw} (${timezoneName})` : raw;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
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
  if (!isRecord(payload) || !workflowRunResumeCorrelationMatches(payload[key], correlationValue)) {
    return `Event matching is unavailable because this wait gate requires correlation field ${key}=${workflowRunResumeCorrelationValueLabel(correlationValue)} in the event payload. Add that field/value before matching this event against waiting runs.`;
  }
  return null;
}

function workflowRunGraphUnavailableMessage({
  manifestMode,
  versionReference,
  versionLoadFailed,
  hasSummarySteps,
  hasCurrentNode,
  hasCheckpoints,
}: {
  manifestMode: string | null;
  versionReference: string;
  versionLoadFailed: boolean;
  hasSummarySteps: boolean;
  hasCurrentNode: boolean;
  hasCheckpoints: boolean;
}): string {
  const recoverySuffix =
    hasCheckpoints || hasSummarySteps || hasCurrentNode
      ? " Use the checkpoint, recovery, and retry-lineage panels below to keep tracing the persisted execution evidence until the graph can be restored."
      : " Use the recovery and retry-lineage panels below to confirm whether any persisted execution evidence still remains for this run.";
  if (manifestMode === "snapshot") {
    return `The draft snapshot captured for this run is not available. Graph replay and manifest-aware debugging are hidden until that snapshot can be restored.${recoverySuffix}`;
  }
  if (versionLoadFailed) {
    return `${versionReference} could not be loaded for replay and debugging. Restore that version row or its persisted manifest to inspect this run again.${recoverySuffix}`;
  }
  return `${versionReference} is not available in the loaded workflow versions. Graph replay and manifest-aware debugging are hidden until that version is restored.${recoverySuffix}`;
}

function syntheticRunGraphFallbackMessage(run: WorkflowRun): string {
  const base =
    "Replay and debugging are using a graph reconstructed from recorded run history and checkpoints because neither the persisted run manifest nor its saved workflow version could be restored.";
  if (run.status === "waiting_approval" || run.status === "waiting_event") {
    return `${base} Use this reconstructed graph to follow the active gate, but rely on the recovery and checkpoint panels for authoritative resume state until a full manifest can be restored.`;
  }
  if (
    run.status === "queued"
    || run.status === "running"
    || run.status === "resuming"
    || run.status === "cancel_requested"
  ) {
    return `${base} Use this reconstructed graph as a best-effort replay while execution is still in flight, and confirm live state with the debugger, recovery, and checkpoint panels.`;
  }
  if (run.status === "completed") {
    return `${base} Use this reconstructed graph as a best-effort replay, then confirm the terminal result with the debugger, final outputs, and generated artifacts.`;
  }
  if (
    run.status === "failed"
    || run.status === "cancelled"
    || run.status === "rejected"
    || run.status === "expired"
    || run.status === "blocked"
  ) {
    return `${base} Use this reconstructed graph as a best-effort replay, then rely on the debugger, recovery diagnostics, and retry lineage to trace where execution stopped.`;
  }
  return `${base} Use the recovery, checkpoint, and debugger panels to confirm any execution details that the reconstructed graph cannot prove on its own.`;
}

function savedVersionRunGraphFallbackMessage(
  run: WorkflowRun,
  versionReference: string,
): string {
  const base =
    `Replay and debugging are using ${versionReference} because the persisted run manifest could not be loaded separately.`;
  if (run.status === "waiting_approval" || run.status === "waiting_event") {
    return `${base} Use that restored version as the last known workflow graph, but rely on the recovery and checkpoint panels for authoritative resume state until the persisted manifest can be restored.`;
  }
  if (
    run.status === "queued"
    || run.status === "running"
    || run.status === "resuming"
    || run.status === "cancel_requested"
  ) {
    return `${base} Use that restored version as a best-effort replay while execution is still in flight, and confirm live state with the debugger, recovery, and checkpoint panels.`;
  }
  if (run.status === "completed") {
    return `${base} Use that restored version as the last known workflow graph, then confirm the terminal result with the debugger, final outputs, and generated artifacts.`;
  }
  if (
    run.status === "failed"
    || run.status === "cancelled"
    || run.status === "rejected"
    || run.status === "expired"
    || run.status === "blocked"
  ) {
    return `${base} Use that restored version as the last known workflow graph, then rely on the debugger, recovery diagnostics, and retry lineage to trace where execution stopped.`;
  }
  return `${base} Use the recovery, checkpoint, and debugger panels to confirm any execution details that may have diverged from the restored graph.`;
}

function emptyRunHistoryMessage({
  capabilitiesUnavailable,
  queueRunsEnabled,
  workflowStatus,
  runAlias,
  runVersionId,
}: {
  capabilitiesUnavailable: boolean;
  queueRunsEnabled: boolean;
  workflowStatus: string | null;
  runAlias: string;
  runVersionId: string | null;
}): string {
  if (capabilitiesUnavailable) {
    return "Workflow run capabilities could not be loaded for this deployment. Verify the CALIBER API and workflow-run settings, then refresh this page before launching the first execution.";
  }
  if (!queueRunsEnabled) {
    return "This deployment has workflow execution disabled. Enable the run queue, then launch the first execution from the run controls above or a deployment-triggered event.";
  }
  if (workflowStatus === "paused") {
    return "This workflow is paused, so no new runs can start yet. Resume it first, then queue the first execution from the run controls above or a deployment-triggered launch.";
  }
  if (workflowStatus === "archived") {
    return "This workflow is archived, so new runs are disabled. Restore it first if you want to create the first execution history for this workflow.";
  }
  if (!runVersionId) {
    return runAlias === "manual"
      ? "No published workflow version is available yet. Publish a version first, then queue the first execution from the run controls above."
      : `Alias ${runAlias} is not deployed yet. Promote a version to that alias, then launch the first execution from this page or an external trigger.`;
  }
  return "Queue a run from the controls above or trigger the workflow from a deployed alias to create the first execution. Once a run starts, this tab will show status, replay, checkpoints, lineage, and debugger history.";
}

function emptyRunLogsMessage(run: WorkflowRun): string {
  if (run.status === "waiting_approval" || run.status === "waiting_event") {
    return "This run only has a node path summary so far. Use the recovery, checkpoint, and debugger panels above to inspect the active gate until step-level logs are persisted.";
  }
  if (
    run.status === "queued"
    || run.status === "running"
    || run.status === "resuming"
    || run.status === "cancel_requested"
  ) {
    return "This run only has a node path summary so far. Execution may still be in flight, so use the debugger and recovery panels above while step-level logs are still catching up.";
  }
  if (run.status === "completed") {
    return "This run completed without persisted step logs. Use the debugger, final outputs, and generated artifacts above to reconstruct how execution finished.";
  }
  if (
    run.status === "failed"
    || run.status === "cancelled"
    || run.status === "rejected"
    || run.status === "expired"
    || run.status === "blocked"
  ) {
    return "This run stopped before persisted step logs were recorded. Use the debugger and recovery panels above to trace where execution failed or was interrupted.";
  }
  return "This run only has a node path summary.";
}

function defaultSelectedRunNodeId(
  run: WorkflowRun | null,
  manifest: WorkflowManifest | null,
): string | null {
  if (!run || !manifest) return null;
  const candidates: unknown[] = [run.current_node_id];
  const summarySteps = Array.isArray(run.summary?.steps) ? run.summary.steps : [];
  for (let index = summarySteps.length - 1; index >= 0; index -= 1) {
    const step = summarySteps[index];
    if (step && typeof step === "object" && "node_id" in step) {
      candidates.push((step as { node_id?: unknown }).node_id);
    }
  }
  const path = Array.isArray(run.summary?.node_path) ? run.summary.node_path : [];
  for (let index = path.length - 1; index >= 0; index -= 1) {
    candidates.push(path[index]);
  }
  for (const candidate of candidates) {
    if (typeof candidate === "string" && manifest.nodes[candidate]) {
      return candidate;
    }
  }
  return null;
}

function StepLogList({ steps, emptyText }: { steps: WorkflowRunStep[]; emptyText: string }) {
  if (steps.length === 0) {
    return <div className="rounded-xl border border-slate-100 bg-slate-50 px-3 py-2 text-xs text-slate-400">{emptyText}</div>;
  }
  return (
    <div className="space-y-2" data-testid="workflow-run-logs">
      {steps.map((step, index) => (
        <div key={`${step.node_id}-${index}`} className="rounded-xl border border-slate-100 bg-slate-50 px-3 py-2">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="font-mono font-semibold text-slate-700">{step.node_id}</span>
            <span className="rounded-md bg-white px-1.5 py-0.5 text-[10px] font-semibold text-slate-500 ring-1 ring-slate-200/70">
              {step.node_type}
            </span>
            <StatusBadge status={step.status} />
            {step.detail && <span className="text-slate-400">{step.detail}</span>}
          </div>
          {step.output && (
            <div className="mt-1.5 max-h-24 overflow-auto rounded-lg bg-white px-2 py-1.5 font-mono text-[11px] leading-relaxed text-slate-500">
              {step.output}
            </div>
          )}
          {step.tool_calls.length > 0 && (
            <div className="mt-1.5 rounded-lg bg-white px-2 py-1.5 font-mono text-[11px] text-slate-500">
              {JSON.stringify(step.tool_calls, null, 2)}
            </div>
          )}
        </div>
      ))}
    </div>
  );
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

const CALIBRATION_RUN_STATUS_BADGE: Record<string, string> = {
  queued: "bg-sky-50 text-sky-700 ring-sky-200/60",
  running: "bg-sky-50 text-sky-700 ring-sky-200/60",
  candidate_ready: "bg-amber-50 text-amber-700 ring-amber-200/60",
  applied: "bg-emerald-50 text-emerald-700 ring-emerald-200/60",
  completed: "bg-emerald-50 text-emerald-700 ring-emerald-200/60",
  failed: "bg-red-50 text-red-600 ring-red-200/60",
  rejected: "bg-red-50 text-red-600 ring-red-200/60",
  cancelled: "bg-slate-100 text-slate-500 ring-slate-200/60",
};

/** Best-effort one-line eval summary from a refinement job's eval_results. */
function calibrationRunEvalSummary(job: WorkflowCalibrationRunResult["job"]): string | null {
  const view = workflowCalibrationView(job);
  if (view) {
    const parts: string[] = [];
    if (view.winnerId) parts.push(`winner ${view.winnerId}`);
    if (view.gatePassed === true) parts.push("gate passed");
    else if (view.gatePassed === false) parts.push("gate blocked");
    if (view.nExamples !== null) parts.push(`${view.nExamples} examples`);
    if (parts.length > 0) return parts.join(" · ");
  }
  const results = job.eval_results;
  if (results && typeof results === "object" && !Array.isArray(results)) {
    const score = (results as Record<string, unknown>).aggregate_score;
    if (typeof score === "number" && Number.isFinite(score)) {
      return `aggregate score ${score.toFixed(3)}`;
    }
  }
  return null;
}

/**
 * This workflow's calibration runs (refinement jobs), rendered inline with the
 * workflow detail surface. A `candidate_ready` run exposes an Apply button
 * that promotes its candidate via POST /jobs/{id}/apply.
 */
function WorkflowCalibrationRunsList({
  runs,
  loading,
  error,
  onApply,
  applyingJobId,
}: {
  runs: WorkflowCalibrationRunResult["job"][];
  loading: boolean;
  error: ApiError | null;
  onApply: (jobId: string) => void;
  applyingJobId: string | null;
}): JSX.Element {
  return (
    <div className="mt-4" data-testid="workflow-calibration-runs">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
        Calibration runs
      </div>
      {error ? (
        <div className="rounded-xl border border-red-100 bg-red-50 px-3 py-2 text-xs text-red-600">
          Failed to load calibration runs: {error.message}
        </div>
      ) : loading && runs.length === 0 ? (
        <div className="rounded-xl border border-slate-100 bg-slate-50 px-3 py-2 text-xs text-slate-400">
          Loading calibration runs…
        </div>
      ) : runs.length === 0 ? (
        <div className="rounded-xl border border-slate-100 bg-slate-50 px-3 py-2 text-xs text-slate-400">
          No calibration runs yet for this workflow. Start one above.
        </div>
      ) : (
        <ul className="space-y-2">
          {runs.map((run) => {
            const badge =
              CALIBRATION_RUN_STATUS_BADGE[run.status] ??
              "bg-slate-100 text-slate-500 ring-slate-200/60";
            const summary = calibrationRunEvalSummary(run);
            const isApplying = applyingJobId === run.job_id;
            return (
              <li
                key={run.job_id}
                data-testid="workflow-calibration-run"
                className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-slate-100 bg-slate-50 px-3 py-2"
              >
                <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs">
                  <span className="font-mono font-semibold text-slate-700">{run.job_id}</span>
                  <span
                    className={`inline-flex items-center rounded-md px-2 py-0.5 text-[10px] font-semibold ring-1 ${badge}`}
                  >
                    {run.status.replace(/_/g, " ")}
                  </span>
                  {summary && <span className="text-slate-500">{summary}</span>}
                </div>
                {run.status === "candidate_ready" && (
                  <button
                    type="button"
                    data-testid="job-apply-btn"
                    disabled={isApplying}
                    onClick={() => onApply(run.job_id)}
                    className="btn-primary px-3 py-1 text-xs disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {isApplying ? "Applying…" : "Apply"}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export function WorkflowDetail(): JSX.Element {
  const { workflowId } = useParams<{ workflowId: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = normalizeWorkflowDetailTab(searchParams.get("tab"));
  const requestedRunId = searchParams.get("run")?.trim() || null;
  const [tab, setTab] = useState<Tab>(() =>
    requestedRunId ? "runs" : requestedTab ?? "graph",
  );
  const invalidate = useInvalidate();
  const queryClient = useQueryClient();
  const [workflowStatusOverride, setWorkflowStatusOverride] = useState<Workflow["status"] | null>(
    null,
  );

  const wfQuery = useApiQuery(["workflow", workflowId], (s) => caliberApi.getWorkflow(workflowId!, s));
  const capabilitiesQuery = useApiQuery(["capabilities"], (s) => caliberApi.getCapabilities(s));
  const workflowComponentsQuery = useApiQuery(
    ["workflow-components"],
    (s) => caliberApi.listWorkflowComponents(s),
  );
  const versionsQuery = useApiQuery(["workflow-versions", workflowId], (s) =>
    caliberApi.listWorkflowVersions(workflowId!, s),
  );
  const deploymentsQuery = useApiQuery(["workflow-deployments", workflowId], (s) =>
    caliberApi.listWorkflowDeployments(workflowId!, s),
  );
  const promotionsQuery = useApiQuery(["workflow-promotions", workflowId], (s) =>
    caliberApi.listWorkflowPromotions(workflowId!, s),
  );
  const patchesQuery = useApiQuery(["workflow-patches", workflowId], (s) =>
    caliberApi.listWorkflowPatches(workflowId!, s),
  );
  const serviceQuery = useApiQuery(
    ["workflow-service", workflowId],
    (s) => caliberApi.getWorkflowService(workflowId!, s),
    { enabled: Boolean(workflowId) && tab === "service" },
  );
  const runsQuery = useApiQuery(["workflow-runs", workflowId], (s) =>
    caliberApi.listWorkflowRuns(workflowId!, s),
  );
  const requestedRunQuery = useApiQuery(
    ["workflow-run-deeplink", requestedRunId],
    (s) => caliberApi.getWorkflowRun(requestedRunId!, s),
    {
      enabled: Boolean(requestedRunId),
    },
  );
  const agentsQuery = useApiQuery(["agents"], (s) => caliberApi.listAgents(s));
  const calibrationOptionsQuery = useApiQuery(
    ["workflow-calibration-options", workflowId],
    (s) => caliberApi.getWorkflowCalibrationOptions(workflowId!, s),
    { enabled: Boolean(workflowId) },
  );
  // Calibration runs are refinement jobs scoped to this workflow and surfaced
  // inline here.
  const calibrationJobsQuery = useApiQuery(
    ["workflow-calibration-jobs", workflowId],
    (s) => caliberApi.listJobs({ workflow_id: workflowId! }, s),
    { enabled: Boolean(workflowId) },
  );
  const [selectedRun, setSelectedRun] = useState<WorkflowRun | null>(null);
  const [runSearch, setRunSearch] = useState("");
  const [runArtifactFilter, setRunArtifactFilter] =
    useState<RunArtifactTriageFilter>("all");
  const deferredRunSearch = useDeferredValue(runSearch);
  const normalizedRunSearch = deferredRunSearch.trim().toLowerCase();
  const runHistoryArtifactFilter = runArtifactFilterQueryValue(runArtifactFilter);
  const selectedRunRef = useRef<{
    runId: string | null;
    sessionId: string | null;
  }>({
    runId: null,
    sessionId: null,
  });
  const selectedRunApprovalsQuery = useApiQuery<WorkflowRuntimeApproval[]>(
    ["workflow-run-approvals", selectedRun?.workflow_run_id],
    (s) => caliberApi.listWorkflowRunApprovals(selectedRun!.workflow_run_id, s),
    {
      enabled: Boolean(
        selectedRun?.workflow_run_id &&
          capabilitiesQuery.data?.workflow_runs.runtime_approvals_enabled,
      ),
      refetchInterval:
        selectedRun?.workflow_run_id &&
        !RUN_TERMINAL_STATUSES.has(selectedRun.status)
          ? RUN_TELEMETRY_REFRESH_INTERVAL_MS
          : false,
    },
  );
  const selectedRunEventsQuery = useApiQuery<WorkflowRunEvent[]>(
    ["workflow-run-events", selectedRun?.workflow_run_id],
    (s) => caliberApi.listWorkflowRunEvents(selectedRun!.workflow_run_id, { limit: 200 }, s),
    {
      enabled: Boolean(selectedRun?.workflow_run_id),
      refetchInterval:
        selectedRun?.workflow_run_id &&
        !RUN_TERMINAL_STATUSES.has(selectedRun.status)
          ? RUN_TELEMETRY_REFRESH_INTERVAL_MS
          : false,
    },
  );
  const selectedRunManifestQuery = useApiQuery<WorkflowRunManifest>(
    ["workflow-run-manifest", selectedRun?.workflow_run_id],
    (s) => caliberApi.getWorkflowRunManifest(selectedRun!.workflow_run_id, s),
    {
      enabled: Boolean(selectedRun?.workflow_run_id),
    },
  );
  const selectedRunLineageQuery = useApiQuery<WorkflowRunLineage>(
    ["workflow-run-lineage", selectedRun?.workflow_run_id],
    (s) => caliberApi.getWorkflowRunLineage(selectedRun!.workflow_run_id, s),
    {
      enabled: Boolean(selectedRun?.workflow_run_id),
      refetchInterval:
        selectedRun?.workflow_run_id &&
        !RUN_TERMINAL_STATUSES.has(selectedRun.status)
          ? RUN_TELEMETRY_REFRESH_INTERVAL_MS
          : false,
    },
  );
  const selectedRunCheckpointsQuery = useApiQuery<WorkflowRunCheckpoint[]>(
    ["workflow-run-checkpoints", selectedRun?.workflow_run_id],
    (s) => caliberApi.listWorkflowRunCheckpoints(selectedRun!.workflow_run_id, { limit: 100 }, s),
    {
      enabled: Boolean(selectedRun?.workflow_run_id),
      refetchInterval:
        selectedRun?.workflow_run_id &&
        !RUN_TERMINAL_STATUSES.has(selectedRun.status)
          ? RUN_TELEMETRY_REFRESH_INTERVAL_MS
          : false,
    },
  );
  const runsTabHistoryQuery = useInfiniteQuery({
    queryKey: [
      "workflow-runs",
      workflowId,
      "runs-tab",
      normalizedRunSearch,
      runHistoryArtifactFilter ?? "all",
    ],
    queryFn: ({ pageParam, signal }) =>
      caliberApi.listWorkflowRunsPage(
        workflowId!,
        {
          search: normalizedRunSearch || undefined,
          artifactPersistence: runHistoryArtifactFilter,
          limit: RUN_HISTORY_PAGE_SIZE,
          cursor: typeof pageParam === "string" ? pageParam : null,
        },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: Boolean(workflowId) && tab === "runs",
  });
  const runHistoryStatsQuery = useApiQuery<WorkflowRunHistoryStats>(
    [
      "workflow-run-history-stats",
      workflowId,
      normalizedRunSearch,
      runHistoryArtifactFilter ?? "all",
    ],
    (s) =>
      caliberApi.getWorkflowRunHistoryStats(
        workflowId!,
        {
          search: normalizedRunSearch || undefined,
          artifactPersistence: runHistoryArtifactFilter,
        },
        s,
      ),
    {
      enabled: Boolean(workflowId),
    },
  );
  const selectedRunResumeCheckpointId = workflowRunResumeCheckpointId(selectedRun);
  const selectedRunResumeSourceRunId = workflowRunResumeCheckpointRunId(selectedRun);
  const selectedRunHasInheritedResumeSource = workflowRunHasInheritedResumeCheckpoint(selectedRun);
  const selectedRunResumeSourceCheckpointsQuery = useApiQuery<WorkflowRunCheckpoint[]>(
    ["workflow-run-source-checkpoints", selectedRun?.workflow_run_id, selectedRunResumeSourceRunId],
    (s) =>
      caliberApi.listWorkflowRunCheckpoints(
        selectedRunResumeSourceRunId!,
        { limit: 1000 },
        s,
      ),
    {
      enabled: Boolean(selectedRunHasInheritedResumeSource && selectedRunResumeSourceRunId),
    },
  );
  const selectedRunSessionMemoryQuery = useApiQuery<WorkflowSessionMemoryEntry[]>(
    ["workflow-session-memory", workflowId, selectedRun?.session_id ?? null],
    (s) => caliberApi.listWorkflowSessionMemory(workflowId!, selectedRun!.session_id!, {}, s),
    {
      enabled: Boolean(workflowId && selectedRun?.session_id),
      refetchInterval:
        selectedRun?.session_id &&
        selectedRun?.workflow_run_id &&
        !RUN_TERMINAL_STATUSES.has(selectedRun.status)
          ? RUN_TELEMETRY_REFRESH_INTERVAL_MS
          : false,
    },
  );
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedRunNodeId, setSelectedRunNodeId] = useState<string | null>(null);
  const [selectedRunNodePinned, setSelectedRunNodePinned] = useState(false);
  const selectedRunNodeIdRef = useRef<string | null>(null);
  const selectedRunNodePinnedRef = useRef(false);
  const [showCalibration, setShowCalibration] = useState(false);
  const [calibrationAgentId, setCalibrationAgentId] = useState("");
  const [calibrationObjective, setCalibrationObjective] =
    useState<WorkflowCalibrationObjective>("quality");
  const [calibrationEpsilon, setCalibrationEpsilon] = useState(0.02);
  const [calibrationMaxCandidates, setCalibrationMaxCandidates] = useState(3);
  const [calibrationUseJudge, setCalibrationUseJudge] = useState(false);
  const [lastCalibrationRun, setLastCalibrationRun] =
    useState<WorkflowCalibrationRunResult | null>(null);
  const [runInput, setRunInput] = useState("What should this workflow do for the customer?");
  const [runSessionId, setRunSessionId] = useState("");
  const [runAlias, setRunAlias] = useState("manual");
  const [liveSteps, setLiveSteps] = useState<WorkflowRunStep[]>([]);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [runMessage, setRunMessage] = useState<string | null>(null);
  const activeRunIdRef = useRef<string | null>(null);
  const lastUrlTabSyncKeyRef = useRef(`${location.pathname}?${location.search}`);
  const [resumeEventName, setResumeEventName] = useState("");
  const [resumeEventPayload, setResumeEventPayload] = useState("");
  const [deployAlias, setDeployAlias] = useState("dev");
  const [deployVersionId, setDeployVersionId] = useState("");
  const [deploymentMessage, setDeploymentMessage] = useState<string | null>(null);
  const [serviceMessage, setServiceMessage] = useState<string | null>(null);
  const [serviceCopied, setServiceCopied] = useState<null | "endpoint" | "curl">(null);
  const workflowRunEvent = useEventStream(WORKFLOW_RUN_EVENTS);
  const previousSelectedRunIdRef = useRef<string | null>(null);
  selectedRunRef.current = {
    runId: selectedRun?.workflow_run_id ?? null,
    sessionId: selectedRun?.session_id ?? null,
  };
  activeRunIdRef.current = activeRunId;
  selectedRunNodeIdRef.current = selectedRunNodeId;
  selectedRunNodePinnedRef.current = selectedRunNodePinned;

  const focusSelectedRunNode = useCallback(
    (nodeId: string | null, options: { pinned?: boolean } = {}) => {
      setSelectedRunNodeId(nodeId);
      setSelectedRunNodePinned(options.pinned ?? true);
    },
    [],
  );
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
  const copyServiceText = useCallback(
    (kind: "endpoint" | "curl", text: string): void => {
      if (!navigator.clipboard?.writeText) {
        showToast.error("Clipboard access is unavailable in this browser.");
        return;
      }
      void navigator.clipboard.writeText(text).then(
        () => {
          setServiceCopied(kind);
          showToast.success("Copied to clipboard.");
        },
        () => showToast.error("Failed to copy to clipboard."),
      );
    },
    [],
  );

  const approveMut = useApiMutation((id: string) => caliberApi.approveWorkflowPromotion(id), {
    onSuccess: () => {
      invalidate(["workflow-promotions", workflowId]);
      invalidate(["workflow-deployments", workflowId]);
    },
  });
  const rejectMut = useApiMutation((id: string) => caliberApi.rejectWorkflowPromotion(id), {
    onSuccess: () => invalidate(["workflow-promotions", workflowId]),
  });
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
      onError: (error) => {
        setRunMessage(`Session memory clear failed: ${error.message}`);
      },
    },
  );
  const calibrationMut = useApiMutation(
    (payload: {
      agentId: string;
      objective: WorkflowCalibrationObjective;
      epsilon: number;
      maxCandidates: number;
      useJudge: boolean;
    }) =>
      caliberApi.createWorkflowCalibrationRun(workflowId!, {
        agent_id: payload.agentId,
        objective: { maximize: payload.objective, epsilon: payload.epsilon },
        budget: {
          max_candidates: payload.maxCandidates,
          max_eval_examples: calibrationOptionsQuery.data?.default_budget.max_eval_examples ?? 20,
          min_examples: calibrationOptionsQuery.data?.default_budget.min_examples ?? 2,
        },
        judge: { enabled: payload.useJudge },
      }),
    {
      onSuccess: (result) => {
        setLastCalibrationRun(result);
        invalidate(["workflow-patches", workflowId]);
        invalidate(["workflow-calibration-jobs", workflowId]);
      },
    },
  );

  const applyJobMut = useApiMutation((jobId: string) => caliberApi.applyJob(jobId), {
    onSuccess: (result) => {
      setRunMessage(`Applied candidate from ${result.job_id} (status: ${result.status}).`);
      invalidate(["workflow-calibration-jobs", workflowId]);
      invalidate(["workflow-patches", workflowId]);
      invalidate(["workflow-versions", workflowId]);
      invalidate(["workflow-deployments", workflowId]);
    },
    onError: (error) => {
      setRunMessage(`Apply failed: ${error.message}`);
    },
  });

  const pauseMut = useApiMutation(
    (status: "active" | "paused") => caliberApi.updateWorkflow(workflowId!, { status }),
    {
      onSuccess: () => {
        invalidate(["workflow", workflowId]);
        invalidate(["workflows"]);
      },
    },
  );

  const publishServiceMut = useApiMutation<WorkflowService, void>(
    () => caliberApi.publishWorkflowService(workflowId!, {}),
    {
      onSuccess: () => {
        setServiceMessage("Published as a callable service.");
        invalidate(["workflow-service", workflowId]);
      },
      onError: (error) => {
        setServiceMessage(`Publish failed: ${error.message}`);
      },
    },
  );
  const unpublishServiceMut = useApiMutation<{ status: string }, void>(
    () => caliberApi.unpublishWorkflowService(workflowId!),
    {
      onSuccess: () => {
        setServiceMessage("Service unpublished.");
        invalidate(["workflow-service", workflowId]);
      },
      onError: (error) => {
        setServiceMessage(`Unpublish failed: ${error.message}`);
      },
    },
  );

  useEffect(() => {
    if (calibrationAgentId) return;
    const enabledAgent = (agentsQuery.data ?? []).find((agent) => agent.enabled);
    if (enabledAgent) setCalibrationAgentId(enabledAgent.agent_id);
  }, [agentsQuery.data, calibrationAgentId]);

  const versions = useMemo(() => versionsQuery.data ?? [], [versionsQuery.data]);
  const latest: WorkflowVersion | undefined =
    versions.find((v) => v.status === "published") ?? versions[0];
  const draft = versions.find((v) => v.status === "draft");
  const deployments = useMemo(() => deploymentsQuery.data ?? [], [deploymentsQuery.data]);
  const hasLiveDeployment = deployments.length > 0;
  const publishedVersions = versions.filter((v) => v.status === "published");
  const versionById = useMemo(
    () => new Map(versions.map((version) => [version.version_id, version])),
    [versions],
  );
  const selectedRunWorkflowVersionId = selectedRun?.workflow_version_id ?? null;
  const selectedRunVersionFromList = selectedRunWorkflowVersionId
    ? versionById.get(selectedRunWorkflowVersionId) ?? null
    : null;
  const selectedRunVersionQuery = useApiQuery<WorkflowVersion>(
    ["workflow-version", selectedRunWorkflowVersionId],
    (s) => caliberApi.getWorkflowVersion(selectedRunWorkflowVersionId!, s),
    {
      enabled: Boolean(selectedRunWorkflowVersionId && !selectedRunVersionFromList),
    },
  );
  const workflowComponentMap = useMemo(
    () =>
      new Map(
        (workflowComponentsQuery.data?.components ?? []).map((component) => [
          component.type,
          component,
        ]),
      ) as Map<WorkflowComponent["type"], WorkflowComponent>,
    [workflowComponentsQuery.data],
  );
  const runAliasOptions = useMemo(() => {
    const options: Array<{ alias: string; label: string }> = [
      { alias: "manual", label: "manual (latest version)" },
    ];
    for (const deployment of deployments) {
      const version = versionById.get(deployment.version_id);
      const label = version
        ? `${deployment.alias} (v${version.version_number})`
        : `${deployment.alias} (${deployment.version_id.slice(0, 10)})`;
      options.push({ alias: deployment.alias, label });
    }
    return options;
  }, [deployments, versionById]);
  const selectedDeployment = deployments.find((deployment) => deployment.alias === runAlias) ?? null;
  const runVersionId = runAlias === "manual" ? latest?.version_id ?? null : selectedDeployment?.version_id ?? null;
  const runVersion = runVersionId ? versionById.get(runVersionId) ?? null : null;
  const missingAliasDeployment = runAlias !== "manual" && selectedDeployment === null;
  const runCapabilities: WorkflowRunCapabilities | null =
    capabilitiesQuery.data?.workflow_runs ?? null;
  const workflowRunCapabilitiesUnavailableMessage =
    "Workflow run capabilities could not be loaded. Refresh the page or verify deployment settings/API health.";
  const queueRunsEnabled = Boolean(runCapabilities?.queue_enabled);
  const workflowStatus = workflowStatusOverride ?? wfQuery.data?.status ?? null;
  const triggerDisabledReason =
    capabilitiesQuery.isLoading
      ? "Loading workflow run capabilities"
      : capabilitiesQuery.isError
        ? workflowRunCapabilitiesUnavailableMessage
      : !queueRunsEnabled
      ? "Enable the run queue to trigger runs"
      : workflowStatus === "paused"
        ? "Resume this workflow before triggering runs"
        : workflowStatus === "archived"
          ? "Archived workflows cannot be triggered"
          : null;
  const runStartDisabledReason =
    capabilitiesQuery.isLoading
      ? "Loading workflow run capabilities"
      : capabilitiesQuery.isError
        ? workflowRunCapabilitiesUnavailableMessage
      : !queueRunsEnabled
        ? "Enable the run queue to execute workflows"
      : !runVersionId
        ? runAlias === "manual"
          ? "No published version is available to run"
          : "Selected alias is not deployed"
        : workflowStatus === "paused"
          ? "Resume this workflow before running it"
          : workflowStatus === "archived"
            ? "Archived workflows cannot be run"
            : null;

  useEffect(() => {
    setWorkflowStatusOverride(null);
  }, [workflowId]);

  useEffect(() => {
    if (wfQuery.data?.status) {
      setWorkflowStatusOverride(wfQuery.data.status);
    }
  }, [wfQuery.data?.status]);

  useEffect(() => {
    if (deployVersionId) return;
    const firstPublished = versions.find((version) => version.status === "published");
    if (firstPublished) setDeployVersionId(firstPublished.version_id);
  }, [deployVersionId, versions]);

  useEffect(() => {
    if (runAlias === "manual") return;
    if (!deployments.some((deployment) => deployment.alias === runAlias)) {
      setRunAlias("manual");
    }
  }, [deployments, runAlias]);

  const deployMut = useApiMutation(
    () => {
      const alias = deployAlias.trim().toLowerCase();
      if (!alias) throw new Error("Alias is required.");
      if (!deployVersionId) throw new Error("Select a published version to deploy.");
      return caliberApi.promoteWorkflow(workflowId!, alias, deployVersionId);
    },
    {
      onSuccess: (result) => {
        const alias = deployAlias.trim().toLowerCase();
        if (result.rotated) {
          setDeploymentMessage(`Alias ${alias} now points to ${deployVersionId}.`);
          setRunAlias(alias);
          setTab("deployments");
        } else {
          const pendingId = result.promotion?.promotion_id ?? "pending";
          setDeploymentMessage(
            `Promotion request ${pendingId} submitted for alias ${alias}. Awaiting approval.`,
          );
          setTab("promotions");
        }
        invalidate(["workflow-deployments", workflowId]);
        invalidate(["workflow-promotions", workflowId]);
      },
      onError: (error) => {
        setDeploymentMessage(`Deployment failed: ${error.message}`);
      },
    },
  );

  const runMut = useApiMutation<WorkflowRun | WorkflowRunResult, void>(
    () => {
      if (!runVersionId) {
        if (runAlias === "manual") {
          throw new Error("No workflow version is available to run.");
        }
        throw new Error(`No deployment found for alias ${runAlias}.`);
      }
      if (queueRunsEnabled) {
        if (runAlias === "manual") {
          return caliberApi.createWorkflowRun({
            workflow_version_id: runVersionId,
            alias: runAlias,
            input: runInput,
            session_id: runSessionId.trim() || undefined,
            source: "manual",
          });
        }
        return caliberApi.createWorkflowRun({
          workflow_id: workflowId!,
          alias: runAlias,
          input: runInput,
          session_id: runSessionId.trim() || undefined,
          source: "manual",
        });
      }
      return caliberApi.runWorkflowVersion(
        runVersionId,
        runInput,
        runSessionId.trim() || undefined,
        runAlias,
      );
    },
    {
      onSuccess: (result) => {
        setTab("runs");
        setActiveRunId(result.workflow_run_id);
        focusLaunchedRun(result);
        if ("steps" in result && Array.isArray(result.steps)) {
          setLiveSteps(result.steps);
          setRunMessage(workflowRunStatusMessage(result.workflow_run_id, result.status));
        } else {
          setLiveSteps([]);
          setRunMessage(workflowRunStatusMessage(result.workflow_run_id, result.status));
        }
        refreshRunHistoryQueries();
      },
      onError: (error) => {
        setRunMessage(`Run failed: ${error.message}`);
      },
    },
  );

  // Aliases whose deployed version has an *event* Start trigger — these get a
  // "Trigger now" button in the Deployments tab so an operator can fire the
  // event from the UI instead of hand-crafting the POST.
  const eventTriggerAliases = useMemo(() => {
    const aliases = new Set<string>();
    for (const deployment of deployments) {
      const version = versionById.get(deployment.version_id);
      const nodes = version?.manifest?.nodes ?? {};
      const start = Object.values(nodes).find((node) => node.type === "start");
      const trigger = start?.trigger;
      const triggerAlias =
        trigger && typeof trigger.alias === "string" && trigger.alias.trim()
          ? trigger.alias.trim()
          : "prod";
      if (
        trigger
        && trigger.mode === "event"
        && trigger.enabled !== false
        && triggerAlias === deployment.alias
      ) {
        aliases.add(deployment.alias);
      }
    }
    return aliases;
  }, [deployments, versionById]);
  const showTriggerCapabilityNote = eventTriggerAliases.size > 0 && Boolean(triggerDisabledReason);

  // Start trigger of the latest version, shown as a header badge.
  const headerTrigger = useMemo<StartTriggerConfig | null>(() => {
    const nodes = latest?.manifest?.nodes ?? {};
    const start = Object.values(nodes).find((node) => node.type === "start");
    return (start?.trigger as StartTriggerConfig | null | undefined) ?? null;
  }, [latest]);

  const [triggerMessage, setTriggerMessage] = useState<string | null>(null);
  const triggerMut = useApiMutation<WorkflowRun, string>(
    (alias) => caliberApi.triggerWorkflowEvent(workflowId!, { alias }),
    {
      onSuccess: (run, alias) => {
        setTriggerMessage(
          `Triggered run ${run.workflow_run_id} on ${run.deployment_alias} (${workflowRunStatusPhrase(run.status)}).`,
        );
        setTab("runs");
        handleRunActionSuccess(
          run,
          `Run ${run.workflow_run_id} triggered on ${run.deployment_alias ?? alias} (${workflowRunStatusPhrase(run.status)}).`,
        );
      },
      onError: (error) => setTriggerMessage(`Trigger failed: ${error.message}`),
    },
  );

  const refreshRunHistoryQueries = useCallback((): void => {
    for (const queryKey of [
      ["workflow-runs", workflowId],
      ["workflow-run-history-stats", workflowId],
    ] as const) {
      void invalidate(queryKey);
      void queryClient.refetchQueries({ queryKey, type: "active" });
    }
  }, [invalidate, queryClient, workflowId]);

  const refreshRunDiagnostics = useCallback(
    (
      runId: string,
      options: {
        refreshApprovals?: boolean;
        refreshEvents?: boolean;
        refreshCheckpoints?: boolean;
        refreshSessionMemory?: boolean;
        sessionId?: string | null;
      } = {},
    ): void => {
      if (options.refreshApprovals) {
        void invalidate(["workflow-run-approvals", runId]);
      }
      if (options.refreshEvents !== false) {
        void invalidate(["workflow-run-events", runId]);
      }
      if (options.refreshCheckpoints) {
        void invalidate(["workflow-run-checkpoints", runId]);
      }
      if (options.refreshSessionMemory && workflowId && options.sessionId) {
        void invalidate(["workflow-session-memory", workflowId, options.sessionId]);
      }
    },
    [invalidate, workflowId],
  );

  const buildProvisionalRunFromResult = useCallback(
    (result: WorkflowRunResult): WorkflowRun => ({
      workflow_run_id: result.workflow_run_id,
      workflow_id: workflowId ?? "",
      project_id: null,
      tenant_id: null,
      workflow_version_id: runVersionId,
      deployment_alias: runAlias === "manual" ? null : runAlias,
      mlflow_run_id: null,
      trace_id: null,
      session_id: runSessionId.trim() || null,
      status: result.status,
      source: "manual",
      priority: null,
      queued_at: null,
      started_at: null,
      completed_at: new Date().toISOString(),
      current_node_id: result.steps[result.steps.length - 1]?.node_id ?? null,
      summary: {
        output: result.output,
        tokens: result.tokens,
        error: result.error,
        preview: result.preview,
        status: result.status,
        manifest_mode: "saved_version",
        manifest_hash: runVersion?.manifest_hash,
        workflow_version_number: runVersion?.version_number,
        node_path: result.steps.map((step) => step.node_id),
        steps: result.steps,
        tags: result.tags,
        guardrail_results: result.guardrail_results,
      },
    }),
    [
      runAlias,
      runSessionId,
      runVersion?.manifest_hash,
      runVersion?.version_number,
      runVersionId,
      workflowId,
    ],
  );

  const focusLaunchedRun = useCallback(
    (result: WorkflowRun | WorkflowRunResult): void => {
      if ("summary" in result) {
        setSelectedRun(result);
        return;
      }
      void caliberApi
        .getWorkflowRun(result.workflow_run_id)
        .then((run) => {
          setSelectedRun(run);
        })
        .catch(() => {
          setSelectedRun(buildProvisionalRunFromResult(result));
        });
    },
    [buildProvisionalRunFromResult],
  );

  const handleRunActionSuccess = useCallback(
    (run: WorkflowRun, message: string): void => {
      setSelectedRun(run);
      setActiveRunId(run.workflow_run_id);
      setLiveSteps([]);
      setRunMessage(message);
      refreshRunHistoryQueries();
      refreshRunDiagnostics(run.workflow_run_id, {
        refreshApprovals: true,
        refreshCheckpoints: true,
        refreshSessionMemory: Boolean(run.session_id),
        sessionId: run.session_id,
      });
    },
    [refreshRunDiagnostics, refreshRunHistoryQueries],
  );

  const cancelRunMut = useApiMutation(
    (runId: string) => caliberApi.cancelWorkflowRun(runId),
    {
      onSuccess: (run) => {
        handleRunActionSuccess(run, workflowRunStatusMessage(run.workflow_run_id, run.status));
      },
      onError: (error) => {
        setRunMessage(`Cancel failed: ${runActionErrorDetail(error)}`);
      },
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
        handleRunActionSuccess(run, `Retry${scope} queued as ${run.workflow_run_id}.`);
      },
      onError: (error) => {
        setRunMessage(
          workflowRunRetryFailureMessage(error) ?? `Retry failed: ${runActionErrorDetail(error)}`,
        );
      },
    },
  );

  const approveRunMut = useApiMutation(
    (payload: { runId: string; runtimeApprovalId?: string }) =>
      caliberApi.approveWorkflowRunApproval(payload.runId, {
        runtime_approval_id: payload.runtimeApprovalId,
      }),
    {
      onSuccess: (run) => {
        handleRunActionSuccess(run, `Run ${run.workflow_run_id} approval recorded.`);
      },
      onError: (error) => {
        setRunMessage(
          workflowRunApprovalActionFailureMessage("Approve", error)
          ?? `Approve failed: ${runActionErrorDetail(error)}`,
        );
      },
    },
  );

  const rejectRunMut = useApiMutation(
    (payload: { runId: string; runtimeApprovalId?: string }) =>
      caliberApi.rejectWorkflowRunApproval(payload.runId, {
        runtime_approval_id: payload.runtimeApprovalId,
      }),
    {
      onSuccess: (run) => {
        handleRunActionSuccess(run, workflowRunStatusMessage(run.workflow_run_id, run.status));
      },
      onError: (error) => {
        setRunMessage(
          workflowRunApprovalActionFailureMessage("Reject", error)
          ?? `Reject failed: ${runActionErrorDetail(error)}`,
        );
      },
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
        handleRunActionSuccess(run, `Run ${run.workflow_run_id} resumed to queue.`);
      },
      onError: (error) => {
        setRunMessage(
          workflowRunResumeFailureMessage(error) ?? `Resume failed: ${runActionErrorDetail(error)}`,
        );
      },
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
        handleRunActionSuccess(
          run,
          `Matched event ${variables.event_name} to run ${run.workflow_run_id} and re-queued it.`,
        );
      },
      onError: (error) => {
        setRunMessage(
          workflowRunResumeByEventFailureMessage(error)
          ?? `External event resume failed: ${runActionErrorDetail(error)}`,
        );
      },
    },
  );

  useEffect(() => {
    if (!workflowRunEvent || String(workflowRunEvent.workflow_id ?? "") !== workflowId) return;
    if (workflowRunEvent.type === "workflow.deleted") {
      const workflowLabel =
        typeof workflowRunEvent.name === "string" && workflowRunEvent.name.trim()
          ? workflowRunEvent.name
          : wfQuery.data?.name ?? workflowId ?? "Workflow";
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
      invalidate(["workflow", workflowId]);
      return;
    }
    const runId = String(workflowRunEvent.workflow_run_id ?? "");
    const selectedRunId = selectedRunRef.current.runId;
    const observedRunId = selectedRunId ?? activeRunIdRef.current;
    const refreshSelectedRun = selectedRunId === runId;
    const selectedSessionId = refreshSelectedRun ? selectedRunRef.current.sessionId : null;
    if (observedRunId && runId !== observedRunId) {
      if (
        workflowRunEvent.type !== "workflow.run.step" &&
        workflowRunEvent.type !== "workflow.run.node_started"
      ) {
        refreshRunHistoryQueries();
      }
      return;
    }
    if (workflowRunEvent.type === "workflow.run.queued") {
      setTab("runs");
      setActiveRunId(runId || null);
      setLiveSteps([]);
      setRunMessage(
        workflowRunLifecycleMessage(runId, workflowRunEvent.type, workflowRunEvent),
      );
      refreshRunHistoryQueries();
      if (refreshSelectedRun) {
        refreshRunDiagnostics(runId, { sessionId: selectedSessionId });
      }
      return;
    }
    if (workflowRunEvent.type === "workflow.run.started") {
      setTab("runs");
      setActiveRunId(runId);
      setLiveSteps([]);
      setRunMessage(
        workflowRunLifecycleMessage(runId, workflowRunEvent.type, workflowRunEvent),
      );
      refreshRunHistoryQueries();
      if (refreshSelectedRun) {
        refreshRunDiagnostics(runId, { sessionId: selectedSessionId });
      }
      return;
    }
    if (workflowRunEvent.type === "workflow.run.node_started") {
      const nextNodeId =
        typeof workflowRunEvent.node_id === "string"
          ? workflowRunEvent.node_id
          : null;
      setTab("runs");
      setActiveRunId(runId || null);
      setSelectedRun((prev) => {
        if (!prev || prev.workflow_run_id !== runId) return prev;
        return {
          ...prev,
          status: "running",
          current_node_id: nextNodeId ?? prev.current_node_id,
          summary: {
            ...(prev.summary ?? {}),
            status: "running",
          },
        };
      });
      if (refreshSelectedRun && nextNodeId && !selectedRunNodePinnedRef.current) {
        setSelectedRunNodeId(nextNodeId);
      }
      return;
    }
    if (workflowRunEvent.type === "workflow.run.step") {
      const step = normalizeRunStep(workflowRunEvent.step);
      if (!step) return;
      const nextStatus = workflowRunStatusFromStep(step);
      setActiveRunId(runId);
      setLiveSteps((prev) => [...prev, step]);
      setSelectedRun((prev) => {
        if (!prev || prev.workflow_run_id !== runId) return prev;
        const summary = prev.summary ?? {};
        const priorSteps = Array.isArray(summary.steps) ? summary.steps : [];
        const nextSteps = [...priorSteps, step];
        return {
          ...prev,
          status: nextStatus ?? prev.status,
          current_node_id: step.node_id,
          summary: {
            ...summary,
            status: nextStatus ?? summary.status,
            steps: nextSteps,
            node_path: nextSteps.map((item) => item.node_id),
          },
        };
      });
      if (refreshSelectedRun) {
        if (!selectedRunNodePinnedRef.current) {
          setSelectedRunNodeId(step.node_id);
        }
        refreshRunDiagnostics(runId, {
          refreshSessionMemory: Boolean(selectedSessionId) && step.node_type === "agent",
          sessionId: selectedSessionId,
        });
      }
      return;
    }
    if (
      workflowRunEvent.type === "workflow.run.approval.approved" ||
      workflowRunEvent.type === "workflow.run.approval.rejected"
    ) {
      setActiveRunId(runId || null);
      setRunMessage(
        workflowRunLifecycleMessage(runId, workflowRunEvent.type, workflowRunEvent),
      );
      refreshRunHistoryQueries();
      if (refreshSelectedRun) {
        refreshRunDiagnostics(runId, {
          refreshApprovals: true,
          refreshCheckpoints: true,
          sessionId: selectedSessionId,
        });
      }
      return;
    }
    if (workflowRunEvent.type === "workflow.run.recovered") {
      setTab("runs");
      setActiveRunId(runId || null);
      setLiveSteps([]);
      setRunMessage(
        workflowRunLifecycleMessage(runId, workflowRunEvent.type, workflowRunEvent),
      );
      refreshRunHistoryQueries();
      if (refreshSelectedRun) {
        refreshRunDiagnostics(runId, {
          refreshApprovals: true,
          refreshCheckpoints: true,
          refreshSessionMemory: Boolean(selectedSessionId),
          sessionId: selectedSessionId,
        });
      }
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
        workflowRunEvent,
      );
      setTab("runs");
      setRunMessage(message);
      refreshRunHistoryQueries();
      if (retriedRunId) {
        setActiveRunId(retriedRunId);
        setLiveSteps([]);
        if (refreshSelectedRun) {
          void caliberApi
            .getWorkflowRun(retriedRunId)
            .then((run) => {
              handleRunActionSuccess(run, message);
            })
            .catch(() => {
              refreshRunDiagnostics(retriedRunId, {
                refreshApprovals: true,
                refreshCheckpoints: true,
                sessionId: selectedSessionId,
              });
            });
        }
      } else if (refreshSelectedRun) {
        refreshRunDiagnostics(runId, {
          refreshApprovals: true,
          refreshCheckpoints: true,
          sessionId: selectedSessionId,
        });
      }
      return;
    }
    if (
      workflowRunEvent.type === "workflow.run.cancel_requested" ||
      workflowRunEvent.type === "workflow.run.waiting_approval" ||
      workflowRunEvent.type === "workflow.run.waiting_event" ||
      workflowRunEvent.type === "workflow.run.resumed" ||
      workflowRunEvent.type === "workflow.run.cancelled"
    ) {
      setActiveRunId(runId || null);
      setRunMessage(
        workflowRunLifecycleMessage(runId, workflowRunEvent.type, workflowRunEvent),
      );
      refreshRunHistoryQueries();
      if (refreshSelectedRun) {
        refreshRunDiagnostics(runId, {
          refreshApprovals: true,
          refreshCheckpoints: true,
          refreshSessionMemory: Boolean(selectedSessionId),
          sessionId: selectedSessionId,
        });
      }
      return;
    }
    if (
      workflowRunEvent.type === "workflow.run.completed" ||
      workflowRunEvent.type === "workflow.run.expired" ||
      workflowRunEvent.type === "workflow.run.failed"
    ) {
      setActiveRunId(runId || null);
      setRunMessage(
        workflowRunLifecycleMessage(runId, workflowRunEvent.type, workflowRunEvent),
      );
      refreshRunHistoryQueries();
      if (refreshSelectedRun) {
        refreshRunDiagnostics(runId, {
          refreshApprovals: true,
          refreshCheckpoints: true,
          refreshSessionMemory: Boolean(selectedSessionId),
          sessionId: selectedSessionId,
        });
      }
      return;
    }
  }, [
    handleRunActionSuccess,
    workflowRunEvent,
    workflowId,
    invalidate,
    navigate,
    refreshRunHistoryQueries,
    refreshRunDiagnostics,
    wfQuery.data?.name,
  ]);

  const runs = useMemo(() => runsQuery.data ?? [], [runsQuery.data]);
  const runsTabHistory = useMemo(() => {
    const rows = new Map<string, WorkflowRun>();
    for (const page of runsTabHistoryQuery.data?.pages ?? []) {
      for (const run of page.data ?? []) {
        rows.set(run.workflow_run_id, run);
      }
    }
    return [...rows.values()];
  }, [runsTabHistoryQuery.data]);
  const runsTabRows = useMemo(() => {
    if (runsTabHistory.length > 0) return runsTabHistory;
    if (runsTabHistoryQuery.isError) return runs;
    return [];
  }, [runs, runsTabHistory, runsTabHistoryQuery.isError]);
  const selectedRunCandidates = useMemo(() => {
    const rows = new Map<string, WorkflowRun>();
    for (const run of runs) rows.set(run.workflow_run_id, run);
    for (const run of runsTabHistory) rows.set(run.workflow_run_id, run);
    return [...rows.values()];
  }, [runs, runsTabHistory]);
  const requestedRunCandidate = useMemo(() => {
    if (!requestedRunId) return null;
    const fromCandidates =
      selectedRunCandidates.find((run) => run.workflow_run_id === requestedRunId) ?? null;
    if (fromCandidates) return fromCandidates;
    if (requestedRunQuery.data?.workflow_id === workflowId) {
      return requestedRunQuery.data;
    }
    return null;
  }, [requestedRunId, requestedRunQuery.data, selectedRunCandidates, workflowId]);
  const requestedRunHydrating = Boolean(
    requestedRunId &&
      requestedRunCandidate == null &&
      requestedRunQuery.isLoading,
  );
  useEffect(() => {
    if (!selectedRun?.workflow_run_id) return;
    const refreshed = selectedRunCandidates.find(
      (run) => run.workflow_run_id === selectedRun.workflow_run_id,
    );
    if (!refreshed) return;
    setSelectedRun((current) => {
      if (!current || current.workflow_run_id !== refreshed.workflow_run_id) return current;
      return refreshed;
    });
  }, [selectedRun?.workflow_run_id, selectedRunCandidates]);
  useEffect(() => {
    const nextKey = `${location.pathname}?${location.search}`;
    if (lastUrlTabSyncKeyRef.current === nextKey) {
      return;
    }
    lastUrlTabSyncKeyRef.current = nextKey;
    const nextTab = requestedRunId ? "runs" : requestedTab ?? "graph";
    setTab((current) => (current === nextTab ? current : nextTab));
  }, [location.pathname, location.search, requestedRunId, requestedTab]);
  useEffect(() => {
    if (!requestedRunId || !requestedRunCandidate) return;
    setSelectedRun((current) =>
      current?.workflow_run_id === requestedRunId ? current : requestedRunCandidate,
    );
  }, [requestedRunCandidate, requestedRunId]);
  useEffect(() => {
    if (
      !requestedRunId ||
      !requestedRunQuery.data ||
      !requestedRunQuery.data.workflow_id ||
      requestedRunQuery.data.workflow_id === workflowId
    ) {
      return;
    }
    navigate(
      `/workflows/${encodeURIComponent(requestedRunQuery.data.workflow_id)}?tab=runs&run=${encodeURIComponent(requestedRunQuery.data.workflow_run_id)}`,
      { replace: true },
    );
  }, [navigate, requestedRunId, requestedRunQuery.data, workflowId]);
  useEffect(() => {
    if (!requestedRunId || !requestedRunQuery.isError) return;
    if (
      requestedRunCandidate?.workflow_run_id === requestedRunId
      || selectedRun?.workflow_run_id === requestedRunId
    ) {
      return;
    }
    setRunMessage(
      `Workflow run ${requestedRunId} could not be loaded. Showing the runs tab instead.`,
    );
  }, [
    requestedRunCandidate?.workflow_run_id,
    requestedRunId,
    requestedRunQuery.isError,
    selectedRun?.workflow_run_id,
  ]);
  useEffect(() => {
    if (requestedRunHydrating) return;
    const current = new URLSearchParams(location.search);
    const next = new URLSearchParams(location.search);
    const desiredTab = tab === "graph" ? null : tab;
    const desiredRun =
      tab === "runs" && selectedRun?.workflow_run_id
        ? selectedRun.workflow_run_id
        : null;
    if (desiredTab) {
      next.set("tab", desiredTab);
    } else {
      next.delete("tab");
    }
    if (desiredRun) {
      next.set("run", desiredRun);
    } else {
      next.delete("run");
    }
    if (next.toString() !== current.toString()) {
      setSearchParams(next, { replace: true });
    }
  }, [
    location.search,
    requestedRunHydrating,
    selectedRun?.workflow_run_id,
    setSearchParams,
    tab,
  ]);

  const filteredRuns = useMemo(
    () =>
      runsTabRows.filter(
        (run) =>
          runArtifactFilterMatches(run, runArtifactFilter)
          && runSearchMatches(run, normalizedRunSearch),
      ),
    [normalizedRunSearch, runArtifactFilter, runsTabRows],
  );

  /* counts for tab badges */
  const versionCount = versions.length;
  const deploymentCount = deployments.length;
  const runHistoryStats = runHistoryStatsQuery.data ?? null;
  const runCount = runHistoryStats?.total_runs ?? runs.length;
  const waitingEventRunCount = runs.filter(
    (run) => run.status === "waiting_event",
  ).length;
  const artifactUploadFailedRunCount =
    runHistoryStats?.artifact_persistence.failed
    ?? runs.filter((run) => workflowRunArtifactPersistence(run)?.status === "failed").length;
  const artifactPersistedRunCount =
    runHistoryStats?.artifact_persistence.persisted
    ?? runs.filter((run) => workflowRunArtifactPersistence(run)?.status === "persisted").length;
  const runHistoryMatchingCount = runHistoryStats?.matching_runs ?? filteredRuns.length;
  const runTriageSummary = useMemo(() => {
    const serverFiltered = normalizedRunSearch.length > 0 || runArtifactFilter !== "all";
    const parts: string[] = [];
    if (serverFiltered) {
      parts.push(
        runHistoryStats
          ? `Showing ${filteredRuns.length} of ${runHistoryMatchingCount} matching run${runHistoryMatchingCount === 1 ? "" : "s"}`
          : `Showing ${filteredRuns.length} matching run${filteredRuns.length === 1 ? "" : "s"}`,
      );
    } else if (runHistoryStats) {
      parts.push(
        `Showing ${filteredRuns.length} of ${runCount} run${runCount === 1 ? "" : "s"}`,
      );
      parts.push(
        `${artifactUploadFailedRunCount} upload failure${artifactUploadFailedRunCount === 1 ? "" : "s"} across all runs`,
      );
      parts.push(
        `${artifactPersistedRunCount} stored artifact run${artifactPersistedRunCount === 1 ? "" : "s"} across all runs`,
      );
    } else {
      parts.push(`Showing ${filteredRuns.length} recent run${filteredRuns.length === 1 ? "" : "s"}`);
      parts.push(
        `${artifactUploadFailedRunCount} upload failure${artifactUploadFailedRunCount === 1 ? "" : "s"} in recent history`,
      );
      parts.push(
        `${artifactPersistedRunCount} stored artifact run${artifactPersistedRunCount === 1 ? "" : "s"} in recent history`,
      );
    }
    if (runsTabHistoryQuery.hasNextPage) {
      parts.push(
        serverFiltered
          ? "More matching runs available"
          : runHistoryStats
            ? "More runs available"
            : "More recent runs available",
      );
    }
    if (runsTabHistoryQuery.isError) {
      parts.push("Full-history search unavailable; showing the recent run index");
    }
    if (!runsTabHistoryQuery.isError && runHistoryStatsQuery.isError) {
      parts.push("Exact totals unavailable; using recent history estimates");
    }
    return parts.join(" · ");
  }, [
    artifactPersistedRunCount,
    artifactUploadFailedRunCount,
    filteredRuns.length,
    normalizedRunSearch.length,
    runCount,
    runArtifactFilter,
    runHistoryMatchingCount,
    runHistoryStats,
    runHistoryStatsQuery.isError,
    runsTabHistoryQuery.hasNextPage,
    runsTabHistoryQuery.isError,
  ]);
  const promotionCount = (promotionsQuery.data ?? []).length;
  const patchCount = (patchesQuery.data ?? []).length;
  const calibrationDataset = calibrationOptionsQuery.data?.data.deploy_gate_dataset;
  const calibrationDatasetAvailable = calibrationDataset?.available === true;
  const calibrationJudge = calibrationOptionsQuery.data?.data.judge;
  const calibrationJudgeAvailable = calibrationJudge?.available === true;
  const calibrationObjectives = calibrationOptionsQuery.data?.supported_objectives ?? [
    "quality",
    "tool_correctness",
    "tool_adherence",
  ];
  const TAB_COUNTS: Record<Tab, number> = {
    graph: 0,
    versions: versionCount,
    deployments: deploymentCount,
    runs: runCount,
    service: 0,
    promotions: promotionCount,
    patches: patchCount,
  };
  const selectedRunApprovals = useMemo(
    () => selectedRunApprovalsQuery.data ?? [],
    [selectedRunApprovalsQuery.data],
  );
  const selectedRunCheckpoints = useMemo(
    () => selectedRunCheckpointsQuery.data ?? [],
    [selectedRunCheckpointsQuery.data],
  );
  const selectedRunResumeSourceCheckpoint = useMemo(() => {
    if (!selectedRunResumeCheckpointId) return null;
    return (
      selectedRunResumeSourceCheckpointsQuery.data?.find(
        (checkpoint) => checkpoint.checkpoint_id === selectedRunResumeCheckpointId,
      ) ?? null
    );
  }, [selectedRunResumeCheckpointId, selectedRunResumeSourceCheckpointsQuery.data]);
  const selectedRunEffectiveCheckpoints = useMemo(
    () =>
      mergeWorkflowRunCheckpoints(
        selectedRunCheckpoints,
        selectedRunResumeSourceCheckpoint,
      ),
    [selectedRunCheckpoints, selectedRunResumeSourceCheckpoint],
  );
  const selectedRunRelevantApprovals = useMemo(() => {
    if (!selectedRun?.current_node_id) {
      return selectedRunApprovals;
    }
    return selectedRunApprovals.filter(
      (approval) => approval.node_id === selectedRun.current_node_id,
    );
  }, [selectedRun?.current_node_id, selectedRunApprovals]);
  const selectedRunApprovalsReady =
    !selectedRunApprovalsQuery.isLoading && !selectedRunApprovalsQuery.isError;
  const selectedPendingApproval =
    selectedRunRelevantApprovals.find((approval) => approval.status === "pending") ?? null;
  const selectedApprovedApproval =
    selectedRunRelevantApprovals.find((approval) => approval.status === "approved") ?? null;
  const selectedRejectedApproval =
    selectedRunRelevantApprovals.find((approval) => approval.status === "rejected") ?? null;
  const selectedRunVersion = selectedRunVersionFromList ?? selectedRunVersionQuery.data ?? null;
  const selectedRunVersionNumber =
    selectedRunVersion?.version_number
    ?? readNumber(selectedRun?.summary?.workflow_version_number)
    ?? null;
  const selectedRunVersionReference =
    selectedRunVersionNumber != null
      ? `workflow version v${selectedRunVersionNumber}`
      : selectedRun?.workflow_version_id
        ? `workflow version ${selectedRun.workflow_version_id}`
        : "the workflow version recorded for this run";
  const selectedRunManifestMode =
    selectedRunManifestQuery.data?.manifest_mode
    ?? selectedRun?.summary?.manifest_mode
    ?? null;
  const selectedRunSavedVersionFallbackManifest = selectedRun
    ? selectedRunManifestMode === "snapshot"
      ? null
      : selectedRun.workflow_version_id
        ? selectedRunVersion?.manifest ?? null
        : latest?.manifest ?? null
    : null;
  const selectedRunSavedVersionFallbackLoading = Boolean(
    selectedRun &&
      selectedRunManifestMode !== "snapshot" &&
      Boolean(selectedRunWorkflowVersionId) &&
      selectedRunVersion == null &&
      selectedRunVersionQuery.isLoading,
  );
  const selectedRunSyntheticManifest = useMemo(
    () => buildSyntheticWorkflowRunManifest(selectedRun, selectedRunEffectiveCheckpoints),
    [selectedRun, selectedRunEffectiveCheckpoints],
  );
  const selectedRunManifest = selectedRun
    ? selectedRunManifestQuery.data?.manifest
      ?? selectedRunSavedVersionFallbackManifest
      ?? (
        !selectedRunManifestQuery.isLoading && !selectedRunSavedVersionFallbackLoading
          ? selectedRunSyntheticManifest
          : null
      )
    : null;
  const selectedRunManifestLoading = Boolean(
    selectedRun && (
      selectedRunManifestQuery.isLoading
      || (
        !selectedRunManifest
        && selectedRunManifestMode !== "snapshot"
        && selectedRunSavedVersionFallbackLoading
      )
    ),
  );
  const selectedRunManifestFallbackNotice =
    selectedRunManifest
    && selectedRunManifestQuery.error
    && selectedRunManifestMode !== "snapshot"
    && selectedRunVersion
    && selectedRunSavedVersionFallbackManifest
      ? savedVersionRunGraphFallbackMessage(
        selectedRun!,
        `workflow version v${selectedRunVersionNumber ?? selectedRunVersion.version_number}`,
      )
      : selectedRunManifest
        && !selectedRunManifestQuery.data
        && !selectedRunSavedVersionFallbackManifest
        && selectedRunSyntheticManifest
          ? syntheticRunGraphFallbackMessage(selectedRun!)
      : null;
  const selectedRunVersionMissingMessage = workflowRunGraphUnavailableMessage({
    manifestMode: selectedRunManifestMode,
    versionReference: selectedRunVersionReference,
    versionLoadFailed: Boolean(selectedRunVersionQuery.error),
    hasSummarySteps: Array.isArray(selectedRun?.summary?.steps) && selectedRun.summary.steps.length > 0,
    hasCurrentNode: Boolean(readString(selectedRun?.current_node_id)),
    hasCheckpoints: selectedRunEffectiveCheckpoints.length > 0,
  });
  const selectedRunVersionMismatch =
    Boolean(
      selectedRunManifestMode !== "snapshot"
      && selectedRunVersionNumber != null
      && latest
      && (
        selectedRunVersion?.version_id
          ? selectedRunVersion.version_id !== latest.version_id
          : selectedRunVersionNumber !== latest.version_number
      ),
    );
  const selectedWaitNode = (
    selectedRun?.status === "waiting_event"
    && selectedRun.current_node_id
    && selectedRunManifest
  )
    ? selectedRunManifest.nodes[selectedRun.current_node_id]
    : null;
  const selectedRunActiveCheckpoint = useMemo(
    () => resolveWorkflowRunActiveCheckpoint(selectedRun, selectedRunEffectiveCheckpoints),
    [selectedRun, selectedRunEffectiveCheckpoints],
  );
  const selectedRunCheckpointStateReady =
    !selectedRunCheckpointsQuery.isLoading && !selectedRunResumeSourceCheckpointsQuery.isLoading;
  const selectedRunActiveCheckpointState = isRecord(selectedRunActiveCheckpoint?.state_blob)
    ? selectedRunActiveCheckpoint.state_blob
    : null;
  const selectedRunApprovalCheckpointKind =
    selectedRun?.status === "waiting_approval"
      ? approvalCheckpointKind(selectedRunActiveCheckpointState)
      : null;
  const selectedRunMissingResumeCheckpointIssue = selectedRunCheckpointStateReady && selectedRun
    ? selectedRun.status === "waiting_event"
      ? !selectedRunActiveCheckpoint
        ? "Manual and event-match resume are unavailable because this paused run no longer has a stored checkpoint. Inspect the recovery, checkpoint, and lineage panels before retrying from a healthy state or starting a new attempt."
        : null
      : selectedRun.status === "waiting_approval"
        ? !selectedRunActiveCheckpoint
          ? "Manual resume is unavailable because this paused run no longer has a stored checkpoint. Inspect the recovery, checkpoint, and lineage panels before retrying from a healthy state or starting a new attempt."
          : null
        : null
    : null;
  const selectedRunCheckpointIdentityIssue = workflowRunCheckpointIdentityIssue(
    selectedRun,
    selectedRunActiveCheckpoint,
  );
  const selectedWaitMode =
    selectedWaitNode?.type === "wait_until"
      ? "wait_until"
      : selectedWaitNode?.type === "wait_for_event"
        ? "wait_for_event"
        : readString(selectedRunActiveCheckpointState?.kind);
  const selectedWaitEventName =
    selectedWaitNode?.type === "wait_for_event" && typeof selectedWaitNode.event_name === "string"
      ? selectedWaitNode.event_name
      : readString(selectedRunActiveCheckpointState?.expected_event_name)
        ?? readString(selectedRunActiveCheckpointState?.event_name)
        ?? "";
  const selectedRunResumeEventNameIssue = workflowRunResumeEventNameIssue({
    status: selectedRun?.status,
    waitMode: selectedWaitMode,
    expectedEventName: selectedWaitEventName,
    resumeEventName,
  });
  const selectedWaitCorrelationKey = readString(selectedRunActiveCheckpointState?.correlation_key)
    ?? "";
  const selectedWaitCorrelationValue = selectedRunActiveCheckpointState?.correlation_value;
  const selectedRunResumeByEventIssue = workflowRunResumeByEventIssue({
    status: selectedRun?.status,
    waitMode: selectedWaitMode,
    correlationKey: selectedWaitCorrelationKey,
    correlationValue: selectedWaitCorrelationValue,
    resumeEventPayload,
  });
  const selectedWaitUntilText =
    checkpointWaitUntilDisplay(selectedRunActiveCheckpointState)
    ?? (
      selectedWaitNode?.type === "wait_until"
        ? waitUntilDisplay(selectedWaitNode)
        : null
    )
    ?? "the configured time";
  const selectedRunBusy =
    cancelRunMut.isPending ||
    retryRunMut.isPending ||
    approveRunMut.isPending ||
    rejectRunMut.isPending ||
    resumeRunMut.isPending ||
    resumeRunByEventMut.isPending;
  const canCancelSelectedRun =
    runCapabilities?.queue_enabled &&
    runCapabilities?.supports_cancel &&
    selectedRun &&
    ["queued", "running", "waiting_approval", "waiting_event"].includes(selectedRun.status);
  const canRetrySelectedRun =
    runCapabilities?.queue_enabled &&
    runCapabilities?.supports_retry &&
    selectedRun &&
    ["failed", "cancelled", "expired"].includes(selectedRun.status);
  const canApproveSelectedRun =
    runCapabilities?.queue_enabled &&
    runCapabilities?.runtime_approvals_enabled &&
    selectedRun?.status === "waiting_approval" &&
    selectedPendingApproval !== null;
  const canRejectSelectedRun = canApproveSelectedRun;
  const queueActionEnabled = Boolean(runCapabilities?.queue_enabled);
  const runtimeApprovalsEnabled = Boolean(runCapabilities?.runtime_approvals_enabled);
  const manualResumeEnabled = Boolean(runCapabilities?.supports_resume);
  const checkpointingEnabled = Boolean(runCapabilities?.checkpointing_enabled);
  const selectedRunApprovalSubject = workflowRunApprovalSubject(selectedRunApprovalCheckpointKind);
  const selectedRunApprovalQueueIssue = Boolean(
    selectedRun?.status === "waiting_approval" && !queueActionEnabled,
  )
    ? "Approval actions are unavailable until the workflow run queue is enabled for this deployment. Re-enable the queue before approving or rejecting this paused run."
    : null;
  const selectedRunApprovalRecordsActionIssue =
    selectedRun?.status === "waiting_approval"
      ? selectedRunApprovalsQuery.isError
        ? selectedRunApprovalCheckpointKind === "runtime_approval"
          ? "Approval actions are unavailable because runtime approval records could not be loaded. Refresh approval history or inspect recovery diagnostics before continuing this run."
          : "Approval actions are unavailable because approval records could not be loaded. Refresh approval history or inspect recovery diagnostics before continuing this run."
        : selectedRunApprovalsQuery.isLoading
          ? selectedRunApprovalCheckpointKind === "runtime_approval"
            ? "Approval actions are unavailable until runtime approval records finish loading."
            : "Approval actions are unavailable until approval records finish loading."
          : null
      : null;
  const showApprovalCapabilityNote = Boolean(
    selectedRun?.status === "waiting_approval"
    && (
      Boolean(selectedRunApprovalQueueIssue)
      || Boolean(selectedRunApprovalRecordsActionIssue)
      || !runtimeApprovalsEnabled
    ),
  );
  const selectedRunResumeQueueIssue = !queueActionEnabled && selectedRun
    ? selectedRun.status === "waiting_event"
      ? "Manual and event-match resume are unavailable until the workflow run queue is enabled for this deployment. Re-enable the queue before continuing this paused event gate."
      : selectedRun.status === "waiting_approval"
        ? "Manual resume is unavailable until the workflow run queue is enabled for this deployment. Re-enable the queue before continuing this paused approval run."
        : null
    : null;
  const selectedRunResumeCheckpointingIssue = Boolean(
    (selectedRun?.status === "waiting_event" || selectedRun?.status === "waiting_approval")
    && !checkpointingEnabled,
  )
    ? selectedRun?.status === "waiting_event"
      ? "Manual and event-match resume are unavailable until checkpoint persistence is enabled for workflow runs. Re-enable checkpointing for this deployment before trying to continue this paused event gate."
      : "Manual resume is unavailable until checkpoint persistence is enabled for workflow runs. Re-enable checkpointing for this deployment before continuing this paused approval run."
    : null;
  const selectedRunApprovalRecordsIssue =
    selectedRun?.status === "waiting_approval"
      ? selectedRunApprovalsQuery.isError
        ? selectedRunApprovalCheckpointKind === "runtime_approval"
          ? "Manual resume is unavailable because runtime approval records could not be loaded. Refresh approval history or inspect recovery diagnostics before continuing this run."
          : "Manual resume is unavailable because approval records could not be loaded. Refresh approval history or inspect recovery diagnostics before continuing this run."
        : selectedRunApprovalsQuery.isLoading
          ? selectedRunApprovalCheckpointKind === "runtime_approval"
            ? "Manual resume is unavailable until runtime approval records finish loading."
            : "Manual resume is unavailable until approval records finish loading."
          : null
      : null;
  const showResumeCapabilityNote = Boolean(
    selectedRun
    && (
      selectedRunApprovalRecordsIssue ||
      selectedRunMissingResumeCheckpointIssue
      || selectedRunCheckpointIdentityIssue
      || selectedRunResumeEventNameIssue
      || selectedRunResumeQueueIssue
      || selectedRunResumeCheckpointingIssue
      || (
        !manualResumeEnabled &&
        (
          selectedRun.status === "waiting_event" ||
          selectedRun.status === "waiting_approval"
        )
      )
    ),
  );
  const resumeCapabilityNote = selectedRunResumeQueueIssue
    ?? selectedRunResumeCheckpointingIssue
    ?? selectedRunApprovalRecordsIssue
    ?? selectedRunMissingResumeCheckpointIssue
    ?? selectedRunCheckpointIdentityIssue
    ?? selectedRunResumeEventNameIssue
    ?? (
      !showResumeCapabilityNote
        ? null
        : selectedRun?.status === "waiting_event"
          ? selectedWaitMode === "wait_until"
            ? "Manual resume override is unavailable until checkpoint persistence is enabled for workflow runs. This scheduled wait will resume automatically when its deadline arrives."
            : "Manual resume is unavailable until checkpoint persistence is enabled for workflow runs. Use the event-match controls below to resume this event gate."
          : "Manual resume is unavailable until checkpoint persistence is enabled for workflow runs. Record the required approval to continue this run."
    );
  const canResumeSelectedRun =
    queueActionEnabled &&
    checkpointingEnabled &&
    runCapabilities?.supports_resume &&
    !selectedRunMissingResumeCheckpointIssue &&
    !selectedRunCheckpointIdentityIssue &&
    (
      selectedRun?.status === "waiting_event" ||
      (
        selectedRun?.status === "waiting_approval" &&
        selectedRunApprovalsReady &&
        selectedApprovedApproval !== null &&
        selectedPendingApproval === null &&
        selectedRejectedApproval === null
      )
    );
  const selectedWaitUntilCapabilityNote = selectedRun?.status === "waiting_event"
    && selectedWaitMode === "wait_until"
    ? selectedRunResumeQueueIssue
      ? "Automatic and manual resume are unavailable until the workflow run queue is enabled for this deployment."
      : selectedRunResumeCheckpointingIssue
        ? "Manual resume is unavailable until checkpoint persistence is enabled for workflow runs. The worker will still re-queue this run automatically when that time arrives."
        : selectedRunMissingResumeCheckpointIssue
          ? "Automatic and manual resume are unavailable because this paused run no longer has a stored checkpoint. Inspect the recovery, checkpoint, and lineage panels before retrying from a healthy state or starting a new attempt."
          : selectedRunCheckpointIdentityIssue
            ? "Automatic and manual resume are unavailable until the stored checkpoint and active node agree again. Inspect the checkpoint and recovery panels before trying to continue this scheduled wait."
            : !manualResumeEnabled
              ? "The worker will resume this run automatically when that time arrives, but manual Resume override is unavailable for this deployment."
              : "The worker will resume this run automatically when that time arrives, and Resume remains available as a manual override."
    : null;

  useEffect(() => {
    if (!selectedRun || !selectedRunManifest) {
      previousSelectedRunIdRef.current = null;
      setSelectedRunNodeId(null);
      setSelectedRunNodePinned(false);
      return;
    }
    const defaultNodeId = defaultSelectedRunNodeId(selectedRun, selectedRunManifest);
    const runChanged = previousSelectedRunIdRef.current !== selectedRun.workflow_run_id;
    previousSelectedRunIdRef.current = selectedRun.workflow_run_id;
    const currentNodeId = selectedRunNodeIdRef.current;
    const currentNodeStillExists = Boolean(
      currentNodeId && selectedRunManifest.nodes[currentNodeId],
    );
    if (runChanged || !currentNodeStillExists) {
      setSelectedRunNodePinned(false);
      setSelectedRunNodeId(defaultNodeId);
    }
  }, [selectedRun, selectedRunManifest]);

  useEffect(() => {
    if (selectedRun?.status !== "waiting_event") {
      setResumeEventName("");
      setResumeEventPayload("");
      return;
    }
    if (selectedWaitMode === "wait_until") {
      setResumeEventName("");
      setResumeEventPayload("");
      return;
    }
    setResumeEventName(selectedWaitEventName);
    setResumeEventPayload((current) => {
      const basePayload: Record<string, unknown> = {
        source: "manual_resume",
        node_id: selectedRun.current_node_id ?? undefined,
      };
      const basePayloadText = formatResumeEventPayload(basePayload);
      const payload: Record<string, unknown> = { ...basePayload };
      if (
        selectedWaitCorrelationKey
        && selectedWaitCorrelationValue !== null
        && selectedWaitCorrelationValue !== undefined
        && selectedWaitCorrelationValue !== ""
      ) {
        payload[selectedWaitCorrelationKey] = selectedWaitCorrelationValue;
      }
      const upgradedPayloadText = formatResumeEventPayload(payload);
      if (!current.trim() || current === basePayloadText || current === upgradedPayloadText) {
        return upgradedPayloadText;
      }
      return current;
    });
  }, [
    selectedRun?.workflow_run_id,
    selectedRun?.status,
    selectedRun?.current_node_id,
    selectedWaitCorrelationKey,
    selectedWaitCorrelationValue,
    selectedWaitEventName,
    selectedWaitMode,
  ]);

  useEffect(() => {
    if (!calibrationJudgeAvailable) {
      setCalibrationUseJudge(false);
    }
  }, [calibrationJudgeAvailable]);

  const submitSelectedRunResume = (): void => {
    if (!selectedRun) return;
    if (selectedRunMissingResumeCheckpointIssue) {
      setRunMessage(selectedRunMissingResumeCheckpointIssue);
      return;
    }
    if (selectedRunCheckpointIdentityIssue) {
      setRunMessage(selectedRunCheckpointIdentityIssue);
      return;
    }
    if (selectedRunResumeEventNameIssue) {
      setRunMessage(selectedRunResumeEventNameIssue);
      return;
    }
    if (selectedRun.status === "waiting_approval") {
      if (!selectedRunApprovalsReady) {
        setRunMessage(
          selectedRunApprovalRecordsIssue
          ?? (
            selectedRunApprovalCheckpointKind === "runtime_approval"
              ? "Resume unavailable until runtime approval records finish loading."
              : "Resume unavailable until approval records finish loading."
          ),
        );
        return;
      }
      if (selectedPendingApproval) {
        setRunMessage(
          `Resume unavailable until the pending ${workflowRunApprovalNoun(
            selectedRunApprovalCheckpointKind,
          )} is resolved.`,
        );
        return;
      }
      if (selectedRejectedApproval) {
        setRunMessage(
          selectedRunApprovalCheckpointKind === "runtime_approval"
            ? "Resume unavailable after runtime approval rejection."
            : "Resume unavailable after approval rejection.",
        );
        return;
      }
      if (!selectedApprovedApproval) {
        setRunMessage(
          selectedRunApprovalCheckpointKind === "runtime_approval"
            ? "Resume unavailable until a runtime approval has been approved."
            : "Resume unavailable until an approval has been recorded.",
        );
        return;
      }
    }
    if (selectedRun.status === "waiting_event") {
      if (selectedWaitMode === "wait_until") {
        resumeRunMut.mutate({ runId: selectedRun.workflow_run_id });
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
        runId: selectedRun.workflow_run_id,
        event_name: resumeEventName.trim() || undefined,
        event_payload: eventPayload,
      });
      return;
    }
    resumeRunMut.mutate({ runId: selectedRun.workflow_run_id });
  };

  const submitSelectedRunResumeByEvent = (): void => {
    if (selectedRunMissingResumeCheckpointIssue) {
      setRunMessage(selectedRunMissingResumeCheckpointIssue);
      return;
    }
    if (selectedRunCheckpointIdentityIssue) {
      setRunMessage(selectedRunCheckpointIdentityIssue);
      return;
    }
    if (selectedRunResumeByEventIssue) {
      setRunMessage(selectedRunResumeByEventIssue);
      return;
    }
    if (selectedRunResumeEventNameIssue) {
      setRunMessage(selectedRunResumeEventNameIssue);
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

  return (
    <div data-testid="workflow-detail" className="space-y-6 animate-fade-in">
      {/* ── Header ── */}
      <div className="flex items-center justify-between">
        <div>
          <Link to="/workflows" className="group inline-flex items-center gap-1 text-xs text-slate-400 transition-colors hover:text-caliber-purple">
            <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M15 18l-6-6 6-6" /></svg>
            Workflows
          </Link>
          <h1 className="mt-1 text-2xl font-bold tracking-tight text-slate-900">
            {wfQuery.data?.name ?? workflowId}
          </h1>
          {wfQuery.data && (
            <div className="mt-1 flex items-center gap-3 text-xs text-slate-400">
              <span className="font-mono">{wfQuery.data.workflow_id}</span>
              {wfQuery.data.owner && (
                <>
                  <span className="text-slate-200">·</span>
                  <span>{wfQuery.data.owner}</span>
                </>
              )}
              <span className="text-slate-200">·</span>
              <StatusBadge status={workflowStatus ?? wfQuery.data.status} />
              <span className="text-slate-200">·</span>
              <TriggerBadge trigger={headerTrigger} />
            </div>
          )}
        </div>
        <div className="flex items-center gap-3">
          {latest && (
            <button
              type="button"
              data-testid="workflow-run-open"
              onClick={() => setTab("runs")}
              className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-2 text-sm font-semibold text-emerald-700 transition-all hover:bg-emerald-100 active:scale-[0.98]"
            >
              Run Pipeline
            </button>
          )}
          {wfQuery.data && workflowStatus !== "archived" && (
            <button
              type="button"
              data-testid="workflow-pause-toggle"
              disabled={pauseMut.isPending}
              onClick={() => {
                const next = workflowStatus === "paused" ? "active" : "paused";
                pauseMut.mutate(next);
              }}
              className="rounded-xl border border-slate-200/70 bg-white px-4 py-2 text-sm font-semibold text-slate-700 shadow-card transition-all hover:shadow-card-hover disabled:opacity-50 active:scale-[0.98]"
            >
              {workflowStatus === "paused" ? "Resume" : "Pause"}
            </button>
          )}
          {latest && (
            <Link
              to={`/workflows/${workflowId}/editor/${latest.version_id}`}
              className="rounded-xl border border-slate-200/60 bg-white px-4 py-2 text-sm font-semibold text-slate-700 shadow-card transition-all hover:shadow-card-hover hover:-translate-y-0.5"
            >
              Open in Editor
            </Link>
          )}
          {draft && (
            <Link
              to={`/workflows/${workflowId}/editor/${draft.version_id}`}
              data-testid="edit-draft"
              className="btn-primary"
            >
              Edit Draft v{draft.version_number}
            </Link>
          )}
          <button
            type="button"
            data-testid="workflow-calibrate"
            onClick={() => setShowCalibration((value) => !value)}
            className="rounded-xl border border-caliber-purple/30 bg-caliber-purple/5 px-4 py-2 text-sm font-semibold text-caliber-purple transition-all hover:bg-caliber-purple/10 active:scale-[0.98]"
          >
            Calibrate
          </button>
        </div>
      </div>

      {showCalibration && (
        <div
          data-testid="workflow-calibration-panel"
          className="rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card"
        >
          <div className="flex flex-col gap-4 lg:flex-row lg:items-end">
            <label className="min-w-[13rem] flex-1 text-xs font-semibold text-slate-500">
              Agent
              <select
                data-testid="workflow-calibration-agent"
                className="mt-1 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 outline-none transition-colors focus:border-caliber-purple"
                value={calibrationAgentId}
                onChange={(event) => setCalibrationAgentId(event.target.value)}
              >
                {(agentsQuery.data ?? []).map((agent) => (
                  <option key={agent.agent_id} value={agent.agent_id} disabled={!agent.enabled}>
                    {agent.name || agent.agent_id}
                  </option>
                ))}
              </select>
            </label>

            <label className="min-w-[12rem] flex-1 text-xs font-semibold text-slate-500">
              Objective
              <select
                data-testid="workflow-calibration-objective"
                className="mt-1 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 outline-none transition-colors focus:border-caliber-purple"
                value={calibrationObjective}
                onChange={(event) =>
                  setCalibrationObjective(event.target.value as WorkflowCalibrationObjective)
                }
              >
                {calibrationObjectives.map((objective) => (
                  <option key={objective} value={objective}>
                    {objective.replace(/_/g, " ")}
                  </option>
                ))}
              </select>
            </label>

            <label className="w-32 text-xs font-semibold text-slate-500">
              Epsilon
              <input
                data-testid="workflow-calibration-epsilon"
                className="mt-1 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 outline-none transition-colors focus:border-caliber-purple"
                type="number"
                min={0}
                max={1}
                step={0.01}
                value={calibrationEpsilon}
                onChange={(event) => setCalibrationEpsilon(Number(event.target.value))}
              />
            </label>

            <label className="w-36 text-xs font-semibold text-slate-500">
              Candidates
              <input
                data-testid="workflow-calibration-max-candidates"
                className="mt-1 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 outline-none transition-colors focus:border-caliber-purple"
                type="number"
                min={1}
                max={5}
                step={1}
                value={calibrationMaxCandidates}
                onChange={(event) => setCalibrationMaxCandidates(Number(event.target.value))}
              />
            </label>

            <button
              type="button"
              data-testid="workflow-calibration-start"
              disabled={
                calibrationMut.isPending ||
                !calibrationAgentId ||
                !calibrationDatasetAvailable
              }
              onClick={() => {
                if (!calibrationAgentId) return;
                calibrationMut.mutate({
                  agentId: calibrationAgentId,
                  objective: calibrationObjective,
                  epsilon: calibrationEpsilon,
                  maxCandidates: calibrationMaxCandidates,
                  useJudge: calibrationUseJudge,
                });
              }}
              className="btn-primary disabled:cursor-not-allowed disabled:opacity-50"
            >
              {calibrationMut.isPending ? "Starting..." : "Start Calibration"}
            </button>
          </div>

          <div className="mt-4 rounded-2xl border border-slate-200/70 bg-slate-50/70 p-4">
            <label
              className={`flex items-start gap-3 ${calibrationJudgeAvailable ? "text-slate-700" : "text-slate-500"}`}
            >
              <input
                data-testid="workflow-calibration-judge-toggle"
                type="checkbox"
                checked={calibrationUseJudge}
                onChange={(event) => setCalibrationUseJudge(event.target.checked)}
                disabled={!calibrationJudgeAvailable}
                className="mt-0.5 h-4 w-4 rounded border-slate-300 text-caliber-purple focus:ring-caliber-purple disabled:cursor-not-allowed disabled:opacity-50"
              />
              <span className="min-w-0">
                <span className="text-sm font-semibold text-slate-900">
                  Use LLM judge for quality scoring
                </span>
                <span className="mt-1 block text-xs leading-relaxed text-slate-500">
                  Replace the structural quality signal with a provider-backed judge over each workflow answer while keeping tool adherence and completion checks intact.
                </span>
              </span>
            </label>

            <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
              <span
                data-testid="workflow-calibration-judge-status"
                className={`rounded-md px-2 py-1 font-semibold ring-1 ${
                  calibrationJudgeAvailable
                    ? "bg-violet-50 text-violet-700 ring-violet-200/70"
                    : "bg-slate-100 text-slate-600 ring-slate-200/70"
                }`}
              >
                {calibrationJudgeAvailable
                  ? `${calibrationJudge?.provider ?? "llm"}${calibrationJudge?.model ? ` · ${calibrationJudge.model}` : ""}`
                  : calibrationJudge?.reason ?? "LLM judge unavailable"}
              </span>
              {calibrationUseJudge && calibrationJudgeAvailable && (
                <span
                  data-testid="workflow-calibration-judge-enabled"
                  className="rounded-md bg-emerald-50 px-2 py-1 font-semibold text-emerald-700 ring-1 ring-emerald-200/70"
                >
                  LLM judge enabled for this run
                </span>
              )}
            </div>
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-3 text-xs text-slate-500">
            <span
              data-testid="workflow-calibration-dataset"
              className={`rounded-md px-2 py-1 font-semibold ring-1 ${
                calibrationDatasetAvailable
                  ? "bg-emerald-50 text-emerald-700 ring-emerald-200/70"
                  : "bg-amber-50 text-amber-700 ring-amber-200/70"
              }`}
            >
              {calibrationDatasetAvailable
                ? `${calibrationDataset?.dataset_name ?? "deploy-gate dataset"} · ${calibrationDataset?.example_count ?? 0} examples`
                : calibrationDataset?.reason ?? "No active deploy-gate dataset"}
            </span>
            {lastCalibrationRun && (
              <span data-testid="workflow-calibration-last-run" className="font-semibold text-slate-600">
                Latest run {lastCalibrationRun.job.job_id} · {lastCalibrationRun.job.status}
              </span>
            )}
            {calibrationMut.error && (
              <span data-testid="workflow-calibration-error" className="font-semibold text-red-600">
                {calibrationMut.error.message}
              </span>
            )}
          </div>

          <WorkflowCalibrationRunsList
            runs={calibrationJobsQuery.data ?? []}
            loading={calibrationJobsQuery.isLoading}
            error={calibrationJobsQuery.error}
            onApply={(jobId) => applyJobMut.mutate(jobId)}
            applyingJobId={applyJobMut.isPending ? applyJobMut.variables ?? null : null}
          />
        </div>
      )}

      {/* ── Tabs ── */}
      <div className="flex gap-1 border-b border-slate-200/60">
        {TABS.map((t) => {
          const count = TAB_COUNTS[t.id];
          const isActive = tab === t.id;
          return (
            <button
              key={t.id}
              type="button"
              data-testid={`tab-${t.id}`}
              onClick={() => setTab(t.id)}
              className={`group -mb-px flex items-center gap-2 border-b-2 px-4 pb-3 pt-1 text-[13px] font-medium transition-colors ${
                isActive
                  ? "border-caliber-purple text-caliber-purple"
                  : "border-transparent text-slate-400 hover:text-slate-600 hover:border-slate-300"
              }`}
            >
              <svg className={`h-3.5 w-3.5 ${isActive ? "text-caliber-purple" : "text-slate-300 group-hover:text-slate-400"}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
                <path d={t.icon} />
              </svg>
              {t.label}
              {count > 0 && (
                <span className={`rounded-full px-1.5 py-0.5 text-[10px] font-bold leading-none ${
                  isActive ? "bg-caliber-purple/10 text-caliber-purple" : "bg-slate-100 text-slate-400"
                }`}>
                  {count}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* ── Graph tab ── */}
      {tab === "graph" && (
        <div className="h-[60vh] overflow-hidden rounded-2xl border border-slate-200/60 bg-white shadow-card flex">
          {latest ? (
            <>
              <div className={`${selectedNodeId ? "flex-1 min-w-0" : "w-full"} transition-all`}>
                <Canvas
                  manifest={latest.manifest}
                  selectedNodeId={selectedNodeId}
                  validationReport={latest.validation_report}
                  componentSpecs={workflowComponentMap}
                  onSelectNode={setSelectedNodeId}
                />
              </div>
              {selectedNodeId && latest.manifest.nodes[selectedNodeId] && (
                <div className="w-80 border-l border-slate-200/60 bg-white overflow-y-auto shrink-0">
                  <NodeDetailPanel
                    manifest={latest.manifest}
                    nodeId={selectedNodeId}
                    componentSpec={workflowComponentMap.get(latest.manifest.nodes[selectedNodeId].type) ?? null}
                    validationReport={latest.validation_report}
                    onClose={() => setSelectedNodeId(null)}
                  />
                </div>
              )}
            </>
          ) : (
            <EmptyState
              icon="M4 5a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1V5zm10 10a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z"
              title="No versions yet"
              desc="Create a version in the editor to see the workflow graph rendered here."
            />
          )}
        </div>
      )}

      {/* ── Versions tab ── */}
      {tab === "versions" && (
        <div className="rounded-2xl border border-slate-200/60 bg-white shadow-card overflow-hidden">
          {versions.length > 0 ? (
            <table className="w-full text-sm" data-testid="versions-table">
              <thead>
                <tr className="border-b border-slate-100 bg-slate-50/50 text-left text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                  <th className="px-5 py-3">Version</th>
                  <th className="px-5 py-3">Status</th>
                  <th className="px-5 py-3">Created by</th>
                  <th className="px-5 py-3"></th>
                </tr>
              </thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.version_id} className="border-t border-slate-100/60 transition-colors hover:bg-slate-50/50">
                    <td className="px-5 py-3">
                      <Link
                        to={`/workflows/${workflowId}/editor/${v.version_id}`}
                        className="font-semibold text-caliber-purple hover:underline"
                      >
                        v{v.version_number}
                      </Link>
                      <div className="mt-0.5 text-[10px] text-slate-300 font-mono">{v.version_id}</div>
                    </td>
                    <td className="px-5 py-3"><StatusBadge status={v.status} /></td>
                    <td className="px-5 py-3 text-slate-500">{v.created_by || "—"}</td>
                    <td className="px-5 py-3 text-right">
                      <Link
                        to={`/workflows/${workflowId}/editor/${v.version_id}`}
                        className="text-xs text-slate-400 hover:text-caliber-purple transition-colors"
                      >
                        Open →
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div data-testid="versions-table">
              <EmptyState
                icon="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"
                title="No versions"
                desc="Versions are created when you save changes in the workflow editor. Start editing to create your first version."
              />
            </div>
          )}
        </div>
      )}

      {/* ── Deployments tab ── */}
      {tab === "deployments" && (
        <div className="rounded-2xl border border-slate-200/60 bg-white shadow-card overflow-hidden">
          <div className="border-b border-slate-100/80 p-5">
            <div className="flex flex-col gap-4 lg:flex-row lg:items-end">
              <label className="w-full text-xs font-semibold text-slate-500 lg:w-56">
                Alias
                <input
                  data-testid="deployment-alias-input"
                  className="mt-1 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 outline-none transition-colors focus:border-caliber-purple"
                  value={deployAlias}
                  onChange={(event) => setDeployAlias(event.target.value)}
                  placeholder="dev"
                />
              </label>
              <label className="w-full text-xs font-semibold text-slate-500 lg:min-w-[16rem] lg:flex-1">
                Published Version
                <select
                  data-testid="deployment-version-select"
                  className="mt-1 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 outline-none transition-colors focus:border-caliber-purple"
                  value={deployVersionId}
                  onChange={(event) => setDeployVersionId(event.target.value)}
                >
                  {publishedVersions.length > 0 ? (
                    publishedVersions.map((version) => (
                      <option key={version.version_id} value={version.version_id}>
                        {`v${version.version_number} · ${version.version_id}`}
                      </option>
                    ))
                  ) : (
                    <option value="">No published versions available</option>
                  )}
                </select>
              </label>
              <button
                type="button"
                data-testid="deployment-promote"
                disabled={
                  deployMut.isPending ||
                  !deployAlias.trim() ||
                  !deployVersionId ||
                  workflowStatus === "archived"
                }
                onClick={() => deployMut.mutate(undefined)}
                className="btn-primary disabled:cursor-not-allowed disabled:opacity-50"
              >
                {deployMut.isPending ? "Deploying..." : "Deploy Alias"}
              </button>
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-slate-500">
              <span className="rounded-md bg-slate-50 px-2 py-1 ring-1 ring-slate-200/70">
                {deployAlias.trim().toLowerCase() === "prod"
                  ? "prod deployments create a promotion request and require approval."
                  : "non-prod aliases rotate immediately once deploy gates pass."}
              </span>
              {deploymentMessage && (
                <span data-testid="deployment-message">{deploymentMessage}</span>
              )}
              {triggerMessage && (
                <span
                  data-testid="trigger-message"
                  className="rounded-md bg-violet-50 px-2 py-1 font-medium text-caliber-purple ring-1 ring-violet-200/70"
                >
                  {triggerMessage}
                </span>
              )}
            </div>
            {showTriggerCapabilityNote && (
              <div
                data-testid="workflow-trigger-capability-note"
                className="mt-3 rounded-xl border border-amber-200/70 bg-amber-50/80 px-3 py-3 text-xs text-amber-900"
              >
                {capabilitiesQuery.isError
                  ? "Event-trigger launches are disabled until workflow run capabilities can be loaded. Verify the CALIBER API and workflow-run settings."
                  : triggerDisabledReason}
              </div>
            )}
          </div>
          {deployments.length > 0 ? (
            <table className="w-full text-sm" data-testid="deployments-table">
              <thead>
                <tr className="border-b border-slate-100 bg-slate-50/50 text-left text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                  <th className="px-5 py-3">Alias</th>
                  <th className="px-5 py-3">Version</th>
                  <th className="px-5 py-3">Deployed by</th>
                  <th className="px-5 py-3 text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {deployments.map((deployment) => (
                  <tr
                    key={deployment.deployment_id}
                    className="border-t border-slate-100/60 transition-colors hover:bg-slate-50/50"
                  >
                    <td className="px-5 py-3 font-semibold text-slate-900">{deployment.alias}</td>
                    <td className="px-5 py-3 font-mono text-xs text-slate-500">
                      {deployment.version_id}
                    </td>
                    <td className="px-5 py-3 text-slate-500">{deployment.deployed_by || "—"}</td>
                    <td className="px-5 py-3 text-right">
                      {eventTriggerAliases.has(deployment.alias) ? (
                        <button
                          type="button"
                          data-testid={`trigger-now-${deployment.alias}`}
                          disabled={triggerMut.isPending || Boolean(triggerDisabledReason)}
                          title={triggerDisabledReason ?? "Start a run via this workflow's event trigger"}
                          onClick={() => {
                            setTriggerMessage(`Triggering ${deployment.alias}…`);
                            triggerMut.mutate(deployment.alias);
                          }}
                          className="rounded-lg border border-caliber-purple/30 bg-caliber-purple/5 px-3 py-1.5 text-xs font-semibold text-caliber-purple transition-colors hover:bg-caliber-purple/10 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          ⚡ Trigger now
                        </button>
                      ) : (
                        <span className="text-xs text-slate-400">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div data-testid="deployments-table">
              <EmptyState
                icon="M5 12h14M12 5l7 7-7 7"
                title="No deployments"
                desc="Deploy a version to make it accessible via an alias. Promote a published version to create your first deployment."
              />
            </div>
          )}
        </div>
      )}

      {/* ── Runs tab ── */}
      {tab === "runs" && (
        <div data-testid="runs-tab" className="space-y-4">
          <div
            data-testid="workflow-run-panel"
            className="rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card"
          >
            <div className="flex flex-col gap-4 lg:flex-row lg:items-end">
              <label className="flex-1 text-xs font-semibold text-slate-500">
                Pipeline Input
                <textarea
                  data-testid="workflow-run-input"
                  className="mt-1 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 outline-none transition-colors focus:border-caliber-purple"
                  rows={3}
                  value={runInput}
                  onChange={(event) => setRunInput(event.target.value)}
                />
              </label>
              <label className="w-full text-xs font-semibold text-slate-500 lg:w-64">
                Session ID
                <input
                  data-testid="workflow-run-session-id"
                  className="mt-1 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 outline-none transition-colors focus:border-caliber-purple"
                  value={runSessionId}
                  onChange={(event) => setRunSessionId(event.target.value)}
                  placeholder="Optional shared conversation key"
                />
              </label>
              <label className="w-full text-xs font-semibold text-slate-500 lg:w-60">
                Target Alias
                <select
                  data-testid="workflow-run-alias"
                  className="mt-1 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 outline-none transition-colors focus:border-caliber-purple"
                  value={runAlias}
                  onChange={(event) => setRunAlias(event.target.value)}
                >
                  {runAliasOptions.map((option) => (
                    <option key={option.alias} value={option.alias}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                data-testid="workflow-run-start"
                disabled={runMut.isPending || Boolean(runStartDisabledReason)}
                title={runStartDisabledReason ?? undefined}
                onClick={() => {
                  if (!runVersionId) return;
                  setRunMessage(queueRunsEnabled ? "Queueing workflow run..." : "Starting workflow run...");
                  setLiveSteps([]);
                  runMut.mutate(undefined);
                }}
                className="btn-primary disabled:cursor-not-allowed disabled:opacity-50"
              >
                {runMut.isPending
                  ? queueRunsEnabled
                    ? "Queueing..."
                    : "Running..."
                  : queueRunsEnabled
                    ? "Queue Run"
                    : "Run Pipeline"}
              </button>
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-slate-500">
              {capabilitiesQuery.isError && (
                <span
                  data-testid="workflow-run-capabilities-note"
                  className="rounded-md bg-rose-50 px-2 py-1 font-semibold text-rose-700 ring-1 ring-rose-200/70"
                >
                  Run controls are disabled until workflow run capabilities can be loaded. Verify the CALIBER API and workflow-run settings.
                </span>
              )}
              <span
                data-testid="workflow-run-target"
                className="rounded-md bg-slate-50 px-2 py-1 font-mono text-slate-500 ring-1 ring-slate-200/70"
              >
                {runVersion
                  ? `alias ${runAlias} · version ${runVersion.version_number} (${runVersion.status})`
                  : runVersionId
                    ? `alias ${runAlias} · ${runVersionId}`
                    : runAlias === "manual"
                      ? "no version"
                      : `alias ${runAlias} is not deployed`}
              </span>
              {runSessionId.trim() && (
                <span className="rounded-md bg-slate-50 px-2 py-1 font-mono text-slate-500 ring-1 ring-slate-200/70">
                  session {runSessionId.trim()}
                </span>
              )}
              {missingAliasDeployment && (
                <span className="rounded-md bg-red-50 px-2 py-1 font-semibold text-red-700 ring-1 ring-red-200/70">
                  Selected alias is not deployed.
                </span>
              )}
              {workflowStatus === "paused" && (
                <span className="rounded-md bg-amber-50 px-2 py-1 font-semibold text-amber-700 ring-1 ring-amber-200/70">
                  paused
                </span>
              )}
              {activeRunId && (
                <span className="rounded-md bg-sky-50 px-2 py-1 font-mono font-semibold text-sky-700 ring-1 ring-sky-200/70">
                  {activeRunId}
                </span>
              )}
              {runMessage && <span data-testid="workflow-run-message">{runMessage}</span>}
            </div>
            <div className="mt-4">
              <StepLogList
                steps={liveSteps}
                emptyText={
                  queueRunsEnabled
                    ? "Live node logs appear here once a worker starts processing this run."
                    : "Live node logs appear here while the workflow runs."
                }
              />
            </div>
          </div>
          {selectedRun ? (
            <div>
              <button
                type="button"
                data-testid="runs-back"
                className="group mb-3 inline-flex items-center gap-1 text-xs text-slate-400 transition-colors hover:text-caliber-purple"
                onClick={() => setSelectedRun(null)}
              >
                <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M15 18l-6-6 6-6" /></svg>
                Back to runs
              </button>
              <div className="mb-3 rounded-2xl border border-slate-200/60 bg-white p-4 shadow-card">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
                    <span className="font-mono text-slate-700">{selectedRun.workflow_run_id}</span>
                    <RunStatusBadge status={selectedRun.status} />
                    <WorkflowRunArtifactPersistenceBadge
                      run={selectedRun}
                      dataTestId="selected-run-artifact-persistence"
                    />
                    {selectedRunVersionNumber != null && (
                      <span
                        data-testid="run-version-chip"
                        className="rounded-md bg-slate-100 px-2 py-1 font-mono font-semibold text-slate-600 ring-1 ring-slate-200/70"
                      >
                        workflow v{selectedRunVersionNumber}
                      </span>
                    )}
                    {selectedRunManifestMode === "snapshot" && (
                      <span
                        data-testid="run-manifest-mode-chip"
                        className="rounded-md bg-violet-50 px-2 py-1 font-mono font-semibold text-violet-700 ring-1 ring-violet-200/70"
                      >
                        draft snapshot
                      </span>
                    )}
                    {selectedPendingApproval && (
                      <span className="rounded-md bg-amber-50 px-2 py-1 font-mono font-semibold text-amber-700 ring-1 ring-amber-200/70">
                        {workflowRunPendingApprovalChipLabel(
                          selectedRunApprovalCheckpointKind,
                        )}{" "}
                        {selectedPendingApproval.runtime_approval_id}
                      </span>
                    )}
                    {selectedRun.status === "waiting_event" && (
                      <span
                        data-testid="run-waiting-event-chip"
                        className="rounded-md bg-sky-50 px-2 py-1 font-mono font-semibold text-sky-700 ring-1 ring-sky-200/70"
                      >
                        {selectedWaitMode === "wait_until" ? "scheduled wait" : "waiting event"}{" "}
                        {selectedRun.current_node_id ?? "resume checkpoint"}
                      </span>
                    )}
                    {selectedRun.mlflow_run_id &&
                      (() => {
                        const expId = wfQuery.data?.default_experiment_id;
                        const chipClass =
                          "rounded-md bg-surface-100 px-2 py-1 font-mono text-slate-600 ring-1 ring-slate-200/70";
                        // Deep-link to the MLflow run UI when we know the experiment id
                        // (the run's workflow carries default_experiment_id); otherwise
                        // show the id so it can be searched in MLflow.
                        return expId ? (
                          <a
                            data-testid="run-mlflow-run-id"
                            href={buildMlflowHref({
                              hash: `#/experiments/${expId}/runs/${selectedRun.mlflow_run_id}`,
                            })}
                            target="_blank"
                            rel="noopener noreferrer"
                            title="Open this run in MLflow"
                            className={`${chipClass} hover:text-caliber-purple hover:underline`}
                          >
                            Open in MLflow ↗
                          </a>
                        ) : (
                          <span
                            data-testid="run-mlflow-run-id"
                            title="MLflow run for this workflow run's trace (search this id in MLflow)"
                            className={chipClass}
                          >
                            MLflow run {selectedRun.mlflow_run_id}
                          </span>
                        );
                      })()}
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Link
                      to={workflowRunPath(selectedRun.workflow_run_id)}
                      target="_blank"
                      rel="noreferrer"
                      data-testid="run-open-link"
                      className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 transition-colors hover:bg-slate-50"
                    >
                      Open link
                    </Link>
                    <button
                      type="button"
                      data-testid="run-copy-link"
                      onClick={() => copyWorkflowRunLink(selectedRun.workflow_run_id)}
                      className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 transition-colors hover:bg-slate-50"
                    >
                      Copy link
                    </button>
                    {canCancelSelectedRun && (
                      <button
                        type="button"
                        data-testid="run-cancel"
                        disabled={selectedRunBusy}
                        onClick={() => cancelRunMut.mutate(selectedRun.workflow_run_id)}
                        className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        Cancel
                      </button>
                    )}
                    {canRetrySelectedRun && (
                      <button
                        type="button"
                        data-testid="run-retry"
                        disabled={selectedRunBusy}
                        onClick={() =>
                          retryRunMut.mutate({
                            runId: selectedRun.workflow_run_id,
                          })}
                        className="rounded-lg border border-violet-200 bg-violet-50 px-3 py-1.5 text-xs font-semibold text-violet-700 transition-colors hover:bg-violet-100 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        Retry
                      </button>
                    )}
                    {canApproveSelectedRun && (
                      <button
                        type="button"
                        data-testid="run-approve"
                        disabled={selectedRunBusy}
                        onClick={() =>
                          approveRunMut.mutate({
                            runId: selectedRun.workflow_run_id,
                            runtimeApprovalId:
                              selectedPendingApproval?.runtime_approval_id,
                          })
                        }
                        className="rounded-lg bg-emerald-500 px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        Approve
                      </button>
                    )}
                    {canRejectSelectedRun && (
                      <button
                        type="button"
                        data-testid="run-reject"
                        disabled={selectedRunBusy}
                        onClick={() =>
                          rejectRunMut.mutate({
                            runId: selectedRun.workflow_run_id,
                            runtimeApprovalId:
                              selectedPendingApproval?.runtime_approval_id,
                          })
                        }
                        className="rounded-lg border border-red-200 bg-red-50 px-3 py-1.5 text-xs font-semibold text-red-700 transition-colors hover:bg-red-100 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        Reject
                      </button>
                    )}
                    {canResumeSelectedRun && (
                      <button
                        type="button"
                        data-testid="run-resume"
                        disabled={selectedRunBusy || Boolean(selectedRunResumeEventNameIssue)}
                        onClick={() => submitSelectedRunResume()}
                        className="rounded-lg bg-sky-500 px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-sky-600 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        Resume
                      </button>
                    )}
                  </div>
                </div>
                {selectedRunApprovalsQuery.isLoading && (
                  <div className="mt-2 text-xs text-slate-400">Loading run approvals…</div>
                )}
                {(showApprovalCapabilityNote || showResumeCapabilityNote) && (
                  <div className="mt-2 space-y-2">
                    {showApprovalCapabilityNote && (
                      <div
                        data-testid="run-approval-capability-note"
                        className="rounded-xl border border-amber-200/70 bg-amber-50/80 px-3 py-3 text-xs text-amber-900"
                      >
                        {selectedRunApprovalQueueIssue
                          ?? selectedRunApprovalRecordsActionIssue
                          ?? approvalCapabilityMessage(selectedRunApprovalSubject)}
                      </div>
                    )}
                    {showResumeCapabilityNote && resumeCapabilityNote && (
                      <div
                        data-testid="run-resume-capability-note"
                        className="rounded-xl border border-sky-200/70 bg-sky-50/70 px-3 py-3 text-xs text-sky-800"
                      >
                        {resumeCapabilityNote}
                      </div>
                    )}
                  </div>
                )}
                {selectedRunManifestMode === "snapshot" && (
                  <div
                    data-testid="run-manifest-mode-notice"
                    className="mt-2 rounded-xl border border-violet-200/70 bg-violet-50/70 px-3 py-3 text-xs text-violet-900"
                  >
                    Viewing the queued draft snapshot captured for this run. Replay and debugger panels stay pinned to that snapshot even if the saved workflow changes afterward.
                  </div>
                )}
                {selectedRunManifestFallbackNotice && (
                  <div
                    data-testid="run-manifest-fallback-notice"
                    className="mt-2 rounded-xl border border-sky-200/70 bg-sky-50/70 px-3 py-3 text-xs text-sky-900"
                  >
                    {selectedRunManifestFallbackNotice}
                  </div>
                )}
                {selectedRunVersionMismatch && selectedRunVersionNumber != null && latest && (
                  <div
                    data-testid="run-version-notice"
                    className="mt-2 rounded-xl border border-sky-200/70 bg-sky-50/70 px-3 py-3 text-xs text-sky-900"
                  >
                    Viewing executed workflow version v{selectedRunVersionNumber}. The latest published version is v{latest.version_number}, so replay and debugger panels stay pinned to the run's original graph for accurate inspection.
                  </div>
                )}
                {!selectedRunManifest && selectedRunManifestLoading && (
                  <div className="mt-2 rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-3 text-xs text-slate-500">
                    Loading the executed workflow graph for this run...
                  </div>
                )}
                {!selectedRunManifest && !selectedRunManifestLoading && (
                  <div
                    data-testid="run-version-missing"
                    className="mt-2 rounded-xl border border-amber-200/70 bg-amber-50/80 px-3 py-3 text-xs text-amber-900"
                  >
                    {selectedRunVersionMissingMessage}
                  </div>
                )}
                {selectedRun.status === "waiting_event" && (
                  selectedWaitMode === "wait_until" ? (
                    <div
                      data-testid="run-wait-until-note"
                      className="mt-2 space-y-3 rounded-xl border border-sky-200/70 bg-sky-50/70 px-3 py-3 text-xs text-sky-800"
                    >
                      <div>
                        This run is paused until {selectedWaitUntilText}. {selectedWaitUntilCapabilityNote}
                      </div>
                    </div>
                  ) : (
                    <div
                      data-testid="run-waiting-event-note"
                      className="mt-2 space-y-3 rounded-xl border border-sky-200/70 bg-sky-50/70 px-3 py-3 text-xs text-sky-800"
                    >
                      <div>
                        This run is paused at an event gate.
                        {selectedWaitEventName
                          ? ` Resume it after ${selectedWaitEventName} has been handled.`
                          : " Resume it after the external event has been handled."}
                      </div>
                      <label className="block text-[11px] font-semibold text-sky-900">
                        Event name
                        <input
                          data-testid="run-resume-event-name"
                          type="text"
                          value={resumeEventName}
                          onChange={(event) => setResumeEventName(event.target.value)}
                          className="mt-1 w-full rounded-lg border border-sky-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 outline-none transition-colors focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                          placeholder="ticket.approved"
                        />
                      </label>
                      <label className="block text-[11px] font-semibold text-sky-900">
                        Event payload JSON
                        <textarea
                          data-testid="run-resume-event-payload"
                          value={resumeEventPayload}
                          onChange={(event) => setResumeEventPayload(event.target.value)}
                          rows={5}
                          className="mt-1 w-full rounded-lg border border-sky-200 bg-white px-3 py-2 font-mono text-[11px] text-slate-700 outline-none transition-colors focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
                          placeholder='{"ticket_id":"T-42","approved":true}'
                        />
                      </label>
                      {selectedRunResumeByEventIssue && (
                        <div
                          data-testid="run-resume-by-event-capability-note"
                          className="rounded-lg border border-sky-200/70 bg-white/80 px-3 py-2 text-[11px] leading-relaxed text-sky-900"
                        >
                          {selectedRunResumeByEventIssue}
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
                            selectedRunBusy
                            || !queueActionEnabled
                            || !checkpointingEnabled
                            || !resumeEventName.trim()
                            || Boolean(selectedRunResumeByEventIssue)
                            || Boolean(selectedRunResumeEventNameIssue)
                            || Boolean(selectedRunMissingResumeCheckpointIssue)
                            || Boolean(selectedRunCheckpointIdentityIssue)
                          }
                          onClick={() => submitSelectedRunResumeByEvent()}
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
              </div>
              <div
                data-testid="run-recovery-section"
                className="mt-3 rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card"
              >
                <div className="mb-3 text-sm font-bold text-slate-900">Recovery Diagnostics</div>
                <WorkflowRunRecoveryPanel
                  run={selectedRun}
                  approvals={selectedRunApprovals}
                  approvalsLoadError={
                    selectedRunApprovalsQuery.isError
                      ? selectedRunApprovalsQuery.error.message
                      : null
                  }
                  checkpoints={selectedRunEffectiveCheckpoints}
                  events={selectedRunEventsQuery.data ?? []}
                  eventsLoadError={
                    selectedRunEventsQuery.isError
                      ? selectedRunEventsQuery.error.message
                      : null
                  }
                  loading={
                    selectedRunEventsQuery.isLoading
                    || selectedRunCheckpointsQuery.isLoading
                    || selectedRunApprovalsQuery.isLoading
                  }
                />
              </div>
              <div
                data-testid="run-lineage-section"
                className="mt-3 rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card"
              >
                <div className="mb-3 text-sm font-bold text-slate-900">Retry Lineage</div>
                <WorkflowRunLineagePanel
                  run={selectedRun}
                  lineage={selectedRunLineageQuery.data ?? null}
                  loading={selectedRunLineageQuery.isLoading}
                  loadError={
                    selectedRunLineageQuery.isError
                      ? selectedRunLineageQuery.error.message
                      : null
                  }
                  runs={[selectedRun, ...runs]}
                  onSelectRun={(item) => setSelectedRun(item)}
                />
              </div>
              {selectedRunManifest ? (
                <div className="rounded-2xl border border-slate-200/60 bg-white shadow-card overflow-hidden">
                  <div className={`grid gap-0 ${selectedRunNodeId && selectedRunManifest.nodes[selectedRunNodeId] ? "xl:grid-cols-[minmax(0,1fr)_20rem]" : "grid-cols-1"}`}>
                    <div className="min-w-0 p-4">
                      {selectedRunEventsQuery.isError ? (
                        <div
                          data-testid="run-trace-replay-events-error"
                          className="rounded-xl border border-red-200/70 bg-red-50 px-4 py-4 text-xs leading-relaxed text-red-700"
                        >
                          {runEventsLoadErrorMessage(
                            selectedRun?.status,
                            selectedRunEventsQuery.error.message,
                          )}
                        </div>
                      ) : (
                        <TraceReplayGraph
                          manifest={selectedRunManifest}
                          run={selectedRun}
                          events={selectedRunEventsQuery.data ?? []}
                          checkpoints={selectedRunEffectiveCheckpoints}
                          selectedNodeId={selectedRunNodeId}
                          onSelectNodeId={focusSelectedRunNode}
                        />
                      )}
                    </div>
                    {selectedRunNodeId && selectedRunManifest.nodes[selectedRunNodeId] && (
                      <div className="border-t border-slate-200/60 bg-white xl:border-l xl:border-t-0">
                        <NodeDetailPanel
                          manifest={selectedRunManifest}
                          nodeId={selectedRunNodeId}
                          componentSpec={
                            workflowComponentMap.get(selectedRunManifest.nodes[selectedRunNodeId].type) ?? null
                          }
                          validationReport={
                            selectedRunManifestMode === "snapshot"
                              ? null
                              : selectedRunVersion?.validation_report ?? null
                          }
                          onClose={() => focusSelectedRunNode(null)}
                        />
                      </div>
                    )}
                  </div>
                </div>
              ) : (
                <div className="rounded-2xl border border-dashed border-amber-200/80 bg-amber-50/60 px-4 py-6 text-sm text-amber-900">
                  {selectedRunVersionMissingMessage}
                </div>
              )}
              <div className="mt-3 rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card">
                <div className="mb-3 text-sm font-bold text-slate-900">Execution Debugger</div>
                {!selectedRunManifest && selectedRunManifestLoading ? (
                  <div className="rounded-xl border border-slate-100 bg-slate-50 px-3 py-2 text-xs text-slate-400">
                    Loading executed manifest...
                  </div>
                ) : !selectedRunManifest ? (
                  <div className="rounded-xl border border-dashed border-amber-200/80 bg-amber-50/60 px-4 py-4 text-xs text-amber-900">
                    {selectedRunVersionMissingMessage}
                  </div>
                ) : selectedRunEventsQuery.isError ? (
                  <div
                    data-testid="run-debugger-events-error"
                    className="rounded-xl border border-red-200/70 bg-red-50 px-4 py-4 text-xs leading-relaxed text-red-700"
                  >
                    {runEventsLoadErrorMessage(
                      selectedRun?.status,
                      selectedRunEventsQuery.error.message,
                    )}
                  </div>
                ) : selectedRunEventsQuery.isLoading ? (
                  <div className="rounded-xl border border-slate-100 bg-slate-50 px-3 py-2 text-xs text-slate-400">
                    Loading run events...
                  </div>
                ) : (
                  <WorkflowRunDebugger
                    manifest={selectedRunManifest}
                    run={selectedRun}
                    events={selectedRunEventsQuery.data ?? []}
                    checkpoints={selectedRunEffectiveCheckpoints}
                    focusedNodeId={selectedRunNodeId}
                    onSelectNodeId={focusSelectedRunNode}
                  />
                )}
              </div>
              <div
                data-testid="run-checkpoints-section"
                className="mt-3 rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card"
              >
                <div className="mb-3 text-sm font-bold text-slate-900">Resume Checkpoints</div>
                {selectedRunCheckpointsQuery.isError ? (
                  <div
                    data-testid="workflow-run-checkpoints-error"
                    className="rounded-xl border border-red-200/70 bg-red-50 px-4 py-3 text-xs leading-relaxed text-red-700"
                  >
                    {checkpointLoadErrorMessage(
                      selectedRun?.status,
                      selectedRunCheckpointsQuery.error.message,
                    )}
                  </div>
                ) : (
                    <WorkflowRunCheckpointPanel
                      run={selectedRun}
                      checkpoints={selectedRunCheckpointsQuery.data ?? []}
                      loading={selectedRunCheckpointsQuery.isLoading}
                      resumeSourceCheckpoint={selectedRunResumeSourceCheckpoint}
                      resumeSourceCheckpointLoading={selectedRunResumeSourceCheckpointsQuery.isLoading}
                      resumeSourceCheckpointError={
                        selectedRunResumeSourceCheckpointsQuery.isError
                          ? selectedRunResumeSourceCheckpointsQuery.error.message
                          : null
                      }
                      canRetryFromCheckpoint={Boolean(canRetrySelectedRun)}
                      retryingCheckpointId={
                        retryRunMut.isPending
                          ? retryRunMut.variables?.checkpointId ?? null
                          : null
                      }
                      onRetryFromCheckpoint={(checkpointId) =>
                        retryRunMut.mutate({
                          runId: selectedRun.workflow_run_id,
                          checkpointId,
                      })}
                  />
                )}
              </div>
              <div
                data-testid="run-session-memory-section"
                className="mt-3 rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card"
              >
                {selectedRunSessionMemoryQuery.isError ? (
                  <div
                    data-testid="workflow-session-memory-error"
                    className="rounded-xl border border-red-200/70 bg-red-50 px-4 py-3 text-xs leading-relaxed text-red-700"
                  >
                    {sessionMemoryLoadErrorMessage(
                      selectedRun?.status,
                      selectedRunSessionMemoryQuery.error.message,
                    )}
                  </div>
                ) : (
                  <WorkflowSessionMemoryPanel
                    sessionId={selectedRun.session_id}
                    runStatus={selectedRun.status}
                    entries={selectedRunSessionMemoryQuery.data ?? []}
                    loading={selectedRunSessionMemoryQuery.isLoading}
                    clearingSession={
                      clearSessionMemoryMut.isPending && !clearSessionMemoryMut.variables?.nodeId
                    }
                    clearingNodeId={clearSessionMemoryMut.variables?.nodeId ?? null}
                    onClearSession={
                      selectedRun.session_id
                        ? () => clearSessionMemoryMut.mutate({ sessionId: selectedRun.session_id! })
                        : undefined
                    }
                    onClearNode={
                      selectedRun.session_id
                        ? (nodeId: string) =>
                            clearSessionMemoryMut.mutate({
                              sessionId: selectedRun.session_id!,
                              nodeId,
                            })
                        : undefined
                    }
                  />
                )}
              </div>
                <div className="mt-3 rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card">
                <div className="mb-3 text-sm font-bold text-slate-900">Run Logs</div>
                <StepLogList
                  steps={selectedRun.summary?.steps ?? []}
                  emptyText={emptyRunLogsMessage(selectedRun)}
                />
              </div>
              <div
                data-testid="run-trace-section"
                className="mt-3 rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card"
              >
                <WorkflowRunTracePanel
                  runId={selectedRun.workflow_run_id}
                  runStatus={selectedRun.status}
                />
              </div>
              <div className="mt-3 rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card">
                <div className="mb-3 text-sm font-bold text-slate-900">Files & Artifact Lineage</div>
                <RunFilePanel
                  runId={selectedRun.workflow_run_id}
                  runStatus={selectedRun.status}
                  runSummary={selectedRun.summary}
                  selectedNodeId={selectedRunNodeId}
                  onSelectNodeId={focusSelectedRunNode}
                />
              </div>
            </div>
          ) : (
            <div className="rounded-2xl border border-slate-200/60 bg-white shadow-card overflow-hidden">
              <div className="border-b border-slate-200/60 px-5 py-4">
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div className="min-w-0 flex-1">
                    <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-400">
                      Run Triage
                    </div>
                    <div className="mt-1 text-sm font-semibold text-slate-900">
                      Filter execution history by artifact upload outcome.
                    </div>
                    <div className="mt-3 flex flex-wrap gap-2">
                      {([
                        {
                          key: "all",
                          label: "All runs",
                          count: runCount,
                          activeClass:
                            "border-slate-300 bg-slate-100 text-slate-700",
                          idleClass:
                            "border-slate-200 bg-white text-slate-500 hover:border-slate-300 hover:text-slate-700",
                        },
                        {
                          key: "upload_failed",
                          label: "Artifact upload failed",
                          count: artifactUploadFailedRunCount,
                          activeClass:
                            "border-red-200 bg-red-50 text-red-700",
                          idleClass:
                            "border-slate-200 bg-white text-slate-500 hover:border-red-200 hover:text-red-700",
                        },
                        {
                          key: "artifacts_stored",
                          label: "Artifacts stored",
                          count: artifactPersistedRunCount,
                          activeClass:
                            "border-emerald-200 bg-emerald-50 text-emerald-700",
                          idleClass:
                            "border-slate-200 bg-white text-slate-500 hover:border-emerald-200 hover:text-emerald-700",
                        },
                      ] as Array<{
                        key: RunArtifactTriageFilter;
                        label: string;
                        count: number;
                        activeClass: string;
                        idleClass: string;
                      }>).map((option) => {
                        const active = runArtifactFilter === option.key;
                        return (
                          <button
                            key={option.key}
                            type="button"
                            data-testid={`runs-filter-${option.key}`}
                            onClick={() => setRunArtifactFilter(option.key)}
                            className={`rounded-full border px-3 py-1.5 text-xs font-semibold transition ${
                              active ? option.activeClass : option.idleClass
                            }`}
                          >
                            {option.label} ({option.count})
                          </button>
                        );
                      })}
                    </div>
                  </div>
                  <div className="w-full sm:ml-auto sm:w-80">
                    <SearchInput
                      value={runSearch}
                      onChange={setRunSearch}
                      ariaLabel="Search runs"
                      placeholder="Search runs, traces, artifacts…"
                      className="w-full"
                    />
                  </div>
                </div>
                <p
                  data-testid="runs-triage-summary"
                  className="mt-3 text-[11px] text-slate-400"
                >
                  {runTriageSummary}
                </p>
              </div>
              {runsTabHistoryQuery.isError ? (
                <div
                  data-testid="runs-query-fallback"
                  className="border-b border-amber-100 bg-amber-50/80 px-5 py-3 text-xs text-amber-800"
                >
                  Full-history run search is temporarily unavailable. Showing the recent run index instead.
                </div>
              ) : null}
              {!runsTabHistoryQuery.isError && runHistoryStatsQuery.isError ? (
                <div
                  data-testid="runs-stats-fallback"
                  className="border-b border-amber-100 bg-amber-50/80 px-5 py-3 text-xs text-amber-800"
                >
                  Exact run totals are temporarily unavailable. Using recent history estimates instead.
                </div>
              ) : null}
              {runsTabHistoryQuery.isLoading ? (
                <div
                  data-testid="runs-table-loading"
                  className="px-5 py-6 text-sm text-slate-500"
                >
                  Loading workflow runs…
                </div>
              ) : filteredRuns.length > 0 ? (
                <>
                  <table className="w-full text-sm" data-testid="runs-table">
                    <thead>
                      <tr className="border-b border-slate-100 bg-slate-50/50 text-left text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                        <th className="px-5 py-3">Run</th>
                        <th className="px-5 py-3">Status</th>
                        <th className="px-5 py-3">Version</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredRuns.map((run) => (
                        <tr key={run.workflow_run_id} className="border-t border-slate-100/60 transition-colors hover:bg-slate-50/50">
                          <td className="px-5 py-3">
                            <div className="flex items-start justify-between gap-3">
                              <button
                                type="button"
                                data-testid={`run-${run.workflow_run_id}`}
                                className="min-w-0 text-left font-semibold text-caliber-purple hover:underline"
                                onClick={() => setSelectedRun(run)}
                              >
                                <span className="block truncate">
                                  {run.trace_id ?? run.workflow_run_id}
                                </span>
                                {run.trace_id && (
                                  <span className="mt-1 block font-mono text-[11px] font-medium text-slate-400">
                                    {run.workflow_run_id}
                                  </span>
                                )}
                              </button>
                              <Link
                                to={workflowRunPath(run.workflow_run_id)}
                                target="_blank"
                                rel="noreferrer"
                                data-testid={`run-open-link-${run.workflow_run_id}`}
                                className="inline-flex shrink-0 items-center rounded-md border border-slate-200 bg-white px-2 py-1 text-[10px] font-semibold text-slate-500 transition-colors hover:border-caliber-300 hover:text-caliber-700"
                              >
                                Open
                              </Link>
                            </div>
                          </td>
                          <td className="px-5 py-3">
                            <div className="flex flex-col items-start gap-1">
                              <RunStatusBadge status={run.status} />
                              <WorkflowRunArtifactPersistenceBadge
                                run={run}
                                compact
                                dataTestId={`run-artifact-persistence-${run.workflow_run_id}`}
                              />
                            </div>
                          </td>
                          <td className="px-5 py-3 text-slate-500 font-mono text-xs">{run.workflow_version_id ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {runsTabHistoryQuery.hasNextPage ? (
                    <div className="border-t border-slate-100/80 px-5 py-4">
                      <button
                        type="button"
                        data-testid="runs-load-more"
                        onClick={() => {
                          void runsTabHistoryQuery.fetchNextPage();
                        }}
                        disabled={runsTabHistoryQuery.isFetchingNextPage}
                        className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {runsTabHistoryQuery.isFetchingNextPage ? "Loading more runs…" : "Load more runs"}
                      </button>
                    </div>
                  ) : null}
                </>
              ) : normalizedRunSearch.length > 0 || runArtifactFilter !== "all" || runsTabRows.length > 0 ? (
                <div data-testid="runs-filtered-empty" className="p-5">
                  <EmptyState
                    icon="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664zM21 12a9 9 0 11-18 0 9 9 0 0118 0z"
                    title="No runs match the current triage view"
                    desc={filteredRunHistoryMessage({
                      search: runSearch.trim(),
                      filter: runArtifactFilter,
                    })}
                  />
                </div>
              ) : (
                <div data-testid="runs-table">
                  <EmptyState
                    icon="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664zM21 12a9 9 0 11-18 0 9 9 0 0118 0z"
                    title="No runs recorded"
                    desc={emptyRunHistoryMessage({
                      capabilitiesUnavailable: capabilitiesQuery.isError,
                      queueRunsEnabled,
                      workflowStatus,
                      runAlias,
                      runVersionId,
                    })}
                  />
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* ── Service tab ── */}
      {tab === "service" && (
        <div className="space-y-4" data-testid="service-tab">
          <div>
            <h2 className="text-base font-bold text-slate-900">Service</h2>
            <p className="mt-0.5 text-sm text-slate-500">
              Expose this workflow as a callable HTTP endpoint.
            </p>
          </div>

          {serviceMessage && (
            <div
              className="rounded-xl border border-slate-200/70 bg-slate-50 px-4 py-2.5 text-xs text-slate-600"
              data-testid="service-message"
            >
              {serviceMessage}
            </div>
          )}

          {serviceQuery.isLoading ? (
            <div className="rounded-2xl border border-slate-200/60 bg-white px-5 py-8 text-center text-sm text-slate-400 shadow-card">
              Loading service…
            </div>
          ) : serviceQuery.data ? (
            (() => {
              const service: WorkflowService = serviceQuery.data;
              const fullEndpoint = `${window.location.origin}${service.endpoint}`;
              const curlSnippet = `curl -X POST '${fullEndpoint}' \\\n  -H 'Content-Type: application/json' \\\n  -d '{"input": {}}'`;
              const openapiUrl = `${window.location.origin}${service.endpoint.replace(/\/invoke$/, "/openapi.json")}`;
              return (
                <div className="rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card space-y-5">
                  <div className="flex items-center gap-2">
                    {service.auth_required ? (
                      <span className="inline-flex items-center gap-1 rounded-md bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700 ring-1 ring-amber-200/60">
                        Token required
                      </span>
                    ) : (
                      <span
                        className="inline-flex items-center gap-1 rounded-md bg-emerald-50 px-2 py-0.5 text-[10px] font-semibold text-emerald-700 ring-1 ring-emerald-200/60"
                        data-testid="service-auth-badge"
                      >
                        Open · no auth
                      </span>
                    )}
                    {!service.auth_required && (
                      <span className="text-[11px] text-slate-400">
                        Authentication can be enabled later.
                      </span>
                    )}
                  </div>

                  <div>
                    <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                      Endpoint
                    </div>
                    <div className="flex items-stretch gap-2">
                      <code
                        data-testid="service-endpoint"
                        className="flex-1 truncate rounded-lg border border-slate-200/70 bg-slate-50 px-3 py-2 font-mono text-xs text-slate-700"
                      >
                        {fullEndpoint}
                      </code>
                      <button
                        type="button"
                        data-testid="service-endpoint-copy-btn"
                        onClick={() => copyServiceText("endpoint", fullEndpoint)}
                        className="rounded-lg border border-slate-200/70 bg-white px-3 py-2 text-xs font-semibold text-slate-600 transition-colors hover:bg-slate-50"
                      >
                        {serviceCopied === "endpoint" ? "Copied" : "Copy"}
                      </button>
                      <a
                        href={openapiUrl}
                        target="_blank"
                        rel="noreferrer"
                        data-testid="service-openapi-link"
                        className="rounded-lg border border-slate-200/70 bg-white px-3 py-2 text-xs font-semibold text-slate-600 transition-colors hover:bg-slate-50"
                      >
                        OpenAPI
                      </a>
                    </div>
                  </div>

                  <div className="grid gap-4 lg:grid-cols-2">
                    <div>
                      <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                        Input schema
                      </div>
                      <pre
                        data-testid="service-input-schema"
                        className="max-h-48 overflow-auto rounded-lg border border-slate-200/70 bg-slate-50 px-3 py-2 font-mono text-[11px] leading-relaxed text-slate-600"
                      >
                        {JSON.stringify(service.input_schema, null, 2)}
                      </pre>
                    </div>
                    <div>
                      <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                        Output schema
                      </div>
                      <pre
                        data-testid="service-output-schema"
                        className="max-h-48 overflow-auto rounded-lg border border-slate-200/70 bg-slate-50 px-3 py-2 font-mono text-[11px] leading-relaxed text-slate-600"
                      >
                        {JSON.stringify(service.output_schema, null, 2)}
                      </pre>
                    </div>
                  </div>

                  <div>
                    <div className="mb-1.5 flex items-center justify-between">
                      <div className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                        Invoke (curl)
                      </div>
                      <button
                        type="button"
                        data-testid="service-curl-copy-btn"
                        onClick={() => copyServiceText("curl", curlSnippet)}
                        className="rounded-lg border border-slate-200/70 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 transition-colors hover:bg-slate-50"
                      >
                        {serviceCopied === "curl" ? "Copied" : "Copy"}
                      </button>
                    </div>
                    <pre
                      data-testid="service-curl"
                      className="overflow-auto rounded-lg border border-slate-900/10 bg-slate-900 px-3 py-2 font-mono text-[11px] leading-relaxed text-slate-100"
                    >
                      {curlSnippet}
                    </pre>
                  </div>

                  <div className="border-t border-slate-100 pt-4">
                    <button
                      type="button"
                      data-testid="service-unpublish-btn"
                      onClick={() => unpublishServiceMut.mutate(undefined)}
                      disabled={unpublishServiceMut.isPending}
                      className="rounded-xl border border-red-200 bg-red-50 px-4 py-2 text-xs font-semibold text-red-600 transition-colors hover:bg-red-100 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {unpublishServiceMut.isPending ? "Unpublishing…" : "Unpublish"}
                    </button>
                  </div>
                </div>
              );
            })()
          ) : !hasLiveDeployment ? (
            <div className="rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card space-y-4">
              <div
                className="rounded-xl border border-amber-200/70 bg-amber-50 px-4 py-3 text-sm text-amber-800"
                data-testid="service-no-deployment-notice"
              >
                Deploy a published version first, then publish it as a service.
              </div>
              <button
                type="button"
                data-testid="service-publish-btn"
                disabled
                className="rounded-xl bg-caliber-purple px-4 py-2 text-sm font-semibold text-white opacity-50 disabled:cursor-not-allowed"
              >
                Publish as service
              </button>
            </div>
          ) : (
            <div className="rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card space-y-4">
              <p className="text-sm text-slate-500">
                This workflow has a live deployment. Publish it as a callable HTTP
                service to share its endpoint.
              </p>
              <button
                type="button"
                data-testid="service-publish-btn"
                onClick={() => publishServiceMut.mutate(undefined)}
                disabled={publishServiceMut.isPending}
                className="rounded-xl bg-caliber-purple px-4 py-2 text-sm font-semibold text-white transition-all hover:opacity-90 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50"
              >
                {publishServiceMut.isPending ? "Publishing…" : "Publish as service"}
              </button>
            </div>
          )}
        </div>
      )}

      {/* ── Promotions tab ── */}
      {tab === "promotions" && (
        <div data-testid="promotions-list">
          {(promotionsQuery.data ?? []).length > 0 ? (
            <div className="space-y-3">
              {(promotionsQuery.data ?? []).map((p) => (
                <div key={p.promotion_id} className="rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card transition-all hover:shadow-card-hover">
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                      <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-brand-subtle">
                        <svg className="h-4 w-4 text-caliber-purple" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"><path d="M5 10l7-7m0 0l7 7m-7-7v18" /></svg>
                      </div>
                      <div>
                        <span className="text-sm font-bold text-slate-900">{p.alias}</span>
                        <span className="mx-2 text-slate-300">→</span>
                        <span className="font-mono text-xs text-slate-500">{p.version_id}</span>
                        <div className="mt-0.5">
                          <StatusBadge status={p.status} />
                        </div>
                      </div>
                    </div>
                    {p.status === "pending" && (
                      <div className="flex gap-2">
                        <button
                          type="button"
                          data-testid={`approve-${p.promotion_id}`}
                          onClick={() => approveMut.mutate(p.promotion_id)}
                          className="rounded-xl bg-emerald-500 px-4 py-2 text-xs font-semibold text-white shadow-sm transition-all hover:bg-emerald-600 hover:shadow-md active:scale-[0.97]"
                        >
                          Approve
                        </button>
                        <button
                          type="button"
                          data-testid={`reject-${p.promotion_id}`}
                          onClick={() => rejectMut.mutate(p.promotion_id)}
                          className="rounded-xl border border-slate-200 bg-white px-4 py-2 text-xs font-semibold text-slate-600 transition-all hover:bg-slate-50 hover:border-slate-300 active:scale-[0.97]"
                        >
                          Reject
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState
              icon="M5 10l7-7m0 0l7 7m-7-7v18"
              title="No promotion requests"
              desc="Promotions move a published version to a deployment alias. Request a promotion from the editor or version list."
            />
          )}
        </div>
      )}

      {/* ── Patches tab ── */}
      {tab === "patches" && (
        <div data-testid="patches-list">
          {(patchesQuery.data ?? []).length > 0 ? (
            <div className="space-y-3">
              {(patchesQuery.data ?? []).map((patch) => (
                <div key={patch.patch_id} className="rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card transition-all hover:shadow-card-hover">
                  <div className="flex items-start gap-3">
                    <div className="mt-0.5 flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-brand-subtle flex-shrink-0">
                      <svg className="h-4 w-4 text-caliber-purple" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75"><path d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" /></svg>
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-bold text-slate-900">{patch.patch_summary || patch.patch_id}</div>
                      <div className="mt-1 text-xs text-slate-400 leading-relaxed">{patch.risk_summary}</div>
                      {patch.graph_diff && (
                        <div className="mt-3">
                          <GraphDiff diff={patch.graph_diff} />
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState
              icon="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"
              title="No CALIBER patches yet"
              desc="Patches are generated by the refinement pipeline when it discovers improvements to this workflow's agent behavior."
            />
          )}
        </div>
      )}
    </div>
  );
}
