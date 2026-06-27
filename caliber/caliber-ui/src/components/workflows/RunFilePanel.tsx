import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Boxes, Download, FileText, Link2, RefreshCw, UploadCloud } from "lucide-react";

import { ApiError, caliberApi } from "@/api/caliberApi";
import type { WorkflowFile, WorkflowRun } from "@/api/workflowTypes";
import { normalizeWorkflowRunArtifactPersistence } from "@/lib/workflowRunSummary";

const TABS: Array<{ key: string; label: string }> = [
  { key: "input", label: "Inputs" },
  { key: "work", label: "Work" },
  { key: "artifact", label: "Artifacts" },
  { key: "log", label: "Logs" },
];

type ScopeMode = "all" | "focused";

interface RunFilePanelErrorState {
  detail: string;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function kindTone(kind: string): string {
  switch (kind) {
    case "input":
      return "border-sky-200 bg-sky-50 text-sky-700 dark:border-sky-800/70 dark:bg-sky-950/40 dark:text-sky-300";
    case "work":
      return "border-violet-200 bg-violet-50 text-violet-700 dark:border-violet-800/70 dark:bg-violet-950/40 dark:text-violet-300";
    case "artifact":
      return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300";
    case "log":
      return "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800/70 dark:bg-amber-950/40 dark:text-amber-300";
    default:
      return "border-slate-200 bg-slate-50 text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300";
  }
}

function statusTone(status: string): string {
  switch (status) {
    case "artifact":
    case "attached":
    case "uploaded":
    case "completed":
      return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300";
    case "pending":
    case "pending_upload":
      return "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800/70 dark:bg-amber-950/40 dark:text-amber-300";
    case "failed":
    case "rejected":
      return "border-red-200 bg-red-50 text-red-700 dark:border-red-800/70 dark:bg-red-950/40 dark:text-red-300";
    default:
      return "border-slate-200 bg-slate-50 text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300";
  }
}

function persistenceTone(status: string): string {
  switch (status) {
    case "persisted":
      return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300";
    case "failed":
      return "border-red-200 bg-red-50 text-red-700 dark:border-red-800/70 dark:bg-red-950/40 dark:text-red-300";
    default:
      return "border-slate-200 bg-slate-50 text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300";
  }
}

function persistenceLabel(status: string): string {
  switch (status) {
    case "persisted":
      return "Persisted";
    case "failed":
      return "Failed";
    default:
      return status;
  }
}

function isActiveRunStatus(runStatus: string | null): boolean {
  return (
    runStatus === "queued"
    || runStatus === "running"
    || runStatus === "resuming"
    || runStatus === "cancel_requested"
    || runStatus === "waiting_approval"
    || runStatus === "waiting_event"
  );
}

function isStoppedRunStatus(runStatus: string | null): boolean {
  return (
    runStatus === "failed"
    || runStatus === "cancelled"
    || runStatus === "rejected"
    || runStatus === "expired"
    || runStatus === "blocked"
  );
}

function runFileLoadErrorMessage({
  runStatus,
  detail,
  selectedNodeId,
  scopeMode,
}: {
  runStatus: string | null;
  detail: string;
  selectedNodeId: string | null;
  scopeMode: ScopeMode;
}): JSX.Element {
  const scopeHint =
    scopeMode === "focused" && selectedNodeId
      ? (
          <>
            Keep the step focus on <span className="font-mono">{selectedNodeId}</span> only after
            the file index recovers; until then, use the debugger and recovery panels to trace
            that node&apos;s execution evidence.
          </>
        )
      : (
          <>
            Use the debugger, recovery, and checkpoint panels while the file index is unavailable,
            then refresh this panel when storage or API health recovers.
          </>
        );
  if (isActiveRunStatus(runStatus)) {
    return (
      <>
        Files and artifact lineage could not be loaded while this run is still active. {scopeHint}
        <span className="mt-2 block text-red-700/80">Latest file error: {detail}</span>
      </>
    );
  }
  if (runStatus === "completed") {
    return (
      <>
        Files and artifact lineage could not be loaded for this completed run. Inspect the
        debugger, final outputs, generated artifacts, and object-store persistence summary to
        reconstruct where execution wrote data until the file index becomes available again.
        <span className="mt-2 block text-red-700/80">Latest file error: {detail}</span>
      </>
    );
  }
  if (isStoppedRunStatus(runStatus)) {
    return (
      <>
        Files and artifact lineage could not be loaded for this stopped run. Inspect the debugger,
        recovery diagnostics, and retry lineage to trace whether execution failed before file
        persistence completed.
        <span className="mt-2 block text-red-700/80">Latest file error: {detail}</span>
      </>
    );
  }
  return (
    <>
      Files and artifact lineage could not be loaded for this run. Use the debugger and recovery
      panels to trace persisted execution evidence until this file index becomes available again.
      <span className="mt-2 block text-red-700/80">Latest file error: {detail}</span>
    </>
  );
}

function runFileUploadErrorMessage(detail: string): JSX.Element {
  return (
    <>
      Uploading files to this run failed. Any files that were already persisted stay visible below,
      so inspect the current lineage before retrying the upload.
      <span className="mt-2 block text-red-700/80">Latest upload error: {detail}</span>
    </>
  );
}

function producerLabel(file: WorkflowFile): string {
  if (file.producer_node_id) return file.producer_node_id;
  if (file.kind === "input") return "manual input";
  if (file.kind === "log") return "runtime log";
  return "unassigned";
}

function sortFiles(files: WorkflowFile[]): WorkflowFile[] {
  return [...files].sort((left, right) => {
    const leftTime = left.created_at ? Date.parse(left.created_at) : 0;
    const rightTime = right.created_at ? Date.parse(right.created_at) : 0;
    if (leftTime !== rightTime) return rightTime - leftTime;
    return left.relative_path.localeCompare(right.relative_path);
  });
}

function SummaryCard({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail: string;
}): JSX.Element {
  return (
    <div className="rounded-2xl border border-slate-200/70 bg-slate-50/70 px-3 py-3 dark:border-slate-700/70 dark:bg-slate-900/70">
      <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
        {label}
      </div>
      <div className="mt-2 text-lg font-semibold text-slate-900 dark:text-slate-100">{value}</div>
      <div className="mt-1 text-[11px] leading-relaxed text-slate-500 dark:text-slate-400">{detail}</div>
    </div>
  );
}

export interface RunFilePanelProps {
  runId: string;
  runStatus?: string | null;
  runSummary?: WorkflowRun["summary"] | null;
  canUpload?: boolean;
  selectedNodeId?: string | null;
  onSelectNodeId?: (nodeId: string | null) => void;
  compact?: boolean;
}

export function RunFilePanel({
  runId,
  runStatus = null,
  runSummary = null,
  canUpload = true,
  selectedNodeId = null,
  onSelectNodeId,
  compact = false,
}: RunFilePanelProps): JSX.Element {
  const [activeKind, setActiveKind] = useState<string>("input");
  const [scopeMode, setScopeMode] = useState<ScopeMode>("all");
  const [files, setFiles] = useState<WorkflowFile[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [loadError, setLoadError] = useState<RunFilePanelErrorState | null>(null);
  const [uploadError, setUploadError] = useState<RunFilePanelErrorState | null>(null);
  const [uploading, setUploading] = useState<boolean>(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const reload = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setLoadError(null);
      setUploadError(null);
      try {
        const result = await caliberApi.listRunFiles(runId, undefined, signal);
        setFiles(sortFiles(result.items));
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setLoadError({
          detail: err instanceof ApiError ? err.message : String(err),
        });
      } finally {
        setLoading(false);
      }
    },
    [runId],
  );

  useEffect(() => {
    const controller = new AbortController();
    void reload(controller.signal);
    return () => controller.abort();
  }, [reload]);

  useEffect(() => {
    setScopeMode(selectedNodeId ? "focused" : "all");
  }, [selectedNodeId, runId]);

  const onUpload = useCallback(
    async (selected: FileList | null) => {
      if (!selected || selected.length === 0) return;
      setUploading(true);
      setUploadError(null);
      try {
        for (const file of Array.from(selected)) {
          await caliberApi.uploadRunFile(runId, file, activeKind);
        }
        await reload();
      } catch (err) {
        setUploadError({
          detail: err instanceof ApiError ? err.message : String(err),
        });
      } finally {
        setUploading(false);
        if (fileInputRef.current) fileInputRef.current.value = "";
      }
    },
    [runId, activeKind, reload],
  );

  const scopedFiles = useMemo(() => {
    if (scopeMode === "focused" && selectedNodeId) {
      return files.filter((file) => file.producer_node_id === selectedNodeId);
    }
    return files;
  }, [files, scopeMode, selectedNodeId]);

  const kindCounts = useMemo(() => {
    const counts = Object.fromEntries(TABS.map((tab) => [tab.key, 0])) as Record<string, number>;
    for (const file of scopedFiles) {
      counts[file.kind] = (counts[file.kind] ?? 0) + 1;
    }
    return counts;
  }, [scopedFiles]);

  useEffect(() => {
    if (scopedFiles.length === 0) return;
    if ((kindCounts[activeKind] ?? 0) > 0) return;
    const fallback = TABS.find((tab) => (kindCounts[tab.key] ?? 0) > 0)?.key;
    if (fallback && fallback !== activeKind) {
      setActiveKind(fallback);
    }
  }, [activeKind, kindCounts, scopedFiles.length]);

  const visible = useMemo(
    () => scopedFiles.filter((file) => file.kind === activeKind),
    [activeKind, scopedFiles],
  );

  const distinctProducerCount = useMemo(
    () =>
      new Set(scopedFiles.map((file) => file.producer_node_id).filter((value): value is string => Boolean(value))).size,
    [scopedFiles],
  );
  const linkedFileCount = useMemo(
    () => scopedFiles.filter((file) => Boolean(file.producer_node_id)).length,
    [scopedFiles],
  );
  const visibleBytes = useMemo(
    () => scopedFiles.reduce((sum, file) => sum + Number(file.size_bytes || 0), 0),
    [scopedFiles],
  );
  const producerCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const file of files) {
      if (!file.producer_node_id) continue;
      counts.set(file.producer_node_id, (counts.get(file.producer_node_id) ?? 0) + 1);
    }
    return [...counts.entries()].sort((left, right) => {
      if (left[1] !== right[1]) return right[1] - left[1];
      return left[0].localeCompare(right[0]);
    });
  }, [files]);
  const artifactPersistence = useMemo(
    () => normalizeWorkflowRunArtifactPersistence(runSummary?.artifact_persistence),
    [runSummary],
  );
  const artifactPlannedObjectCount = artifactPersistence?.object_count ?? 0;
  const artifactPersistedObjectCount = artifactPersistence
    ? artifactPersistence.status === "persisted"
      ? artifactPersistence.object_count
      : artifactPersistence.persisted_object_count ?? null
    : null;
  const artifactPersistenceDetail = useMemo(() => {
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
      return `${artifactPersistence.object_count} object${artifactPersistence.object_count === 1 ? "" : "s"} were written to ${artifactPersistence.bucket}. This can include persisted run logs even when no artifact files are indexed in this panel.`;
    }
    if (persistedObjectCount > 0) {
      return `The run completed, but object-store persistence failed for ${artifactPersistence.bucket} after ${persistedObjectCount} of ${artifactPersistence.object_count} object${artifactPersistence.object_count === 1 ? "" : "s"} were stored. Inspect the error, stored-object keys, and recovery panel before retrying or rebuilding this run.`;
    }
    return `The run completed, but object-store persistence failed for ${artifactPersistence.bucket} before any of the ${artifactPersistence.object_count} planned object${artifactPersistence.object_count === 1 ? "" : "s"} were stored. Inspect the error below and the recovery panel before retrying or rebuilding this run.`;
  }, [artifactPersistence]);

  const emptyState = useMemo(() => {
    const kindLabel = `${activeKind} files`;
    const waitingStatus = runStatus === "waiting_approval" || runStatus === "waiting_event";
    const activeStatus =
      runStatus === "queued" || runStatus === "running" || runStatus === "resuming" || runStatus === "cancel_requested";
    const stoppedStatus =
      runStatus === "failed" ||
      runStatus === "cancelled" ||
      runStatus === "rejected" ||
      runStatus === "expired" ||
      runStatus === "blocked";
    const completedStatus = runStatus === "completed";

    if (scopeMode === "focused" && selectedNodeId) {
      if (completedStatus) {
        return {
          title: `No ${kindLabel} are linked to ${selectedNodeId}.`,
          detail:
            "This step finished without persisting files of this kind. Clear the step focus or inspect the debugger to confirm where artifacts were produced.",
        };
      }
      if (waitingStatus || activeStatus) {
        return {
          title: `No ${kindLabel} are linked to ${selectedNodeId} yet.`,
          detail:
            "This step may not have produced files yet. Continue the run, refresh this panel, or clear the step focus to inspect artifacts from other nodes.",
        };
      }
      if (stoppedStatus) {
        return {
          title: `No ${kindLabel} are linked to ${selectedNodeId}.`,
          detail:
            "This step did not persist files of this kind before the run stopped. Inspect the debugger and recovery panels, or clear the step focus to inspect earlier artifacts.",
        };
      }
      return {
        title: `No ${kindLabel} are linked to ${selectedNodeId} yet.`,
        detail:
          "Select another step, clear the step focus, or continue the run to generate new artifacts.",
      };
    }

    if (
      activeKind === "artifact" &&
      artifactPersistence &&
      (artifactPersistence.status === "failed" || artifactPersistence.object_count > 0)
    ) {
      if (artifactPersistence.status === "failed") {
        const partialDetail =
          artifactPersistedObjectCount && artifactPersistedObjectCount > 0
            ? `Execution finished, but object-store artifact persistence failed after ${artifactPersistedObjectCount} of ${artifactPlannedObjectCount} objects were stored. Inspect the object-store persistence summary above and the recovery panel to see which generated artifacts already landed and which one failed.`
            : "Execution finished, but object-store artifact persistence failed after the run completed. Inspect the object-store persistence summary above and the recovery panel to see which generated artifacts were not stored.";
        return {
          title: "No artifact files are indexed for this run.",
          detail: partialDetail,
        };
      }
      return {
        title: "No artifact files are indexed in the run file registry.",
        detail:
          "This execution still reported object-store artifact persistence. Inspect the object-store persistence summary above for the target bucket, stored object count, and generated artifact names.",
      };
    }

    if (completedStatus) {
      return {
        title: `No ${kindLabel} were persisted for this run.`,
        detail:
          "This execution completed without writing files of this kind. Inspect the debugger, final output, and other file tabs to confirm where the recorded result landed.",
      };
    }
    if (waitingStatus) {
      return {
        title: `No ${kindLabel} are available yet.`,
        detail:
          "This run is paused at a resume gate before files of this kind were written. Inspect recovery, checkpoints, or the debugger, then refresh after the run continues.",
      };
    }
    if (activeStatus) {
      return {
        title: `No ${kindLabel} are available yet.`,
        detail:
          "The workflow is still executing. Refresh this panel as nodes attach inputs, work files, artifacts, or logs.",
      };
    }
    if (stoppedStatus) {
      return {
        title: `No ${kindLabel} were persisted before this run stopped.`,
        detail:
          "Inspect the debugger, recovery timeline, and retry lineage to see whether the execution failed before any files of this kind were written.",
      };
    }
    return {
      title: `No ${kindLabel}.`,
      detail:
        "Upload inputs, let the workflow execute, or switch tabs to inspect a different file kind.",
    };
  }, [
    activeKind,
    artifactPersistence,
    artifactPersistedObjectCount,
    artifactPlannedObjectCount,
    runStatus,
    scopeMode,
    selectedNodeId,
  ]);

  return (
    <section
      aria-label="Run files"
      data-testid="workflow-run-file-panel"
      className={compact ? "space-y-3" : "space-y-4"}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-wrap gap-2" role="tablist" aria-label="Run file kinds">
          {TABS.map((tab) => {
            const active = activeKind === tab.key;
            return (
              <button
                key={tab.key}
                type="button"
                role="tab"
                aria-selected={active}
                data-testid={`run-file-tab-${tab.key}`}
                className={`rounded-full border px-3 py-1.5 text-xs font-semibold transition ${
                  active
                    ? "border-caliber-200 bg-caliber-50 text-caliber-700 dark:border-violet-500/40 dark:bg-violet-500/15 dark:text-violet-100"
                    : "border-slate-200 bg-white text-slate-500 hover:border-slate-300 hover:text-slate-700 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-300 dark:hover:border-slate-600 dark:hover:text-slate-100"
                }`}
                onClick={() => setActiveKind(tab.key)}
              >
                {tab.label} ({kindCounts[tab.key] ?? 0})
              </button>
            );
          })}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            data-testid="run-file-refresh"
            onClick={() => void reload()}
            className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 transition hover:border-slate-300 hover:text-slate-800 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-300 dark:hover:border-slate-600 dark:hover:text-slate-100"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Refresh
          </button>
          {canUpload && (
            <label className="inline-flex cursor-pointer items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 transition hover:border-slate-300 hover:text-slate-800 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-300 dark:hover:border-slate-600 dark:hover:text-slate-100">
              <UploadCloud className="h-3.5 w-3.5" />
              {uploading ? "Uploading…" : `Upload to ${activeKind}`}
              <input
                ref={fileInputRef}
                type="file"
                multiple
                aria-label="Upload files"
                className="sr-only"
                disabled={uploading}
                onChange={(event) => void onUpload(event.target.files)}
              />
            </label>
          )}
        </div>
      </div>

      {selectedNodeId && (
        <div
          data-testid="workflow-run-file-scope"
          className="rounded-2xl border border-sky-200 bg-sky-50/70 px-4 py-3 text-sm dark:border-sky-800/70 dark:bg-sky-950/30"
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-sky-700 dark:text-sky-300">
                File Lineage Scope
              </div>
              <div className="mt-1 font-semibold text-sky-950 dark:text-sky-100">
                {scopeMode === "focused" ? `Focused on ${selectedNodeId}` : "All run files"}
              </div>
              <div className="mt-1 text-xs leading-relaxed text-sky-800 dark:text-sky-200">
                {scopeMode === "focused"
                  ? "The list is scoped to files produced by the selected step. Switch back to all run files whenever you want a wider view."
                  : "Keep the selected step in the debugger but inspect the complete run file set here."}
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                data-testid="workflow-run-file-scope-focused"
                onClick={() => setScopeMode("focused")}
                className={`rounded-full border px-3 py-1.5 text-xs font-semibold transition ${
                  scopeMode === "focused"
                    ? "border-sky-300 bg-white text-sky-800 dark:border-sky-600 dark:bg-sky-950 dark:text-sky-100"
                    : "border-sky-200 bg-sky-50 text-sky-700 hover:border-sky-300 dark:border-sky-800 dark:bg-sky-950/50 dark:text-sky-300"
                }`}
              >
                Step files
              </button>
              <button
                type="button"
                data-testid="workflow-run-file-scope-all"
                onClick={() => setScopeMode("all")}
                className={`rounded-full border px-3 py-1.5 text-xs font-semibold transition ${
                  scopeMode === "all"
                    ? "border-sky-300 bg-white text-sky-800 dark:border-sky-600 dark:bg-sky-950 dark:text-sky-100"
                    : "border-sky-200 bg-sky-50 text-sky-700 hover:border-sky-300 dark:border-sky-800 dark:bg-sky-950/50 dark:text-sky-300"
                }`}
              >
                All files
              </button>
              {onSelectNodeId && (
                <button
                  type="button"
                  data-testid="workflow-run-file-clear-node"
                  onClick={() => onSelectNodeId(null)}
                  className="rounded-full border border-sky-200 bg-white px-3 py-1.5 text-xs font-semibold text-sky-700 transition hover:border-sky-300 hover:text-sky-900 dark:border-sky-800 dark:bg-slate-950 dark:text-sky-300 dark:hover:border-sky-700 dark:hover:text-sky-100"
                >
                  Clear step focus
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {!compact && !loading && !loadError && (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <SummaryCard
            label="Visible files"
            value={String(scopedFiles.length)}
            detail={
              scopeMode === "focused" && selectedNodeId
                ? `Produced by ${selectedNodeId}`
                : "Across the current run scope"
            }
          />
          <SummaryCard
            label="Visible bytes"
            value={formatBytes(visibleBytes)}
            detail="Compressed and raw artifacts share the same lineage index."
          />
          <SummaryCard
            label="Node-linked"
            value={String(linkedFileCount)}
            detail={`${distinctProducerCount} producing node${distinctProducerCount === 1 ? "" : "s"} recorded`}
          />
          <SummaryCard
            label="Newest file"
            value={scopedFiles[0] ? formatDate(scopedFiles[0].created_at) : "—"}
            detail={scopedFiles[0]?.name ?? "No files in scope"}
          />
        </div>
      )}

      {!loading && !loadError && scopeMode === "all" && artifactPersistence && artifactPersistenceDetail && (
        <div
          data-testid="workflow-run-file-persistence"
          className={`rounded-2xl border px-4 py-3 ${persistenceTone(artifactPersistence.status)}`}
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="text-[11px] font-semibold uppercase tracking-[0.16em] opacity-80">
                Object Store Persistence
              </div>
              <div className="mt-1 font-semibold">
                {artifactPersistence.status === "persisted"
                  ? "Run artifacts were written to object storage."
                  : "Run artifact upload failed after execution completed."}
              </div>
              <div className="mt-1 text-xs leading-relaxed opacity-90">
                {artifactPersistenceDetail}
              </div>
              {artifactPersistence.error && (
                <div className="mt-2 rounded-xl border border-current/20 bg-white/60 px-3 py-2 font-mono text-[11px] leading-relaxed dark:bg-slate-950/40">
                  {artifactPersistence.error}
                </div>
              )}
              {artifactPersistence.failed_object_key && (
                <div className="mt-2 text-[11px] leading-relaxed opacity-90">
                  Failing object: <span className="font-mono">{artifactPersistence.failed_object_key}</span>
                </div>
              )}
              {artifactPersistence.recent_persisted_keys && artifactPersistence.recent_persisted_keys.length > 0 && artifactPersistence.status === "failed" && (
                <div className="mt-2 text-[11px] leading-relaxed opacity-90">
                  Stored before failure: <span className="font-mono">{artifactPersistence.recent_persisted_keys.join(", ")}</span>
                </div>
              )}
              {artifactPersistence.artifact_names.length > 0 && (
                <div className="mt-2 text-[11px] leading-relaxed opacity-90">
                  Named artifacts: {artifactPersistence.artifact_names.join(", ")}
                </div>
              )}
            </div>
            <div className="flex flex-wrap gap-2">
              <span
                data-testid="workflow-run-file-persistence-status"
                className="rounded-full border border-current/20 bg-white/70 px-3 py-1 text-[11px] font-semibold dark:bg-slate-950/40"
              >
                {persistenceLabel(artifactPersistence.status)}
              </span>
              <span className="rounded-full border border-current/20 bg-white/70 px-3 py-1 text-[11px] font-semibold dark:bg-slate-950/40">
                Bucket {artifactPersistence.bucket}
              </span>
              <span className="rounded-full border border-current/20 bg-white/70 px-3 py-1 text-[11px] font-semibold dark:bg-slate-950/40">
                {artifactPersistence.status === "failed" && artifactPersistedObjectCount !== null
                  ? `${artifactPersistedObjectCount} stored before failure`
                  : `${artifactPersistence.object_count} object${artifactPersistence.object_count === 1 ? "" : "s"}`}
              </span>
              {artifactPersistence.status === "failed" && (
                <span className="rounded-full border border-current/20 bg-white/70 px-3 py-1 text-[11px] font-semibold dark:bg-slate-950/40">
                  {artifactPersistence.object_count} planned object{artifactPersistence.object_count === 1 ? "" : "s"}
                </span>
              )}
              <span className="rounded-full border border-current/20 bg-white/70 px-3 py-1 text-[11px] font-semibold dark:bg-slate-950/40">
                {artifactPersistence.artifact_names.length} named artifact{artifactPersistence.artifact_names.length === 1 ? "" : "s"}
              </span>
            </div>
          </div>
        </div>
      )}

      {!loading && !loadError && scopeMode === "all" && producerCounts.length > 0 && onSelectNodeId && (
        <div className="rounded-2xl border border-slate-200/70 bg-slate-50/70 px-4 py-3 dark:border-slate-700/70 dark:bg-slate-900/70">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">
                Producing Nodes
              </div>
              <div className="mt-1 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                Jump from a file-producing node straight back into the run debugger or inspector.
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              {producerCounts.slice(0, compact ? 4 : 8).map(([nodeId, count]) => (
                <button
                  key={nodeId}
                  type="button"
                  data-testid={`workflow-run-file-node-${nodeId}`}
                  onClick={() => {
                    setScopeMode("focused");
                    onSelectNodeId?.(nodeId);
                  }}
                  className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 transition hover:border-caliber-200 hover:text-caliber-purple dark:border-slate-700 dark:bg-slate-950 dark:text-slate-300 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
                >
                  {nodeId} ({count})
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {loading && (
        <div className="rounded-2xl border border-slate-200/70 bg-slate-50/70 px-4 py-8 text-center text-sm text-slate-500 dark:border-slate-700/70 dark:bg-slate-900/70 dark:text-slate-400">
          Loading run files…
        </div>
      )}

      {loadError && (
        <div
          role="alert"
          data-testid="workflow-run-file-load-error"
          className="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm leading-relaxed text-red-700 dark:border-red-800/70 dark:bg-red-950/30 dark:text-red-300"
        >
          {runFileLoadErrorMessage({
            runStatus,
            detail: loadError.detail,
            selectedNodeId,
            scopeMode,
          })}
        </div>
      )}

      {uploadError && !loadError && (
        <div
          role="alert"
          data-testid="workflow-run-file-upload-error"
          className="rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm leading-relaxed text-red-700 dark:border-red-800/70 dark:bg-red-950/30 dark:text-red-300"
        >
          {runFileUploadErrorMessage(uploadError.detail)}
        </div>
      )}

      {!loading && !loadError && visible.length === 0 && (
        <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50/60 px-4 py-8 text-center dark:border-slate-700 dark:bg-slate-900/70">
          <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-2xl bg-white text-slate-400 shadow-sm dark:bg-slate-950 dark:text-slate-500">
            <Boxes className="h-5 w-5" />
          </div>
          <div className="mt-3 text-sm font-semibold text-slate-600 dark:text-slate-200">{emptyState.title}</div>
          <div className="mt-1 text-xs text-slate-400 dark:text-slate-500">{emptyState.detail}</div>
        </div>
      )}

      {!loading && !loadError && visible.length > 0 && (
        <div className="space-y-3">
          {visible.map((file) => {
            const producerNodeId = file.producer_node_id;
            return (
              <article
                key={file.file_id}
                data-file-id={file.file_id}
                data-testid={`workflow-run-file-${file.file_id}`}
                className="rounded-2xl border border-slate-200/70 bg-white px-4 py-4 shadow-sm dark:border-slate-700/70 dark:bg-slate-950/90"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <div className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100">
                        {file.name}
                      </div>
                      <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] ${kindTone(file.kind)}`}>
                        {file.kind}
                      </span>
                      <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] ${statusTone(file.status)}`}>
                        {file.status}
                      </span>
                    </div>
                    <div className="mt-1 break-all text-[11px] text-slate-400 dark:text-slate-500">
                      {file.relative_path}
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    {producerNodeId ? (
                      <button
                        type="button"
                        data-testid={`workflow-run-file-producer-${file.file_id}`}
                        onClick={() => {
                          setScopeMode("focused");
                          onSelectNodeId?.(producerNodeId);
                        }}
                        className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-[11px] font-semibold text-slate-600 transition hover:border-caliber-200 hover:text-caliber-purple dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300 dark:hover:border-violet-500/40 dark:hover:text-violet-100"
                      >
                        <Link2 className="h-3.5 w-3.5" />
                        {producerNodeId}
                      </button>
                    ) : (
                      <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-[11px] font-semibold text-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400">
                        {producerLabel(file)}
                      </span>
                    )}
                    <a
                      href={caliberApi.runFileContentUrl(runId, file.file_id)}
                      download={file.name}
                      className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-3 py-1 text-[11px] font-semibold text-slate-600 transition hover:border-slate-300 hover:text-slate-800 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-300 dark:hover:border-slate-600 dark:hover:text-slate-100"
                    >
                      <Download className="h-3.5 w-3.5" />
                      Download
                    </a>
                  </div>
                </div>

                <div className={`mt-4 grid gap-3 ${compact ? "sm:grid-cols-2" : "md:grid-cols-2 xl:grid-cols-4"}`}>
                  <div className="rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-2 dark:border-slate-700/70 dark:bg-slate-900/70">
                    <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
                      Size
                    </div>
                    <div className="mt-1 text-xs font-medium text-slate-700 dark:text-slate-200">
                      {formatBytes(file.size_bytes)}
                    </div>
                  </div>
                  <div className="rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-2 dark:border-slate-700/70 dark:bg-slate-900/70">
                    <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
                      Media type
                    </div>
                    <div className="mt-1 break-all text-xs font-medium text-slate-700 dark:text-slate-200">
                      {file.media_type ?? "—"}
                    </div>
                  </div>
                  <div className="rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-2 dark:border-slate-700/70 dark:bg-slate-900/70">
                    <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
                      Created
                    </div>
                    <div className="mt-1 text-xs font-medium text-slate-700 dark:text-slate-200">
                      {formatDate(file.created_at)}
                    </div>
                  </div>
                  <div className="rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-2 dark:border-slate-700/70 dark:bg-slate-900/70">
                    <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400 dark:text-slate-500">
                      Ref
                    </div>
                    <div className="mt-1 break-all font-mono text-[11px] text-slate-500 dark:text-slate-300">
                      {file.file_ref}
                    </div>
                  </div>
                </div>

                {file.sha256 && !compact && (
                  <div className="mt-3 flex items-start gap-2 rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-2 text-[11px] text-slate-500 dark:border-slate-700/70 dark:bg-slate-900/70 dark:text-slate-300">
                    <FileText className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    <span className="break-all font-mono">{file.sha256}</span>
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
