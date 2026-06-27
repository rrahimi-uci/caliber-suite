/**
 * Visual router condition builder — n8n-inspired monochromatic cards.
 *
 * Each branch is a clean zinc-bordered card with IF / THEN fields; the ELSE
 * fallback is at the bottom. Serializes to the manifest branch shape the
 * runtime's ``_route`` understands (``{condition: {field, op, value}, to}``).
 */

import type { ManifestNode } from "@/api/workflowTypes";

export interface RouterBranch {
  condition?: { field?: string; op?: string; value?: string } | null;
  to: string;
}

interface RouterConditionBuilderProps {
  branches: RouterBranch[];
  nodeIds: string[];
  onChange: (branches: RouterBranch[]) => void;
}

const OPS = [
  "contains",
  "not_contains",
  "equals",
  "not_equals",
  "mentions",
  "starts_with",
  "ends_with",
  "regex",
  "exists",
  "gt",
  "gte",
  "lt",
  "lte",
];

export function RouterConditionBuilder({
  branches,
  nodeIds,
  onChange,
}: RouterConditionBuilderProps): JSX.Element {
  const conditional = branches.filter((b) => b.condition != null);
  const fallback = branches.find((b) => b.condition == null) ?? null;

  function rebuild(nextConditional: RouterBranch[], nextFallback: RouterBranch | null): void {
    const result = [...nextConditional];
    if (nextFallback) result.push(nextFallback);
    onChange(result);
  }

  function updateCondition(idx: number, patch: Partial<NonNullable<RouterBranch["condition"]>>): void {
    const next = conditional.map((b, i) =>
      i === idx ? { ...b, condition: { ...(b.condition ?? {}), ...patch } } : b,
    );
    rebuild(next, fallback);
  }

  function updateTarget(idx: number, to: string): void {
    rebuild(conditional.map((b, i) => (i === idx ? { ...b, to } : b)), fallback);
  }

  function addBranch(): void {
    rebuild(
      [...conditional, { condition: { field: "output", op: "contains", value: "" }, to: nodeIds[0] ?? "" }],
      fallback,
    );
  }

  function removeBranch(idx: number): void {
    rebuild(conditional.filter((_, i) => i !== idx), fallback);
  }

  const selectClass =
    "rounded-lg border border-zinc-200 bg-white px-2 py-1 text-xs transition-colors focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900";
  const inputClass =
    "w-24 rounded-lg border border-zinc-200 bg-white px-2 py-1 text-xs transition-colors focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900";

  return (
    <div data-testid="router-condition-builder" className="space-y-2">
      {conditional.map((branch, idx) => (
        <div
          key={idx}
          data-testid={`router-branch-${idx}`}
          className="rounded-lg border border-zinc-200 bg-zinc-50 p-3 text-xs"
        >
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="rounded-md bg-zinc-900 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-white">
              IF
            </span>
            <input
              data-testid={`router-field-${idx}`}
              className={inputClass}
              placeholder="field"
              value={branch.condition?.field ?? ""}
              onChange={(e) => updateCondition(idx, { field: e.target.value })}
            />
            <select
              data-testid={`router-op-${idx}`}
              className={selectClass}
              value={branch.condition?.op ?? "contains"}
              onChange={(e) => updateCondition(idx, { op: e.target.value })}
            >
              {OPS.map((op) => (
                <option key={op} value={op}>
                  {op}
                </option>
              ))}
            </select>
            <input
              data-testid={`router-value-${idx}`}
              className={inputClass}
              placeholder="value"
              value={branch.condition?.value ?? ""}
              onChange={(e) => updateCondition(idx, { value: e.target.value })}
            />
          </div>
          <div className="mt-2 flex items-center gap-1.5">
            <span className="rounded-md bg-zinc-700 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-white">
              THEN
            </span>
            <span className="text-zinc-300">→</span>
            <select
              data-testid={`router-to-${idx}`}
              className={selectClass + " flex-1"}
              value={branch.to}
              onChange={(e) => updateTarget(idx, e.target.value)}
            >
              {nodeIds.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
            <button
              type="button"
              data-testid={`router-remove-${idx}`}
              className="rounded-md p-1 text-red-400 transition-colors hover:bg-red-50 hover:text-red-600"
              onClick={() => removeBranch(idx)}
            >
              ✕
            </button>
          </div>
        </div>
      ))}

      {/* Else fallback */}
      <div className="flex items-center gap-1.5 rounded-lg border border-dashed border-zinc-300 bg-zinc-50/50 px-3 py-2 text-xs">
        <span className="rounded-md bg-zinc-200 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-zinc-600">
          ELSE
        </span>
        <span className="text-zinc-300">→</span>
        <select
          data-testid="router-else"
          className={selectClass + " flex-1"}
          value={fallback?.to ?? ""}
          onChange={(e) =>
            rebuild(conditional, e.target.value ? { condition: null, to: e.target.value } : null)
          }
        >
          <option value="">(none)</option>
          {nodeIds.map((id) => (
            <option key={id} value={id}>
              {id}
            </option>
          ))}
        </select>
      </div>

      <button
        type="button"
        data-testid="router-add-condition"
        className="flex items-center gap-1 text-xs font-medium text-caliber-600 transition-colors hover:text-caliber-800 active:scale-[0.97]"
        onClick={addBranch}
      >
        <span className="text-sm">+</span> Add Condition
      </button>
    </div>
  );
}

/** Node ids eligible as router targets (everything except start). */
export function routerTargets(nodes: Record<string, ManifestNode>): string[] {
  return Object.values(nodes)
    .filter((n) => n.type !== "start")
    .map((n) => n.id);
}
