import { useMemo } from "react";

import type { HandoffSpec, ManifestNode, WorkflowManifest } from "@/api/workflowTypes";

interface AgentHandoffEditorProps {
  agentId: string;
  nodes: WorkflowManifest["nodes"];
  handoffs: HandoffSpec[];
  onChange: (next: HandoffSpec[]) => void;
}

interface HandoffTargetOption {
  value: string;
  label: string;
}

function supportedAgentTargets(
  nodes: WorkflowManifest["nodes"],
  agentId: string,
): HandoffTargetOption[] {
  return Object.values(nodes)
    .filter((candidate): candidate is ManifestNode => candidate.id !== agentId && candidate.type === "agent")
    .map((candidate) => ({
      value: candidate.id,
      label: candidate.name ? `${candidate.name} (${candidate.id})` : candidate.id,
    }));
}

function targetOptionsForSelection(
  nodes: WorkflowManifest["nodes"],
  agentId: string,
  selectedTarget: string,
): HandoffTargetOption[] {
  const supported = supportedAgentTargets(nodes, agentId);
  if (!selectedTarget || supported.some((option) => option.value === selectedTarget)) {
    return supported;
  }
  const candidate = nodes[selectedTarget];
  const fallbackLabel = candidate
    ? `${selectedTarget} (unsupported target: ${candidate.type})`
    : `${selectedTarget} (missing target)`;
  return [{ value: selectedTarget, label: fallbackLabel }, ...supported];
}

function normalizeOptionalText(value: string): string | null {
  return value.trim() ? value : null;
}

