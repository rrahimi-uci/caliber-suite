/**
 * EvaluationDetail — the scorecard for one evaluation run.
 *
 * Shows the aggregate (overall + per-grader means + pass rate), an optional
 * delta vs. a baseline run on the same dataset, and the full per-example table
 * (input → prediction vs. expected, with per-grader scores and pass/fail).
 */

import { useCallback, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, FlaskConical } from "lucide-react";

import { caliberApi } from "@/api/caliberApi";
import type { EvalRun, EvalRunSummary, Judge } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { useApi } from "@/hooks/useApi";
import { relativeTime } from "@/lib/time";

import { fmtPct, scorerLabel } from "./Evaluations";

function tone(n: number | null | undefined): string {
  if (n === null || n === undefined) return "text-slate-400";
  if (n >= 0.8) return "text-emerald-600";
  if (n >= 0.5) return "text-amber-600";
  return "text-red-600";
}

function previewValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

// Renders a score delta vs. the baseline run as percentage points (pp). `delta`
// is a difference of 0–1 scores; we display it rounded to whole percent.
function DeltaChip({ delta }: { delta: number }): JSX.Element | null {
  // Below half a percentage point it rounds to 0 — show a neutral "±0" rather
  // than a misleading "+0pp"/"-0pp".
  if (Math.abs(delta) < 0.005) {
    return <span className="text-[11px] font-medium text-slate-400">±0</span>;
  }
  const up = delta > 0;
  return (
    <span
      className={`text-[11px] font-semibold ${up ? "text-emerald-600" : "text-red-600"}`}
      title="vs. baseline run"
    >
      {up ? "+" : ""}
      {Math.round(delta * 100)}pp
    </span>
  );
}

