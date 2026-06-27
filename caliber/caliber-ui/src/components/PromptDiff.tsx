/**
 * PromptDiff — side-by-side prompt diff visualization.
 *
 * Renders a unified-diff style view comparing baseline (production) and
 * candidate prompt content. Inspired by registry-seeded prompt
 * versioning pattern where rules evolve incrementally.
 *
 * Pure component — no API calls, no state management. The parent
 * (ApprovalDetail) passes the two strings and this component renders
 * the diff with line-by-line highlighting.
 */

import { useMemo } from "react";

interface PromptDiffProps {
  baseline: string | null;
  candidate: string;
  /** Show side-by-side (default) or unified */
  mode?: "side-by-side" | "unified";
}

interface DiffLine {
  type: "added" | "removed" | "unchanged";
  content: string;
  lineNumber: { left: number | null; right: number | null };
}

function computeDiff(baseline: string, candidate: string): DiffLine[] {
  const baseLines = baseline.split("\n");
  const candLines = candidate.split("\n");

  // Simple LCS-based diff
  const m = baseLines.length;
  const n = candLines.length;
  const dp: number[][] = Array.from({ length: m + 1 }, () =>
    Array(n + 1).fill(0),
  );

  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      if (baseLines[i - 1] === candLines[j - 1]) {
        dp[i]![j] = (dp[i - 1]?.[j - 1] ?? 0) + 1;
      } else {
        dp[i]![j] = Math.max(dp[i - 1]?.[j] ?? 0, dp[i]?.[j - 1] ?? 0);
      }
    }
  }

  // Backtrack to produce diff
  const result: DiffLine[] = [];
  let i = m;
  let j = n;
  const stack: DiffLine[] = [];

  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && baseLines[i - 1] === candLines[j - 1]) {
      stack.push({
        type: "unchanged",
        content: baseLines[i - 1] ?? "",
        lineNumber: { left: i, right: j },
      });
      i--;
      j--;
    } else if (j > 0 && (i === 0 || (dp[i]?.[j - 1] ?? 0) >= (dp[i - 1]?.[j] ?? 0))) {
      stack.push({
        type: "added",
        content: candLines[j - 1] ?? "",
        lineNumber: { left: null, right: j },
      });
      j--;
    } else {
      stack.push({
        type: "removed",
        content: baseLines[i - 1] ?? "",
        lineNumber: { left: i, right: null },
      });
      i--;
    }
  }

  stack.reverse();
  result.push(...stack);
  return result;
}

const LINE_STYLES: Record<DiffLine["type"], string> = {
  added: "bg-green-50 text-green-800 border-l-2 border-green-400",
  removed: "bg-red-50 text-red-800 border-l-2 border-red-400",
  unchanged: "text-gray-700",
};

const LINE_PREFIX: Record<DiffLine["type"], string> = {
  added: "+",
  removed: "-",
  unchanged: " ",
};

