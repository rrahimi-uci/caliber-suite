/**
 * Problems panel — n8n-inspired validation feedback.
 *
 * Monochromatic design with severity-colored indicators. Renders errors
 * (block publish), warnings (require ack), and info hints. Clicking an
 * issue with a node path focuses that node on the canvas.
 */

import type { ValidationReport } from "@/api/workflowTypes";

export interface ProblemFocusTarget {
  nodeId: string;
  fieldKey: string | null;
  code: string;
  path: string;
}

interface ProblemsPanelProps {
  report: ValidationReport | null;
  onFocusNode?: (nodeId: string) => void;
  onFocusIssue?: (target: ProblemFocusTarget) => void;
}

const SEVERITY_STYLE: Record<string, string> = {
  error: "text-red-600",
  warning: "text-amber-600",
  info: "text-caliber-600",
};

const SEVERITY_DOT: Record<string, string> = {
  error: "bg-red-500",
  warning: "bg-amber-500",
  info: "bg-caliber-500",
};

function nodeIdFromPath(path: string): string | null {
  const match = /^nodes\.([^.]+)/.exec(path);
  return match?.[1] ?? null;
}

function fieldKeyFromPath(path: string): string | null {
  const match = /^nodes\.[^.]+\.([^.[]+)/.exec(path);
  return match?.[1] ?? null;
}

export function ProblemsPanel({
  report,
  onFocusNode,
  onFocusIssue,
}: ProblemsPanelProps): JSX.Element {
  const issues = report ? [...report.errors, ...report.warnings] : [];

  return (
    <div data-testid="wf-problems" className="border-t border-zinc-200 bg-zinc-50 px-3 py-2">
      <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-zinc-400">
        Problems
        {report && (
          <span data-testid="wf-problems-count" className="normal-case tracking-normal text-zinc-400">
            ({report.errors.length} error{report.errors.length === 1 ? "" : "s"},{" "}
            {report.warnings.length} warning{report.warnings.length === 1 ? "" : "s"})
          </span>
        )}
      </div>
      {issues.length === 0 ? (
        <div className="mt-1 text-xs text-zinc-400">
          {report ? "No problems — workflow is valid." : "Run Validate to check this workflow."}
        </div>
      ) : (
        <ul className="mt-1.5 space-y-1">
          {issues.map((issue, idx) => {
            const nodeId = nodeIdFromPath(issue.path);
            const fieldKey = fieldKeyFromPath(issue.path);
            return (
              <li key={`${issue.code}-${idx}`}>
                <button
                  type="button"
                  disabled={!nodeId}
                  onClick={() => {
                    if (!nodeId) return;
                    if (onFocusIssue) {
                      onFocusIssue({
                        nodeId,
                        fieldKey,
                        code: issue.code,
                        path: issue.path,
                      });
                      return;
                    }
                    onFocusNode?.(nodeId);
                  }}
                  data-testid={`problem-${issue.code}`}
                  className={`flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-xs transition-colors ${
                    nodeId ? "hover:bg-zinc-100" : "cursor-default"
                  } ${SEVERITY_STYLE[issue.severity] ?? "text-zinc-600"}`}
                >
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${SEVERITY_DOT[issue.severity] ?? "bg-zinc-400"}`} aria-hidden />
                  <span>{issue.message}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
