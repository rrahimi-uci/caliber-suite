/**
 * In-canvas NL copilot (Lakeflow "Genie Code" / describe-to-build) with a
 * run→inspect→retry iterate loop.
 *
 * The user describes a change in plain English; the copilot calls
 * `…/copilot-edit` (modify-in-place: the backend returns the *full* proposed
 * manifest grounded in the registry), and we render the base→proposed change as
 * a {@link GraphDiff} overlay with Accept / Reject. Accepting applies the
 * proposed manifest to the canvas via `onApply`.
 *
 * **Iterate loop:** the dock can also *run a preview* of the current canvas
 * (including an unsaved, just-applied edit — `preview-run` accepts the inline
 * manifest) and show each node's status. Any step that isn't `ok` gets a
 * one-click "Fix with copilot" that feeds the failure back into a new edit —
 * Lakeflow's execute → inspect → retry.
 *
 * With the default `fake` provider the proposal echoes the manifest back
 * unchanged (an empty diff), so the dock is safe to use without an LLM
 * configured — Accept is disabled when there is nothing to apply.
 */

import { useState } from "react";

import { caliberApi } from "@/api/caliberApi";
import type {
  CopilotEditResult,
  PreviewResult,
  PreviewStep,
  WorkflowManifest,
} from "@/api/workflowTypes";

import { GraphDiff } from "./GraphDiff";
import { stepStatusStyle } from "./StepPreview";

interface WorkflowCopilotProps {
  versionId: string;
  /** The current (possibly unsaved) canvas manifest — the edit/preview base. */
  manifest: WorkflowManifest;
  /** Apply an accepted proposal to the canvas. */
  onApply: (manifest: WorkflowManifest) => void;
}

type Phase =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "result"; result: CopilotEditResult }
  | { status: "error"; error: string };

type PreviewPhase =
  | { status: "idle" }
  | { status: "running" }
  | { status: "done"; result: PreviewResult }
  | { status: "error"; error: string };

const SUGGESTIONS = [
  "Add a PII-redact guardrail after the agent",
  "Route low-confidence output to human approval",
  "Add an output node that returns the final answer",
];