export function PromptDiff({
  baseline,
  candidate,
  mode = "unified",
}: PromptDiffProps): JSX.Element {
  const diffLines = useMemo(
    () => computeDiff(baseline ?? "", candidate),
    [baseline, candidate],
  );

  const stats = useMemo(() => {
    let added = 0;
    let removed = 0;
    for (const line of diffLines) {
      if (line.type === "added") added++;
      if (line.type === "removed") removed++;
    }
    return { added, removed };
  }, [diffLines]);

  if (baseline === null) {
    return (
      <div className="rounded-md border border-surface-200 bg-surface-50 p-4">
        <div className="text-xs text-gray-500 mb-2">
          Cold start — no baseline to compare against.
        </div>
        <pre className="font-mono text-xs whitespace-pre-wrap text-gray-700">
          {candidate}
        </pre>
      </div>
    );
  }

  if (baseline === candidate) {
    return (
      <div className="rounded-md border border-surface-200 bg-surface-50 p-3 text-sm text-gray-500">
        No changes — candidate matches baseline.
      </div>
    );
  }

  if (mode === "side-by-side") {
    return (
      <SideBySideView
        diffLines={diffLines}
        baseline={baseline}
        candidate={candidate}
        stats={stats}
      />
    );
  }

  return (
    <div className="rounded-md border border-surface-200 overflow-hidden">
      <div className="flex items-center gap-3 px-3 py-2 bg-surface-50 border-b border-surface-200 text-xs text-gray-500">
        <span className="text-green-700 font-medium">+{stats.added}</span>
        <span className="text-red-700 font-medium">-{stats.removed}</span>
        <span>
          {diffLines.length} line{diffLines.length !== 1 ? "s" : ""}
        </span>
      </div>
      <div className="overflow-x-auto max-h-96 overflow-y-auto">
        <table className="w-full font-mono text-xs">
          <tbody>
            {diffLines.map((line, idx) => (
              <tr key={idx} className={LINE_STYLES[line.type]}>
                <td className="text-right text-gray-400 select-none px-2 py-0.5 w-8 border-r border-surface-100">
                  {line.lineNumber.left ?? ""}
                </td>
                <td className="text-right text-gray-400 select-none px-2 py-0.5 w-8 border-r border-surface-100">
                  {line.lineNumber.right ?? ""}
                </td>
                <td className="select-none px-1 py-0.5 w-4 text-center text-gray-400">
                  {LINE_PREFIX[line.type]}
                </td>
                <td className="px-2 py-0.5 whitespace-pre-wrap">
                  {line.content}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SideBySideView({
  diffLines,
  stats,
}: {
  diffLines: DiffLine[];
  baseline: string;
  candidate: string;
  stats: { added: number; removed: number };
}): JSX.Element {
  return (
    <div className="rounded-md border border-surface-200 overflow-hidden">
      <div className="flex items-center gap-3 px-3 py-2 bg-surface-50 border-b border-surface-200 text-xs text-gray-500">
        <span className="text-green-700 font-medium">+{stats.added}</span>
        <span className="text-red-700 font-medium">-{stats.removed}</span>
      </div>
      <div className="grid grid-cols-2 divide-x divide-surface-200 overflow-x-auto max-h-96 overflow-y-auto">
        <div>
          <div className="px-3 py-1.5 text-[10px] font-medium uppercase tracking-wide text-gray-500 bg-red-50/30 border-b border-surface-100">
            Baseline (production)
          </div>
          <div className="font-mono text-xs">
            {diffLines
              .filter((l) => l.type !== "added")
              .map((line, idx) => (
                <div
                  key={idx}
                  className={`px-3 py-0.5 ${
                    line.type === "removed"
                      ? "bg-red-50 text-red-800"
                      : "text-gray-700"
                  }`}
                >
                  <span className="inline-block w-6 text-right text-gray-400 select-none mr-2">
                    {line.lineNumber.left ?? ""}
                  </span>
                  <span className="whitespace-pre-wrap">{line.content}</span>
                </div>
              ))}
          </div>
        </div>
        <div>
          <div className="px-3 py-1.5 text-[10px] font-medium uppercase tracking-wide text-gray-500 bg-green-50/30 border-b border-surface-100">
            Candidate
          </div>
          <div className="font-mono text-xs">
            {diffLines
              .filter((l) => l.type !== "removed")
              .map((line, idx) => (
                <div
                  key={idx}
                  className={`px-3 py-0.5 ${
                    line.type === "added"
                      ? "bg-green-50 text-green-800"
                      : "text-gray-700"
                  }`}
                >
                  <span className="inline-block w-6 text-right text-gray-400 select-none mr-2">
                    {line.lineNumber.right ?? ""}
                  </span>
                  <span className="whitespace-pre-wrap">{line.content}</span>
                </div>
              ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// Exported for testing
export { computeDiff, type DiffLine };
