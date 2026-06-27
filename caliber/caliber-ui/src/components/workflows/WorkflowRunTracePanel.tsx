import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Activity, ExternalLink, RefreshCw, Workflow } from "lucide-react";

import { ApiError, caliberApi } from "@/api/caliberApi";
import type { WorkflowRun, WorkflowRunTraceSpan } from "@/api/workflowTypes";
import { TraceSpanTree } from "@/components/observability/TraceSpanTree";

interface WorkflowRunTracePanelProps {
  runId: string;
  runStatus?: WorkflowRun["status"] | null;
}

interface PanelErrorState {
  detail: string;
}

export function WorkflowRunTracePanel({
  runId,
  runStatus = null,
}: WorkflowRunTracePanelProps): JSX.Element {
  const [spans, setSpans] = useState<WorkflowRunTraceSpan[]>([]);
  const [traceId, setTraceId] = useState<string | null>(null);
  const [mlflowUrl, setMlflowUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [loadError, setLoadError] = useState<PanelErrorState | null>(null);

  const reload = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setLoadError(null);
      try {
        const result = await caliberApi.getWorkflowRunTrace(runId, signal);
        setSpans(result.spans ?? []);
        setTraceId(result.trace_id ?? null);
        setMlflowUrl(result.mlflow_url ?? null);
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setLoadError({ detail: err instanceof ApiError ? err.message : String(err) });
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

  const count = spans.length;

  return (
    <div data-testid="run-trace">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-bold text-slate-900 dark:text-slate-100">
          <Workflow className="h-4 w-4 text-slate-400" />
          <span data-testid="run-trace-tab">Execution Trace</span>
          {count > 0 ? (
            <span className="rounded-full border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[10px] font-semibold text-slate-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400">
              {count} span{count === 1 ? "" : "s"}
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          {traceId ? (
            <Link
              data-testid="run-trace-observability-link"
              to={`/observability?trace=${encodeURIComponent(traceId)}`}
              className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2 py-1 text-[11px] font-semibold text-caliber-700 transition-colors hover:bg-caliber-50 dark:border-slate-700 dark:bg-slate-900 dark:text-caliber-300"
            >
              <Activity className="h-3 w-3" />
              Open in Observability
            </Link>
          ) : null}
          {mlflowUrl ? (
            <a
              data-testid="run-trace-mlflow-link"
              href={mlflowUrl}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2 py-1 text-[11px] font-semibold text-sky-700 transition-colors hover:bg-sky-50 dark:border-slate-700 dark:bg-slate-900 dark:text-sky-300"
            >
              <ExternalLink className="h-3 w-3" />
              Open in MLflow
            </a>
          ) : null}
          <button
            type="button"
            onClick={() => void reload()}
            disabled={loading}
            className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2 py-1 text-[11px] font-semibold text-slate-600 transition-colors hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300"
          >
            <RefreshCw className={`h-3 w-3 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>
      </div>

      {loadError ? (
        <div
          data-testid="run-trace-error"
          className="rounded-xl border border-red-200/70 bg-red-50 px-4 py-3 text-xs leading-relaxed text-red-700 dark:border-red-800/70 dark:bg-red-950/40 dark:text-red-300"
        >
          Could not load the run trace: {loadError.detail}
        </div>
      ) : loading ? (
        <div className="rounded-xl border border-slate-100 bg-slate-50 px-3 py-2 text-xs text-slate-400 dark:border-slate-800 dark:bg-slate-900">
          Loading trace…
        </div>
      ) : count === 0 ? (
        <div
          data-testid="run-trace-empty"
          className="rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-4 py-6 text-xs leading-relaxed text-slate-500 dark:border-slate-700 dark:bg-slate-900/40 dark:text-slate-400"
        >
          No trace spans for this run. Tracing may be off, the run may use the fake
          provider, or MLflow may be unavailable in this environment.
          {runStatus && (runStatus === "running" || runStatus === "queued")
            ? " The trace appears once the run finishes."
            : ""}
        </div>
      ) : (
        <TraceSpanTree spans={spans} />
      )}
    </div>
  );
}
