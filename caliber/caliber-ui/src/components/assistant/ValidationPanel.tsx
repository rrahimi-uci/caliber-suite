/* ValidationPanel — renders a validation report. */

import type { ValidationReport } from "@/api/assistantTypes";

interface Props {
  report: ValidationReport | null;
}

export function ValidationPanel({ report }: Props): JSX.Element {
  if (!report) {
    return (
      <p className="text-sm text-slate-400 italic">
        No validation report yet. Click &quot;Validate&quot; to run checks.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        {report.valid ? (
          <span className="inline-flex items-center gap-1 text-sm font-medium text-green-700">
            <svg className="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
              <path
                fillRule="evenodd"
                d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z"
                clipRule="evenodd"
              />
            </svg>
            Valid
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 text-sm font-medium text-red-700">
            <svg className="w-4 h-4" viewBox="0 0 20 20" fill="currentColor">
              <path
                fillRule="evenodd"
                d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z"
                clipRule="evenodd"
              />
            </svg>
            Invalid
          </span>
        )}
      </div>

      {report.errors.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-semibold text-red-600 uppercase tracking-wide">Errors</p>
          <ul className="space-y-1">
            {report.errors.map((e, i) => (
              <li key={i} className="text-sm text-red-700 flex gap-1.5">
                <span className="text-red-400 mt-0.5">•</span>
                {e}
              </li>
            ))}
          </ul>
        </div>
      )}

      {report.warnings.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-semibold text-amber-600 uppercase tracking-wide">Warnings</p>
          <ul className="space-y-1">
            {report.warnings.map((w, i) => (
              <li key={i} className="text-sm text-amber-700 flex gap-1.5">
                <span className="text-amber-400 mt-0.5">•</span>
                {w}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