export function AgentHandoffEditor({
  agentId,
  nodes,
  handoffs,
  onChange,
}: AgentHandoffEditorProps): JSX.Element {
  const availableTargets = useMemo(() => supportedAgentTargets(nodes, agentId), [nodes, agentId]);
  const canAdd = availableTargets.length > 0;

  function updateHandoff(index: number, patch: Partial<HandoffSpec>): void {
    onChange(
      handoffs.map((handoff, currentIndex) =>
        currentIndex === index ? { ...handoff, ...patch } : handoff,
      ),
    );
  }

  function removeHandoff(index: number): void {
    onChange(handoffs.filter((_, currentIndex) => currentIndex !== index));
  }

  function addHandoff(): void {
    if (!canAdd) return;
    onChange([
      ...handoffs,
      {
        target: availableTargets[0]?.value ?? "",
        description: "",
        condition: null,
        input_filter: null,
      },
    ]);
  }

  return (
    <div data-testid="handoffs-editor" className="space-y-3">
      <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5 text-xs leading-relaxed text-zinc-500">
        Handoffs delegate work to another agent and render as dashed edges on the canvas. Chain them across specialists when needed, and add clear conditions or prompts so the delegation path can terminate before the runtime handoff cap is reached.
      </div>

      {handoffs.length === 0 ? (
        <div className="rounded-lg border border-dashed border-zinc-200 bg-white px-3 py-3 text-xs text-zinc-400">
          {canAdd
            ? "No handoffs configured yet. Add one to delegate specialist work."
            : "Add another Agent node to unlock delegation handoffs."}
        </div>
      ) : (
        <div className="space-y-3">
          {handoffs.map((handoff, index) => {
            const targetOptions = targetOptionsForSelection(nodes, agentId, handoff.target);
            const selectedTargetNode = handoff.target ? nodes[handoff.target] : undefined;
            const targetInvalid = Boolean(
              handoff.target &&
                (!selectedTargetNode || selectedTargetNode.type !== "agent" || selectedTargetNode.id === agentId),
            );
            return (
              <div
                key={`${handoff.target || "handoff"}-${index}`}
                data-testid={`handoff-card-${index}`}
                className="space-y-3 rounded-xl border border-zinc-200 bg-white px-3 py-3 shadow-sm"
              >
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="text-xs font-semibold uppercase tracking-[0.14em] text-zinc-400">
                      Handoff {index + 1}
                    </div>
                    <div className="mt-1 text-sm font-semibold text-zinc-900">
                      {handoff.description?.trim() || handoff.target || "New handoff"}
                    </div>
                  </div>
                  <button
                    type="button"
                    data-testid={`handoff-remove-${index}`}
                    onClick={() => removeHandoff(index)}
                    className="rounded-lg border border-red-200 px-2.5 py-1 text-xs font-medium text-red-600 transition-colors hover:bg-red-50"
                  >
                    Remove
                  </button>
                </div>

                <label className="block">
                  <span className="text-xs font-medium text-zinc-600">Target agent</span>
                  <select
                    data-testid={`handoff-target-${index}`}
                    className="mt-1 w-full rounded-lg border border-zinc-200 bg-white px-3 py-1.5 text-sm text-zinc-900 transition-colors hover:border-zinc-300 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
                    value={handoff.target}
                    onChange={(event) => updateHandoff(index, { target: event.target.value })}
                  >
                    <option value="">Select an agent</option>
                    {targetOptions.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                  {targetInvalid ? (
                    <div className="mt-1 text-[11px] text-amber-600">
                      This handoff currently points to an invalid target. Pick another agent to resolve it.
                    </div>
                  ) : (
                    <div className="mt-1 text-[11px] text-zinc-500">
                      Choose the specialist agent this node may delegate to.
                    </div>
                  )}
                </label>

                <label className="block">
                  <span className="text-xs font-medium text-zinc-600">Description</span>
                  <input
                    data-testid={`handoff-description-${index}`}
                    className="mt-1 w-full rounded-lg border border-zinc-200 bg-white px-3 py-1.5 text-sm text-zinc-900 transition-colors hover:border-zinc-300 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
                    placeholder="e.g. Escalate billing disputes"
                    value={handoff.description ?? ""}
                    onChange={(event) => updateHandoff(index, { description: event.target.value })}
                  />
                </label>

                <label className="block">
                  <span className="text-xs font-medium text-zinc-600">Condition gate</span>
                  <input
                    data-testid={`handoff-condition-${index}`}
                    className="mt-1 w-full rounded-lg border border-zinc-200 bg-white px-3 py-1.5 font-mono text-sm text-zinc-900 transition-colors hover:border-zinc-300 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
                    placeholder="e.g. refund_total > 1000"
                    value={handoff.condition ?? ""}
                    onChange={(event) =>
                      updateHandoff(index, {
                        condition: normalizeOptionalText(event.target.value),
                      })
                    }
                  />
                  <div className="mt-1 text-[11px] text-zinc-500">
                    Optional CALIBER-side gate that decides whether the handoff should be available.
                  </div>
                </label>

                <label className="block">
                  <span className="text-xs font-medium text-zinc-600">Input filter</span>
                  <input
                    data-testid={`handoff-input-filter-${index}`}
                    className="mt-1 w-full rounded-lg border border-zinc-200 bg-white px-3 py-1.5 font-mono text-sm text-zinc-900 transition-colors hover:border-zinc-300 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
                    placeholder="e.g. Forward only the refund summary"
                    value={handoff.input_filter ?? ""}
                    onChange={(event) =>
                      updateHandoff(index, {
                        input_filter: normalizeOptionalText(event.target.value),
                      })
                    }
                  />
                  <div className="mt-1 text-[11px] text-zinc-500">
                    Optional filter that narrows what context is forwarded to the target agent.
                  </div>
                </label>
              </div>
            );
          })}
        </div>
      )}

      <button
        type="button"
        data-testid="handoffs-add"
        disabled={!canAdd}
        onClick={addHandoff}
        className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-semibold transition-colors ${
          canAdd
            ? "border-zinc-200 bg-white text-zinc-700 hover:border-zinc-300 hover:text-zinc-900"
            : "cursor-not-allowed border-zinc-200 bg-zinc-100 text-zinc-400"
        }`}
      >
        ＋ Add handoff
      </button>
    </div>
  );
}