export function EvaluationDetail(): JSX.Element {
  const { runId = "" } = useParams();
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.getEvaluation(runId, signal),
    [runId],
  );
  const { data: run, error, loading } = useApi<EvalRun>(fetcher, [runId]);

  const [baselineId, setBaselineId] = useState("");
  const otherRunsFetcher = useCallback(
    (signal: AbortSignal) =>
      run ? caliberApi.listEvaluations(run.dataset_id, signal) : Promise.resolve([]),
    [run],
  );
  const { data: otherRuns } = useApi<EvalRunSummary[]>(otherRunsFetcher, [run?.dataset_id]);
  const baselineFetcher = useCallback(
    (signal: AbortSignal) =>
      baselineId ? caliberApi.getEvaluation(baselineId, signal) : Promise.resolve(null),
    [baselineId],
  );
  const { data: baseline } = useApi<EvalRun | null>(baselineFetcher, [baselineId]);

  // Resolve ``Judge.<id>`` scorer columns to readable judge names.
  const judgesFetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listJudges({ status: "all" }, signal),
    [],
  );
  const { data: judges } = useApi<Judge[]>(judgesFetcher, []);
  const judgeNames = useMemo(
    () => Object.fromEntries((judges ?? []).map((j) => [j.judge_id, j.name])),
    [judges],
  );

  const scorerKeys = useMemo(() => run?.scorers ?? [], [run]);

  if (loading && !run) {
    return <div className="px-2 py-12 text-center text-sm text-slate-400">Loading run…</div>;
  }
  if (error) {
    return (
      <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
        {error.message}
      </div>
    );
  }
  if (!run) {
    return <div className="px-2 py-12 text-center text-sm text-slate-400">Run not found.</div>;
  }

  const delta = (key: string, value: number | null | undefined): number | null => {
    if (!baseline || value === null || value === undefined) return null;
    const base = key === "overall" ? baseline.overall_score : baseline.aggregate[key];
    if (base === null || base === undefined) return null;
    return value - base;
  };

  return (
    <div className="space-y-5">
      <Link
        to="/evaluations"
        className="inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-700"
      >
        <ArrowLeft className="h-4 w-4" />
        Evaluations
      </Link>

      <PageHeader title={run.label || run.run_id} subtitle="" />

      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <Link
          to={`/eval-datasets/${run.dataset_id}`}
          className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 hover:bg-slate-100"
        >
          <FlaskConical className="h-3 w-3" />
          <span className="font-mono">{run.dataset_id}</span> · v{run.dataset_version}
        </Link>
        {run.model ? <Chip>{run.model}</Chip> : null}
        <Chip>
          {run.predict_target}
          {run.subject_ref ? `: ${run.subject_ref}` : ""}
        </Chip>
        <Chip>graders: {run.scorers.map((s) => scorerLabel(s, judgeNames)).join(", ")}</Chip>
        <Chip>pass ≥ {fmtPct(run.pass_threshold)}</Chip>
        <span>{relativeTime(run.created_at)}</span>
      </div>

      {run.status === "failed" && (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          This run failed: {run.error_message || "every example errored."}
        </div>
      )}

      {/* Compare-to */}
      <div className="flex items-center gap-2">
        <label htmlFor="eval-baseline" className="text-xs font-semibold text-slate-500">
          Compare to
        </label>
        <select
          id="eval-baseline"
          aria-label="Compare to baseline run"
          value={baselineId}
          onChange={(e) => setBaselineId(e.target.value)}
          className="rounded-xl border border-slate-200 bg-white px-2 py-1 text-xs text-slate-900 outline-none focus:border-caliber-400 focus:ring-2 focus:ring-caliber-400/20"
        >
          <option value="">No baseline</option>
          {(otherRuns ?? [])
            .filter((r) => r.run_id !== run.run_id)
            .map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {r.label || r.run_id} ({fmtPct(r.overall_score)})
              </option>
            ))}
        </select>
      </div>

      {/* Aggregate cards */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4" data-testid="eval-aggregate">
        <ScoreCard
          label="Overall"
          value={fmtPct(run.overall_score)}
          valueTone={tone(run.overall_score)}
          delta={delta("overall", run.overall_score)}
        />
        <ScoreCard
          label="Pass rate"
          value={fmtPct(run.pass_rate)}
          valueTone={tone(run.pass_rate)}
          hint={`${run.passed_count}/${run.n_examples} passed`}
        />
        {scorerKeys.map((key) => (
          <ScoreCard
            key={key}
            label={scorerLabel(key, judgeNames)}
            value={fmtPct(run.aggregate[key])}
            valueTone={tone(run.aggregate[key])}
            delta={delta(key, run.aggregate[key])}
          />
        ))}
      </div>

      {/* Per-example scorecard */}
      <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
              <th className="px-4 py-3 text-left font-medium">Input</th>
              <th className="px-4 py-3 text-left font-medium">Expected</th>
              <th className="px-4 py-3 text-left font-medium">Prediction</th>
              {scorerKeys.map((key) => (
                <th key={key} className="px-3 py-3 text-left font-medium">
                  {scorerLabel(key)}
                </th>
              ))}
              <th className="px-4 py-3 text-left font-medium">Result</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {run.results.length === 0 && (
              <tr>
                <td
                  colSpan={4 + scorerKeys.length}
                  className="px-4 py-10 text-center text-sm text-slate-500"
                >
                  No example results.
                </td>
              </tr>
            )}
            {run.results.map((row) => (
              <tr key={row.example_id} className="align-top hover:bg-slate-50" data-testid="eval-result-row">
                <td className="max-w-[16rem] px-4 py-3 text-xs text-slate-700">
                  <div className="line-clamp-3 whitespace-pre-wrap break-words">
                    {previewValue(row.input.input ?? row.input)}
                  </div>
                </td>
                <td className="max-w-[12rem] px-4 py-3 text-xs text-slate-600">
                  <div className="line-clamp-3 whitespace-pre-wrap break-words">
                    {previewValue(row.expected.expected ?? row.expected)}
                  </div>
                </td>
                <td className="max-w-[16rem] px-4 py-3 text-xs text-slate-700">
                  {row.error ? (
                    <span className="text-red-600">error: {row.error}</span>
                  ) : (
                    <div className="line-clamp-3 whitespace-pre-wrap break-words">
                      {row.prediction}
                    </div>
                  )}
                </td>
                {scorerKeys.map((key) => (
                  <td key={key} className={`px-3 py-3 tabular-nums ${tone(row.scores[key])}`}>
                    {row.scores[key] === undefined ? "—" : fmtPct(row.scores[key])}
                  </td>
                ))}
                <td className="px-4 py-3">
                  {row.passed ? (
                    <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-emerald-700">
                      Pass
                    </span>
                  ) : (
                    <span className="rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-red-700">
                      Fail
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Chip({ children }: { children: React.ReactNode }): JSX.Element {
  return (
    <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-slate-600">
      {children}
    </span>
  );
}

function ScoreCard({
  label,
  value,
  valueTone,
  hint,
  delta,
}: {
  label: string;
  value: string;
  valueTone: string;
  hint?: string;
  delta?: number | null;
}): JSX.Element {
  return (
    <div className="rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-2">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-0.5 flex items-baseline gap-1.5">
        <span className={`text-lg font-semibold tabular-nums ${valueTone}`}>{value}</span>
        {delta !== null && delta !== undefined ? <DeltaChip delta={delta} /> : null}
      </div>
      {hint ? <div className="text-[10px] text-slate-400">{hint}</div> : null}
    </div>
  );
}
