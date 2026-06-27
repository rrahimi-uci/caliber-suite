/**
 * Connection map popover — n8n-inspired port wiring.
 *
 * Shadow-lg elevation, monochromatic design with data-type colored port dots.
 * Shows after drawing an edge: lets each target input be wired from a source
 * output. Auto-Map, Remove, and Done actions.
 */

import type { ManifestNode } from "@/api/workflowTypes";
import {
  autoMapCompatiblePorts,
  compatibleSourceOutputs,
  nodeInputs,
  nodeOutputs,
  nodeColor,
  portColor,
  portSpecAssignable,
} from "@/lib/workflowGraph";

interface ConnectMapPopoverProps {
  source: ManifestNode;
  target: ManifestNode;
  map: Record<string, string>;
  anchor?: { x: number; y: number } | null;
  onChange: (map: Record<string, string>) => void;
  onDone: () => void;
  onRemove: () => void;
}

const UNMAPPED = "";

export function ConnectMapPopover({
  source,
  target,
  map,
  anchor,
  onChange,
  onDone,
  onRemove,
}: ConnectMapPopoverProps): JSX.Element {
  const outputs = nodeOutputs(source);
  const inputs = nodeInputs(target);
  const sourceColor = nodeColor(source.type);
  const targetColor = nodeColor(target.type);

  const inputToOutput: Record<string, string> = {};
  for (const [out, inp] of Object.entries(map)) inputToOutput[inp] = out;

  const compatibilityWarnings = Object.entries(map)
    .filter(([outputName, inputName]) =>
      !portSpecAssignable(target.inputs?.[inputName], source.outputs?.[outputName]),
    )
    .map(([outputName, inputName]) => ({
      outputName,
      inputName,
      sourceType: source.outputs?.[outputName]?.type ?? "unknown",
      targetType: target.inputs?.[inputName]?.type ?? "unknown",
    }));

  function setInput(inp: string, out: string): void {
    const next: Record<string, string> = {};
    for (const [o, i] of Object.entries(map)) {
      if (i !== inp) next[o] = i;
    }
    if (out !== UNMAPPED) next[out] = inp;
    onChange(next);
  }

  const anchored = Boolean(anchor);
  let anchoredStyle: { left: number; top: number } | undefined;
  if (anchored && anchor) {
    const width = 384; // Tailwind w-96
    const viewWidth = typeof window !== "undefined" ? window.innerWidth : width + 24;
    const viewHeight = typeof window !== "undefined" ? window.innerHeight : 768;
    const left = Math.max(12, Math.min(anchor.x - width / 2, viewWidth - width - 12));
    const top = Math.max(12, Math.min(anchor.y + 12, viewHeight - 260));
    anchoredStyle = { left, top };
  }

  return (
    <div
      data-testid="connect-map-popover"
      className={
        anchored
          ? "fixed z-10 w-96 rounded-xl border border-zinc-200 bg-white p-4 shadow-lg"
          : "absolute left-1/2 top-4 z-10 w-96 -translate-x-1/2 rounded-xl border border-zinc-200 bg-white p-4 shadow-lg"
      }
      style={anchoredStyle}
    >
      {/* Header */}
      <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-zinc-900">
        <span
          className="inline-block h-2.5 w-2.5 rounded-full"
          style={{ backgroundColor: sourceColor }}
        />
        <span>{source.name ?? source.id}</span>
        <span className="text-zinc-300">→</span>
        <span
          className="inline-block h-2.5 w-2.5 rounded-full"
          style={{ backgroundColor: targetColor }}
        />
        <span>{target.name ?? target.id}</span>
      </div>
      <div className="mb-3 flex flex-wrap gap-1.5 text-[10px]">
        <span className="rounded-full border border-zinc-200 bg-zinc-50 px-2 py-0.5 font-semibold text-zinc-600">
          {outputs.length} source output{outputs.length === 1 ? "" : "s"}
        </span>
        <span className="rounded-full border border-zinc-200 bg-zinc-50 px-2 py-0.5 font-semibold text-zinc-600">
          {inputs.length} target input{inputs.length === 1 ? "" : "s"}
        </span>
        <span className="rounded-full border border-sky-200 bg-sky-50 px-2 py-0.5 font-semibold text-sky-700">
          Type-aware auto-map
        </span>
      </div>

      {compatibilityWarnings.length > 0 && (
        <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] leading-relaxed text-amber-800">
          {compatibilityWarnings.length === 1
            ? "This edge currently contains one incompatible port mapping."
            : `This edge currently contains ${compatibilityWarnings.length} incompatible port mappings.`}
          <div className="mt-1">
            {compatibilityWarnings.map((warning) => (
              <div key={`${warning.outputName}-${warning.inputName}`}>
                {warning.outputName} ({warning.sourceType}) → {warning.inputName} ({warning.targetType})
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Port mappings */}
      <div className="space-y-2 rounded-lg border border-zinc-100 bg-zinc-50 p-3">
        <div className="flex items-center justify-between text-[10px] font-semibold uppercase tracking-wider text-zinc-400">
          <span>Target Input</span>
          <span>Source Output</span>
        </div>
        {inputs.length === 0 && (
          <div className="text-xs text-zinc-400 py-2 text-center">
            Target has no declared inputs.
          </div>
        )}
        {inputs.map((inp) => {
          const inputSpec = target.inputs?.[inp];
          const pColor = inputSpec ? portColor(inputSpec.type) : "#6B7280";
          const compatibleOutputs = compatibleSourceOutputs(source, target, inp);
          const selectedOutput = inputToOutput[inp] ?? UNMAPPED;
          return (
            <div key={inp} className="space-y-1.5">
              <div className="flex items-center justify-between gap-3">
                <span className="flex items-center gap-1.5 rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs font-mono text-zinc-700">
                  <span
                    className="h-1.5 w-1.5 rounded-full"
                    style={{ backgroundColor: pColor }}
                  />
                  {inp}
                  {inputSpec?.type && (
                    <span className="rounded-full bg-zinc-100 px-1.5 py-0.5 text-[10px] font-semibold text-zinc-500">
                      {inputSpec.type}
                    </span>
                  )}
                </span>
                <span className="text-zinc-300">←</span>
                <select
                  data-testid={`map-input-${inp}`}
                  className="flex-1 rounded-lg border border-zinc-200 bg-white px-2 py-1 text-xs text-zinc-700 transition-colors focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
                  value={selectedOutput}
                  onChange={(e) => setInput(inp, e.target.value)}
                >
                  <option value={UNMAPPED}>(unmapped)</option>
                  {outputs.map((out) => {
                    const outputSpec = source.outputs?.[out];
                    const assignable = portSpecAssignable(inputSpec, outputSpec);
                    const isCurrentSelection = selectedOutput === out;
                    const label = `${out}${outputSpec?.type ? ` · ${outputSpec.type}` : ""}${assignable ? "" : " — incompatible"}`;
                    return (
                      <option
                        key={out}
                        value={out}
                        disabled={!assignable && !isCurrentSelection}
                      >
                        {label}
                      </option>
                    );
                  })}
                </select>
              </div>
              {compatibleOutputs.length === 0 && (
                <div className="pl-1 text-[11px] text-amber-700">
                  No compatible source outputs are available for this input yet.
                </div>
              )}
            </div>
          );
        })}
      </div>

      {compatibilityWarnings.length === 0 && outputs.length > 0 && inputs.length > 0 && (
        <div className="mt-3 text-[11px] leading-relaxed text-zinc-500">
          Auto-map prefers same-name ports first, then fills the remaining inputs with the first
          compatible source outputs. A `messages` input may also accept a `string` output.
        </div>
      )}

      {/* Actions */}
      <div className="mt-4 flex items-center justify-between">
        <button
          type="button"
          data-testid="map-auto"
          className="flex items-center gap-1 text-xs font-medium text-caliber-600 transition-colors hover:text-caliber-800 active:scale-[0.97]"
          onClick={() => onChange(autoMapCompatiblePorts(source, target))}
        >
          ⚡ Auto-Map
        </button>
        <div className="flex gap-2">
          <button
            type="button"
            data-testid="map-remove"
            className="rounded-lg border border-red-200 px-3 py-1.5 text-xs font-medium text-red-600 transition-colors hover:bg-red-50 active:scale-[0.97]"
            onClick={onRemove}
          >
            Remove
          </button>
          <button
            type="button"
            data-testid="map-done"
            className="rounded-lg bg-zinc-900 px-4 py-1.5 text-xs font-medium text-white transition-colors hover:bg-zinc-800 active:scale-[0.97]"
            onClick={onDone}
          >
            Done
          </button>
        </div>
      </div>
    </div>
  );
}
