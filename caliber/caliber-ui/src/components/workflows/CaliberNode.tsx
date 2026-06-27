/**
 * Custom React Flow node renderer — Langflow-inspired "detail card".
 *
 * Each node reads as a self-describing box: an icon-chip header with the node
 * name + type, a one-line component description, a compact config detail, and
 * labeled, data-type-colored input/output rows (Langflow's per-field layout).
 * Connections stay node-to-node (one target handle left, one source handle
 * right) — CALIBER auto-derives the per-port `edge.map`, so the field rows are
 * informational rather than individually wired. Indigo selection ring + a
 * floating hover toolbar round out the n8n/Langflow feel.
 */

import { Handle, Position, type NodeProps } from "@xyflow/react";
import { Code2, Copy, Plus } from "lucide-react";

import {
  type FlowNodeData,
  nodeExecutionColor,
  nodeColor,
  nodeGuide,
  nodeSubtitle,
  portColor,
} from "@/lib/workflowGraph";
import { NodeIcon } from "@/components/workflows/NodeIcon";

/** Workflow node shadow — soft ambient lift off canvas. */
const NODE_SHADOW = "0 0px 15px -3px rgba(0,0,0,0.1), 0 0px 6px -4px rgba(0,0,0,0.1)";
const SELECTED_SHADOW =
  "0 0 0 2px rgba(79,70,229,0.3), 0 0px 15px -3px rgba(0,0,0,0.1), 0 0px 6px -4px rgba(0,0,0,0.1)";

