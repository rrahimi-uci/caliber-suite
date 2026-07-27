/**
 * EvaluationDetail — the scorecard for one evaluation run.
 *
 * Shows the aggregate (overall + per-grader means + pass rate), an optional
 * delta vs. a baseline run on the same dataset, and the full per-example table
 * (input → prediction vs. expected, with per-grader scores and pass/fail).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
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

function sameScorerSuite(left: string[], right: string[]): boolean {
  if (left.length !== right.length) return false;
  const sortedLeft = [...left].sort();
  const sortedRight = [...right].sort();
  return sortedLeft.every((value, index) => value === sortedRight[index]);
}

function fmtWeight(value: number): string {
  return value.toFixed(2);
}

function comparisonValue(value: string | null | undefined): string {
  return value?.trim() || "(none)";
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

  // A displayed delta is only controlled when both runs use the same dataset
  // snapshot and scorer suite. Subject/model/target changes are allowed because
  // they are often the variable under test, but are disclosed after selection.
  const compatibleBaselines = useMemo(
    () =>
      (otherRuns ?? []).filter(
        (candidate) =>
          candidate.run_id !== run?.run_id &&
          candidate.dataset_version === run?.dataset_version &&
          candidate.status === "completed" &&
          candidate.overall_score !== null &&
          sameScorerSuite(candidate.scorers, run?.scorers ?? []),
      ),
    [otherRuns, run],
  );
  const selectedBaselineSummary = useMemo(
    () => compatibleBaselines.find((candidate) => candidate.run_id === baselineId) ?? null,
    [baselineId, compatibleBaselines],
  );

  useEffect(() => {
    if (baselineId && !compatibleBaselines.some((candidate) => candidate.run_id === baselineId)) {
      setBaselineId("");
    }
  }, [baselineId, compatibleBaselines]);

  // Aggregate / overall / pass-rate are weighted means over the dataset's
  // example weights. Surface the column only when the dataset actually uses
  // non-default weights, so the headline numbers stay explicable without
  // adding noise to the common unweighted case.
  const isWeighted = useMemo(
    () => (run?.results ?? []).some((row) => (row.weight ?? 1) !== 1),
    [run],
  );
  const totalWeight = useMemo(
    () => (run?.results ?? []).reduce((sum, row) => sum + (row.weight ?? 1), 0),
    [run],
  );
  const passedWeight = useMemo(
    () =>
      (run?.results ?? []).reduce(
        (sum, row) => sum + (row.passed ? (row.weight ?? 1) : 0),
        0,
      ),
    [run],
  );
  const allZeroWeightFallback = Boolean(run?.results.length) && totalWeight === 0;
  const usesWeightedMetrics = isWeighted && !allZeroWeightFallback;
  const hasIncompleteRows = (run?.results ?? []).some((row) => Boolean(row.error));
  const scorerCoverage = useMemo(
    () =>
      Object.fromEntries(
        scorerKeys.map((key) => {
          const coveredRows = (run?.results ?? []).filter(
            (row) => !row.error && row.scores[key] !== undefined,
          );
          return [
            key,
            {
              rows: coveredRows.length,
              weight: coveredRows.reduce((sum, row) => sum + (row.weight ?? 1), 0),
            },
          ];
        }),
      ) as Record<string, { rows: number; weight: number }>,
    [run, scorerKeys],
  );

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

      {hasIncompleteRows && (
        <div
          data-testid="eval-incomplete-warning"
          className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800"
        >
          Some rows have incomplete scorer evidence. Surviving raw scorer values remain visible
          for diagnosis but do not enter per-scorer aggregates; overall and pass-rate metrics
          conservatively count each error-bearing row as zero, and the row cannot pass.
        </div>
      )}

      {allZeroWeightFallback && (
        <div
          data-testid="eval-zero-weight-warning"
          className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800"
        >
          Every stored row weight is 0.00. This legacy run predates zero-total validation and used
          an equal-row fallback for its displayed metrics, so the zero weights did not exclude
          these rows.
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
          {compatibleBaselines.map((r) => (
            <option key={r.run_id} value={r.run_id}>
              {r.label || r.run_id} ({fmtPct(r.overall_score)}) · {r.predict_target}
              {r.subject_ref ? `: ${r.subject_ref}` : ""} · {r.model || "default model"}
            </option>
          ))}
        </select>
      </div>

      {selectedBaselineSummary && (
        <div
          data-testid="baseline-comparison-context"
          className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600"
        >
          <span className="font-semibold text-slate-700">
            Dataset/scorer-compatible comparison:
          </span>{" "}
          dataset v{run.dataset_version} and grader suite match; target, subject, and model are
          disclosed rather than matched. Current: target{" "}
          {comparisonValue(run.predict_target)}, subject {comparisonValue(run.subject_ref)}, model{" "}
          {comparisonValue(run.model)}. Baseline: target{" "}
          {comparisonValue(selectedBaselineSummary.predict_target)}, subject{" "}
          {comparisonValue(selectedBaselineSummary.subject_ref)}, model{" "}
          {comparisonValue(selectedBaselineSummary.model)}.
        </div>
      )}

      {/* Aggregate cards */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4" data-testid="eval-aggregate">
        <ScoreCard
          label="Overall"
          value={fmtPct(run.overall_score)}
          valueTone={tone(run.overall_score)}
          delta={delta("overall", run.overall_score)}
          hint={
            usesWeightedMetrics
              ? `Weighted mean · ${fmtWeight(totalWeight)} total weight`
              : allZeroWeightFallback
                ? "Equal-row fallback · stored total weight 0.00"
                : "Unweighted row mean"
          }
        />
        <ScoreCard
          label={usesWeightedMetrics ? "Weighted pass rate" : "Pass rate"}
          value={fmtPct(run.pass_rate)}
          valueTone={tone(run.pass_rate)}
          hint={
            usesWeightedMetrics
              ? `${run.passed_count}/${run.n_examples} raw rows · ${fmtWeight(passedWeight)}/${fmtWeight(totalWeight)} passing weight`
              : `${run.passed_count}/${run.n_examples} rows passed${allZeroWeightFallback ? " · equal-row fallback" : ""}`
          }
        />
        {scorerKeys.map((key) => (
          <ScoreCard
            key={key}
            label={scorerLabel(key, judgeNames)}
            value={fmtPct(run.aggregate[key])}
            valueTone={tone(run.aggregate[key])}
            delta={delta(key, run.aggregate[key])}
            hint={`valid ${scorerCoverage[key]?.rows ?? 0}/${run.n_examples} rows · ${fmtWeight(scorerCoverage[key]?.weight ?? 0)}/${fmtWeight(totalWeight)} weight`}
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
                  {scorerLabel(key, judgeNames)}
                </th>
              ))}
              {isWeighted && (
                <th
                  className="px-3 py-3 text-left font-medium"
                  title="Relative weight of this example in the run's aggregate scores"
                >
                  Weight
                </th>
              )}
              <th className="px-4 py-3 text-left font-medium">Result</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {run.results.length === 0 && (
              <tr>
                <td
                  colSpan={4 + scorerKeys.length + (isWeighted ? 1 : 0)}
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
                  {(row.tags ?? []).length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1">
                      {(row.tags ?? []).map((tag, index) => (
                        <span
                          key={`${tag}-${index}`}
                          data-testid="eval-result-tag"
                          className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-500"
                        >
                          {tag}
                        </span>
                      ))}
                    </div>
                  )}
                </td>
                <td className="max-w-[12rem] px-4 py-3 text-xs text-slate-600">
                  <div className="line-clamp-3 whitespace-pre-wrap break-words">
                    {previewValue(row.expected.expected ?? row.expected)}
                  </div>
                </td>
                <td className="max-w-[16rem] px-4 py-3 text-xs text-slate-700">
                  <div className="line-clamp-3 whitespace-pre-wrap break-words">
                    {row.prediction || "—"}
                  </div>
                  {row.error && (
                    <div
                      data-testid="eval-row-error"
                      className="mt-2 whitespace-pre-wrap break-words text-red-600"
                    >
                      Incomplete: {row.error}
                    </div>
                  )}
                </td>
                {scorerKeys.map((key) => (
                  <td key={key} className={`px-3 py-3 tabular-nums ${tone(row.scores[key])}`}>
                    {row.scores[key] === undefined ? "—" : fmtPct(row.scores[key])}
                  </td>
                ))}
                {isWeighted && (
                  <td className="px-3 py-3 tabular-nums text-xs text-slate-600">
                    {(row.weight ?? 1).toFixed(2)}
                  </td>
                )}
                <td className="px-4 py-3">
                  {row.error ? (
                    <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-amber-700">
                      Incomplete
                    </span>
                  ) : row.passed ? (
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
