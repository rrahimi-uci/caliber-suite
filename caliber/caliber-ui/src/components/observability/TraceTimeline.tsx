import type { WorkflowRunTraceSpan } from "@/api/workflowTypes";

import { flattenSpans, formatDuration, statusTone } from "./TraceSpanTree";

/**
 * Waterfall / Gantt view of a trace: one bar per span, positioned by its start
 * offset and sized by its duration relative to the whole trace — the timeline
 * MLflow shows, in CALIBER's design system.
 */
function barTone(spanType: string): string {
  switch (spanType.toUpperCase()) {
    case "AGENT":
      return "bg-violet-400 dark:bg-violet-500";
    case "TOOL":
      return "bg-sky-400 dark:bg-sky-500";
    case "GUARDRAIL":
      return "bg-amber-400 dark:bg-amber-500";
    case "ROUTER":
      return "bg-fuchsia-400 dark:bg-fuchsia-500";
    case "RETRIEVER":
      return "bg-emerald-400 dark:bg-emerald-500";
    case "PARSER":
      return "bg-teal-400 dark:bg-teal-500";
    default:
      return "bg-slate-400 dark:bg-slate-500";
  }
}

export function TraceTimeline({ spans }: { spans: WorkflowRunTraceSpan[] }): JSX.Element {
  const rows = flattenSpans(spans);
  const starts = rows
    .map((row) => row.start_time_ms)
    .filter((value): value is number => value !== null && value !== undefined);
  const ends = rows
    .map((row) => row.end_time_ms)
    .filter((value): value is number => value !== null && value !== undefined);
  const traceStart = starts.length ? Math.min(...starts) : 0;
  const traceEnd = ends.length ? Math.max(...ends) : traceStart;
  const total = Math.max(1, traceEnd - traceStart);

  return (
    <div
      data-testid="trace-timeline"
      className="overflow-hidden rounded-xl border border-slate-200/70 dark:border-slate-800"
    >
      {rows.map((node, index) => {
        const start = node.start_time_ms;
        const end = node.end_time_ms;
        const left = start !== null && start !== undefined ? ((start - traceStart) / total) * 100 : 0;
        const rawWidth =
          start !== null && start !== undefined && end !== null && end !== undefined
            ? ((end - start) / total) * 100
            : 0;
        // Clamp the bar width (as a % of the track): floor at 1.5% so a
        // near-instant span is still visible, and cap at the remaining track
        // (100 - left) so a long span never overflows past the right edge.
        const width = Math.min(100 - left, Math.max(1.5, rawWidth));
        return (
          <div
            key={node.span_id ?? `${node.name}-${index}`}
            data-testid="trace-timeline-row"
            className="flex items-center gap-2 border-b border-slate-100 px-2 py-1 text-xs last:border-0 dark:border-slate-800"
          >
            <span
              className="w-48 shrink-0 truncate font-medium text-slate-700 dark:text-slate-200"
              style={{ paddingLeft: `${node.depth * 0.75}rem` }}
              title={node.name}
            >
              {node.name}
            </span>
            <div className="relative h-3 flex-1 rounded bg-slate-100 dark:bg-slate-800">
              <div
                className={`absolute top-0 h-3 rounded ${barTone(node.span_type)}`}
                style={{ left: `${left}%`, width: `${width}%` }}
                title={`${node.span_type} · ${formatDuration(node.duration_ms)}`}
              />
            </div>
            <span className="w-16 shrink-0 text-right tabular-nums text-slate-500">
              {formatDuration(node.duration_ms)}
            </span>
            <span className={`w-12 shrink-0 text-right font-semibold uppercase ${statusTone(node.status)}`}>
              {node.status}
            </span>
          </div>
        );
      })}
    </div>
  );
}