export function CaliberNode({ data, selected }: NodeProps): JSX.Element {
  const flowData = data as FlowNodeData;
  const node = flowData.node;
  const color = nodeColor(node.type);
  const guide = nodeGuide(node, flowData.componentSpec ?? null, flowData.manifest ?? null);
  const onQuickAdd = (flowData as Record<string, unknown>).onQuickAdd as
    | ((nodeId: string) => void)
    | undefined;
  const onDuplicate = (flowData as Record<string, unknown>).onDuplicate as
    | ((nodeId: string) => void)
    | undefined;
  const onViewCode = (flowData as Record<string, unknown>).onViewCode as
    | ((nodeId: string) => void)
    | undefined;
  const canDuplicate = Boolean(onDuplicate && node.type !== "start" && node.type !== "output");
  const executionBadge = flowData.executionBadge;
  const validationSummary = flowData.validationSummary;

  // Port specs for data-type colored dots
  const inputPorts = Object.entries(node.inputs ?? {});
  const outputPorts = Object.entries(node.outputs ?? {});
  const hasInputHandle = inputPorts.length > 0;
  const hasOutputHandle = outputPorts.length > 0;
  const canQuickAdd = Boolean(onQuickAdd && hasOutputHandle && node.type !== "output");
  const primaryInputColor = inputPorts[0]?.[1]?.type
    ? portColor(inputPorts[0][1].type)
    : color;
  const primaryOutputColor = outputPorts[0]?.[1]?.type
    ? portColor(outputPorts[0][1].type)
    : color;

  const handleBase: React.CSSProperties = {
    width: 12,
    height: 12,
    border: "2.5px solid white",
    boxShadow: "0 0 0 1px rgba(0,0,0,0.08)",
    /* Enlarged hit-pad — visible dot stays 12px, clickable zone 30px */
    padding: 9,
    margin: -9,
  };

  const validationBadge =
    validationSummary && validationSummary.severity !== "ok" ? (
      <span
        data-testid={`node-validation-${node.id}`}
        className={`shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-semibold ${
          validationSummary.severity === "error"
            ? "bg-red-50 text-red-700"
            : "bg-amber-50 text-amber-700"
        }`}
        title={validationSummary.title}
      >
        {validationSummary.severity === "error"
          ? `${validationSummary.errors} error${validationSummary.errors === 1 ? "" : "s"}`
          : validationSummary.severity === "warning"
            ? `${validationSummary.warnings} warning${validationSummary.warnings === 1 ? "" : "s"}`
            : `${validationSummary.missingLabels.length} setup`}
      </span>
    ) : null;

  // Start node renders as a circular "trigger" puck rather than a card —
  // it has no inputs and a single outgoing edge, so a circle reads cleaner.
  if (node.type === "start") {
    return (
      <div
        data-testid={`wf-node-${node.id}`}
        data-node-type={node.type}
        className="group relative flex flex-col items-center"
        title={(typeof node.description === "string" && node.description.trim()) || guide.summary}
      >
        <div
          className="flex h-16 w-16 items-center justify-center rounded-full bg-white transition-all"
          style={{
            border: selected ? "2px solid #4F46E5" : `2px solid ${color}`,
            boxShadow: selected ? SELECTED_SHADOW : NODE_SHADOW,
            color,
          }}
        >
          <NodeIcon type={node.type} size={26} />
        </div>
        <div className="mt-1.5 max-w-[120px] truncate text-center text-[11px] font-semibold text-zinc-700">
          {flowData.label}
        </div>
        {validationBadge && <div className="mt-1">{validationBadge}</div>}

        {hasOutputHandle && (
          <Handle
            data-testid={`wf-node-source-${node.id}`}
            type="source"
            position={Position.Right}
            style={{ ...handleBase, top: 32, background: primaryOutputColor }}
          />
        )}

        {(canQuickAdd || onViewCode) && (
          <div className="absolute -top-10 left-1/2 flex -translate-x-1/2 items-center gap-0.5 rounded-xl border border-zinc-200 bg-white px-1.5 py-1 shadow-sm transition-all">
            {onViewCode && (
              <button
                type="button"
                data-testid={`view-code-${node.id}`}
                onClick={(e) => {
                  e.stopPropagation();
                  onViewCode(node.id);
                }}
                className="flex h-6 w-6 items-center justify-center rounded-md text-zinc-400 opacity-0 transition-colors hover:bg-zinc-100 hover:text-zinc-700 group-hover:opacity-100"
                title="View / edit node code"
              >
                <Code2 size={13} strokeWidth={2} aria-hidden />
              </button>
            )}
            {canQuickAdd && (
              <button
                type="button"
                data-testid={`quick-add-${node.id}`}
                onClick={(e) => {
                  e.stopPropagation();
                  onQuickAdd?.(node.id);
                }}
                className="flex h-6 w-6 items-center justify-center rounded-md text-zinc-400 opacity-0 transition-colors hover:bg-caliber-50 hover:text-caliber-600 group-hover:opacity-100"
                title="Add connected node"
              >
                <Plus size={13} strokeWidth={2.25} aria-hidden />
              </button>
            )}
          </div>
        )}
      </div>
    );
  }

  const detail = nodeSubtitle(node);

  return (
    <div
      data-testid={`wf-node-${node.id}`}
      data-node-type={node.type}
      className="group relative bg-white transition-all"
      title={(typeof node.description === "string" && node.description.trim()) || guide.summary}
      style={{
        borderRadius: 10,
        border: selected ? "2px solid #4F46E5" : "1px solid #E4E4E7",
        boxShadow: selected ? SELECTED_SHADOW : NODE_SHADOW,
        minWidth: 240,
        maxWidth: 300,
      }}
    >
      {/* Target handle — data-type colored */}
      {hasInputHandle && (
        <Handle
          data-testid={`wf-node-target-${node.id}`}
          type="target"
          position={Position.Left}
          style={{ ...handleBase, background: primaryInputColor }}
        />
      )}

      {/* Header — accent strip + icon + name + type + status */}
      <div
        className="flex items-center gap-2 rounded-t-[9px] px-3 py-2"
        style={{
          borderBottom: "1px solid #F4F4F5",
          background: `linear-gradient(to right, ${color}0F, transparent 70%)`,
        }}
      >
        <span
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md"
          style={{ backgroundColor: `${color}1A`, color }}
          aria-hidden
        >
          <NodeIcon type={node.type} size={16} />
        </span>
        <div className="flex-1 min-w-0">
          <div className="truncate text-[13px] font-semibold text-zinc-900 leading-tight">
            {flowData.label}
          </div>
          <div
            className="text-[10px] font-medium uppercase tracking-wider leading-tight mt-0.5"
            style={{ color }}
          >
            {node.type.replace(/_/g, " ")}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {validationBadge}
          {executionBadge ? (
            <span
              data-testid={`node-preview-${node.id}`}
              className="shrink-0 rounded-full px-1.5 py-0.5 text-[9px] font-semibold capitalize"
              style={{
                backgroundColor: `${nodeExecutionColor(executionBadge.status)}1A`,
                color: nodeExecutionColor(executionBadge.status),
              }}
              title={`${executionBadge.source === "run" ? "Run" : "Preview"}: ${executionBadge.status}${executionBadge.current ? " (current node)" : ""}`}
            >
              {executionBadge.label}
            </span>
          ) : !validationBadge ? (
            <span
              className="h-2 w-2 shrink-0 rounded-full"
              style={{ backgroundColor: "#6B7280" }}
              title="Idle"
            />
          ) : null}
        </div>
      </div>

      {/* Body — component description + config detail + typed port rows */}
      <div className="px-3 py-2.5">
        <div className="text-[11px] leading-snug text-zinc-500 line-clamp-2">
          {guide.summary}
        </div>
        {detail && (
          <div className="mt-1.5 truncate rounded-md bg-zinc-50 px-2 py-1 font-mono text-[10px] text-zinc-600">
            {detail}
          </div>
        )}

        {inputPorts.length > 0 && (
          <div className="mt-2.5 space-y-1">
            <div className="text-[9px] font-semibold uppercase tracking-wider text-zinc-300">
              Inputs
            </div>
            {inputPorts.map(([name, spec]) => (
              <PortRow key={`in-${name}`} name={name} type={spec.type} side="input" />
            ))}
          </div>
        )}

        {outputPorts.length > 0 && (
          <div
            className="mt-2.5 space-y-1 border-t border-dashed border-zinc-100 pt-2"
          >
            <div className="text-right text-[9px] font-semibold uppercase tracking-wider text-zinc-300">
              Outputs
            </div>
            {outputPorts.map(([name, spec]) => (
              <PortRow key={`out-${name}`} name={name} type={spec.type} side="output" />
            ))}
          </div>
        )}
      </div>

      {/* Source handle — data-type colored */}
      {hasOutputHandle && (
        <Handle
          data-testid={`wf-node-source-${node.id}`}
          type="source"
          position={Position.Right}
          style={{ ...handleBase, background: primaryOutputColor }}
        />
      )}

      {/* Floating toolbar — visible on hover/selection */}
      <div
        className="absolute -top-10 left-1/2 -translate-x-1/2 flex items-center gap-0.5 rounded-xl border border-zinc-200 bg-white px-1.5 py-1 shadow-sm transition-all"
        style={{ opacity: selected ? 1 : undefined }}
      >
        {onViewCode && (
          <button
            type="button"
            data-testid={`view-code-${node.id}`}
            onClick={(e) => {
              e.stopPropagation();
              onViewCode(node.id);
            }}
            className="flex h-6 w-6 items-center justify-center rounded-md text-zinc-400 transition-colors hover:bg-zinc-100 hover:text-zinc-700 opacity-0 group-hover:opacity-100"
            title="View / edit node code"
          >
            <Code2 size={13} strokeWidth={2} aria-hidden />
          </button>
        )}
        {canDuplicate && (
          <button
            type="button"
            data-testid={`duplicate-${node.id}`}
            onClick={(e) => {
              e.stopPropagation();
              onDuplicate?.(node.id);
            }}
            className="flex h-6 w-6 items-center justify-center rounded-md text-zinc-400 transition-colors hover:bg-zinc-100 hover:text-zinc-700 opacity-0 group-hover:opacity-100"
            title="Duplicate node"
          >
            <Copy size={13} strokeWidth={2} aria-hidden />
          </button>
        )}
        {canQuickAdd && (
          <button
            type="button"
            data-testid={`quick-add-${node.id}`}
            onClick={(e) => {
              e.stopPropagation();
              onQuickAdd?.(node.id);
            }}
            className="flex h-6 w-6 items-center justify-center rounded-md text-zinc-400 transition-colors hover:bg-caliber-50 hover:text-caliber-600 opacity-0 group-hover:opacity-100"
            title="Add connected node"
          >
            <Plus size={13} strokeWidth={2.25} aria-hidden />
          </button>
        )}
      </div>
    </div>
  );
}

/** A single Langflow-style port row: a data-type dot at the card edge + label + type. */
function PortRow({
  name,
  type,
  side,
}: {
  name: string;
  type: string;
  side: "input" | "output";
}): JSX.Element {
  const dot = (
    <span
      className="h-1.5 w-1.5 shrink-0 rounded-full"
      style={{ backgroundColor: portColor(type) }}
    />
  );
  return (
    <div
      className={`flex items-center gap-1.5 ${side === "output" ? "flex-row-reverse text-right" : ""}`}
    >
      {dot}
      <span className="truncate font-mono text-[10px] text-zinc-600">{name}</span>
      <span className="ml-auto shrink-0 rounded bg-zinc-100 px-1 py-px text-[9px] font-medium text-zinc-400">
        {type}
      </span>
    </div>
  );
}
