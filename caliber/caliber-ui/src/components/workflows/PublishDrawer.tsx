/**
 * Multi-step publish drawer — n8n-inspired clean overlay.
 *
 * Shadow-lg drawer with monochromatic stepper, zinc/indigo buttons, and
 * clean validation feedback. Step 1 (Validate): errors block Next, warnings
 * require ack. Step 2 (Review): change summary. Step 3 (Publish): immutable
 * version creation.
 */

import { useState } from "react";

import type { ValidationReport } from "@/api/workflowTypes";
import { SINGLE_ENVIRONMENT } from "@/lib/environment";

interface PublishDrawerProps {
  versionLabel: string;
  report: ValidationReport | null;
  changeSummary: string[];
  publishing: boolean;
  onValidate: () => void;
  onPublish: () => void;
  onClose: () => void;
}

export function PublishDrawer({
  versionLabel,
  report,
  changeSummary,
  publishing,
  onValidate,
  onPublish,
  onClose,
}: PublishDrawerProps): JSX.Element {
  const [step, setStep] = useState(1);
  const [acknowledged, setAcknowledged] = useState(false);

  const hasErrors = !report || report.errors.length > 0;
  const hasWarnings = (report?.warnings.length ?? 0) > 0;
  const canAdvanceFromValidate = !hasErrors && (!hasWarnings || acknowledged);

  return (
    <div
      data-testid="publish-drawer"
      className="absolute right-0 top-0 z-20 h-full w-96 overflow-y-auto border-l border-zinc-200 bg-white p-5 shadow-lg"
    >
      <div className="mb-4 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-zinc-900">Publish {versionLabel}</h3>
        <button
          type="button"
          data-testid="publish-close"
          onClick={onClose}
          className="flex h-6 w-6 items-center justify-center rounded-md text-zinc-400 transition-colors hover:bg-zinc-100 hover:text-zinc-600"
        >
          ✕
        </button>
      </div>

      {/* Stepper */}
      <ol className="mb-4 flex gap-1 text-xs">
        {["Validate", "Review", "Publish"].map((label, i) => (
          <li
            key={label}
            data-testid={`publish-step-${i + 1}`}
            className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 font-medium transition-colors ${
              step === i + 1
                ? "bg-zinc-900 text-white"
                : step > i + 1
                  ? "bg-zinc-100 text-zinc-600"
                  : "text-zinc-400"
            }`}
          >
            <span className="text-[10px]">{i + 1}</span> {label}
          </li>
        ))}
      </ol>

      {step === 1 && (
        <div data-testid="publish-validate" className="space-y-3">
          <button
            type="button"
            data-testid="publish-run-validate"
            onClick={onValidate}
            className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 active:scale-[0.97]"
          >
            Run Validation
          </button>
          {report ? (
            <ul className="space-y-1 text-xs">
              {report.errors.map((e, i) => (
                <li key={`e${i}`} className="flex items-center gap-1.5 text-red-600">
                  <span className="h-1.5 w-1.5 rounded-full bg-red-500" /> {e.message}
                </li>
              ))}
              {report.warnings.map((w, i) => (
                <li key={`w${i}`} className="flex items-center gap-1.5 text-amber-600">
                  <span className="h-1.5 w-1.5 rounded-full bg-amber-500" /> {w.message}
                </li>
              ))}
              {report.errors.length === 0 && report.warnings.length === 0 && (
                <li className="flex items-center gap-1.5 text-emerald-600">
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" /> No problems.
                </li>
              )}
            </ul>
          ) : (
            <div className="text-xs text-zinc-400">Run validation to continue.</div>
          )}
          {hasWarnings && !hasErrors && (
            <label className="flex items-center gap-2 text-xs text-zinc-700">
              <input
                type="checkbox"
                data-testid="publish-ack"
                checked={acknowledged}
                onChange={(e) => setAcknowledged(e.target.checked)}
                className="rounded border-zinc-300"
              />
              Acknowledge warnings
            </label>
          )}
          <div className="flex justify-end">
            <button
              type="button"
              data-testid="publish-next-1"
              disabled={!canAdvanceFromValidate}
              onClick={() => setStep(2)}
              className="rounded-lg bg-zinc-900 px-4 py-1.5 text-xs font-medium text-white transition-colors hover:bg-zinc-800 disabled:opacity-40 active:scale-[0.97]"
            >
              Next →
            </button>
          </div>
        </div>
      )}

      {step === 2 && (
        <div data-testid="publish-review" className="space-y-3">
          <div className="text-xs text-zinc-500">Changes since the previous version:</div>
          <ul className="space-y-1 text-xs">
            {changeSummary.length === 0 ? (
              <li className="text-zinc-400">First version.</li>
            ) : (
              changeSummary.map((c, i) => (
                <li key={i} className="flex items-center gap-1.5 text-zinc-700">
                  <span className="h-1 w-1 rounded-full bg-zinc-400" /> {c}
                </li>
              ))
            )}
          </ul>
          <div className="flex justify-between">
            <button
              type="button"
              data-testid="publish-back-2"
              onClick={() => setStep(1)}
              className="rounded-lg border border-zinc-200 px-3 py-1.5 text-xs font-medium text-zinc-700 transition-colors hover:bg-zinc-50 active:scale-[0.97]"
            >
              ← Back
            </button>
            <button
              type="button"
              data-testid="publish-next-2"
              onClick={() => setStep(3)}
              className="rounded-lg bg-zinc-900 px-4 py-1.5 text-xs font-medium text-white transition-colors hover:bg-zinc-800 active:scale-[0.97]"
            >
              Next →
            </button>
          </div>
        </div>
      )}

      {step === 3 && (
        <div data-testid="publish-finalize" className="space-y-3">
          <div className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5 text-xs text-zinc-500">
            {SINGLE_ENVIRONMENT
              ? "Publishing creates an immutable version, then deploys it live. A deploy gate can run as an optional check — no separate approval step is required."
              : "Publishing creates an immutable version. Deploy to prod is a separate, gated promotion that requires a passing deploy gate + approval."}
          </div>
          <button
            type="button"
            data-testid="publish-confirm"
            disabled={publishing}
            onClick={onPublish}
            className="w-full rounded-lg bg-zinc-900 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-zinc-800 disabled:opacity-40 active:scale-[0.97]"
          >
            {publishing ? "Publishing…" : "Publish ↑"}
          </button>
        </div>
      )}
    </div>
  );
}