export function WorkflowCopilot({ versionId, manifest, onApply }: WorkflowCopilotProps): JSX.Element {
  const [instruction, setInstruction] = useState("");
  const [phase, setPhase] = useState<Phase>({ status: "idle" });
  const [sampleInput, setSampleInput] = useState("Hello");
  const [preview, setPreview] = useState<PreviewPhase>({ status: "idle" });

  async function propose(explicit?: string): Promise<void> {
    const trimmed = (explicit ?? instruction).trim();
    if (!trimmed) return;
    setPhase({ status: "loading" });
    try {
      const result = await caliberApi.copilotEditWorkflow(versionId, {
        instruction: trimmed,
        manifest,
      });
      setPhase({ status: "result", result });
    } catch (e) {
      setPhase({ status: "error", error: e instanceof Error ? e.message : "copilot failed" });
    }
  }

  function accept(): void {
    if (phase.status !== "result") return;
    onApply(phase.result.proposed_manifest);
    setPhase({ status: "idle" });
    setInstruction("");
    // Invalidate any stale preview — the canvas just changed.
    setPreview({ status: "idle" });
  }

  function reject(): void {
    // Keep the instruction so the user can refine and retry.
    setPhase({ status: "idle" });
  }

  async function runPreview(): Promise<void> {
    setPreview({ status: "running" });
    try {
      const result = await caliberApi.previewWorkflowVersion(
        versionId,
        sampleInput.trim() || "Hello",
        undefined,
        manifest,
      );
      setPreview({ status: "done", result });
    } catch (e) {
      setPreview({ status: "error", error: e instanceof Error ? e.message : "preview failed" });
    }
  }

  function fixStep(step: PreviewStep): void {
    const detail = step.detail || step.output;
    const text =
      `The "${step.node_id}" step (${step.node_type}) returned status "${step.status}"` +
      `${detail ? `: ${detail}` : ""}. Update the workflow so this step succeeds.`;
    setInstruction(text);
    void propose(text);
  }

  const result = phase.status === "result" ? phase.result : null;
  const errorCount = result ? (result.report?.errors?.length ?? 0) : 0;

  return (
    <div data-testid="copilot-dock" className="flex h-full flex-col bg-white">
      <div className="border-b border-zinc-200 px-4 py-3">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-zinc-900">Copilot</span>
          <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-medium text-violet-700">
            beta
          </span>
        </div>
        <p className="mt-0.5 text-xs text-zinc-500">Describe a change, then preview & iterate.</p>
      </div>

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-auto p-4">
        <textarea
          data-testid="copilot-input"
          aria-label="Describe a workflow change"
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void propose();
          }}
          placeholder="e.g. add a PII-redact guardrail after the rules agent"
          rows={3}
          className="resize-none rounded-lg border border-zinc-200 bg-zinc-50 p-2.5 text-sm text-zinc-800 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
        />

        {phase.status === "idle" && (
          <div className="flex flex-wrap gap-1.5">
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                type="button"
                data-testid="copilot-suggestion"
                onClick={() => setInstruction(s)}
                className="rounded-full border border-zinc-200 px-2.5 py-1 text-[11px] text-zinc-600 transition-colors hover:bg-zinc-50"
              >
                {s}
              </button>
            ))}
          </div>
        )}

        <button
          type="button"
          data-testid="copilot-submit"
          disabled={!instruction.trim() || phase.status === "loading"}
          onClick={() => void propose()}
          className="self-start rounded-lg bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-zinc-800 disabled:opacity-40 active:scale-[0.97]"
        >
          {phase.status === "loading" ? "Thinking…" : "Propose change"}
        </button>

        {phase.status === "error" && (
          <div
            data-testid="copilot-error"
            className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
          >
            {phase.error}
          </div>
        )}

        {result && (
          <div className="space-y-3 rounded-lg border border-zinc-200 p-3">
            <div>
              <p data-testid="copilot-summary" className="text-sm font-medium text-zinc-900">
                {result.summary || "Proposed change"}
              </p>
              {result.rationale && (
                <p className="mt-1 text-xs text-zinc-500">{result.rationale}</p>
              )}
            </div>

            {!result.valid && (
              <div
                data-testid="copilot-invalid"
                className="rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-xs text-amber-700"
              >
                ⚠ The proposal has {errorCount} validation {errorCount === 1 ? "error" : "errors"}.
                Review before applying.
              </div>
            )}

            <GraphDiff diff={result.graph_diff} />

            <div data-testid="copilot-grounding" className="text-[11px] text-zinc-400">
              Grounded in {result.grounding.tools.length} tools ·{" "}
              {result.grounding.skills.length} skills ·{" "}
              {result.grounding.eval_datasets.length} datasets
            </div>

            <div className="flex items-center gap-2">
              <button
                type="button"
                data-testid="copilot-accept"
                disabled={result.graph_diff.empty}
                onClick={accept}
                title={result.graph_diff.empty ? "No changes to apply" : "Apply to canvas"}
                className="rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-emerald-700 disabled:opacity-40 active:scale-[0.97]"
              >
                Accept
              </button>
              <button
                type="button"
                data-testid="copilot-reject"
                onClick={reject}
                className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 active:scale-[0.97]"
              >
                Reject
              </button>
              {result.graph_diff.empty && (
                <span className="text-[11px] text-zinc-400">No changes proposed.</span>
              )}
            </div>
          </div>
        )}

        {/* ── Preview & iterate (execute → inspect → retry) ── */}
        <div className="border-t border-zinc-100 pt-3">
          <span className="text-xs font-semibold text-zinc-700">Preview &amp; iterate</span>
          <div className="mt-2 flex items-center gap-2">
            <input
              data-testid="copilot-preview-input"
              aria-label="Preview sample input"
              value={sampleInput}
              onChange={(e) => setSampleInput(e.target.value)}
              placeholder="Sample input"
              className="min-w-0 flex-1 rounded-md border border-zinc-200 bg-white px-2 py-1 text-xs text-zinc-800 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
            />
            <button
              type="button"
              data-testid="copilot-run-preview"
              disabled={preview.status === "running"}
              onClick={() => void runPreview()}
              className="shrink-0 rounded-md border border-zinc-200 px-2.5 py-1 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 disabled:opacity-40 active:scale-[0.97]"
            >
              {preview.status === "running" ? "Running…" : "▶ Run preview"}
            </button>
          </div>

          {preview.status === "error" && (
            <div
              data-testid="copilot-preview-error"
              className="mt-2 rounded-md border border-red-200 bg-red-50 px-2.5 py-1.5 text-xs text-red-700"
            >
              {preview.error}
            </div>
          )}

          {preview.status === "done" && (
            <div className="mt-2 space-y-1">
              <div data-testid="copilot-preview-output" className="text-[11px] text-zinc-500">
                Result: <span className="font-medium">{preview.result.status}</span>
              </div>
              {preview.result.steps.map((step) => {
                const problem = step.status !== "ok";
                return (
                  <div
                    key={step.node_id}
                    data-testid="copilot-preview-step"
                    className="flex items-center gap-2 rounded-md border border-zinc-100 px-2 py-1 text-[11px]"
                  >
                    <span
                      data-testid="copilot-preview-status"
                      className={`shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] font-semibold ${stepStatusStyle(step.status)}`}
                    >
                      {step.status}
                    </span>
                    <span className="shrink-0 font-medium text-zinc-700">{step.node_id}</span>
                    <span className="truncate text-zinc-400">{step.detail || step.output}</span>
                    {problem && (
                      <button
                        type="button"
                        data-testid="copilot-fix-step"
                        onClick={() => fixStep(step)}
                        className="ml-auto shrink-0 rounded border border-violet-200 px-1.5 py-0.5 text-[10px] font-medium text-violet-700 transition-colors hover:bg-violet-50"
                      >
                        Fix with copilot
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
