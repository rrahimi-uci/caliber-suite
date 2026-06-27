import { useCallback, useMemo, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import type { WorkflowRunTraceSpan } from "@/api/workflowTypes";

/**
 * Shared, self-contained renderer for an MLflow span tree — used by both the
 * workflow run-detail trace panel and the Observability page so the trace view
 * looks identical everywhere. Takes a flat span list, builds the parent/child
 * tree, and renders an indented, expandable list (spans, tool calls, timings,
 * redacted IO + attributes).
 */
export interface TraceNode extends WorkflowRunTraceSpan {
  depth: number;
  children: TraceNode[];
}

/** Build + flatten the span tree to depth-ordered rows (shared with the timeline). */
export function flattenSpans(spans: WorkflowRunTraceSpan[]): TraceNode[] {
  return flatten(buildTree(spans));
}

/** Color tone for a span_type badge. */
export function spanTypeTone(spanType: string): string {
  switch (spanType.toUpperCase()) {
    case "AGENT":
      return "border-violet-200 bg-violet-50 text-violet-700 dark:border-violet-800/70 dark:bg-violet-950/40 dark:text-violet-300";
    case "TOOL":
      return "border-sky-200 bg-sky-50 text-sky-700 dark:border-sky-800/70 dark:bg-sky-950/40 dark:text-sky-300";
    case "GUARDRAIL":
      return "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800/70 dark:bg-amber-950/40 dark:text-amber-300";
    case "ROUTER":
      return "border-fuchsia-200 bg-fuchsia-50 text-fuchsia-700 dark:border-fuchsia-800/70 dark:bg-fuchsia-950/40 dark:text-fuchsia-300";
    case "RETRIEVER":
      return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300";
    case "PARSER":
      return "border-teal-200 bg-teal-50 text-teal-700 dark:border-teal-800/70 dark:bg-teal-950/40 dark:text-teal-300";
    default:
      return "border-slate-200 bg-slate-50 text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300";
  }
}

/** Status text color (errors red, ok green, anything else muted). */
export function statusTone(status: string): string {
  const upper = status.toUpperCase();
  if (upper.includes("ERROR") || upper.includes("FAIL")) {
    return "text-red-600 dark:text-red-400";
  }
  if (upper === "OK" || upper === "COMPLETED") {
    return "text-emerald-600 dark:text-emerald-400";
  }
  return "text-slate-400 dark:text-slate-500";
}

export function formatDuration(ms: number | null): string {
  if (ms === null || Number.isNaN(ms)) return "—";
  if (ms < 1) return `${(ms * 1000).toFixed(0)} µs`;
  if (ms < 1000) return `${ms.toFixed(ms < 10 ? 1 : 0)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

function previewValue(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/**
 * Build the indented span tree from a flat span list using ``parent_id``.
 * Orphan spans (parent missing from the list) and roots become top-level rows,
 * so the viewer never silently drops a span.
 */
function buildTree(spans: WorkflowRunTraceSpan[]): TraceNode[] {
  const byId = new Map<string, TraceNode>();
  for (const span of spans) {
    if (span.span_id) {
      byId.set(span.span_id, { ...span, depth: 0, children: [] });
    }
  }
  const roots: TraceNode[] = [];
  for (const span of spans) {
    const node = span.span_id ? byId.get(span.span_id) : undefined;
    const created: TraceNode = node ?? { ...span, depth: 0, children: [] };
    const parent = span.parent_id ? byId.get(span.parent_id) : undefined;
    if (parent && parent !== created) {
      parent.children.push(created);
    } else {
      roots.push(created);
    }
  }
  const assignDepth = (nodes: TraceNode[], depth: number): void => {
    for (const node of nodes) {
      node.depth = depth;
      assignDepth(node.children, depth + 1);
    }
  };
  assignDepth(roots, 0);
  return roots;
}

function flatten(nodes: TraceNode[]): TraceNode[] {
  const out: TraceNode[] = [];
  const walk = (items: TraceNode[]): void => {
    for (const item of items) {
      out.push(item);
      walk(item.children);
    }
  };
  walk(nodes);
  return out;
}

interface TraceSpanRowProps {
  node: TraceNode;
  expanded: boolean;
  onToggle: (spanId: string) => void;
}

function TraceSpanRow({ node, expanded, onToggle }: TraceSpanRowProps): JSX.Element {
  const rowKey = node.span_id ?? node.name;
  const hasDetails =
    node.inputs !== undefined ||
    node.outputs !== undefined ||
    Object.keys(node.attributes ?? {}).length > 0;
  const inputs = previewValue(node.inputs);
  const outputs = previewValue(node.outputs);
  const attributes = Object.keys(node.attributes ?? {}).length > 0 ? node.attributes : null;

  return (
    <div data-testid="trace-span-row" className="border-b border-slate-100 last:border-0 dark:border-slate-800">
      <button
        type="button"
        disabled={!hasDetails}
        onClick={() => node.span_id && onToggle(node.span_id)}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-xs hover:bg-slate-50 disabled:cursor-default disabled:hover:bg-transparent dark:hover:bg-slate-900/50"
        style={{ paddingLeft: `${0.5 + node.depth * 1.25}rem` }}
      >
        <span className="flex w-3 shrink-0 justify-center text-slate-400">
          {hasDetails ? (
            expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />
          ) : null}
        </span>
        <span
          className={`shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${spanTypeTone(node.span_type)}`}
        >
          {node.span_type}
        </span>
        <span className="min-w-0 flex-1 truncate font-medium text-slate-800 dark:text-slate-200">
          {node.name}
        </span>
        <span className="shrink-0 tabular-nums text-slate-500">{formatDuration(node.duration_ms)}</span>
        <span className={`shrink-0 font-semibold uppercase ${statusTone(node.status)}`}>
          {node.status}
        </span>
      </button>
      {expanded && hasDetails ? (
        <div
          data-testid="trace-span-detail"
          className="space-y-2 px-2 pb-2 text-[11px]"
          style={{ paddingLeft: `${1.75 + node.depth * 1.25}rem` }}
        >
          {inputs ? (
            <div>
              <div className="font-semibold text-slate-500">Inputs</div>
              <pre className="mt-0.5 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-slate-50 p-2 text-slate-700 dark:bg-slate-900 dark:text-slate-300">
                {inputs}
              </pre>
            </div>
          ) : null}
          {outputs ? (
            <div>
              <div className="font-semibold text-slate-500">Outputs</div>
              <pre className="mt-0.5 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-slate-50 p-2 text-slate-700 dark:bg-slate-900 dark:text-slate-300">
                {outputs}
              </pre>
            </div>
          ) : null}
          {attributes ? (
            <div>
              <div className="font-semibold text-slate-500">Attributes</div>
              <pre className="mt-0.5 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-slate-50 p-2 text-slate-700 dark:bg-slate-900 dark:text-slate-300">
                {JSON.stringify(attributes, null, 2)}
              </pre>
            </div>
          ) : null}
        </div>
      ) : null}
      <span className="sr-only" data-row-key={rowKey} />
    </div>
  );
}

/** Number of spans the tree will render (every span becomes one row). */
export function spanRowCount(spans: WorkflowRunTraceSpan[]): number {
  return spans.length;
}

export function TraceSpanTree({ spans }: { spans: WorkflowRunTraceSpan[] }): JSX.Element {
  const rows = useMemo(() => flatten(buildTree(spans)), [spans]);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const onToggle = useCallback((spanId: string) => {
    setExpanded((prev) => ({ ...prev, [spanId]: !prev[spanId] }));
  }, []);

  return (
    <div className="overflow-hidden rounded-xl border border-slate-200/70 dark:border-slate-800">
      {rows.map((node) => (
        <TraceSpanRow
          key={node.span_id ?? `${node.name}-${node.depth}`}
          node={node}
          expanded={Boolean(node.span_id && expanded[node.span_id])}
          onToggle={onToggle}
        />
      ))}
    </div>
  );
}
