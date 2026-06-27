/* TestPanel — renders a test report for a draft. */

import type { TestReport } from "@/api/assistantTypes";

interface Props {
  report: TestReport | null;
}

export function TestPanel({ report }: Props): JSX.Element {
  if (!report) {
    return (
      <p className="text-sm text-slate-400 italic">
        No test report yet. Click &quot;Test&quot; to run draft tests.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        {report.passed ? (
          <span className="text-sm font-medium text-green-700">All tests passed</span>
        ) : (
          <span className="text-sm font-medium text-red-700">
            {report.failures}/{report.total} failed
          </span>
        )}
      </div>

      {report.error && (
        <div className="text-sm text-red-600 bg-red-50 rounded p-2 border border-red-200">
          {report.error}
        </div>
      )}

      {report.details.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide">Details</p>
          <ul className="space-y-1">
            {report.details.map((d, i) => (
              <li key={i} className="text-xs text-slate-600 flex items-center gap-1.5">
                {(d as Record<string, unknown>).passed ? (
                  <span className="text-green-500">✓</span>
                ) : (
                  <span className="text-red-500">✗</span>
                )}
                <span>{String((d as Record<string, unknown>).test ?? `Test ${i + 1}`)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
