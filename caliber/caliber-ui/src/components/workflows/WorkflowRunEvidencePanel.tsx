import { useState } from "react";

import { caliberApi } from "@/api/caliberApi";
import type { WorkflowRunReplayReport } from "@/api/workflowTypes";
import { useApiMutation } from "@/hooks/useApiQuery";

/** Trigger an authenticated evidence archive download without navigating away. */
export function triggerWorkflowRunEvidenceDownload(
  blob: Blob,
  runId: string,
): boolean {
  if (
    typeof document === "undefined" ||
    typeof URL === "undefined" ||
    typeof URL.createObjectURL !== "function"
  ) {
    return false;
  }
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  const safeRunId = runId.replace(/[^a-zA-Z0-9._-]+/g, "-");
  anchor.href = objectUrl;
  anchor.download = `caliber-evidence-${safeRunId}.zip`;
  document.body.appendChild(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    URL.revokeObjectURL(objectUrl);
  }
  return true;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * Read-only evidence and historical replay actions for one selected run.
 *
 * The replay request deliberately has no free-form controls: the backend
 * replays the run's already-admitted, bounded observation set and remains the
 * authority for refusing unavailable or unauthorized historical replay.
 */
export function WorkflowRunEvidencePanel({
  runId,
}: {
  runId: string;
}): JSX.Element {
  const [downloadMessage, setDownloadMessage] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [replayReport, setReplayReport] =
    useState<WorkflowRunReplayReport | null>(null);
  const [replayError, setReplayError] = useState<string | null>(null);

  const downloadMutation = useApiMutation<Blob, void>(() =>
    caliberApi.downloadWorkflowRunEvidence(runId),
  );
  const replayMutation = useApiMutation<WorkflowRunReplayReport, void>(() =>
    caliberApi.replayWorkflowRun(runId),
  );

  const downloadEvidence = async (): Promise<void> => {
    setDownloadMessage(null);
    setDownloadError(null);
    try {
      const blob = await downloadMutation.mutateAsync();
      if (triggerWorkflowRunEvidenceDownload(blob, runId)) {
        setDownloadMessage("Evidence archive downloaded.");
      } else {
        setDownloadError("Evidence download is unavailable in this browser.");
      }
    } catch (error) {
      setDownloadError(`Evidence download failed: ${errorMessage(error)}`);
    }
  };

  const replayHistoricalRun = async (): Promise<void> => {
    setReplayReport(null);
    setReplayError(null);
    try {
      setReplayReport(await replayMutation.mutateAsync());
    } catch (error) {
      setReplayError(
        `Historical replay refused or failed: ${errorMessage(error)}`,
      );
    }
  };

  return (
    <section
      data-testid="workflow-run-evidence-panel"
      aria-labelledby="workflow-run-evidence-heading"
      className="mt-3 rounded-2xl border border-slate-200/60 bg-white p-5 shadow-card"
    >
      <div className="mb-2">
        <h2
          id="workflow-run-evidence-heading"
          className="text-sm font-bold text-slate-900"
        >
          Evidence and historical replay
        </h2>
        <p className="mt-1 max-w-3xl text-xs leading-relaxed text-slate-500">
          Download the immutable evidence archive or run a read-only replay of
          this run&apos;s already-admitted observations. Replay is bounded by
          the server&apos;s recorded observation limit and never executes live
          tools.
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          data-testid="workflow-run-evidence-download"
          aria-describedby="workflow-run-evidence-help"
          disabled={downloadMutation.isPending}
          onClick={() => void downloadEvidence()}
          className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {downloadMutation.isPending
            ? "Downloading…"
            : "Download evidence archive"}
        </button>
        <button
          type="button"
          data-testid="workflow-run-historical-replay"
          aria-describedby="workflow-run-evidence-help"
          disabled={replayMutation.isPending}
          onClick={() => void replayHistoricalRun()}
          className="rounded-lg border border-violet-200 bg-violet-50 px-3 py-1.5 text-xs font-semibold text-violet-700 transition-colors hover:bg-violet-100 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {replayMutation.isPending
            ? "Replaying recorded evidence…"
            : "Replay historical evidence"}
        </button>
      </div>
      <p
        id="workflow-run-evidence-help"
        className="mt-2 text-[11px] text-slate-400"
      >
        Run {runId}; historical replay is read-only and uses no new external
        effects.
      </p>
      {(downloadMessage || downloadError) && (
        <div
          data-testid="workflow-run-evidence-status"
          role={downloadError ? "alert" : "status"}
          aria-live="polite"
          className={`mt-3 rounded-lg border px-3 py-2 text-xs ${
            downloadError
              ? "border-red-200 bg-red-50 text-red-700"
              : "border-emerald-200 bg-emerald-50 text-emerald-700"
          }`}
        >
          {downloadError ?? downloadMessage}
        </div>
      )}
      {replayReport && (
        <div
          data-testid="workflow-run-replay-result"
          role="status"
          aria-live="polite"
          className={`mt-3 rounded-lg border px-3 py-3 text-xs ${
            replayReport.matches_original
              ? "border-emerald-200 bg-emerald-50 text-emerald-800"
              : "border-amber-200 bg-amber-50 text-amber-900"
          }`}
        >
          <div className="font-semibold">
            {replayReport.matches_original
              ? "Historical replay matches the original run."
              : "Historical replay does not match the original run."}
          </div>
          <div
            data-testid="workflow-run-replay-matches-original"
            className="mt-1 font-mono"
          >
            matches_original: {String(replayReport.matches_original)}
          </div>
          <div className="mt-1">
            Compared {replayReport.observation_count} recorded observation
            {replayReport.observation_count === 1 ? "" : "s"}.
          </div>
        </div>
      )}
      {replayError && (
        <div
          data-testid="workflow-run-replay-error"
          role="alert"
          aria-live="assertive"
          className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
        >
          {replayError}
        </div>
      )}
    </section>
  );
}
