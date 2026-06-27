import { useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Activity,
  BarChart3,
  Clock,
  Check,
  Coins,
  ListPlus,
  ExternalLink,
  FlaskConical,
  GitCompare,
  Layers,
  ListTree,
  RefreshCw,
  Search,
  Send,
  ThumbsDown,
  ThumbsUp,
  User,
  Wrench,
  X,
} from "lucide-react";

import { caliberApi } from "@/api/caliberApi";
import type { EvalDataset } from "@/api/types";
import type {
  ObservabilityExperiment,
  ObservabilityMetrics,
  ObservabilityTrace,
  ObservabilityTraceDetail,
} from "@/api/workflowTypes";
import { PageHeader } from "@/components/PageHeader";
import { TraceCompare } from "@/components/observability/TraceCompare";
import { TraceMetricsCharts } from "@/components/observability/TraceMetricsCharts";
import { TraceSpanTree, formatDuration, statusTone } from "@/components/observability/TraceSpanTree";
import { TraceTimeline } from "@/components/observability/TraceTimeline";
import { useApiMutation, useApiQuery, useInvalidate } from "@/hooks/useApiQuery";
import { showToast } from "@/lib/toast";

const STATUS_OPTIONS = [
  { value: "", label: "All statuses" },
  { value: "OK", label: "OK" },
  { value: "ERROR", label: "Error" },
  { value: "IN_PROGRESS", label: "In progress" },
];

const TIME_RANGES: Array<{ value: string; label: string; ms: number | null }> = [
  { value: "all", label: "All time", ms: null },
  { value: "1h", label: "Last hour", ms: 60 * 60 * 1000 },
  { value: "24h", label: "Last 24h", ms: 24 * 60 * 60 * 1000 },
  { value: "7d", label: "Last 7 days", ms: 7 * 24 * 60 * 60 * 1000 },
];

