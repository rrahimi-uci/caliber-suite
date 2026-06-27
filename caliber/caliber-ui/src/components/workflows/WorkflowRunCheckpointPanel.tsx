import { useEffect, useMemo, useState } from "react";

import type {
  WorkflowRun,
  WorkflowRunCheckpoint,
} from "@/api/workflowTypes";
import { workflowRunCheckpointLabel } from "@/lib/workflowRunLabels";

interface WorkflowRunCheckpointPanelProps {
  run: WorkflowRun;
  checkpoints?: WorkflowRunCheckpoint[];
  loading?: boolean;
  resumeSourceCheckpoint?: WorkflowRunCheckpoint | null;
  resumeSourceCheckpointLoading?: boolean;
  resumeSourceCheckpointError?: string | null;
  canRetryFromCheckpoint?: boolean;
  retryingCheckpointId?: string | null;
  onRetryFromCheckpoint?: (checkpointId: string) => void;
}

function isActiveRunStatus(runStatus: string | null | undefined): boolean {
  return (
    runStatus === "queued"
    || runStatus === "running"
    || runStatus === "resuming"
    || runStatus === "cancel_requested"
    || runStatus === "waiting_approval"
    || runStatus === "waiting_event"
  );
}

function isStoppedRunStatus(runStatus: string | null | undefined): boolean {
  return (
    runStatus === "failed"
    || runStatus === "cancelled"
    || runStatus === "rejected"
    || runStatus === "expired"
    || runStatus === "blocked"
  );
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

function formatJson(value: unknown): string {
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return String(value);
  }
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

function portSummary(value: unknown): string | null {
  if (!isRecord(value)) return null;
  const keys = Object.keys(value);
  if (keys.length === 0) return "0 ports";
  return `${keys.length} port${keys.length === 1 ? "" : "s"}: ${keys.join(", ")}`;
}

function stateBlob(checkpoint: WorkflowRunCheckpoint | null): Record<string, unknown> | null {
  return isRecord(checkpoint?.state_blob) ? checkpoint.state_blob : null;
}

function checkpointIntegrityWarning({
  checkpointId,
  checkpointNodeId,
  stateNodeId,
  currentNodeId,
  active,
}: {
  checkpointId: string | null;
  checkpointNodeId: string | null;
  stateNodeId: string | null;
  currentNodeId: string | null;
  active: boolean;
}): string | null {
  if (!active) return null;
  const issues: string[] = [];
  if (currentNodeId && checkpointNodeId && checkpointNodeId !== currentNodeId) {
    issues.push(
      `the active run is waiting on node ${currentNodeId}, but this checkpoint row points at ${checkpointNodeId}`,
    );
  }
  if (currentNodeId && stateNodeId && stateNodeId !== currentNodeId) {
    issues.push(
      `the checkpoint payload points at ${stateNodeId} instead of the active node ${currentNodeId}`,
    );
  }
  if (checkpointNodeId && stateNodeId && checkpointNodeId !== stateNodeId) {
    issues.push(
      `the checkpoint row and payload disagree (${checkpointNodeId} vs ${stateNodeId})`,
    );
  }
  if (issues.length === 0) return null;
  const checkpointLabel = checkpointId ? `Checkpoint ${checkpointId}` : "This checkpoint";
  return `${checkpointLabel} has inconsistent node identity: ${issues.join("; ")}. Treat it as recovery evidence only, then inspect recovery, lineage, and debugger panels before retrying or attempting resume.`;
}

function summaryCheckpointId(run: WorkflowRun): string | null {
  return readString(run.summary?.resume_checkpoint_id);
}

function summaryCheckpointRunId(run: WorkflowRun): string | null {
  return readString(run.summary?.resume_checkpoint_run_id);
}

function summaryRetryMode(run: WorkflowRun): string | null {
  return readString(run.summary?.retry_mode);
}

function selectedCardClasses(selected: boolean): string {
  return selected
    ? "border-sky-300 bg-sky-50/70 shadow-sm dark:border-sky-500/60 dark:bg-sky-950/30"
    : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50/60 dark:border-slate-700 dark:bg-slate-900 dark:hover:border-slate-600 dark:hover:bg-slate-800/70";
}

function inheritedCheckpointUnavailableMessage(
  {
    checkpointId,
    checkpointRunId,
    retryMode,
    runStatus,
  }: {
    checkpointId: string | null;
    checkpointRunId: string | null;
    retryMode: string | null;
    runStatus: string;
  },
): string {
  const label = retryMode === "checkpoint" ? "checkpoint retry" : "run";
  const source =
    checkpointId && checkpointRunId
      ? `inherited checkpoint ${checkpointId} on ${checkpointRunId}`
      : "the inherited source checkpoint";
  const base =
    `This ${label} is queued to resume from ${source}, but the source checkpoint details are unavailable right now.`;
  if (
    runStatus === "queued"
    || runStatus === "running"
    || runStatus === "resuming"
    || runStatus === "cancel_requested"
  ) {
    return `${base} Inspect the lineage, recovery, and debugger panels to follow the originating run while the current attempt is still in flight, then refresh when the checkpoint trail catches up.`;
  }
  if (runStatus === "waiting_approval" || runStatus === "waiting_event") {
    return `${base} Inspect the lineage and recovery panels to follow the originating run and current resume gate until the checkpoint trail can be restored.`;
  }
  if (runStatus === "completed") {
    return `${base} Inspect the lineage, debugger, final outputs, and generated artifacts to reconstruct how the inherited resume path finished without restoring the original checkpoint details.`;
  }
  if (
    runStatus === "failed"
    || runStatus === "cancelled"
    || runStatus === "rejected"
    || runStatus === "expired"
    || runStatus === "blocked"
  ) {
    return `${base} Inspect the lineage, recovery, and debugger panels to trace where the inherited resume path failed or was interrupted before the checkpoint trail could be restored.`;
  }
  return `${base} Refresh the workflow run history or inspect the lineage and recovery panels to follow the originating run and current resume gate until the checkpoint trail can be restored.`;
}

export function inheritedCheckpointLoadErrorMessage(
  {
    checkpointId,
    checkpointRunId,
    retryMode,
    runStatus,
    errorMessage,
  }: {
    checkpointId: string | null;
    checkpointRunId: string | null;
    retryMode: string | null;
    runStatus: string | null | undefined;
    errorMessage: string;
  },
): JSX.Element {
  const label = retryMode === "checkpoint" ? "checkpoint retry" : "run";
  const source =
    checkpointId && checkpointRunId
      ? `inherited checkpoint ${checkpointId} on ${checkpointRunId}`
      : "the inherited source checkpoint";
  const detail = errorMessage.trim() || "Unknown error";
  const base =
    `This ${label} is queued to resume from ${source}, but CALIBER could not load the original source checkpoint details.`;
  if (isActiveRunStatus(runStatus)) {
    return (
      <>
        {base} Inspect the lineage, recovery, and debugger panels to follow the originating run
        while the current attempt is still in flight, then retry this lookup when the checkpoint
        trail is available again.
        <span className="mt-2 block text-red-700/80">Latest source checkpoint error: {detail}</span>
      </>
    );
  }
  if (runStatus === "completed") {
    return (
      <>
        {base} Inspect the lineage, debugger, final outputs, and generated artifacts to
        reconstruct how the inherited resume path finished without restoring the original checkpoint
        details.
        <span className="mt-2 block text-red-700/80">Latest source checkpoint error: {detail}</span>
      </>
    );
  }
  if (isStoppedRunStatus(runStatus)) {
    return (
      <>
        {base} Inspect the lineage, recovery, and debugger panels to trace where the inherited
        resume path failed or was interrupted before the source checkpoint trail could be restored.
        <span className="mt-2 block text-red-700/80">Latest source checkpoint error: {detail}</span>
      </>
    );
  }
  return (
    <>
      {base} Inspect the lineage and recovery panels to follow the originating run and current
      resume gate until the source checkpoint trail can be restored.
      <span className="mt-2 block text-red-700/80">Latest source checkpoint error: {detail}</span>
    </>
  );
}

function emptyCheckpointMessage(run: WorkflowRun): string {
  const hasRetryLineage = Boolean(
    run.parent_run_id || readString(run.summary?.retry_of),
  );
  const base =
    "No persisted checkpoints exist for this run yet. Runs only create checkpoints when they pause for approvals, scheduled waits, or resume events.";
  if (
    run.status === "queued"
    || run.status === "running"
    || run.status === "resuming"
    || run.status === "cancel_requested"
  ) {
    return hasRetryLineage
      ? `${base} This attempt may still be executing before it reaches a resumable boundary. Inspect recovery and lineage to confirm where the current run resumed from, then refresh if you expect a wait state or approval gate.`
      : `${base} This execution may still be in flight before it reaches a resumable boundary. Inspect the recovery and debugger panels, then refresh if you expect a wait state or approval gate.`;
  }
  if (run.status === "waiting_approval" || run.status === "waiting_event") {
    return hasRetryLineage
      ? `${base} Inspect the recovery panel to confirm the active gate, and open lineage to trace earlier attempts until checkpoint persistence appears.`
      : `${base} Inspect the recovery panel to confirm the active gate until checkpoint persistence appears.`;
  }
  if (run.status === "completed") {
    return hasRetryLineage
      ? `${base} This attempt completed without creating a new resumable checkpoint after it resumed from earlier lineage. Inspect lineage, debugger output, and generated artifacts to confirm where the terminal result was produced.`
      : `${base} This run completed without ever pausing at a resumable boundary. Inspect the debugger, final output, and generated artifacts to confirm how execution finished.`;
  }
  if (
    run.status === "failed"
    || run.status === "cancelled"
    || run.status === "rejected"
    || run.status === "expired"
    || run.status === "blocked"
  ) {
    return hasRetryLineage
      ? `${base} This attempt stopped before it captured a new checkpoint after resuming from earlier lineage. Inspect recovery, lineage, and debugger details to find where execution failed or was interrupted.`
      : `${base} This run stopped before it captured any resumable checkpoint. Inspect the recovery timeline and debugger panels to find where execution failed or was interrupted.`;
  }
  return hasRetryLineage
    ? `${base} Use the recovery and lineage panels to confirm whether this attempt resumed from an earlier run before any new checkpoint was captured.`
    : `${base} Use the recovery and debugger panels to confirm whether this run ever reached a resumable boundary.`;
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

export function WorkflowRunCheckpointPanel({
  run,
  checkpoints = [],
  loading = false,
  resumeSourceCheckpoint = null,
  resumeSourceCheckpointLoading = false,
  resumeSourceCheckpointError = null,
  canRetryFromCheckpoint = false,
  retryingCheckpointId = null,
  onRetryFromCheckpoint,
}: WorkflowRunCheckpointPanelProps): JSX.Element {
  const activeCheckpointId = summaryCheckpointId(run);
  const activeCheckpointRunId = summaryCheckpointRunId(run);
  const retryMode = summaryRetryMode(run);
  const orderedCheckpoints = useMemo(
    () => [...checkpoints].sort((left, right) => right.sequence - left.sequence),
    [checkpoints],
  );
  const inheritedResumeCheckpoint = useMemo(() => {
    if (!resumeSourceCheckpoint) return null;
    if (resumeSourceCheckpoint.workflow_run_id === run.workflow_run_id) return null;
    if (activeCheckpointId && resumeSourceCheckpoint.checkpoint_id !== activeCheckpointId) {
      return null;
    }
    return resumeSourceCheckpoint;
  }, [activeCheckpointId, resumeSourceCheckpoint, run.workflow_run_id]);
  const displayCheckpoints = useMemo(() => {
    const next: WorkflowRunCheckpoint[] = [];
    const seen = new Set<string>();
    if (inheritedResumeCheckpoint) {
      next.push(inheritedResumeCheckpoint);
      seen.add(inheritedResumeCheckpoint.checkpoint_id);
    }
    for (const checkpoint of orderedCheckpoints) {
      if (seen.has(checkpoint.checkpoint_id)) continue;
      next.push(checkpoint);
      seen.add(checkpoint.checkpoint_id);
    }
    return next;
  }, [inheritedResumeCheckpoint, orderedCheckpoints]);
  const [selectedCheckpointId, setSelectedCheckpointId] = useState<string | null>(null);

  useEffect(() => {
    if (displayCheckpoints.length === 0) {
      setSelectedCheckpointId(null);
      return;
    }
    const defaultId = activeCheckpointId ?? displayCheckpoints[0]?.checkpoint_id ?? null;
    setSelectedCheckpointId((current) => {
      if (current && displayCheckpoints.some((item) => item.checkpoint_id === current)) {
        return current;
      }
      return defaultId;
    });
  }, [activeCheckpointId, displayCheckpoints]);

  const selectedCheckpoint = useMemo(
    () =>
      displayCheckpoints.find((checkpoint) => checkpoint.checkpoint_id === selectedCheckpointId)
      ?? displayCheckpoints[0]
      ?? null,
    [displayCheckpoints, selectedCheckpointId],
  );
  const selectedState = stateBlob(selectedCheckpoint);
  const selectedKind = workflowRunCheckpointLabel(readString(selectedState?.kind));
  const expectedEventName = readString(selectedState?.expected_event_name);
  const waitUntil = readString(selectedState?.wait_until);
  const resumeAt = readString(selectedState?.resume_at);
  const timezoneName = readString(selectedState?.timezone);
  const correlationKey = readString(selectedState?.correlation_key);
  const correlationValue = formatValue(selectedState?.correlation_value);
  const timeoutSeconds = readPositiveNumber(selectedState?.timeout_seconds);
  const timeoutDeadline = formatDeadline(selectedCheckpoint?.created_at, timeoutSeconds);
  const outputPreview = readString(selectedState?.output);
  const inputPorts = portSummary(selectedState?.input_by_port);
  const outputPorts = portSummary(selectedState?.output_by_port);
  const selectedStateNodeId = readString(selectedState?.node_id);
  const totalCheckpoints = checkpoints.length;
  const sourceCheckpointExpected = Boolean(
    activeCheckpointId &&
    activeCheckpointRunId &&
    activeCheckpointRunId !== run.workflow_run_id,
  );
  const selectedCheckpointInherited = Boolean(
    selectedCheckpoint && selectedCheckpoint.workflow_run_id !== run.workflow_run_id,
  );
  const checkpointRetryEnabled = Boolean(
    selectedCheckpoint &&
    selectedCheckpoint.workflow_run_id === run.workflow_run_id &&
    canRetryFromCheckpoint &&
    onRetryFromCheckpoint,
  );
  const selectedCheckpointIntegrityWarning = checkpointIntegrityWarning({
    checkpointId: selectedCheckpoint?.checkpoint_id ?? null,
    checkpointNodeId: selectedCheckpoint?.node_id ?? null,
    stateNodeId: selectedStateNodeId,
    currentNodeId: readString(run.current_node_id),
    active: selectedCheckpoint?.checkpoint_id === activeCheckpointId,
  });

  if (loading) {
    return (
      <div
        data-testid="workflow-run-checkpoint-panel"
        className="rounded-xl border border-slate-200 bg-slate-50/70 px-4 py-4 text-xs text-slate-400 dark:border-slate-700 dark:bg-slate-900/70 dark:text-slate-500"
      >
        Loading persisted checkpoints…
      </div>
    );
  }

  if (displayCheckpoints.length === 0) {
    if (sourceCheckpointExpected && resumeSourceCheckpointLoading) {
      return (
        <div
          data-testid="workflow-run-checkpoint-panel"
          className="rounded-xl border border-sky-200 bg-sky-50/70 px-4 py-4 text-xs leading-relaxed text-sky-800 dark:border-sky-800/70 dark:bg-sky-950/30 dark:text-sky-200"
        >
          Loading inherited resume checkpoint {activeCheckpointId} from {activeCheckpointRunId}.
        </div>
      );
    }
    if (sourceCheckpointExpected && resumeSourceCheckpointError) {
      return (
        <div
          data-testid="workflow-run-checkpoint-source-error"
          className="rounded-xl border border-red-200 bg-red-50/70 px-4 py-4 text-xs leading-relaxed text-red-800 dark:border-red-800/70 dark:bg-red-950/30 dark:text-red-200"
        >
          {inheritedCheckpointLoadErrorMessage({
            checkpointId: activeCheckpointId,
            checkpointRunId: activeCheckpointRunId,
            retryMode,
            runStatus: run.status,
            errorMessage: resumeSourceCheckpointError,
          })}
        </div>
      );
    }
    if (sourceCheckpointExpected) {
      return (
        <div
          data-testid="workflow-run-checkpoint-panel"
          className="rounded-xl border border-amber-200 bg-amber-50/70 px-4 py-4 text-xs leading-relaxed text-amber-800 dark:border-amber-800/70 dark:bg-amber-950/30 dark:text-amber-200"
        >
          {inheritedCheckpointUnavailableMessage({
            checkpointId: activeCheckpointId,
            checkpointRunId: activeCheckpointRunId,
            retryMode,
            runStatus: run.status,
          })}
        </div>
      );
    }
    return (
      <div
        data-testid="workflow-run-checkpoint-empty"
        className="rounded-xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-4 text-xs leading-relaxed text-slate-500 dark:border-slate-700 dark:bg-slate-900/70 dark:text-slate-400"
      >
        {emptyCheckpointMessage(run)}
      </div>
    );
  }

  return (
    <div
      data-testid="workflow-run-checkpoint-panel"
      className="grid grid-cols-1 gap-4 xl:grid-cols-[18rem_minmax(0,1fr)]"
    >
      <div className="rounded-2xl border border-slate-200/70 bg-slate-50/70 p-3 dark:border-slate-700/70 dark:bg-slate-900/70">
        <div className="mb-3 flex items-start justify-between gap-3">
          <div>
            <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">
              Checkpoint Trail
            </div>
            <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
              {totalCheckpoints} persisted checkpoint{totalCheckpoints === 1 ? "" : "s"}
            </div>
          </div>
          {activeCheckpointId && (
            <span className="rounded-full bg-sky-50 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-sky-700 ring-1 ring-sky-200/80 dark:bg-sky-950/40 dark:text-sky-300 dark:ring-sky-700/60">
              Resume target
            </span>
          )}
        </div>
        {sourceCheckpointExpected && (
          <div
            data-testid={
              resumeSourceCheckpointError
                ? "workflow-run-checkpoint-source-error"
                : "workflow-run-checkpoint-source-banner"
            }
            className={`mb-3 rounded-xl border px-3 py-3 text-xs leading-relaxed ${
              resumeSourceCheckpointError
                ? "border-red-200 bg-red-50/80 text-red-800 dark:border-red-800/70 dark:bg-red-950/30 dark:text-red-200"
                : "border-sky-200 bg-sky-50/80 text-sky-800 dark:border-sky-800/70 dark:bg-sky-950/30 dark:text-sky-200"
            }`}
          >
            {resumeSourceCheckpointError
              ? inheritedCheckpointLoadErrorMessage({
                  checkpointId: activeCheckpointId,
                  checkpointRunId: activeCheckpointRunId,
                  retryMode,
                  runStatus: run.status,
                  errorMessage: resumeSourceCheckpointError,
                })
              : resumeSourceCheckpoint
                ? `This ${retryMode === "checkpoint" ? "checkpoint retry" : "run"} resumes from ${resumeSourceCheckpoint.checkpoint_id} captured on ${resumeSourceCheckpoint.workflow_run_id}. New checkpoints created by the current attempt will appear below.`
                : `This run references inherited checkpoint ${activeCheckpointId} on ${activeCheckpointRunId}.`}
          </div>
        )}
        <div className="space-y-2">
          {displayCheckpoints.map((checkpoint) => {
            const state = stateBlob(checkpoint);
            const kind = workflowRunCheckpointLabel(readString(state?.kind));
            const selected = checkpoint.checkpoint_id === selectedCheckpoint?.checkpoint_id;
            const active = checkpoint.checkpoint_id === activeCheckpointId;
            const inherited = checkpoint.workflow_run_id !== run.workflow_run_id;
            return (
              <button
                key={checkpoint.checkpoint_id}
                type="button"
                data-testid={
                  inherited
                    ? "workflow-run-checkpoint-item-source"
                    : `workflow-run-checkpoint-item-${checkpoint.sequence}`
                }
                onClick={() => setSelectedCheckpointId(checkpoint.checkpoint_id)}
                className={`w-full rounded-2xl border px-3 py-3 text-left transition-colors ${selectedCardClasses(selected)}`}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <div className="truncate text-sm font-semibold text-slate-800 dark:text-slate-100">
                        #{checkpoint.sequence} · {kind}
                      </div>
                      {inherited && (
                        <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-amber-700 dark:bg-amber-950/50 dark:text-amber-200">
                          Inherited
                        </span>
                      )}
                    </div>
                    <div className="mt-0.5 truncate font-mono text-[11px] text-slate-500 dark:text-slate-400">
                      {checkpoint.node_id}
                    </div>
                    {inherited && (
                      <div className="mt-1 text-[11px] text-slate-400 dark:text-slate-500">
                        source run {checkpoint.workflow_run_id}
                      </div>
                    )}
                  </div>
                  {active && (
                    <span className="rounded-full bg-sky-100 px-2 py-0.5 text-[10px] font-semibold text-sky-700 dark:bg-sky-950/50 dark:text-sky-300">
                      Active
                    </span>
                  )}
                </div>
                <div className="mt-2 text-[11px] text-slate-400 dark:text-slate-500">
                  {formatTimestamp(checkpoint.created_at)}
                </div>
              </button>
            );
          })}
        </div>
      </div>

      {selectedCheckpoint && (
        <div
          data-testid="workflow-run-checkpoint-detail"
          className="rounded-2xl border border-slate-200/70 bg-white p-4 shadow-sm dark:border-slate-700/70 dark:bg-slate-900"
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">
                Checkpoint Detail
              </div>
              <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
                {selectedKind}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-slate-500 dark:text-slate-400">
                <span>sequence {selectedCheckpoint.sequence}</span>
                <span>created {formatTimestamp(selectedCheckpoint.created_at)}</span>
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              {selectedCheckpoint.checkpoint_id === activeCheckpointId && (
                <span className="rounded-full bg-sky-50 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-sky-700 ring-1 ring-sky-200/80 dark:bg-sky-950/40 dark:text-sky-300 dark:ring-sky-700/60">
                  Current resume target
                </span>
              )}
              {checkpointRetryEnabled && (
                <button
                  type="button"
                  data-testid="workflow-run-checkpoint-retry"
                  disabled={retryingCheckpointId === selectedCheckpoint.checkpoint_id}
                  onClick={() => onRetryFromCheckpoint?.(selectedCheckpoint.checkpoint_id)}
                  className="rounded-lg border border-violet-200 bg-violet-50 px-3 py-1.5 text-[11px] font-semibold text-violet-700 transition-colors hover:bg-violet-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-violet-500/40 dark:bg-violet-500/10 dark:text-violet-200 dark:hover:bg-violet-500/20"
                >
                  {retryingCheckpointId === selectedCheckpoint.checkpoint_id
                    ? "Retrying…"
                    : "Retry from this checkpoint"}
                </button>
              )}
              <span className="rounded-md bg-slate-50 px-2 py-1 font-mono text-[11px] text-slate-600 ring-1 ring-slate-200/70 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700/70">
                {selectedCheckpoint.checkpoint_id}
              </span>
            </div>
          </div>

          <div className="mt-4 grid gap-3 md:grid-cols-2">
            <SummaryRow label="Node" value={selectedCheckpoint.node_id} mono />
            <SummaryRow label="Kind" value={selectedKind} />
            <SummaryRow label="Checkpoint run" value={selectedCheckpoint.workflow_run_id} mono />
            {expectedEventName && <SummaryRow label="Expected event" value={expectedEventName} mono />}
            {waitUntil && <SummaryRow label="Wait until" value={waitUntil} mono />}
            {resumeAt && <SummaryRow label="Resume at" value={resumeAt} mono />}
            {timezoneName && <SummaryRow label="Timezone" value={timezoneName} mono />}
            {correlationKey && <SummaryRow label="Correlation key" value={correlationKey} mono />}
            {correlationValue && <SummaryRow label="Correlation value" value={correlationValue} mono />}
            {timeoutSeconds !== null && <SummaryRow label="Wait timeout" value={`${timeoutSeconds}s`} />}
            {timeoutDeadline && <SummaryRow label="Timeout deadline" value={timeoutDeadline} />}
            {inputPorts && <SummaryRow label="Input ports" value={inputPorts} />}
            {outputPorts && <SummaryRow label="Output ports" value={outputPorts} />}
          </div>

          {selectedCheckpointIntegrityWarning && (
            <div
              data-testid="workflow-run-checkpoint-integrity-note"
              className="mt-4 rounded-xl border border-red-200 bg-red-50/80 px-3 py-3 text-xs leading-relaxed text-red-800 dark:border-red-800/70 dark:bg-red-950/30 dark:text-red-200"
            >
              {selectedCheckpointIntegrityWarning}
            </div>
          )}

          {selectedCheckpointInherited && (
            <div
              data-testid="workflow-run-checkpoint-source-note"
              className="mt-4 rounded-xl border border-amber-200 bg-amber-50/80 px-3 py-3 text-xs leading-relaxed text-amber-800 dark:border-amber-800/70 dark:bg-amber-950/30 dark:text-amber-200"
            >
              This checkpoint belongs to {selectedCheckpoint.workflow_run_id} and is shown for
              retry lineage context. Retry from checkpoint is only available for checkpoints
              persisted by the current run.
            </div>
          )}

          {outputPreview && (
            <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50/80 p-3 dark:border-slate-700 dark:bg-slate-950/70">
              <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
                Output snapshot
              </div>
              <div className="mt-2 whitespace-pre-wrap text-xs leading-relaxed text-slate-600 dark:text-slate-300">
                {outputPreview}
              </div>
            </div>
          )}

          <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50/80 p-3 dark:border-slate-700 dark:bg-slate-950/70">
            <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
              Raw checkpoint state
            </div>
            <pre
              data-testid="workflow-run-checkpoint-json"
              className="max-h-80 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-900 px-3 py-3 text-[11px] leading-5 text-slate-100"
            >
              {formatJson(selectedCheckpoint.state_blob)}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}

export function checkpointLoadErrorMessage(
  runStatus: string | null | undefined,
  errorMessage: string,
): JSX.Element {
  const detail = errorMessage.trim() || "Unknown error";
  if (isActiveRunStatus(runStatus)) {
    return (
      <>
        Resume checkpoints could not be loaded while this run is still active. Use the recovery,
        debugger, and lineage panels to confirm the current gate or retry source while checkpoint
        history is unavailable, then retry this lookup after persistence catches up.
        <span className="mt-2 block text-red-700/80">Latest checkpoint error: {detail}</span>
      </>
    );
  }
  if (runStatus === "completed") {
    return (
      <>
        Resume checkpoints could not be loaded for this completed run. Inspect the recovery,
        debugger, final outputs, and generated artifacts to reconstruct how it crossed any resume
        boundary, then retry this lookup if you need the stored checkpoint trail restored.
        <span className="mt-2 block text-red-700/80">Latest checkpoint error: {detail}</span>
      </>
    );
  }
  if (isStoppedRunStatus(runStatus)) {
    return (
      <>
        Resume checkpoints could not be loaded for this stopped run. Inspect the recovery,
        debugger, and lineage panels to trace where execution failed or was interrupted before the
        checkpoint trail could be restored.
        <span className="mt-2 block text-red-700/80">Latest checkpoint error: {detail}</span>
      </>
    );
  }
  return (
    <>
      Resume checkpoints could not be loaded for this run. Use the recovery, debugger, and lineage
      panels to follow the current execution evidence until checkpoint history is available again.
      <span className="mt-2 block text-red-700/80">Latest checkpoint error: {detail}</span>
    </>
  );
}
