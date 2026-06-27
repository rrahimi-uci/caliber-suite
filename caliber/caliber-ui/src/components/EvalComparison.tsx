/**
 * Side-by-side candidate vs. baseline eval scores + per-dimension delta
 * pills + gate verdict. Used by both the Job Detail and Approval Detail
 * pages.
 *
 * Accepts the raw `eval_results` JSON from the backend so callers don't
 * have to pre-shape it. Unknown / missing fields render as "—" rather
 * than blowing up.
 */

type EvalSide = {
  overall?: number;
  dimensions?: Record<string, number>;
};

interface EvalComparisonProps {
  results: Record<string, unknown> | null | undefined;
}

export function EvalComparison({ results }: EvalComparisonProps): JSX.Element {
  if (!results) {
    return (
      <div className="text-sm text-gray-500">No eval results recorded.</div>
    );
  }

  const candidate = (results["candidate"] as EvalSide | undefined) ?? {};
  const baseline = (results["baseline"] as EvalSide | null | undefined) ?? null;
  const deltas = (results["deltas"] as Record<string, number> | undefined) ?? {};
  const gate =
    (results["gate"] as { passed?: boolean; reasons?: string[] } | undefined) ?? {};
  const tags = (results["caliber_tags"] as Record<string, unknown> | undefined) ?? {};
  const nExamples = typeof results["n_examples"] === "number" ? (results["n_examples"] as number) : null;
  const datasetId =
    typeof results["eval_dataset_id"] === "string"
      ? (results["eval_dataset_id"] as string)
      : null;

  return (
    <>
      <div className="grid grid-cols-2 gap-4 mb-4">
        <ScoreBlock label="Candidate" overall={candidate.overall} dimensions={candidate.dimensions} />
        {baseline ? (
          <ScoreBlock
            label="Baseline"
            overall={baseline.overall}
            dimensions={baseline.dimensions}
            tone="muted"
          />
        ) : (
          <div className="rounded-md border border-surface-200 p-3 text-xs text-gray-500">
            <div className="uppercase tracking-wide text-gray-400 mb-1">Baseline</div>
            Cold start — no baseline to compare against.
          </div>
        )}
      </div>

      {Object.keys(deltas).length > 0 && (
        <div className="flex flex-wrap gap-2 mb-3">
          {Object.entries(deltas).map(([key, value]) => (
            <DeltaPill key={key} dim={key} value={value} />
          ))}
        </div>
      )}

      <GateBanner passed={gate.passed} reasons={gate.reasons} />

      {(nExamples !== null || datasetId) && (
        <div className="mt-3 text-xs text-gray-500 flex flex-wrap gap-x-4">
          {nExamples !== null && (
            <span>
              <span className="text-gray-400">N=</span>
              {nExamples} examples
            </span>
          )}
          {datasetId && (
            <span>
              <span className="text-gray-400">Dataset:</span>{" "}
              <span className="font-mono">{datasetId}</span>
            </span>
          )}
        </div>
      )}

      {Object.keys(tags).length > 0 && (
        <div className="mt-3 pt-3 border-t border-surface-100 text-xs text-gray-500 grid grid-cols-2 gap-x-4 gap-y-1">
          {Object.entries(tags).map(([k, v]) => (
            <div key={k} className="truncate">
              <span className="font-mono text-gray-700">{k}</span>:{" "}
              <span className="font-mono text-gray-900">{String(v)}</span>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

function ScoreBlock({
  label,
  overall,
  dimensions,
  tone = "primary",
}: {
  label: string;
  overall: number | undefined;
  dimensions: Record<string, number> | undefined;
  tone?: "primary" | "muted";
}): JSX.Element {
  const headerClass = tone === "muted" ? "text-gray-400" : "text-gray-500";
  return (
    <div className="rounded-md border border-surface-200 p-3">
      <div className={`uppercase tracking-wide text-xs ${headerClass} mb-1`}>{label}</div>
      <div className="text-2xl font-bold text-gray-900">
        {overall !== undefined ? overall.toFixed(3) : "—"}
      </div>
      {dimensions && Object.keys(dimensions).length > 0 && (
        <div className="mt-2 space-y-0.5">
          {Object.entries(dimensions).map(([k, v]) => (
            <div key={k} className="text-xs text-gray-600 flex justify-between">
              <span className="text-gray-500">{k}</span>
              <span className="font-mono">{v.toFixed(3)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function DeltaPill({ dim, value }: { dim: string; value: number }): JSX.Element {
  const positive = value > 0;
  const negative = value < 0;
  const tone = positive
    ? "bg-green-50 text-green-700 border-green-200"
    : negative
      ? "bg-red-50 text-red-700 border-red-200"
      : "bg-gray-50 text-gray-700 border-gray-200";
  const sign = positive ? "+" : "";
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-mono border ${tone}`}
    >
      <span className="text-[10px] uppercase tracking-wide mr-1 opacity-80">{dim}</span>
      {sign}
      {(value * 100).toFixed(1)}pp
    </span>
  );
}

function GateBanner({
  passed,
  reasons,
}: {
  passed: boolean | undefined;
  reasons: string[] | undefined;
}): JSX.Element {
  if (passed === undefined) {
    return (
      <div className="rounded-md border border-surface-200 bg-surface-50 px-3 py-2 text-xs text-gray-600">
        Gate decision pending.
      </div>
    );
  }
  if (passed) {
    return (
      <div className="rounded-md border border-green-200 bg-green-50 px-3 py-2 text-xs text-green-800">
        <span className="font-medium">Gate passed</span>
        {" — "}eligible for approval.
      </div>
    );
  }
  return (
    <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800">
      <div className="font-medium">Gate failed</div>
      {reasons && reasons.length > 0 && (
        <ul className="mt-1 list-disc list-inside space-y-0.5">
          {reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
