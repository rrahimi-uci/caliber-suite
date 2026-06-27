import { useMemo } from "react";

import type {
  WorkflowRun,
  WorkflowRunCheckpoint,
  WorkflowRunEvent,
  WorkflowRuntimeApproval,
} from "@/api/workflowTypes";
import {
  approvalCheckpointKind,
  type WorkflowRunApprovalCheckpointKind,
  workflowRunApprovalBlockedLabel,
  workflowRunApprovalNoun,
  workflowRunApprovalRecordNoun,
  workflowRunApprovalRecordedLabel,
  workflowRunApprovalRejectedLabel,
  workflowRunApprovalTitle,
  workflowRunAwaitingApprovalLabel,
  workflowRunCheckpointLabel,
  workflowRunLifecycleReason,
  workflowRunLifecycleSummary,
  workflowRunStatusLabel,
  workflowRunStatusPhrase,
  workflowRunStatusRingClass,
} from "@/lib/workflowRunLabels";
import { workflowRunArtifactPersistence } from "@/lib/workflowRunSummary";

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled", "rejected", "expired"]);
const RECOVERY_EVENT_TYPES = new Set([
  "workflow.run.queued",
  "workflow.run.recovered",
  "workflow.run.started",
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

interface WorkflowRunRecoveryPanelProps {
  run: WorkflowRun;
  approvals?: WorkflowRuntimeApproval[];
  checkpoints?: WorkflowRunCheckpoint[];
  events?: WorkflowRunEvent[];
  loading?: boolean;
  approvalsLoadError?: string | null;
  eventsLoadError?: string | null;
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "n/a";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readPositiveNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed > 0) {
      return parsed;
    }
  }
  return null;
}

