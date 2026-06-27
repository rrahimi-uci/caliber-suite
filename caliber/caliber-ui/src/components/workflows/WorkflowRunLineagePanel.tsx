import { useMemo } from "react";

import type { WorkflowRun, WorkflowRunLineage } from "@/api/workflowTypes";
import {
  workflowRunStatusLabel,
  workflowRunStatusRingClass,
} from "@/lib/workflowRunLabels";
import {
  workflowRunRetryEntryDetail,
  workflowRunRetryEntryLabel,
  workflowRunRetryLineageDetail,
} from "@/lib/workflowRunSummary";

interface WorkflowRunLineagePanelProps {
  run: WorkflowRun;
  lineage?: WorkflowRunLineage | null;
  loading?: boolean;
  loadError?: string | null;
  runs?: WorkflowRun[];
  onSelectRun?: (run: WorkflowRun) => void;
}

interface ResolvedLineageState {
  rootRunId: string;
  totalAttempts: number;
  parentCount: number;
  childCount: number;
  missingParentId: string | null;
  truncated: boolean;
  lineageRuns: WorkflowRun[];
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

function formatTimestamp(run: WorkflowRun): string {
  const value = run.completed_at ?? run.started_at ?? run.queued_at;
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

function cardClasses(selected: boolean, clickable: boolean): string {
  const base = selected
    ? "border-sky-300 bg-sky-50/70 shadow-sm dark:border-sky-500/60 dark:bg-sky-950/30"
    : "border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900";
  const hover = clickable
    ? " hover:border-slate-300 hover:bg-slate-50/60 dark:hover:border-slate-600 dark:hover:bg-slate-800/70"
    : "";
  return `${base}${hover}`;
}

function sortLineageRuns(runs: WorkflowRun[]): WorkflowRun[] {
  return [...runs].sort((left, right) => {
    const attemptLeft = Math.max(1, Number(left.attempt_number ?? 1));
    const attemptRight = Math.max(1, Number(right.attempt_number ?? 1));
    if (attemptLeft !== attemptRight) return attemptLeft - attemptRight;
    const timeLeft = left.queued_at ?? left.started_at ?? left.completed_at ?? "";
    const timeRight = right.queued_at ?? right.started_at ?? right.completed_at ?? "";
    if (timeLeft !== timeRight) return timeLeft.localeCompare(timeRight);
    return left.workflow_run_id.localeCompare(right.workflow_run_id);
  });
}

function resolveFallbackLineage(
  run: WorkflowRun,
  runs: WorkflowRun[],
): ResolvedLineageState {
  const map = new Map<string, WorkflowRun>();
  for (const item of [run, ...runs]) {
    map.set(item.workflow_run_id, item);
  }
  const childrenByParent = new Map<string, WorkflowRun[]>();
  for (const item of map.values()) {
    if (!item.parent_run_id) continue;
    const next = childrenByParent.get(item.parent_run_id) ?? [];
    next.push(item);
    childrenByParent.set(item.parent_run_id, next);
  }

  const ancestorIds: string[] = [run.workflow_run_id];
  let missingParentId: string | null = null;
  let cursor = run;
  const seenAncestors = new Set<string>(ancestorIds);
  while (cursor.parent_run_id) {
    const parentId = cursor.parent_run_id;
    if (seenAncestors.has(parentId)) break;
    seenAncestors.add(parentId);
    const parent = map.get(parentId);
    if (!parent) {
      missingParentId = parentId;
      break;
    }
    ancestorIds.push(parent.workflow_run_id);
    cursor = parent;
  }

  const rootId = ancestorIds[ancestorIds.length - 1] ?? run.workflow_run_id;
  const connectedIds = new Set<string>();
  const queue: string[] = [rootId];
  while (queue.length > 0) {
    const currentId = queue.shift()!;
    if (connectedIds.has(currentId)) continue;
    connectedIds.add(currentId);
    for (const child of childrenByParent.get(currentId) ?? []) {
      queue.push(child.workflow_run_id);
    }
  }

  const lineageRuns = sortLineageRuns(
    [...connectedIds]
      .map((id) => map.get(id))
      .filter((item): item is WorkflowRun => Boolean(item)),
  );

  return {
    rootRunId: rootId,
    totalAttempts: lineageRuns.length,
    parentCount: ancestorIds.length - 1,
    childCount: childrenByParent.get(run.workflow_run_id)?.length ?? 0,
    missingParentId,
    truncated: false,
    lineageRuns,
  };
}

function emptyLineageMessage(run: WorkflowRun): string {
  const base =
    "No retries have been recorded for this run yet. When operators retry a failed or cancelled run, the new attempt will appear here with its own run id and status.";
  if (
    run.status === "queued"
    || run.status === "running"
    || run.status === "resuming"
    || run.status === "cancel_requested"
  ) {
    return `${base} This attempt is still in flight, so no retry lineage exists yet. Use the debugger and recovery panels to inspect the current execution until it either completes or spawns another attempt.`;
  }
  if (run.status === "waiting_approval" || run.status === "waiting_event") {
    return `${base} Use the recovery and checkpoint panels to inspect the active gate on the current attempt while the retry chain remains empty.`;
  }
  if (run.status === "failed" || run.status === "cancelled" || run.status === "rejected" || run.status === "expired" || run.status === "blocked") {
    return `${base} Use the debugger and recovery panels to inspect the current failure before starting the next attempt.`;
  }
  if (run.status === "completed") {
    return `${base} This run completed on its first attempt, so no retry chain was created. Use the debugger, outputs, and generated artifacts to inspect the terminal result.`;
  }
  return `${base} Use the debugger and recovery panels to inspect the current attempt until another run is spawned.`;
}

function missingParentLineageMessage(run: WorkflowRun, missingParentId: string): string {
  const base =
    `Parent run ${missingParentId} is outside the currently loaded run history, so this retry chain is partial.`;
  if (isActiveRunStatus(run.status)) {
    return `${base} This attempt is still active, so refresh workflow run history or inspect the nearest visible parent/current attempts, recovery state, and checkpoints while newer lineage evidence is still arriving.`;
  }
  if (run.status === "waiting_approval" || run.status === "waiting_event") {
    return `${base} Use the nearest visible parent/current attempts plus the recovery and checkpoint panels to trace the active gate until the missing branch can be restored.`;
  }
  if (run.status === "completed") {
    return `${base} Open the nearest visible parent/current attempts, debugger state, outputs, and generated artifacts to reconstruct how this retry chain resolved.`;
  }
  if (isStoppedRunStatus(run.status)) {
    return `${base} Open the nearest visible parent/current attempts, debugger state, and recovery diagnostics to trace where this retry branch failed or was interrupted.`;
  }
  return `${base} Refresh workflow run history or open the nearest visible parent/current attempts to continue tracing the missing branch.`;
}

function truncatedLineageMessage(run: WorkflowRun): string {
  const base =
    "This retry chain is longer than the current panel limit, so CALIBER is showing the nearest attempts only.";
  if (isActiveRunStatus(run.status)) {
    return `${base} Keep following the visible attempts plus the recovery and debugger panels while execution is still in flight.`;
  }
  if (run.status === "waiting_approval" || run.status === "waiting_event") {
    return `${base} Use the visible attempts together with the checkpoint and recovery panels to follow the active gate.`;
  }
  if (run.status === "completed") {
    return `${base} Use the visible attempts, outputs, and generated artifacts to inspect how the chain converged on its completed result.`;
  }
  if (isStoppedRunStatus(run.status)) {
    return `${base} Use the visible attempts, debugger state, and recovery diagnostics to trace where the retry chain stopped.`;
  }
  return `${base} Open the visible attempts to inspect the current branch of the chain.`;
}

export function lineageLoadErrorMessage(
  runStatus: string | null | undefined,
  errorMessage: string,
): JSX.Element {
  const detail = errorMessage.trim() || "Unknown error";
  if (isActiveRunStatus(runStatus ?? "")) {
    return (
      <>
        Canonical retry lineage could not be loaded while this run is still active. CALIBER is
        showing the nearest retry chain reconstructed from the loaded runs instead, so use the
        debugger, recovery, and checkpoint panels while lineage history catches up.
        <span className="mt-2 block text-red-700/80">Latest lineage error: {detail}</span>
      </>
    );
  }
  if (runStatus === "waiting_approval" || runStatus === "waiting_event") {
    return (
      <>
        Canonical retry lineage could not be loaded for this paused run. CALIBER is showing the
        nearest retry chain reconstructed from the loaded runs instead, so use the checkpoint and
        recovery panels to follow the active gate until lineage history is restored.
        <span className="mt-2 block text-red-700/80">Latest lineage error: {detail}</span>
      </>
    );
  }
  if (runStatus === "completed") {
    return (
      <>
        Canonical retry lineage could not be loaded for this completed run. CALIBER is showing the
        nearest retry chain reconstructed from the loaded runs instead, so use the debugger, final
        outputs, and generated artifacts to reconstruct how the retry path resolved.
        <span className="mt-2 block text-red-700/80">Latest lineage error: {detail}</span>
      </>
    );
  }
  if (isStoppedRunStatus(runStatus ?? "")) {
    return (
      <>
        Canonical retry lineage could not be loaded for this stopped run. CALIBER is showing the
        nearest retry chain reconstructed from the loaded runs instead, so use the debugger,
        recovery, and checkpoint panels to trace where the retry path failed or was interrupted.
        <span className="mt-2 block text-red-700/80">Latest lineage error: {detail}</span>
      </>
    );
  }
  return (
    <>
      Canonical retry lineage could not be loaded for this run. CALIBER is showing the nearest
      retry chain reconstructed from the loaded runs until server lineage history is restored.
      <span className="mt-2 block text-red-700/80">Latest lineage error: {detail}</span>
    </>
  );
}

export function WorkflowRunLineagePanel({
  run,
  lineage = null,
  loading = false,
  loadError = null,
  runs = [],
  onSelectRun,
}: WorkflowRunLineagePanelProps): JSX.Element {
  const resolvedLineage = useMemo<ResolvedLineageState>(() => {
    if (lineage?.runs?.length) {
      return {
        rootRunId: lineage.root_run_id || run.workflow_run_id,
        totalAttempts: Math.max(
          1,
          Number(lineage.total_attempts ?? lineage.runs.length ?? 1),
        ),
        parentCount: Math.max(0, Number(lineage.parent_count ?? 0)),
        childCount: Math.max(0, Number(lineage.child_count ?? 0)),
        missingParentId: lineage.missing_parent_id ?? null,
        truncated: Boolean(lineage.truncated),
        lineageRuns: sortLineageRuns(lineage.runs),
      };
    }
    return resolveFallbackLineage(run, runs);
  }, [lineage, run, runs]);

  const currentAttempt = Math.max(1, Number(run.attempt_number ?? 1));
  const totalAttempts = Math.max(
    resolvedLineage.totalAttempts,
    resolvedLineage.lineageRuns.length,
    currentAttempt,
  );
  const currentEntryMode = workflowRunRetryEntryLabel(run);
  const currentEntryDetail = workflowRunRetryEntryDetail(run);

  return (
    <div
      data-testid="workflow-run-lineage-panel"
      className="space-y-4 rounded-2xl border border-slate-200/70 bg-white p-4 shadow-sm dark:border-slate-700/70 dark:bg-slate-900"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">
            Retry Lineage
          </div>
          <div className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
            Attempt {currentAttempt} of {Math.max(totalAttempts, currentAttempt)}
          </div>
          <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">
            {currentEntryDetail}
          </div>
        </div>
        <div className={`rounded-full px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.14em] ring-1 ${workflowRunStatusRingClass(run.status)}`}>
          {workflowRunStatusLabel(run.status)}
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <SummaryRow label="Current attempt" value={String(currentAttempt)} />
        <SummaryRow label="Entry mode" value={currentEntryMode} />
        <SummaryRow label="Root run" value={resolvedLineage.rootRunId} mono />
        <SummaryRow label="Parent run" value={run.parent_run_id ?? "None"} mono />
        <SummaryRow
          label="Child retries"
          value={String(resolvedLineage.childCount)}
        />
      </div>

      {loading && !lineage && resolvedLineage.lineageRuns.length <= 1 && (
        <div className="rounded-xl border border-sky-200 bg-sky-50 px-3 py-3 text-xs text-sky-800 dark:border-sky-800/70 dark:bg-sky-950/30 dark:text-sky-200">
          Loading canonical retry lineage from the server.
        </div>
      )}

      {loadError && (
        <div
          data-testid="workflow-run-lineage-error"
          className="rounded-xl border border-red-200/70 bg-red-50 px-3 py-3 text-xs leading-relaxed text-red-700 dark:border-red-800/70 dark:bg-red-950/30 dark:text-red-200"
        >
          {lineageLoadErrorMessage(run.status, loadError)}
        </div>
      )}

      {resolvedLineage.missingParentId && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-3 text-xs text-amber-800 dark:border-amber-800/70 dark:bg-amber-950/30 dark:text-amber-200">
          {missingParentLineageMessage(run, resolvedLineage.missingParentId)}
        </div>
      )}

      {resolvedLineage.truncated && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-3 text-xs text-amber-800 dark:border-amber-800/70 dark:bg-amber-950/30 dark:text-amber-200">
          {truncatedLineageMessage(run)}
        </div>
      )}

      {resolvedLineage.lineageRuns.length <= 1 ? (
        <div
          data-testid="workflow-run-lineage-empty"
          className="rounded-xl border border-dashed border-slate-200 bg-slate-50/70 px-4 py-4 text-xs leading-relaxed text-slate-500 dark:border-slate-700 dark:bg-slate-950/60 dark:text-slate-400"
        >
          {emptyLineageMessage(run)}
        </div>
      ) : (
        <div className="space-y-2">
          {resolvedLineage.lineageRuns.map((item, index) => {
            const selected = item.workflow_run_id === run.workflow_run_id;
            const clickable = Boolean(onSelectRun) && !selected;
            const relationBadges: string[] = [];
            if (item.workflow_run_id === resolvedLineage.rootRunId || index === 0)
              relationBadges.push("root");
            if (selected) relationBadges.push("current");
            if (item.workflow_run_id === run.parent_run_id) relationBadges.push("parent");
            if (item.parent_run_id === run.workflow_run_id) relationBadges.push("child");
            const body = (
              <>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <div className="font-mono text-sm font-semibold text-slate-800 dark:text-slate-100">
                        {item.workflow_run_id}
                      </div>
                      <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                        attempt {Math.max(1, Number(item.attempt_number ?? 1))}
                      </span>
                      {relationBadges.map((badge) => (
                        <span
                          key={`${item.workflow_run_id}-${badge}`}
                          className="rounded-full bg-white px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500 ring-1 ring-slate-200/80 dark:bg-slate-900 dark:text-slate-400 dark:ring-slate-700/70"
                        >
                          {badge}
                        </span>
                      ))}
                    </div>
                    <div className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
                      {formatTimestamp(item)}
                      {` · ${workflowRunRetryLineageDetail(item)}`}
                    </div>
                    {item.error_summary && (
                      <div className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
                        {item.error_summary}
                      </div>
                    )}
                  </div>
                  <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] ring-1 ${workflowRunStatusRingClass(item.status)}`}>
                    {workflowRunStatusLabel(item.status)}
                  </span>
                </div>
              </>
            );
            if (clickable) {
              return (
                <button
                  key={item.workflow_run_id}
                  type="button"
                  data-testid={`workflow-run-lineage-item-${item.workflow_run_id}`}
                  onClick={() => onSelectRun?.(item)}
                  className={`w-full rounded-2xl border px-3 py-3 text-left transition-colors ${cardClasses(selected, clickable)}`}
                >
                  {body}
                </button>
              );
            }
            return (
              <div
                key={item.workflow_run_id}
                data-testid={`workflow-run-lineage-item-${item.workflow_run_id}`}
                className={`rounded-2xl border px-3 py-3 ${cardClasses(selected, clickable)}`}
              >
                {body}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
