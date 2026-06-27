/**
 * Component palette — n8n-inspired sidebar panel.
 *
 * Monochromatic design with grouped, draggable component cards. Each item
 * shows a type-accented icon, name, and description. Items can be dragged
 * onto the Canvas or clicked to add at a default position.
 */

import {
  ArrowLeftRight,
  Bot,
  Boxes,
  GitBranch,
  Plug,
  ShieldCheck,
  Workflow,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { useMemo, useState } from "react";

import type { WorkflowComponent } from "@/api/workflowTypes";
import { NodeIcon } from "@/components/workflows/NodeIcon";
import { WorkflowComponentSchemaSummary } from "@/components/workflows/WorkflowComponentSchemaSummary";
import { buildNodePalette, nodeColor, type NodePaletteItem } from "@/lib/workflowGraph";

/** Lucide icon per palette group, matching the Langflow category-header pattern. */
const GROUP_ICONS: Record<string, LucideIcon> = {
  "Inputs & Outputs": ArrowLeftRight,
  Orchestration: Workflow,
  Agents: Bot,
  Logic: GitBranch,
  Safety: ShieldCheck,
  Integrations: Plug,
  Utilities: Wrench,
};

/** Resolve a group's header icon, falling back to a neutral "boxes" glyph. */
function groupIcon(group: string): LucideIcon {
  return GROUP_ICONS[group] ?? Boxes;
}

interface NodePaletteProps {
  onAddNode: (type: string) => void;
  components?: WorkflowComponent[] | null;
}

export function NodePalette({ onAddNode, components = null }: NodePaletteProps): JSX.Element {
  const [filter, setFilter] = useState("");
  const [showLegacy, setShowLegacy] = useState(false);
  const [inspectedType, setInspectedType] = useState<WorkflowComponent["type"] | null>(null);
  const paletteItems = buildNodePalette(components);
  const hasLegacy = paletteItems.some((item) => item.legacy);
  const normalizedFilter = filter.trim().toLowerCase();
  const componentMap = useMemo(
    () =>
      new Map(
        (components ?? []).map((component) => [component.type, component] as const),
      ),
    [components],
  );
  const inspectedComponent = inspectedType ? componentMap.get(inspectedType) ?? null : null;

  const groups = paletteItems.reduce<Record<string, NodePaletteItem[]>>((acc, item) => {
    // Legacy components are hidden unless revealed (or surfaced by a search).
    if (item.legacy && !showLegacy && !normalizedFilter) return acc;
    if (normalizedFilter) {
      const haystack = [
        item.label,
        item.group,
        item.description,
        ...(item.docs ?? []),
      ]
        .join(" ")
        .toLowerCase();
      if (!haystack.includes(normalizedFilter)) return acc;
    }
    (acc[item.group] ??= []).push(item);
    return acc;
  }, {});

  return (
    <div data-testid="wf-node-palette" className="space-y-3">
      <h3 className="text-[11px] font-semibold uppercase tracking-wider text-zinc-400">
        Components
      </h3>

      <input
        type="text"
        placeholder="Search components…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        className="w-full rounded-lg border border-zinc-200 bg-white px-3 py-1.5 text-xs text-zinc-700 placeholder:text-zinc-400 transition-colors focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
      />

      {hasLegacy && (
        <label className="flex items-center gap-1.5 px-0.5 text-[11px] text-zinc-500">
          <input
            type="checkbox"
            data-testid="palette-show-legacy"
            checked={showLegacy}
            onChange={(e) => setShowLegacy(e.target.checked)}
            className="h-3.5 w-3.5 rounded border-zinc-300"
          />
          Show legacy components
        </label>
      )}

      {Object.entries(groups).map(([group, items]) => {
        const GroupIcon = groupIcon(group);
        return (
        <div key={group} className="space-y-1">
          <div className="flex items-center gap-1.5 px-0.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
            <GroupIcon size={12} strokeWidth={2} aria-hidden />
            {group}
          </div>
          {items.map((item) => {
            const color = nodeColor(item.type);
            const hasComponentSpec = componentMap.has(item.type);
            const selectedReference = inspectedType === item.type;
            return (
              <div
                key={item.type}
                className={`rounded-lg border bg-white transition-all ${
                  selectedReference
                    ? "border-sky-300 shadow-sm ring-1 ring-sky-200"
                    : "border-zinc-200 hover:border-zinc-300 hover:shadow-sm"
                }`}
              >
                <div className="flex items-start gap-2 px-2.5 py-2">
                  <button
                    type="button"
                    data-testid={`palette-${item.type}`}
                    title={[item.description, ...(item.docs ?? []).slice(0, 2)].join("\n")}
                    draggable
                    onDragStart={(e) => {
                      e.dataTransfer.setData("application/caliber-node-type", item.type);
                      e.dataTransfer.effectAllowed = "move";
                    }}
                    onClick={() => onAddNode(item.type)}
                    className="flex min-w-0 flex-1 cursor-grab items-start gap-2.5 text-left active:cursor-grabbing active:scale-[0.99]"
                  >
                    <span
                      className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md"
                      style={{ backgroundColor: `${color}12`, color }}
                      aria-hidden
                    >
                      <NodeIcon type={item.type} size={16} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="text-xs font-medium text-zinc-900">{item.label}</div>
                      <div className="mt-0.5 text-[10px] leading-relaxed text-zinc-500">
                        {item.description}
                      </div>
                    </div>
                  </button>
                  {hasComponentSpec && (
                    <button
                      type="button"
                      data-testid={`palette-inspect-${item.type}`}
                      aria-pressed={selectedReference}
                      onClick={() =>
                        setInspectedType((current) => (current === item.type ? null : item.type))
                      }
                      className={`shrink-0 rounded-md border px-2 py-1 text-[10px] font-semibold transition-colors ${
                        selectedReference
                          ? "border-sky-200 bg-sky-50 text-sky-700"
                          : "border-zinc-200 bg-white text-zinc-500 hover:border-zinc-300 hover:text-zinc-700"
                      }`}
                    >
                      Inspect
                    </button>
                  )}
                </div>
                <div className="flex flex-wrap gap-1.5 border-t border-zinc-100 px-2.5 pb-2 pt-1.5 text-[10px]">
                  {item.legacy && (
                    <span
                      data-testid={`palette-legacy-${item.type}`}
                      className="rounded-full border border-amber-300 bg-amber-100 px-2 py-0.5 font-semibold uppercase tracking-wide text-amber-800"
                      title={
                        item.legacyReplacement
                          ? `Legacy — prefer ${item.legacyReplacement}`
                          : "Legacy component"
                      }
                    >
                      Legacy
                    </span>
                  )}
                  {(item.fieldCount ?? 0) > 0 && (
                    <span className="rounded-full border border-zinc-200 bg-zinc-50 px-2 py-0.5 font-medium text-zinc-600">
                      {item.fieldCount} field{item.fieldCount === 1 ? "" : "s"}
                    </span>
                  )}
                  {(item.setupRuleCount ?? 0) > 0 && (
                    <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 font-medium text-amber-700">
                      {item.setupRuleCount} setup rule{item.setupRuleCount === 1 ? "" : "s"}
                    </span>
                  )}
                  {((item.defaultInputCount ?? 0) > 0 || (item.defaultOutputCount ?? 0) > 0) && (
                    <span className="rounded-full border border-sky-200 bg-sky-50 px-2 py-0.5 font-medium text-sky-700">
                      {item.defaultInputCount ?? 0} in · {item.defaultOutputCount ?? 0} out
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
        );
      })}

      {inspectedComponent && (
        <div
          data-testid="palette-component-reference"
          className="space-y-3 rounded-2xl border border-slate-200/80 bg-white p-3 shadow-card"
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500">
                Component Reference
              </div>
              <div className="mt-1 text-sm font-semibold text-slate-900">
                {inspectedComponent.label}
              </div>
              <div className="mt-1 text-xs leading-relaxed text-slate-500">
                Review setup rules, ports, and config fields before adding this node to the canvas.
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                data-testid="palette-reference-add"
                onClick={() => onAddNode(inspectedComponent.type)}
                className="rounded-lg bg-zinc-900 px-3 py-1.5 text-[11px] font-semibold text-white transition-colors hover:bg-zinc-800"
              >
                Add to canvas
              </button>
              <button
                type="button"
                data-testid="palette-reference-close"
                onClick={() => setInspectedType(null)}
                className="rounded-lg border border-zinc-200 px-3 py-1.5 text-[11px] font-semibold text-zinc-600 transition-colors hover:bg-zinc-50 hover:text-zinc-900"
              >
                Close
              </button>
            </div>
          </div>
          <WorkflowComponentSchemaSummary component={inspectedComponent} />
        </div>
      )}

      {Object.keys(groups).length === 0 && (
        <div className="py-4 text-center text-xs text-zinc-400">No matching components</div>
      )}
    </div>
  );
}