function fmtTime(ms: number | null): string {
  if (!ms) return "—";
  const date = new Date(ms);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function fmtTokens(n: number | null): string {
  return n === null || n === undefined ? "—" : n.toLocaleString();
}

function fmtCost(n: number | null): string {
  if (n === null || n === undefined) return "—";
  return `$${n < 0.01 ? n.toFixed(4) : n.toFixed(2)}`;
}

function shortId(id: string | null): string {
  if (!id) return "";
  return id.length > 10 ? `${id.slice(0, 8)}…` : id;
}

function previewText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

const selectClass =
  "rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 outline-none focus:border-caliber-400 focus:ring-2 focus:ring-caliber-400/20";

export function Observability(): JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const [status, setStatus] = useState("");
  const [experimentId, setExperimentId] = useState("");
  const [timeRange, setTimeRange] = useState("all");
  const [search, setSearch] = useState("");
  const [view, setView] = useState<"tree" | "timeline">("tree");
  const [compareMode, setCompareMode] = useState(false);
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const [tab, setTab] = useState<"traces" | "monitor">("traces");
  const invalidate = useInvalidate();

  const sinceForRange = (): number | undefined => {
    const range = TIME_RANGES.find((r) => r.value === timeRange);
    return range?.ms ? Date.now() - range.ms : undefined;
  };

  const metricsQuery = useApiQuery<ObservabilityMetrics>(
    ["observability", "metrics", experimentId, timeRange],
    (signal) =>
      caliberApi.getObservabilityMetrics(
        { experimentId: experimentId || undefined, sinceMs: sinceForRange() },
        signal,
      ),
    { enabled: tab === "monitor" },
  );

  const sessionFilter = searchParams.get("session") ?? "";

  const experimentsQuery = useApiQuery<ObservabilityExperiment[]>(
    ["observability", "experiments"],
    (signal) => caliberApi.listObservabilityExperiments(signal),
  );
  const experiments = experimentsQuery.data ?? [];

  const tracesQuery = useApiQuery<ObservabilityTrace[]>(
    ["observability", "traces", experimentId, status, timeRange, sessionFilter],
    (signal) => {
      const range = TIME_RANGES.find((r) => r.value === timeRange);
      const sinceMs = range?.ms ? Date.now() - range.ms : undefined;
      return caliberApi.listObservabilityTraces(
        {
          limit: 100,
          status: status || undefined,
          experimentId: experimentId || undefined,
          session: sessionFilter || undefined,
          sinceMs,
        },
        signal,
      );
    },
  );
  const allTraces = tracesQuery.data ?? [];
  const q = search.trim().toLowerCase();
  const traces = q
    ? allTraces.filter(
        (t) =>
          t.name.toLowerCase().includes(q) ||
          (t.trace_id ?? "").toLowerCase().includes(q) ||
          t.request_preview.toLowerCase().includes(q),
      )
    : allTraces;

  const urlTrace = searchParams.get("trace");
  const activeId = urlTrace ?? traces[0]?.trace_id ?? null;
  const activeSummary = allTraces.find((t) => t.trace_id === activeId) ?? null;

  const detailQuery = useApiQuery<ObservabilityTraceDetail>(
    ["observability", "trace", activeId ?? ""],
    (signal) => caliberApi.getObservabilityTrace(activeId as string, signal),
    { enabled: Boolean(activeId) },
  );
  const detail = detailQuery.data ?? null;

  const patchParams = (patch: Record<string, string | null>): void => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [key, value] of Object.entries(patch)) {
          if (value) next.set(key, value);
          else next.delete(key);
        }
        return next;
      },
      { replace: true },
    );
  };

  const selectTrace = (traceId: string | null): void => patchParams({ trace: traceId });
  const setSession = (sessionId: string | null): void =>
    patchParams({ session: sessionId, trace: null });

  const toggleCompareMode = (): void => {
    setCompareMode((on) => !on);
    setCompareIds([]);
  };
  const onRowClick = (traceId: string | null): void => {
    if (!compareMode) {
      selectTrace(traceId);
      return;
    }
    if (!traceId) return;
    setCompareIds((prev) =>
      prev.includes(traceId)
        ? prev.filter((id) => id !== traceId)
        : prev.length < 2
          ? [...prev, traceId]
          : prev,
    );
  };
  const comparing = compareMode && compareIds.length === 2;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Observability"
        subtitle="Traces, spans, tool calls, token usage, and cost for every agent and workflow run — no need to leave CALIBER."
      />

      {/* Traces ⇄ Monitor */}
      <div className="inline-flex rounded-xl border border-slate-200 bg-slate-50 p-0.5 text-sm font-semibold">
        <button
          type="button"
          onClick={() => setTab("traces")}
          className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 ${tab === "traces" ? "bg-white text-caliber-700 shadow-sm" : "text-slate-500"}`}
        >
          <ListTree className="h-4 w-4" />
          Traces
        </button>
        <button
          type="button"
          onClick={() => setTab("monitor")}
          className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 ${tab === "monitor" ? "bg-white text-caliber-700 shadow-sm" : "text-slate-500"}`}
        >
          <BarChart3 className="h-4 w-4" />
          Monitor
        </button>
      </div>

      {/* Filter toolbar */}
      <div className="card flex flex-wrap items-center gap-2 p-3">
        {tab === "traces" ? (
          <div className="relative min-w-[200px] flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search traces…"
              aria-label="Search traces"
              className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2 pl-9 text-sm text-slate-900 outline-none focus:border-caliber-400 focus:ring-2 focus:ring-caliber-400/20"
            />
          </div>
        ) : (
          <div className="flex-1" />
        )}
        <label className="inline-flex items-center gap-1.5">
          <FlaskConical className="h-4 w-4 text-slate-400" />
          <select
            aria-label="Filter by experiment"
            data-testid="observability-experiment-select"
            value={experimentId}
            onChange={(event) => {
              setExperimentId(event.target.value);
              selectTrace(null);
            }}
            className={selectClass}
          >
            <option value="">All experiments</option>
            {experiments.map((exp) => (
              <option key={exp.experiment_id} value={exp.experiment_id}>
                {exp.name || exp.experiment_id}
              </option>
            ))}
          </select>
        </label>
        <label className="inline-flex items-center gap-1.5">
          <Clock className="h-4 w-4 text-slate-400" />
          <select
            aria-label="Time range"
            value={timeRange}
            onChange={(event) => setTimeRange(event.target.value)}
            className={selectClass}
          >
            {TIME_RANGES.map((range) => (
              <option key={range.value} value={range.value}>
                {range.label}
              </option>
            ))}
          </select>
        </label>
        {tab === "traces" ? (
          <>
            <select
              aria-label="Filter by status"
              value={status}
              onChange={(event) => setStatus(event.target.value)}
              className={selectClass}
            >
              {STATUS_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={toggleCompareMode}
              className={`inline-flex items-center gap-1.5 rounded-xl border px-3 py-2 text-sm font-medium transition ${
                compareMode
                  ? "border-caliber-300 bg-caliber-50 text-caliber-700"
                  : "border-slate-200 bg-white text-slate-600 hover:border-slate-300"
              }`}
            >
              <GitCompare className="h-4 w-4" />
              Compare
            </button>
          </>
        ) : null}
        <button
          type="button"
          onClick={() => void invalidate(["observability"])}
          disabled={tracesQuery.isFetching || metricsQuery.isFetching}
          className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-600 transition hover:border-slate-300 disabled:opacity-50"
        >
          <RefreshCw
            className={`h-4 w-4 ${tracesQuery.isFetching || metricsQuery.isFetching ? "animate-spin" : ""}`}
          />
          Refresh
        </button>
      </div>

      {tab === "monitor" ? (
        metricsQuery.isLoading ? (
          <div className="card px-4 py-12 text-center text-sm text-slate-400">Loading metrics…</div>
        ) : metricsQuery.error ? (
          <div className="card px-4 py-3 text-sm text-red-700">{metricsQuery.error.message}</div>
        ) : (
          <TraceMetricsCharts
            metrics={
              metricsQuery.data ?? {
                buckets: [],
                bucket_ms: 0,
                totals: {
                  count: 0,
                  error_rate: 0,
                  p50_ms: null,
                  p95_ms: null,
                  tokens: 0,
                  cost_usd: 0,
                },
              }
            }
          />
        )
      ) : (
        <>
      {/* Compare-mode hint */}
      {compareMode && !comparing ? (
        <div className="text-sm text-slate-500" data-testid="observability-compare-hint">
          Select 2 traces to compare ({compareIds.length}/2).
        </div>
      ) : null}

      {/* Active session filter */}
      {sessionFilter ? (
        <div className="flex items-center gap-2 text-sm" data-testid="observability-session-filter">
          <span className="text-slate-500">Filtered to session</span>
          <span className="inline-flex items-center gap-1.5 rounded-full border border-caliber-200 bg-caliber-50 px-3 py-1 font-mono text-xs font-semibold text-caliber-700">
            <Layers className="h-3 w-3" />
            {shortId(sessionFilter)}
            <button
              type="button"
              aria-label="Clear session filter"
              onClick={() => setSession(null)}
              className="ml-0.5 text-caliber-500 hover:text-caliber-800"
            >
              <X className="h-3 w-3" />
            </button>
          </span>
        </div>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
        {/* Trace list */}
        <section className="card overflow-hidden">
          <div className="flex items-center justify-between border-b border-slate-200/70 px-4 py-3">
            <span className="text-sm font-semibold text-slate-700">Traces</span>
            <span className="rounded-full border border-slate-200/70 bg-slate-50 px-2.5 py-0.5 text-[11px] font-semibold text-slate-500">
              {traces.length}
            </span>
          </div>
          <div className="max-h-[74vh] overflow-y-auto">
            {tracesQuery.isLoading ? (
              <div className="px-4 py-12 text-center text-sm text-slate-400">Loading traces…</div>
            ) : tracesQuery.error ? (
              <div className="m-3 rounded-xl border border-red-200/70 bg-red-50 px-4 py-3 text-sm text-red-700">
                {tracesQuery.error.message}
              </div>
            ) : traces.length === 0 ? (
              <div
                data-testid="observability-empty"
                className="px-4 py-12 text-center text-sm text-slate-400"
              >
                No traces{allTraces.length ? " match your search" : " yet"}. Run a workflow or agent
                with tracing enabled, then refresh.
              </div>
            ) : (
              <ul className="divide-y divide-slate-100">
                {traces.map((trace) => {
                  const id = trace.trace_id ?? "";
                  const inCompare = compareIds.includes(id);
                  const isActive = compareMode ? inCompare : id === activeId;
                  return (
                    <li key={id || trace.name}>
                      <button
                        type="button"
                        data-testid="observability-trace-row"
                        onClick={() => onRowClick(id || null)}
                        className={`flex w-full flex-col gap-1.5 px-4 py-3 text-left transition ${
                          isActive
                            ? "bg-caliber-50/70 shadow-[inset_3px_0_0] shadow-caliber-400"
                            : "hover:bg-slate-50"
                        }`}
                      >
                        <div className="flex items-center justify-between gap-2">
                          {compareMode ? (
                            <span
                              className={`grid h-4 w-4 shrink-0 place-items-center rounded border ${inCompare ? "border-caliber-500 bg-caliber-500 text-white" : "border-slate-300"}`}
                            >
                              {inCompare ? <Check className="h-3 w-3" /> : null}
                            </span>
                          ) : null}
                          <span className="min-w-0 flex-1 truncate text-sm font-semibold text-slate-800">
                            {trace.name}
                          </span>
                          <span
                            className={`shrink-0 text-[11px] font-semibold uppercase ${statusTone(trace.status)}`}
                          >
                            {trace.status || "—"}
                          </span>
                        </div>
                        {trace.request_preview ? (
                          <span className="truncate text-xs text-slate-500">
                            {trace.request_preview}
                          </span>
                        ) : null}
                        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-400">
                          <span>{fmtTime(trace.timestamp_ms)}</span>
                          <span className="tabular-nums">{formatDuration(trace.execution_time_ms)}</span>
                          <span>
                            {trace.span_count} span{trace.span_count === 1 ? "" : "s"}
                          </span>
                          {trace.tool_call_count > 0 ? (
                            <span className="inline-flex items-center gap-1">
                              <Wrench className="h-3 w-3" />
                              {trace.tool_call_count}
                            </span>
                          ) : null}
                          {trace.total_tokens !== null ? (
                            <span className="tabular-nums">{fmtTokens(trace.total_tokens)} tok</span>
                          ) : null}
                          {trace.cost_usd !== null ? (
                            <span className="tabular-nums">{fmtCost(trace.cost_usd)}</span>
                          ) : null}
                        </div>
                        {(trace.experiment_name || trace.experiment_id || trace.session_id) ? (
                          <div className="flex flex-wrap items-center gap-1.5">
                            {trace.experiment_name || trace.experiment_id ? (
                              <span className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-1.5 py-0.5 text-[10px] text-slate-500">
                                <FlaskConical className="h-2.5 w-2.5" />
                                {trace.experiment_name || trace.experiment_id}
                              </span>
                            ) : null}
                            {trace.session_id ? (
                              <span
                                role="button"
                                tabIndex={0}
                                data-testid="observability-session-chip"
                                onClick={(event) => {
                                  event.stopPropagation();
                                  setSession(trace.session_id);
                                }}
                                onKeyDown={(event) => {
                                  if (event.key === "Enter") {
                                    event.stopPropagation();
                                    setSession(trace.session_id);
                                  }
                                }}
                                className="inline-flex items-center gap-1 rounded-full border border-caliber-200 bg-caliber-50 px-1.5 py-0.5 font-mono text-[10px] text-caliber-700 hover:bg-caliber-100"
                                title={`Filter to session ${trace.session_id}`}
                              >
                                <Layers className="h-2.5 w-2.5" />
                                {shortId(trace.session_id)}
                              </span>
                            ) : null}
                          </div>
                        ) : null}
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </section>

        {/* Trace detail */}
        <section className="card overflow-hidden">
          <div className="flex items-center justify-between gap-2 border-b border-slate-200/70 px-4 py-3">
            <div className="flex min-w-0 items-center gap-2 text-sm font-semibold text-slate-700">
              <Activity className="h-4 w-4 text-slate-400" />
              <span className="truncate">
                {comparing ? "Comparing 2 traces" : detail?.name || activeSummary?.name || "Trace"}
              </span>
              {(detail?.status || activeSummary?.status) ? (
                <span
                  className={`shrink-0 text-[11px] font-semibold uppercase ${statusTone(detail?.status || activeSummary?.status || "")}`}
                >
                  {detail?.status || activeSummary?.status}
                </span>
              ) : null}
            </div>
            {detail?.mlflow_url ? (
              <a
                href={detail.mlflow_url}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-slate-200 bg-white px-2 py-1 text-[11px] font-semibold text-sky-700 transition hover:bg-sky-50"
              >
                <ExternalLink className="h-3 w-3" />
                Open in MLflow
              </a>
            ) : null}
          </div>

          <div className="space-y-4 p-4">
            {comparing ? (
              <TraceCompare idA={compareIds[0] as string} idB={compareIds[1] as string} />
            ) : !activeId ? (
              <div className="px-2 py-12 text-center text-sm text-slate-400">
                Select a trace to inspect its spans, tool calls, tokens, and cost.
              </div>
            ) : detailQuery.isLoading ? (
              <div className="px-2 py-12 text-center text-sm text-slate-400">Loading trace…</div>
            ) : detailQuery.error ? (
              <div className="rounded-xl border border-red-200/70 bg-red-50 px-4 py-3 text-sm text-red-700">
                {detailQuery.error.message}
              </div>
            ) : detail ? (
              <>
                {/* Identity chips */}
                <div className="flex flex-wrap items-center gap-1.5" data-testid="trace-identity">
                  {activeSummary?.experiment_name || detail.experiment_id ? (
                    <Chip icon={<FlaskConical className="h-3 w-3" />}>
                      {activeSummary?.experiment_name || `exp ${detail.experiment_id}`}
                    </Chip>
                  ) : null}
                  {detail.session_id ? (
                    <button
                      type="button"
                      onClick={() => setSession(detail.session_id)}
                      className="inline-flex items-center gap-1 rounded-full border border-caliber-200 bg-caliber-50 px-2 py-0.5 font-mono text-[11px] text-caliber-700 hover:bg-caliber-100"
                      title={`Filter to session ${detail.session_id}`}
                    >
                      <Layers className="h-3 w-3" />
                      {shortId(detail.session_id)}
                    </button>
                  ) : null}
                  {detail.user ? (
                    <Chip icon={<User className="h-3 w-3" />}>{detail.user}</Chip>
                  ) : null}
                </div>

                {/* Metrics */}
                <div data-testid="trace-usage" className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <Metric label="Started" value={fmtTime(detail.request_time_ms)} />
                  <Metric label="Duration" value={formatDuration(detail.execution_time_ms)} />
                  <Metric
                    label="Tokens"
                    value={fmtTokens(detail.total_tokens)}
                    hint={
                      detail.prompt_tokens !== null || detail.completion_tokens !== null
                        ? `${fmtTokens(detail.prompt_tokens)} in · ${fmtTokens(detail.completion_tokens)} out`
                        : undefined
                    }
                  />
                  <Metric label="Cost" value={fmtCost(detail.cost_usd)} icon />
                </div>

                {/* Tags */}
                {Object.keys(detail.tags).length > 0 ? (
                  <div className="flex flex-wrap gap-1.5" data-testid="trace-tags">
                    {Object.entries(detail.tags)
                      .filter(([key]) => !key.startsWith("mlflow."))
                      .map(([key, value]) => (
                        <span
                          key={key}
                          className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] text-slate-600"
                        >
                          <span className="font-semibold">{key}</span>: {value}
                        </span>
                      ))}
                  </div>
                ) : null}

                {/* Assessments */}
                {detail.assessments.length > 0 ? (
                  <div data-testid="trace-assessments" className="space-y-1.5">
                    <div className="text-xs font-semibold text-slate-500">Assessments</div>
                    {detail.assessments.map((a, index) => (
                      <div
                        key={`${a.name}-${index}`}
                        className="rounded-lg border border-slate-200/70 bg-slate-50/70 px-3 py-1.5 text-xs"
                      >
                        <span className="font-semibold text-slate-700">{a.name}</span>
                        <span className="text-slate-500">: {String(a.value)}</span>
                        {a.rationale ? (
                          <span className="block text-[11px] text-slate-400">{a.rationale}</span>
                        ) : null}
                      </div>
                    ))}
                  </div>
                ) : null}

                {/* Human feedback */}
                <TraceFeedback traceId={activeId} />

                {/* Capture this trace as an eval example */}
                <AddTraceToDataset traceId={activeId} />

                {/* Request / response */}
                {previewText(detail.request) ? (
                  <details className="rounded-xl border border-slate-200/70">
                    <summary className="cursor-pointer px-3 py-2 text-xs font-semibold text-slate-600">
                      Request
                    </summary>
                    <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words border-t border-slate-100 px-3 py-2 text-[11px] text-slate-700">
                      {previewText(detail.request)}
                    </pre>
                  </details>
                ) : null}
                {previewText(detail.response) ? (
                  <details className="rounded-xl border border-slate-200/70">
                    <summary className="cursor-pointer px-3 py-2 text-xs font-semibold text-slate-600">
                      Response
                    </summary>
                    <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words border-t border-slate-100 px-3 py-2 text-[11px] text-slate-700">
                      {previewText(detail.response)}
                    </pre>
                  </details>
                ) : null}

                {/* Spans: tree or timeline */}
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs font-semibold text-slate-500">
                    Spans ({detail.spans.length})
                  </span>
                  <div className="inline-flex rounded-lg border border-slate-200 bg-slate-50 p-0.5 text-[11px] font-semibold">
                    <button
                      type="button"
                      onClick={() => setView("tree")}
                      className={`inline-flex items-center gap-1 rounded-md px-2 py-1 ${view === "tree" ? "bg-white text-caliber-700 shadow-sm" : "text-slate-500"}`}
                    >
                      <ListTree className="h-3 w-3" />
                      Tree
                    </button>
                    <button
                      type="button"
                      onClick={() => setView("timeline")}
                      className={`inline-flex items-center gap-1 rounded-md px-2 py-1 ${view === "timeline" ? "bg-white text-caliber-700 shadow-sm" : "text-slate-500"}`}
                    >
                      <Clock className="h-3 w-3" />
                      Timeline
                    </button>
                  </div>
                </div>
                {detail.spans.length === 0 ? (
                  <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-4 py-6 text-center text-sm text-slate-400">
                    No spans recorded for this trace.
                  </div>
                ) : view === "tree" ? (
                  <TraceSpanTree spans={detail.spans} />
                ) : (
                  <TraceTimeline spans={detail.spans} />
                )}
              </>
            ) : null}
          </div>
        </section>
      </div>
        </>
      )}
    </div>
  );
}

function TraceFeedback({ traceId }: { traceId: string }): JSX.Element {
  const invalidate = useInvalidate();
  const [value, setValue] = useState<boolean | null>(null);
  const [note, setNote] = useState("");
  const mutation = useApiMutation(
    (body: { value: boolean; rationale?: string }) =>
      caliberApi.submitObservabilityFeedback(traceId, { name: "feedback", ...body }),
    {
      onSuccess() {
        void invalidate(["observability", "trace", traceId]);
        showToast.success("Feedback recorded");
        setValue(null);
        setNote("");
      },
      onError(error) {
        showToast.error(error.message || "Failed to record feedback");
      },
    },
  );

  const toneBtn = (active: boolean, good: boolean): string =>
    active
      ? good
        ? "border-emerald-300 bg-emerald-50 text-emerald-700"
        : "border-red-300 bg-red-50 text-red-700"
      : "border-slate-200 bg-white text-slate-500 hover:border-slate-300";

  return (
    <div data-testid="trace-feedback" className="space-y-2 rounded-xl border border-slate-200/70 p-3">
      <div className="text-xs font-semibold text-slate-500">Your feedback</div>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          aria-label="Helpful"
          onClick={() => setValue(true)}
          className={`inline-flex items-center gap-1 rounded-lg border px-2.5 py-1.5 text-xs font-semibold transition ${toneBtn(value === true, true)}`}
        >
          <ThumbsUp className="h-3.5 w-3.5" />
          Helpful
        </button>
        <button
          type="button"
          aria-label="Not helpful"
          onClick={() => setValue(false)}
          className={`inline-flex items-center gap-1 rounded-lg border px-2.5 py-1.5 text-xs font-semibold transition ${toneBtn(value === false, false)}`}
        >
          <ThumbsDown className="h-3.5 w-3.5" />
          Not helpful
        </button>
        <input
          value={note}
          onChange={(event) => setNote(event.target.value)}
          aria-label="Feedback note"
          placeholder="Add a note (optional)…"
          className="min-w-[160px] flex-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-900 outline-none focus:border-caliber-400 focus:ring-2 focus:ring-caliber-400/20"
        />
        <button
          type="button"
          disabled={value === null || mutation.isPending}
          onClick={() => mutation.mutate({ value: value as boolean, rationale: note || undefined })}
          className="inline-flex items-center gap-1 rounded-lg bg-caliber-600 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-caliber-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Send className="h-3.5 w-3.5" />
          {mutation.isPending ? "Sending…" : "Submit"}
        </button>
      </div>
    </div>
  );
}

function AddTraceToDataset({ traceId }: { traceId: string }): JSX.Element {
  const [datasetId, setDatasetId] = useState("");
  const datasetsQuery = useApiQuery<EvalDataset[]>(
    ["eval-datasets", "active"],
    (signal) => caliberApi.listEvalDatasets({ status: "active" }, signal),
  );
  const datasets = datasetsQuery.data ?? [];
  const mutation = useApiMutation(
    (_variables: void) => caliberApi.addEvalExampleFromTrace(datasetId, { trace_id: traceId }),
    {
      onSuccess() {
        showToast.success("Captured trace as a test-set example");
        setDatasetId("");
      },
      onError(error) {
        showToast.error(error.message || "Failed to add example");
      },
    },
  );

  return (
    <div
      data-testid="trace-add-to-dataset"
      className="space-y-2 rounded-xl border border-slate-200/70 p-3"
    >
      <div className="text-xs font-semibold text-slate-500">Add to test set</div>
      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label="Choose test set"
          value={datasetId}
          onChange={(event) => setDatasetId(event.target.value)}
          className="min-w-[160px] flex-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-900 outline-none focus:border-caliber-400 focus:ring-2 focus:ring-caliber-400/20"
        >
          <option value="">
            {datasets.length ? "Choose a test set…" : "No test sets available"}
          </option>
          {datasets.map((dataset) => (
            <option key={dataset.dataset_id} value={dataset.dataset_id}>
              {dataset.name} (v{dataset.version})
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={!datasetId || mutation.isPending}
          onClick={() => mutation.mutate()}
          className="inline-flex items-center gap-1 rounded-lg bg-caliber-600 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-caliber-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <ListPlus className="h-3.5 w-3.5" />
          {mutation.isPending ? "Adding…" : "Add example"}
        </button>
      </div>
      <p className="text-[11px] text-slate-400">
        Captures this trace's request as the input and its response as the expected answer.
      </p>
    </div>
  );
}

function Chip({ icon, children }: { icon: JSX.Element; children: ReactNode }): JSX.Element {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] text-slate-600">
      {icon}
      {children}
    </span>
  );
}

function Metric({
  label,
  value,
  hint,
  icon,
}: {
  label: string;
  value: string;
  hint?: string;
  icon?: boolean;
}): JSX.Element {
  return (
    <div className="rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-2">
      <div className="flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
        {icon ? <Coins className="h-3 w-3" /> : null}
        {label}
      </div>
      <div className="mt-0.5 text-sm font-semibold tabular-nums text-slate-800">{value}</div>
      {hint ? <div className="text-[10px] text-slate-400">{hint}</div> : null}
    </div>
  );
}
