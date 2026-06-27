/**
 * Side-by-side comparison of two traces — fetches both details and shows their
 * metrics (duration / tokens / cost / tool calls), responses, and span trees in
 * two columns so a regression between runs is easy to spot. Rendered by the
 * Observability page's Compare mode once two traces are selected.
 */
import type { ObservabilityTraceDetail, WorkflowRunTraceSpan } from "@/api/workflowTypes";
import { caliberApi } from "@/api/caliberApi";
import { useApiQuery } from "@/hooks/useApiQuery";

import { TraceSpanTree, formatDuration, statusTone } from "./TraceSpanTree";

/** Count TOOL spans in a trace — surfaced as a comparison metric. */
function toolCalls(spans: WorkflowRunTraceSpan[]): number {
  return spans.filter((s) => (s.span_type ?? "").toUpperCase() === "TOOL").length;
}

function fmtNum(n: number | null): string {
  return n === null || n === undefined ? "—" : n.toLocaleString();
}

function fmtCost(n: number | null): string {
  if (n === null || n === undefined) return "—";
  return `$${n < 0.01 ? n.toFixed(4) : n.toFixed(2)}`;
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

/** Color a numeric delta (B vs A): more tokens/cost/duration = worse (amber). */
function deltaTone(a: number | null, b: number | null): string {
  if (a === null || b === null || a === b) return "text-slate-400";
  return b > a ? "text-amber-600" : "text-emerald-600";
}

function deltaLabel(a: number | null, b: number | null): string {
  if (a === null || b === null || a === b) return "";
  const diff = b - a;
  return diff > 0 ? `+${diff.toLocaleString()}` : diff.toLocaleString();
}

export function TraceCompare({ idA, idB }: { idA: string; idB: string }): JSX.Element {
  const a = useApiQuery<ObservabilityTraceDetail>(["observability", "trace", idA], (signal) =>
    caliberApi.getObservabilityTrace(idA, signal),
  );
  const b = useApiQuery<ObservabilityTraceDetail>(["observability", "trace", idB], (signal) =>
    caliberApi.getObservabilityTrace(idB, signal),
  );

  if (a.isLoading || b.isLoading) {
    return <div className="card p-10 text-center text-sm text-slate-400">Loading comparison…</div>;
  }
  if (a.error || b.error || !a.data || !b.data) {
    return (
      <div className="card p-4 text-sm text-red-700">
        {a.error?.message || b.error?.message || "Could not load both traces."}
      </div>
    );
  }
  const da = a.data;
  const db = b.data;

  const rows: Array<{ label: string; a: string; b: string; numA?: number | null; numB?: number | null }> = [
    { label: "Status", a: da.status || "—", b: db.status || "—" },
    {
      label: "Duration",
      a: formatDuration(da.execution_time_ms),
      b: formatDuration(db.execution_time_ms),
      numA: da.execution_time_ms,
      numB: db.execution_time_ms,
    },
    {
      label: "Tokens",
      a: fmtNum(da.total_tokens),
      b: fmtNum(db.total_tokens),
      numA: da.total_tokens,
      numB: db.total_tokens,
    },
    {
      label: "Cost",
      a: fmtCost(da.cost_usd),
      b: fmtCost(db.cost_usd),
      numA: da.cost_usd,
      numB: db.cost_usd,
    },
    {
      label: "Spans",
      a: String(da.spans.length),
      b: String(db.spans.length),
      numA: da.spans.length,
      numB: db.spans.length,
    },
    {
      label: "Tool calls",
      a: String(toolCalls(da.spans)),
      b: String(toolCalls(db.spans)),
      numA: toolCalls(da.spans),
      numB: toolCalls(db.spans),
    },
  ];

  const header = (d: ObservabilityTraceDetail): JSX.Element => (
    <div className="flex items-center gap-2">
      <span className="min-w-0 flex-1 truncate text-sm font-semibold text-slate-800">{d.name || d.trace_id}</span>
      <span className={`shrink-0 text-[11px] font-semibold uppercase ${statusTone(d.status)}`}>{d.status}</span>
    </div>
  );

  return (
    <section data-testid="trace-compare" className="card space-y-4 p-4">
      <div className="grid grid-cols-2 gap-3">
        <div className="rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-2">{header(da)}</div>
        <div className="rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-2">{header(db)}</div>
      </div>

      <table className="w-full text-sm">
        <tbody>
          {rows.map((row) => (
            <tr key={row.label} className="border-b border-slate-100 last:border-0">
              <td className="py-1.5 pr-3 text-xs font-semibold uppercase tracking-wide text-slate-400">
                {row.label}
              </td>
              <td className="py-1.5 pr-3 tabular-nums text-slate-800">{row.a}</td>
              <td className="py-1.5 tabular-nums text-slate-800">
                {row.b}
                {row.numA !== undefined && row.numB !== undefined && deltaLabel(row.numA, row.numB) ? (
                  <span className={`ml-2 text-[11px] font-semibold ${deltaTone(row.numA, row.numB)}`}>
                    {deltaLabel(row.numA, row.numB)}
                  </span>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {previewText(da.response) || previewText(db.response) ? (
        <div className="grid grid-cols-2 gap-3">
          {[da, db].map((d, i) => (
            <div key={i}>
              <div className="mb-1 text-xs font-semibold text-slate-500">Response</div>
              <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-slate-200/70 bg-slate-50 p-2 text-[11px] text-slate-700">
                {previewText(d.response) || "—"}
              </pre>
            </div>
          ))}
        </div>
      ) : null}

      <div className="grid grid-cols-2 gap-3">
        <div>
          <div className="mb-1 text-xs font-semibold text-slate-500">Spans</div>
          <TraceSpanTree spans={da.spans} />
        </div>
        <div>
          <div className="mb-1 text-xs font-semibold text-slate-500">Spans</div>
          <TraceSpanTree spans={db.spans} />
        </div>
      </div>
    </section>
  );
}
