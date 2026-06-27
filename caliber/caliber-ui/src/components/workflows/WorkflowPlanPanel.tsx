/**
 * Plan-to-build surface (the "Plan" view tab).
 *
 * The user describes, in plain language, the workflow they want; the panel
 * calls `…/plan-build` (the blank-slate sibling of the copilot's
 * `…/copilot-edit`: the backend *authors* a full manifest toward the goal,
 * grounded in the registry) and renders the base→proposed change as a
 * {@link GraphDiff} with Accept / Reject. Accepting drops the authored graph
 * onto the canvas via `onApply` — nothing clobbers the canvas without the diff
 * being shown first, and nothing persists until Save.
 *
 * Division of labour: Plan builds the scaffold from a goal; the in-canvas
 * Copilot refines an existing graph. Plan shines on an empty/new workflow; on a
 * populated one the diff makes clear what's being added/replaced.
 *
 * With the default `fake` provider the proposal echoes the manifest back
 * unchanged (an empty diff), so the tab is safe without an LLM configured —
 * Accept is disabled when there is nothing to apply.
 */

import { useState } from "react";

import { caliberApi } from "@/api/caliberApi";
import type { CopilotEditResult, WorkflowManifest } from "@/api/workflowTypes";

import { GraphDiff } from "./GraphDiff";

interface WorkflowPlanPanelProps {
  versionId: string;
  /** The current (possibly unsaved) canvas manifest — the diff base. */
  manifest: WorkflowManifest;
  /** Apply an accepted authored manifest to the canvas. */
  onApply: (manifest: WorkflowManifest) => void;
}

type Phase =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "result"; result: CopilotEditResult }
  | { status: "error"; error: string };

const EXAMPLES = [
  "Classify a support ticket, draft a reply, then run a PII guardrail before output",
  "Summarize an uploaded PDF, extract named entities, and return them as JSON",
  "Answer a question using the policy lookup tool, then have a judge score the answer",
];

export function WorkflowPlanPanel({
  versionId,
  manifest,
  onApply,
}: WorkflowPlanPanelProps): JSX.Element {
  const [goal, setGoal] = useState("");
  const [phase, setPhase] = useState<Phase>({ status: "idle" });

  // The canvas already has authored nodes beyond an empty/seed graph — building
  // will propose a replacement, surfaced as a diff (never a silent clobber).
  const nodeCount = Object.keys(manifest.nodes ?? {}).length;
  const hasExisting = nodeCount > 1;

  async function build(): Promise<void> {
    const trimmed = goal.trim();
    if (!trimmed) return;
    setPhase({ status: "loading" });
    try {
      const result = await caliberApi.planBuildWorkflow(versionId, { goal: trimmed, manifest });
      setPhase({ status: "result", result });
    } catch (e) {
      setPhase({ status: "error", error: e instanceof Error ? e.message : "plan-build failed" });
    }
  }

  function accept(): void {
    if (phase.status !== "result") return;
    onApply(phase.result.proposed_manifest);
    setPhase({ status: "idle" });
    setGoal("");
  }

  function reject(): void {
    // Keep the goal so the user can refine and rebuild.
    setPhase({ status: "idle" });
  }

  const result = phase.status === "result" ? phase.result : null;
  const errorCount = result ? (result.report?.errors?.length ?? 0) : 0;

  return (
    <div
      data-testid="plan-panel"
      className="flex h-full w-full flex-col items-center overflow-auto bg-zinc-50/80 p-8"
    >
      <div className="w-full max-w-2xl space-y-4">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-lg font-semibold text-zinc-900">Plan a workflow</span>
            <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-medium text-violet-700">
              beta
            </span>
          </div>
          <p className="mt-1 text-sm text-zinc-500">
            Describe what you want the workflow to do. Plan authors a starting graph from your goal —
            then refine it on the canvas or with Copilot.
          </p>
        </div>

        <textarea
          data-testid="plan-input"
          aria-label="Describe the workflow to build"
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void build();
          }}
          placeholder="e.g. classify a support ticket, draft a reply, then guard the output for PII"
          rows={4}
          className="w-full resize-none rounded-lg border border-zinc-200 bg-white p-3 text-sm text-zinc-800 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900"
        />

        {phase.status === "idle" && (
          <div className="flex flex-wrap gap-1.5">
            {EXAMPLES.map((s) => (
              <button
                key={s}
                type="button"
                data-testid="plan-example"
                onClick={() => setGoal(s)}
                className="rounded-full border border-zinc-200 bg-white px-2.5 py-1 text-[11px] text-zinc-600 transition-colors hover:bg-zinc-50"
              >
                {s}
              </button>
            ))}
          </div>
        )}

        <div className="flex items-center gap-3">
          <button
            type="button"
            data-testid="plan-submit"
            disabled={!goal.trim() || phase.status === "loading"}
            onClick={() => void build()}
            className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-zinc-800 disabled:opacity-40 active:scale-[0.97]"
          >
            {phase.status === "loading" ? "Building…" : "✨ Build it"}
          </button>
          {hasExisting && phase.status === "idle" && (
            <span data-testid="plan-replace-note" className="text-[11px] text-amber-600">
              This canvas already has nodes — building proposes a replacement (shown as a diff).
            </span>
          )}
        </div>

        {phase.status === "error" && (
          <div
            data-testid="plan-error"
            className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
          >
            {phase.error}
          </div>
        )}

        {result && (
          <div className="space-y-3 rounded-lg border border-zinc-200 bg-white p-4">
            <div>
              <p data-testid="plan-summary" className="text-sm font-medium text-zinc-900">
                {result.summary || "Proposed workflow"}
              </p>
              {result.rationale && <p className="mt-1 text-xs text-zinc-500">{result.rationale}</p>}
            </div>

            {!result.valid && (
              <div
                data-testid="plan-invalid"
                className="rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-xs text-amber-700"
              >
                ⚠ The proposal has {errorCount} validation {errorCount === 1 ? "error" : "errors"}.
                Review before applying.
              </div>
            )}

            <GraphDiff diff={result.graph_diff} />

            <div data-testid="plan-grounding" className="text-[11px] text-zinc-400">
              Grounded in {result.grounding.tools.length} tools ·{" "}
              {result.grounding.skills.length} skills ·{" "}
              {result.grounding.eval_datasets.length} datasets
            </div>

            <div className="flex items-center gap-2">
              <button
                type="button"
                data-testid="plan-accept"
                disabled={result.graph_diff.empty}
                onClick={accept}
                title={result.graph_diff.empty ? "No changes to apply" : "Apply to canvas"}
                className="rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-emerald-700 disabled:opacity-40 active:scale-[0.97]"
              >
                Accept & build
              </button>
              <button
                type="button"
                data-testid="plan-reject"
                onClick={reject}
                className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 active:scale-[0.97]"
              >
                Reject
              </button>
              {result.graph_diff.empty && (
                <span data-testid="plan-empty-note" className="text-[11px] text-zinc-400">
                  No workflow proposed.
                </span>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