function formatValue(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function formatDeadline(
  createdAt: string | null | undefined,
  timeoutSeconds: number | null,
): string | null {
  if (!createdAt || timeoutSeconds === null) return null;
  const created = new Date(createdAt);
  if (Number.isNaN(created.getTime())) return null;
  return new Date(created.getTime() + timeoutSeconds * 1000).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function checkpointState(checkpoint: WorkflowRunCheckpoint | null): Record<string, unknown> | null {
  return isRecord(checkpoint?.state_blob) ? checkpoint.state_blob : null;
}

function checkpointNodeIntegrityWarning({
  checkpointId,
  checkpointNodeId,
  stateNodeId,
  currentNodeId,
}: {
  checkpointId: string | null;
  checkpointNodeId: string | null;
  stateNodeId: string | null;
  currentNodeId: string | null;
}): string | null {
  const issues: string[] = [];
  if (currentNodeId && checkpointNodeId && checkpointNodeId !== currentNodeId) {
    issues.push(`checkpoint row points at ${checkpointNodeId} instead of active node ${currentNodeId}`);
  }
  if (currentNodeId && stateNodeId && stateNodeId !== currentNodeId) {
    issues.push(`checkpoint payload points at ${stateNodeId} instead of active node ${currentNodeId}`);
  }
  if (checkpointNodeId && stateNodeId && checkpointNodeId !== stateNodeId) {
    issues.push(`checkpoint row and payload disagree (${checkpointNodeId} vs ${stateNodeId})`);
  }
  if (issues.length === 0) return null;
  const label = checkpointId ? `Checkpoint ${checkpointId}` : "The active checkpoint";
  return `${label} has inconsistent node identity: ${issues.join("; ")}. Treat the stored checkpoint as recovery evidence only, then inspect lineage, debugger state, and run events before attempting resume or retry.`;
}

function approvalPolicy(
  approval: WorkflowRuntimeApproval | null | undefined,
): {
  requiredRole: string | null;
  approvalCount: number | null;
  timeoutBehavior: string | null;
} {
  const snapshot = isRecord(approval?.policy_snapshot) ? approval.policy_snapshot : null;
  const requiredRole = readString(snapshot?.required_role);
  const rawApprovalCount = snapshot?.approval_count;
  const approvalCount =
    typeof rawApprovalCount === "number" && Number.isFinite(rawApprovalCount)
      ? Math.max(1, Math.trunc(rawApprovalCount))
      : null;
  const timeoutBehavior = readString(snapshot?.timeout_behavior);
  return { requiredRole, approvalCount, timeoutBehavior };
}

function statusTone(status: string): string {
  switch (status) {
    case "waiting_approval":
      return "bg-amber-50 text-amber-700 ring-amber-200/80 dark:bg-amber-950/40 dark:text-amber-300 dark:ring-amber-800/70";
    case "waiting_event":
      return "bg-sky-50 text-sky-700 ring-sky-200/80 dark:bg-sky-950/40 dark:text-sky-300 dark:ring-sky-800/70";
    case "running":
      return "bg-emerald-50 text-emerald-700 ring-emerald-200/80 dark:bg-emerald-950/40 dark:text-emerald-300 dark:ring-emerald-800/70";
    case "queued":
      return "bg-violet-50 text-violet-700 ring-violet-200/80 dark:bg-violet-950/40 dark:text-violet-300 dark:ring-violet-800/70";
    case "completed":
      return "bg-emerald-50 text-emerald-700 ring-emerald-200/80 dark:bg-emerald-950/40 dark:text-emerald-300 dark:ring-emerald-800/70";
    case "failed":
    case "rejected":
      return "bg-red-50 text-red-700 ring-red-200/80 dark:bg-red-950/40 dark:text-red-300 dark:ring-red-800/70";
    default:
      return "bg-slate-100 text-slate-600 ring-slate-200/80 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700/70";
  }
}

function noteTone(kind: "info" | "warn" | "danger"): string {
  switch (kind) {
    case "warn":
      return "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-800/70 dark:bg-amber-950/30 dark:text-amber-200";
    case "danger":
      return "border-red-200 bg-red-50 text-red-800 dark:border-red-800/70 dark:bg-red-950/30 dark:text-red-200";
    default:
      return "border-sky-200 bg-sky-50 text-sky-800 dark:border-sky-800/70 dark:bg-sky-950/30 dark:text-sky-200";
  }
}

function artifactStatusTone(status: string): string {
  switch (status) {
    case "persisted":
      return "bg-emerald-50 text-emerald-700 ring-emerald-200/80 dark:bg-emerald-950/40 dark:text-emerald-300 dark:ring-emerald-800/70";
    case "failed":
      return "bg-red-50 text-red-700 ring-red-200/80 dark:bg-red-950/40 dark:text-red-300 dark:ring-red-800/70";
    default:
      return "bg-slate-100 text-slate-600 ring-slate-200/80 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700/70";
  }
}

function artifactStatusLabel(status: string): string {
  switch (status) {
    case "persisted":
      return "Persisted";
    case "failed":
      return "Failed";
    default:
      return status;
  }
}

function recoveryTimelineEmptyMessage({
  status,
  hasApprovals,
  hasCheckpoint,
}: {
  status: string;
  hasApprovals: boolean;
  hasCheckpoint: boolean;
}): string {
  const activeRun =
    status === "queued"
    || status === "running"
    || status === "resuming"
    || status === "cancel_requested";
  const stoppedRun =
    status === "failed"
    || status === "cancelled"
    || status === "rejected"
    || status === "expired"
    || status === "blocked";
  const completedRun = status === "completed";

  if (hasApprovals && hasCheckpoint) {
    if (activeRun) {
      return "No recovery-specific lifecycle events have been recorded yet. This run may still be executing or recovery persistence may still be catching up, so use the approval card and stored checkpoint details above while execution continues.";
    }
    if (completedRun) {
      return "No recovery-specific lifecycle events have been recorded yet. This execution completed without stored recovery history, so use the approval card, stored checkpoint details, and final outputs above to reconstruct how it crossed the active resume gate.";
    }
    if (stoppedRun) {
      return "No recovery-specific lifecycle events have been recorded yet. Use the approval card, stored checkpoint details, and debugger state above to trace where execution stopped after recovery state was captured.";
    }
    return "No recovery-specific lifecycle events have been recorded yet. Use the approval card and stored checkpoint details above to trace the active resume gate until lifecycle history catches up.";
  }
  if (hasCheckpoint) {
    if (activeRun) {
      return "No recovery-specific lifecycle events have been recorded yet. This run may still be executing or recovery persistence may still be catching up, so use the stored checkpoint details above while execution continues.";
    }
    if (completedRun) {
      return "No recovery-specific lifecycle events have been recorded yet. This execution completed without stored recovery history, so use the stored checkpoint details, debugger state, and final outputs above to reconstruct how it finished.";
    }
    if (stoppedRun) {
      return "No recovery-specific lifecycle events have been recorded yet. Use the stored checkpoint details and debugger state above to trace where execution stopped.";
    }
    return "No recovery-specific lifecycle events have been recorded yet. Use the stored checkpoint details above to trace the active resume gate until lifecycle history catches up.";
  }
  if (hasApprovals) {
    if (activeRun) {
      return "No recovery-specific lifecycle events have been recorded yet. This run may still be executing or recovery persistence may still be catching up, so use the approval details above while execution continues.";
    }
    if (completedRun) {
      return "No recovery-specific lifecycle events have been recorded yet. This execution completed without stored recovery history, so use the approval details, debugger state, and final outputs above to confirm how it crossed the approval boundary.";
    }
    if (stoppedRun) {
      return "No recovery-specific lifecycle events have been recorded yet. Use the approval details and debugger state above to confirm what was still blocking or failing when execution stopped.";
    }
    return "No recovery-specific lifecycle events have been recorded yet. Use the approval details above to confirm what is still blocking this run until lifecycle history catches up.";
  }
  if (activeRun) {
    return "No recovery-specific lifecycle events have been recorded yet. This run may still be executing or recovery persistence may still be catching up, so use the current status, heartbeat, and lease details above while execution continues.";
  }
  if (completedRun) {
    return "No recovery-specific lifecycle events have been recorded yet. This execution completed without stored recovery history, so use the current status, debugger state, and final outputs above to reconstruct how it finished.";
  }
  if (stoppedRun) {
    return "No recovery-specific lifecycle events have been recorded yet. Use the current status, recovery summary, and debugger details above to trace where execution stopped.";
  }
  return "No recovery-specific lifecycle events have been recorded yet. Use the current status, heartbeat, and lease details above to verify whether the worker has started emitting recovery history.";
}

export function recoveryTimelineLoadErrorMessage({
  status,
  hasApprovals,
  hasCheckpoint,
  errorMessage,
}: {
  status: string;
  hasApprovals: boolean;
  hasCheckpoint: boolean;
  errorMessage: string;
}): JSX.Element {
  const detail = errorMessage.trim() || "Unknown error";
  if (
    status === "queued"
    || status === "running"
    || status === "resuming"
    || status === "cancel_requested"
  ) {
    return (
      <>
        Recovery timeline events could not be loaded while this run is still active.{" "}
        {hasApprovals && hasCheckpoint
          ? "Use the approval card and stored checkpoint details above to follow the active recovery state until lifecycle history catches up."
          : hasCheckpoint
            ? "Use the stored checkpoint details above to follow the active recovery state until lifecycle history catches up."
            : hasApprovals
              ? "Use the approval details above to follow the active recovery state until lifecycle history catches up."
              : "Use the current status, heartbeat, and lease details above while lifecycle history catches up."}
        <span className="mt-2 block text-red-700/80">Latest recovery event error: {detail}</span>
      </>
    );
  }
  if (status === "completed") {
    return (
      <>
        Recovery timeline events could not be loaded for this completed run.{" "}
        {hasApprovals && hasCheckpoint
          ? "Use the approval card, stored checkpoint details, final outputs, and debugger state above to reconstruct how this execution crossed its recovery boundaries."
          : hasCheckpoint
            ? "Use the stored checkpoint details, final outputs, and debugger state above to reconstruct how this execution finished."
            : hasApprovals
              ? "Use the approval details, final outputs, and debugger state above to reconstruct how this execution crossed its approval boundary."
              : "Use the final outputs and debugger state above to reconstruct how this execution finished."}
        <span className="mt-2 block text-red-700/80">Latest recovery event error: {detail}</span>
      </>
    );
  }
  if (
    status === "failed"
    || status === "cancelled"
    || status === "rejected"
    || status === "expired"
    || status === "blocked"
  ) {
    return (
      <>
        Recovery timeline events could not be loaded for this stopped run.{" "}
        {hasApprovals && hasCheckpoint
          ? "Use the approval card, stored checkpoint details, and debugger state above to trace where execution stopped."
          : hasCheckpoint
            ? "Use the stored checkpoint details and debugger state above to trace where execution stopped."
            : hasApprovals
              ? "Use the approval details and debugger state above to trace where execution stopped."
              : "Use the current status and debugger state above to trace where execution stopped."}
        <span className="mt-2 block text-red-700/80">Latest recovery event error: {detail}</span>
      </>
    );
  }
  return (
    <>
      Recovery timeline events could not be loaded for this run. Use the remaining recovery
      diagnostics above to inspect execution state until lifecycle history is restored.
      <span className="mt-2 block text-red-700/80">Latest recovery event error: {detail}</span>
    </>
  );
}

function approvalRecordsEmptyMessage({
  status,
  approvalKind,
  hasCheckpoint,
  hasHistoricalApprovals,
}: {
  status: string;
  approvalKind: WorkflowRunApprovalCheckpointKind | null;
  hasCheckpoint: boolean;
  hasHistoricalApprovals: boolean;
}): string {
  const historicalSuffix = hasHistoricalApprovals
    ? " Earlier approval rows exist on other nodes, but they do not unblock the active gate."
    : "";
  if (status === "waiting_approval") {
    if (hasCheckpoint) {
      return `No runtime approval records are attached to the active gate on this run. Use the active ${workflowRunApprovalNoun(
        approvalKind,
      )} checkpoint and recovery warning above to confirm whether approval persistence lagged or this run paused before an approval row was recorded.${historicalSuffix}`;
    }
    return `No runtime approval records are attached to the active gate on this run. Use the recovery warning and current run status above to confirm whether this approval gate paused before an approval row was recorded.${historicalSuffix}`;
  }
  if (status === "completed") {
    if (hasCheckpoint) {
      return `No runtime approval records are attached to the active gate on this run. Inspect the active checkpoint details, final outputs, and debugger state above to confirm whether this execution resumed past approval before approval history was persisted.${historicalSuffix}`;
    }
    return `No runtime approval records are attached to the active gate on this run. This execution completed without any persisted approval history on the active node, so inspect the debugger, outputs, and recovery summary above to confirm whether it ever crossed an approval boundary.${historicalSuffix}`;
  }
  if (status === "failed" || status === "cancelled" || status === "rejected" || status === "expired" || status === "blocked") {
    if (hasCheckpoint) {
      return `No runtime approval records are attached to the active gate on this run. Inspect the active checkpoint details, recovery warning, and debugger state above to confirm whether this execution stopped after approval state was captured but before approval history was persisted.${historicalSuffix}`;
    }
    return `No runtime approval records are attached to the active gate on this run. Use the current failure state, recovery timeline, and debugger details above to confirm whether this execution ever reached an approval boundary before it stopped.${historicalSuffix}`;
  }
  if (hasCheckpoint) {
    return `No runtime approval records are attached to the active gate on this run. Inspect the active checkpoint details above to confirm whether this run resumed past approval before approval history was persisted.${historicalSuffix}`;
  }
  return `No runtime approval records are attached to the active gate on this run. Use the current status and recovery timeline to confirm whether this execution ever reached an approval boundary.${historicalSuffix}`;
}

export function approvalLoadErrorMessage(
  runStatus: string | null | undefined,
  errorMessage: string,
): JSX.Element {
  const detail = errorMessage.trim() || "Unknown error";
  if (runStatus === "waiting_approval") {
    return (
      <>
        Runtime approval history could not be loaded for this paused run. Recovery diagnostics may
        still show the active checkpoint and recovery timeline, but approval decisions are
        temporarily unavailable, so inspect the checkpoint details and debugger state before
        attempting resume.
        <span className="mt-2 block text-red-700/80">Latest approval error: {detail}</span>
      </>
    );
  }
  if (
    runStatus === "queued"
    || runStatus === "running"
    || runStatus === "resuming"
    || runStatus === "cancel_requested"
    || runStatus === "waiting_event"
  ) {
    return (
      <>
        Runtime approval history could not be loaded while this run is still active. Recovery
        diagnostics may still show checkpoints and lifecycle history, so use those panels while
        approval history catches up.
        <span className="mt-2 block text-red-700/80">Latest approval error: {detail}</span>
      </>
    );
  }
  if (runStatus === "completed") {
    return (
      <>
        Runtime approval history could not be loaded for this completed run. Recovery diagnostics
        may still show checkpoints and lifecycle history, so inspect the debugger, final outputs,
        and generated artifacts to reconstruct whether this execution crossed an approval
        boundary.
        <span className="mt-2 block text-red-700/80">Latest approval error: {detail}</span>
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
        Runtime approval history could not be loaded for this stopped run. Recovery diagnostics may
        still show checkpoints and lifecycle history, so inspect the debugger and recovery timeline
        to trace whether this execution stalled or failed near an approval boundary.
        <span className="mt-2 block text-red-700/80">Latest approval error: {detail}</span>
      </>
    );
  }
  return (
    <>
      Runtime approval history could not be loaded for this run. Use the remaining recovery
      diagnostics to inspect execution state until approval history is restored.
      <span className="mt-2 block text-red-700/80">Latest approval error: {detail}</span>
    </>
  );
}

function summarizeEvent(
  event: WorkflowRunEvent,
  approvalKind: WorkflowRunApprovalCheckpointKind | null,
): string {
  return workflowRunLifecycleSummary(event.event_type, event.payload, {
    approvalKind,
  });
}

function SummaryRow({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}): JSX.Element {
  return (
    <div className="rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-2 dark:border-slate-700/70 dark:bg-slate-900/70">
      <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
        {label}
      </div>
      <div className={`mt-1 text-xs text-slate-700 dark:text-slate-200 ${mono ? "font-mono" : ""}`}>
        {value}
      </div>
    </div>
  );
}

export function WorkflowRunRecoveryPanel({
  run,
  approvals = [],
  checkpoints = [],
  events = [],
  loading = false,
  approvalsLoadError = null,
  eventsLoadError = null,
}: WorkflowRunRecoveryPanelProps): JSX.Element {
  const artifactPersistence = useMemo(() => workflowRunArtifactPersistence(run), [run]);
  const relevantApprovals = useMemo(() => {
    if (!run.current_node_id) {
      return approvals;
    }
    return approvals.filter((approval) => approval.node_id === run.current_node_id);
  }, [approvals, run.current_node_id]);
  const pendingApproval =
    relevantApprovals.find((approval) => approval.status === "pending") ?? null;
  const approvedApproval =
    relevantApprovals.find((approval) => approval.status === "approved") ?? null;
  const rejectedApproval =
    relevantApprovals.find((approval) => approval.status === "rejected") ?? null;
  const representativeApproval =
    pendingApproval
    ?? approvedApproval
    ?? rejectedApproval
    ?? relevantApprovals[0]
    ?? approvals[0]
    ?? null;
  const hasHistoricalApprovals =
    Boolean(run.current_node_id)
    && relevantApprovals.length === 0
    && approvals.length > 0;
  const approvalCards = relevantApprovals.length > 0 || !run.current_node_id
    ? relevantApprovals
    : [];
  const pendingApprovalCount = relevantApprovals.filter(
    (approval) => approval.status === "pending",
  ).length;
  const approvalPolicySnapshot = approvalPolicy(representativeApproval);
  const activeCheckpointId = readString(run.summary?.resume_checkpoint_id);
  const orderedCheckpoints = useMemo(
    () => [...checkpoints].sort((left, right) => right.sequence - left.sequence),
    [checkpoints],
  );
  const activeCheckpoint = useMemo(() => {
    if (activeCheckpointId) {
      return (
        orderedCheckpoints.find((checkpoint) => checkpoint.checkpoint_id === activeCheckpointId)
        ?? null
      );
    }
    return orderedCheckpoints[0] ?? null;
  }, [activeCheckpointId, orderedCheckpoints]);
  const activeState = checkpointState(activeCheckpoint);
  const activeStateNodeId = readString(activeState?.node_id);
  const activeCheckpointKind = approvalCheckpointKind(activeState);
  const checkpointKind = workflowRunCheckpointLabel(readString(activeState?.kind));
  const expectedEventName = readString(activeState?.expected_event_name);
  const waitUntil = readString(activeState?.wait_until) ?? readString(activeState?.resume_at);
  const timezoneName = readString(activeState?.timezone);
  const correlationKey = readString(activeState?.correlation_key);
  const correlationValue = formatValue(activeState?.correlation_value);
  const timeoutSeconds = readPositiveNumber(activeState?.timeout_seconds);
  const timeoutDeadline = formatDeadline(activeCheckpoint?.created_at, timeoutSeconds);
  const leaseExpired = Boolean(
    run.lease_expires_at
    && !TERMINAL_STATUSES.has(run.status)
    && new Date(run.lease_expires_at).getTime() < Date.now(),
  );
  const activeCheckpointIntegrityWarning = checkpointNodeIntegrityWarning({
    checkpointId: activeCheckpoint?.checkpoint_id ?? null,
    checkpointNodeId: activeCheckpoint?.node_id ?? null,
    stateNodeId: activeStateNodeId,
    currentNodeId: readString(run.current_node_id),
  });

  const primaryReason = (() => {
    if (pendingApproval) {
      return {
        label: workflowRunAwaitingApprovalLabel(activeCheckpointKind),
        detail:
          `${workflowRunApprovalTitle(
            activeCheckpointKind,
          )} ${pendingApproval.runtime_approval_id} is pending on ${pendingApproval.node_id}.`,
      };
    }
    if (approvedApproval && run.status === "waiting_approval") {
      return {
        label: workflowRunApprovalRecordedLabel(activeCheckpointKind),
        detail:
          `${workflowRunApprovalTitle(
            activeCheckpointKind,
          )} ${approvedApproval.runtime_approval_id} is approved on ${approvedApproval.node_id}. Resume can re-queue this run from the stored checkpoint.`,
      };
    }
    if (rejectedApproval && run.status === "waiting_approval") {
      return {
        label: workflowRunApprovalRejectedLabel(activeCheckpointKind),
        detail:
          `${workflowRunApprovalTitle(
            activeCheckpointKind,
          )} ${rejectedApproval.runtime_approval_id} was rejected on ${rejectedApproval.node_id}. Resume remains blocked until the run is retried.`,
      };
    }
    if (run.status === "waiting_approval") {
      if (activeCheckpointKind === "runtime_approval") {
        return {
          label: workflowRunApprovalBlockedLabel(activeCheckpointKind),
          detail:
            "This run is paused behind a runtime approval gate and still needs an approved decision before it can resume.",
        };
      }
      if (activeCheckpointKind === "human_approval") {
        return {
          label: workflowRunApprovalBlockedLabel(activeCheckpointKind),
          detail:
            "This run is paused at a human approval node and still needs an approved decision before it can resume.",
        };
      }
      return {
        label: workflowRunApprovalBlockedLabel(activeCheckpointKind),
        detail:
          "This run is paused at an approval gate and still needs an approved decision before it can resume.",
      };
    }
    if (readString(activeState?.kind) === "wait_until" && waitUntil) {
      return {
        label: "Scheduled resume",
        detail: timezoneName ? `${waitUntil} (${timezoneName})` : waitUntil,
      };
    }
    if (expectedEventName) {
      return {
        label: workflowRunLifecycleSummary("workflow.run.waiting_event", null),
        detail: expectedEventName,
      };
    }
    if (run.status === "waiting_event") {
      return {
        label: "External event gate",
        detail: "This run is paused until an operator or external system resumes it.",
      };
    }
    if (run.status === "queued") {
      return {
        label: "Queued for execution",
        detail: "The worker has not started processing this run yet.",
      };
    }
    if (run.status === "running") {
      return {
        label: "Actively executing",
        detail: "The worker is currently advancing this run.",
      };
    }
    if (TERMINAL_STATUSES.has(run.status)) {
      return {
        label: "Terminal state",
        detail: `This run ended in ${workflowRunStatusPhrase(run.status)}.`,
      };
    }
    return {
      label: "Runtime state",
      detail: workflowRunStatusLabel(run.status),
    };
  })();

  const warning = (() => {
    if (activeCheckpointIntegrityWarning) {
      return {
        kind: "danger" as const,
        text: activeCheckpointIntegrityWarning,
      };
    }
    if (leaseExpired) {
      return {
        kind: "danger" as const,
        text: "The worker lease expired before this run reached a terminal state. Check whether the worker stopped or lost its lease before recovery completed.",
      };
    }
    if (pendingApproval) {
      return {
        kind: "warn" as const,
        text: `Pending ${workflowRunApprovalNoun(
          activeCheckpointKind,
        )} since ${formatTimestamp(pendingApproval.requested_at)}. The run will not advance until it is approved or rejected.`,
      };
    }
    if (approvedApproval && run.status === "waiting_approval") {
      return {
        kind: "info" as const,
        text: `${workflowRunApprovalTitle(
          activeCheckpointKind,
        )} ${approvedApproval.runtime_approval_id} is already approved. Resume can re-queue this run from the stored checkpoint.`,
      };
    }
    if (rejectedApproval && run.status === "waiting_approval") {
      return {
        kind: "danger" as const,
        text: `${workflowRunApprovalTitle(
          activeCheckpointKind,
        )} ${rejectedApproval.runtime_approval_id} was rejected. This run cannot resume until you retry from a checkpoint or start a new attempt.`,
      };
    }
    if (run.status === "waiting_approval") {
      return {
        kind: "warn" as const,
        text: `This run is still marked as waiting for approval, but no approved ${workflowRunApprovalRecordNoun(
          activeCheckpointKind,
        )} is attached. Resume stays blocked until an approval is recorded or the run is retried.`,
      };
    }
    if (readString(activeState?.kind) === "wait_until" && waitUntil) {
      return {
        kind: "info" as const,
        text: `Scheduled resume target ${timezoneName ? `${waitUntil} (${timezoneName})` : waitUntil}. Resume remains available as a manual override.`,
      };
    }
    if (expectedEventName) {
      return {
        kind: "info" as const,
        text: timeoutDeadline
          ? `Waiting for the event ${expectedEventName}. This checkpoint will time out around ${timeoutDeadline} unless the run resumes first.`
          : `Waiting for the event ${expectedEventName}. Resume this run once the external condition has been satisfied.`,
      };
    }
    if (run.status === "waiting_event") {
      return {
        kind: "info" as const,
        text: "This run is paused at an external event gate. Use Resume once the blocking condition has been resolved.",
      };
    }
    if (artifactPersistence?.status === "failed") {
      const persistedObjectCount = artifactPersistence.persisted_object_count ?? 0;
      const partialPrefix =
        persistedObjectCount > 0
          ? ` after ${persistedObjectCount} of ${artifactPersistence.object_count} object${artifactPersistence.object_count === 1 ? "" : "s"} were stored`
          : "";
      return {
        kind: "danger" as const,
        text: artifactPersistence.error
          ? `Object-store upload to ${artifactPersistence.bucket} failed after execution completed${partialPrefix}: ${artifactPersistence.error}`
          : `Object-store upload to ${artifactPersistence.bucket} failed after execution completed. Inspect the artifact persistence section below before retrying this run.`,
      };
    }
    return null;
  })();

  const artifactPersistenceDetail = (() => {
    if (!artifactPersistence) return null;
    const namedArtifactCount = artifactPersistence.artifact_names.length;
    const persistedObjectCount =
      artifactPersistence.status === "persisted"
        ? artifactPersistence.object_count
        : artifactPersistence.persisted_object_count ?? 0;
    const auxiliaryObjectCount = Math.max(persistedObjectCount - namedArtifactCount, 0);
    if (artifactPersistence.status === "persisted") {
      if (namedArtifactCount > 0) {
        return auxiliaryObjectCount > 0
          ? `${namedArtifactCount} named artifact${namedArtifactCount === 1 ? "" : "s"} and ${auxiliaryObjectCount} additional object${auxiliaryObjectCount === 1 ? "" : "s"} were written to ${artifactPersistence.bucket}. Additional objects can include the persisted run log.`
          : `${namedArtifactCount} named artifact${namedArtifactCount === 1 ? "" : "s"} were written to ${artifactPersistence.bucket}.`;
      }
      return `${artifactPersistence.object_count} object${artifactPersistence.object_count === 1 ? "" : "s"} were written to ${artifactPersistence.bucket}.`;
    }
    if (persistedObjectCount > 0) {
      return `Execution completed, but post-run object-store persistence failed for ${artifactPersistence.bucket} after ${persistedObjectCount} of ${artifactPersistence.object_count} object${artifactPersistence.object_count === 1 ? "" : "s"} were stored. Use the failed object key, stored-object keys, and upload error below before retrying or rebuilding the run.`;
    }
    return `Execution completed, but post-run object-store persistence failed for ${artifactPersistence.bucket} before any of the ${artifactPersistence.object_count} planned object${artifactPersistence.object_count === 1 ? "" : "s"} were stored. Use the stored error and named artifact list below before retrying or rebuilding the run.`;
  })();

  const recoveryEvents = useMemo(
    () =>
      events
        .filter((event) => RECOVERY_EVENT_TYPES.has(event.event_type))
        .sort((left, right) => right.sequence - left.sequence)
        .slice(0, 8),
    [events],
  );

  if (loading) {
    return (
      <div
        data-testid="workflow-run-recovery-panel"
        className="rounded-xl border border-slate-200 bg-slate-50/70 px-4 py-4 text-xs text-slate-400 dark:border-slate-700 dark:bg-slate-900/70 dark:text-slate-500"
      >
        Loading recovery diagnostics…
      </div>
    );
  }

  return (
    <div
      data-testid="workflow-run-recovery-panel"
      className="space-y-4 rounded-2xl border border-slate-200/70 bg-white p-4 shadow-sm dark:border-slate-700/70 dark:bg-slate-900"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">
            Recovery Diagnostics
          </div>
          <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
            {primaryReason.label}
          </div>
          <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            {primaryReason.detail}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className={`rounded-full px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] ring-1 ${workflowRunStatusRingClass(run.status)}`}>
            {workflowRunStatusLabel(run.status)}
          </span>
          {activeCheckpointId && (
            <span className="rounded-md bg-slate-50 px-2 py-1 font-mono text-[11px] text-slate-600 ring-1 ring-slate-200/80 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700/70">
              {activeCheckpointId}
            </span>
          )}
        </div>
      </div>

      {warning && (
        <div
          data-testid="workflow-run-recovery-warning"
          className={`rounded-xl border px-3 py-3 text-xs leading-relaxed ${noteTone(warning.kind)}`}
        >
          {warning.text}
        </div>
      )}

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <SummaryRow label="Current node" value={run.current_node_id ?? "n/a"} mono />
        <SummaryRow label="Active checkpoint" value={activeCheckpoint ? checkpointKind : "None"} />
        <SummaryRow label="Last heartbeat" value={formatTimestamp(run.last_heartbeat_at)} />
        <SummaryRow label="Lease expires" value={formatTimestamp(run.lease_expires_at)} />
        <SummaryRow label="Queued" value={formatTimestamp(run.queued_at)} />
        <SummaryRow label="Started" value={formatTimestamp(run.started_at)} />
        <SummaryRow label="Pending approvals" value={String(pendingApprovalCount)} />
        <SummaryRow label="Stored checkpoints" value={String(checkpoints.length)} />
        {expectedEventName && (
          <SummaryRow label="Expected event" value={expectedEventName} mono />
        )}
        {correlationKey && (
          <SummaryRow label="Correlation key" value={correlationKey} mono />
        )}
        {correlationValue && (
          <SummaryRow label="Correlation value" value={correlationValue} mono />
        )}
        {timeoutSeconds !== null && (
          <SummaryRow label="Wait timeout" value={`${timeoutSeconds}s`} />
        )}
        {timeoutDeadline && (
          <SummaryRow label="Timeout deadline" value={timeoutDeadline} />
        )}
        {approvalPolicySnapshot.approvalCount !== null && (
          <SummaryRow label="Required approvals" value={String(approvalPolicySnapshot.approvalCount)} />
        )}
        {approvalPolicySnapshot.requiredRole && (
          <SummaryRow label="Approval scope" value={approvalPolicySnapshot.requiredRole} mono />
        )}
        {approvalPolicySnapshot.timeoutBehavior && (
          <SummaryRow label="Timeout policy" value={approvalPolicySnapshot.timeoutBehavior} />
        )}
        {artifactPersistence && (
          <SummaryRow
            label="Artifact upload"
            value={artifactStatusLabel(artifactPersistence.status)}
          />
        )}
      </div>

      {artifactPersistence && artifactPersistenceDetail && (
        <div
          data-testid="workflow-run-recovery-artifact-persistence"
          className="rounded-xl border border-slate-200/70 bg-slate-50/70 p-3 dark:border-slate-700/70 dark:bg-slate-950/60"
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
                Artifact persistence
              </div>
              <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
                {artifactPersistence.status === "persisted"
                  ? "Run artifacts reached object storage"
                  : "Object-store upload failed after completion"}
              </div>
              <div className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                {artifactPersistenceDetail}
              </div>
            </div>
            <span
              className={`rounded-full px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] ring-1 ${artifactStatusTone(artifactPersistence.status)}`}
            >
              {artifactStatusLabel(artifactPersistence.status)}
            </span>
          </div>
          <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <SummaryRow label="Bucket" value={artifactPersistence.bucket} mono />
            <SummaryRow
              label={
                artifactPersistence.status === "failed" ? "Stored before failure" : "Stored objects"
              }
              value={String(
                artifactPersistence.status === "failed"
                  ? artifactPersistence.persisted_object_count ?? 0
                  : artifactPersistence.object_count,
              )}
            />
            {artifactPersistence.status === "failed" && (
              <SummaryRow
                label="Planned objects"
                value={String(artifactPersistence.object_count)}
              />
            )}
            <SummaryRow
              label="Named artifacts"
              value={String(artifactPersistence.artifact_names.length)}
            />
            {artifactPersistence.failed_object_key && (
              <SummaryRow
                label="Failed object"
                value={artifactPersistence.failed_object_key}
                mono
              />
            )}
            {artifactPersistence.error && (
              <SummaryRow
                label="Upload error"
                value={artifactPersistence.error}
                mono
              />
            )}
          </div>
          {artifactPersistence.recent_persisted_keys && artifactPersistence.recent_persisted_keys.length > 0 && (
            <div className="mt-3 rounded-lg border border-white/80 bg-white px-3 py-2 text-[11px] text-slate-500 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
              {artifactPersistence.status === "failed" ? "Stored before failure" : "Recent stored objects"}:{" "}
              <span className="font-mono">
                {artifactPersistence.recent_persisted_keys.join(", ")}
              </span>
            </div>
          )}
          {artifactPersistence.artifact_names.length > 0 && (
            <div className="mt-3 rounded-lg border border-white/80 bg-white px-3 py-2 text-[11px] text-slate-500 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
              Named artifacts:{" "}
              <span className="font-mono">
                {artifactPersistence.artifact_names.join(", ")}
              </span>
            </div>
          )}
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,0.92fr)_minmax(0,1.08fr)]">
        <div className="rounded-xl border border-slate-200/70 bg-slate-50/70 p-3 dark:border-slate-700/70 dark:bg-slate-950/60">
          <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
            Runtime approvals
          </div>
          <div data-testid="workflow-run-recovery-approvals" className="space-y-2">
            {approvalsLoadError ? (
              <div
                data-testid="workflow-run-recovery-approvals-error"
                className="rounded-lg border border-red-200/70 bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-700 dark:border-red-800/70 dark:bg-red-950/30 dark:text-red-200"
              >
                {approvalLoadErrorMessage(run.status, approvalsLoadError)}
              </div>
            ) : approvalCards.length === 0 ? (
              <div className="text-xs text-slate-500 dark:text-slate-400">
                {approvalRecordsEmptyMessage({
                  status: run.status,
                  approvalKind: activeCheckpointKind,
                  hasCheckpoint: Boolean(activeCheckpoint),
                  hasHistoricalApprovals,
                })}
              </div>
            ) : (
              approvalCards.map((approval) => (
                <div
                  key={approval.runtime_approval_id}
                  data-testid={`workflow-run-recovery-approval-${approval.runtime_approval_id}`}
                  className="rounded-lg border border-white/80 bg-white px-3 py-2 shadow-sm dark:border-slate-800 dark:bg-slate-900"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="font-mono text-xs font-semibold text-slate-700 dark:text-slate-200">
                      {approval.runtime_approval_id}
                    </div>
                    <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] ring-1 ${statusTone(approval.status)}`}>
                      {approval.status}
                    </span>
                  </div>
                  <div className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
                    node {approval.node_id} · requested {formatTimestamp(approval.requested_at)}
                  </div>
                  {(() => {
                    const policy = approvalPolicy(approval);
                    if (!policy.requiredRole && policy.approvalCount === null && !policy.timeoutBehavior) {
                      return null;
                    }
                    return (
                      <div className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
                        {policy.requiredRole ? `scope ${policy.requiredRole}` : "scope n/a"}
                        {policy.approvalCount !== null ? ` · requires ${policy.approvalCount}` : ""}
                        {policy.timeoutBehavior ? ` · timeout ${policy.timeoutBehavior}` : ""}
                      </div>
                    );
                  })()}
                  {approval.decided_at && (
                    <div className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
                      decided {formatTimestamp(approval.decided_at)}
                      {approval.decided_by ? ` by ${approval.decided_by}` : ""}
                      {approval.decision_reason ? ` · ${approval.decision_reason}` : ""}
                    </div>
                  )}
                </div>
              ))
            )}
            {hasHistoricalApprovals && (
              <div
                data-testid="workflow-run-recovery-historical-approvals"
                className="rounded-lg border border-dashed border-slate-200/80 bg-white/70 px-3 py-2 text-[11px] text-slate-500 dark:border-slate-700/70 dark:bg-slate-900/70 dark:text-slate-400"
              >
                Historical approval records exist on other nodes for this run. They remain visible below for audit context, but they do not unblock the current gate on{" "}
                <span className="font-mono">{run.current_node_id}</span>.
                <div className="mt-2 space-y-2">
                  {approvals.map((approval) => (
                    <div
                      key={`historical-${approval.runtime_approval_id}`}
                      data-testid={`workflow-run-recovery-historical-approval-${approval.runtime_approval_id}`}
                      className="rounded-lg border border-slate-200/80 bg-slate-50/80 px-3 py-2 dark:border-slate-700/70 dark:bg-slate-950/70"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="font-mono text-xs font-semibold text-slate-700 dark:text-slate-200">
                          {approval.runtime_approval_id}
                        </div>
                        <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] ring-1 ${statusTone(approval.status)}`}>
                          {approval.status}
                        </span>
                      </div>
                      <div className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
                        node {approval.node_id} · requested {formatTimestamp(approval.requested_at)}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
            {pendingApprovalCount > 1 && (
              <div className="text-[11px] text-slate-500 dark:text-slate-400">
                {pendingApprovalCount} approval decisions are still pending before this run can resume.
              </div>
            )}
          </div>
        </div>

        <div className="rounded-xl border border-slate-200/70 bg-slate-50/70 p-3 dark:border-slate-700/70 dark:bg-slate-950/60">
          <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
            Recovery timeline
          </div>
          <div data-testid="workflow-run-recovery-events" className="space-y-2">
            {eventsLoadError ? (
              <div
                data-testid="workflow-run-recovery-events-error"
                className="rounded-lg border border-red-200/70 bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-700 dark:border-red-800/70 dark:bg-red-950/30 dark:text-red-200"
              >
                {recoveryTimelineLoadErrorMessage({
                  status: run.status,
                  hasApprovals: approvals.length > 0,
                  hasCheckpoint: Boolean(activeCheckpoint),
                  errorMessage: eventsLoadError,
                })}
              </div>
            ) : recoveryEvents.length === 0 ? (
              <div className="text-xs text-slate-500 dark:text-slate-400">
                {recoveryTimelineEmptyMessage({
                  status: run.status,
                  hasApprovals: approvals.length > 0,
                  hasCheckpoint: Boolean(activeCheckpoint),
                })}
              </div>
            ) : (
              recoveryEvents.map((event) => {
                const eventReason = workflowRunLifecycleReason(event.payload);
                return (
                  <div
                    key={`${event.event_id}-${event.sequence}`}
                    className="rounded-lg border border-white/80 bg-white px-3 py-2 shadow-sm dark:border-slate-800 dark:bg-slate-900"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="text-xs font-semibold text-slate-700 dark:text-slate-200">
                        #{event.sequence} · {summarizeEvent(event, activeCheckpointKind)}
                      </div>
                      <div className="text-[11px] text-slate-400 dark:text-slate-500">
                        {formatTimestamp(event.created_at)}
                      </div>
                    </div>
                    {(event.node_id || isRecord(event.payload)) && (
                      <div className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
                        {event.node_id ? `node ${event.node_id}` : "run-level event"}
                        {eventReason ? ` · ${eventReason}` : ""}
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
