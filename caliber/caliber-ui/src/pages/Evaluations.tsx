/**
 * Evaluations — the scorecard surface.
 *
 * Lists evaluation runs (each scores a dataset's examples through a predict
 * target + deterministic scorers) and launches new ones. The per-example
 * scorecard + run-over-run comparison lives on {@link EvaluationDetail}.
 */

import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { FlaskConical, Play } from "lucide-react";

import { ApiError, caliberApi } from "@/api/caliberApi";
import type {
  EvalDataset,
  EvalPredictTarget,
  EvalRunCreatePayload,
  EvalRunSummary,
  Judge,
} from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { useApi } from "@/hooks/useApi";
import { relativeTime } from "@/lib/time";

/** The ``Judge.<judge_id>`` scorer-token prefix (mirror of the backend). */
export const JUDGE_SCORER_PREFIX = "Judge.";

/** Build the scorer token an evaluation run carries for a custom judge. */
export function judgeScorerName(judgeId: string): string {
  return `${JUDGE_SCORER_PREFIX}${judgeId}`;
}

/** Scorers the backend exposes (mirror of caliber.eval.scorecard.AVAILABLE_SCORERS). */
export const AVAILABLE_SCORERS: ReadonlyArray<{ name: string; label: string; description: string }> =
  [
    {
      name: "exact_match",
      label: "Exact match",
      description: "Prediction equals the expected answer (case/space-insensitive).",
    },
    {
      name: "token_f1",
      label: "Token F1",
      description: "Token-overlap F1 between prediction and expected answer.",
    },
    {
      name: "contains_expected",
      label: "Contains expected",
      description: "Expected answer text appears within the prediction.",
    },
    {
      name: "non_empty",
      label: "Non-empty",
      description: "Prediction is non-empty.",
    },
  ];

const DEFAULT_SCORERS = ["exact_match", "token_f1", "contains_expected"];

const SCORER_LABELS: Record<string, string> = Object.fromEntries(
  AVAILABLE_SCORERS.map((s) => [s.name, s.label]),
);

/** Human-facing grader label (falls back to the raw key for unknown scorers).
 *
 * ``Judge.<id>`` tokens render with the judge's name when ``judgeNames`` maps it
 * (the page fetches the judge registry), else the bare id, both marked as a judge.
 */
export function scorerLabel(name: string, judgeNames: Record<string, string> = {}): string {
  if (name.startsWith(JUDGE_SCORER_PREFIX)) {
    const id = name.slice(JUDGE_SCORER_PREFIX.length);
    return `⚖ ${judgeNames[id] ?? id}`;
  }
  return SCORER_LABELS[name] ?? name;
}

export function fmtPct(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `${Math.round(n * 100)}%`;
}

function scoreTone(n: number | null | undefined): string {
  if (n === null || n === undefined) return "text-slate-400";
  if (n >= 0.8) return "text-emerald-600";
  if (n >= 0.5) return "text-amber-600";
  return "text-red-600";
}

/** id → name map so ``Judge.<id>`` scorer tokens render with a readable name. */
function useJudgeNames(): Record<string, string> {
  const judgesFetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listJudges({ status: "all" }, signal),
    [],
  );
  const { data } = useApi<Judge[]>(judgesFetcher, []);
  return Object.fromEntries((data ?? []).map((j) => [j.judge_id, j.name]));
}

export function Evaluations(): JSX.Element {
  const fetcher = useCallback((signal: AbortSignal) => caliberApi.listEvaluations(undefined, signal), []);
  const { data, error, loading, refresh } = useApi<EvalRunSummary[]>(fetcher, []);
  const judgeNames = useJudgeNames();
  const [showRun, setShowRun] = useState(false);

  return (
    <div className="space-y-5">
      <PageHeader
        title="Evaluations"
        subtitle="Score a test set's examples through the configured model and graders — see which examples pass, and compare runs over time."
      />

      <div className="flex justify-end">
        <button
          type="button"
          onClick={() => setShowRun((v) => !v)}
          className="inline-flex items-center gap-1.5 rounded-xl bg-caliber-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-caliber-700"
        >
          <Play className="h-4 w-4" />
          {showRun ? "Cancel" : "Run evaluation"}
        </button>
      </div>

      {showRun && (
        <RunEvaluationPanel
          onCancel={() => setShowRun(false)}
          onSuccess={() => {
            setShowRun(false);
            refresh();
          }}
        />
      )}

      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          <div className="font-medium">Failed to load evaluations</div>
          <div className="mt-0.5 text-xs">{error.message}</div>
        </div>
      )}

      <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
              <th className="px-4 py-3 text-left font-medium">Run</th>
              <th className="px-4 py-3 text-left font-medium">Graders</th>
              <th className="px-4 py-3 text-left font-medium">Overall</th>
              <th className="px-4 py-3 text-left font-medium">Pass rate</th>
              <th className="px-4 py-3 text-left font-medium">Examples</th>
              <th className="px-4 py-3 text-left font-medium">Status</th>
              <th className="px-4 py-3 text-left font-medium">When</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {loading && !data && (
              <tr>
                <td colSpan={7} className="px-4 py-10 text-center text-sm text-slate-500">
                  Loading…
                </td>
              </tr>
            )}
            {data && data.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-12 text-center text-sm text-slate-500">
                  No evaluation runs yet. Run one against a test set to see a scorecard here.
                </td>
              </tr>
            )}
            {(data ?? []).map((run) => (
              <tr key={run.run_id} className="hover:bg-slate-50" data-testid="eval-run-row">
                <td className="px-4 py-3">
                  <Link
                    to={`/evaluations/${run.run_id}`}
                    className="font-medium text-slate-900 hover:text-caliber-700 hover:underline"
                  >
                    {run.label || run.run_id}
                  </Link>
                  <div className="mt-0.5 flex items-center gap-1 text-xs text-slate-500">
                    <FlaskConical className="h-3 w-3" />
                    <span className="font-mono">{run.dataset_id}</span>
                    <span>· v{run.dataset_version}</span>
                    {run.model ? <span>· {run.model}</span> : null}
                  </div>
                </td>
                <td className="px-4 py-3 text-xs text-slate-600">
                  {run.scorers.map((s) => scorerLabel(s, judgeNames)).join(", ")}
                </td>
                <td className={`px-4 py-3 font-semibold tabular-nums ${scoreTone(run.overall_score)}`}>
                  {fmtPct(run.overall_score)}
                </td>
                <td className="px-4 py-3 tabular-nums text-slate-700">{fmtPct(run.pass_rate)}</td>
                <td className="px-4 py-3 tabular-nums text-slate-700">
                  {run.passed_count}/{run.n_examples}
                </td>
                <td className="px-4 py-3">
                  <StatusPill status={run.status} />
                </td>
                <td className="px-4 py-3 text-xs text-slate-500">{relativeTime(run.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function StatusPill({ status }: { status: string }): JSX.Element {
  const cls =
    status === "completed"
      ? "bg-emerald-100 text-emerald-700"
      : status === "failed"
        ? "bg-red-100 text-red-700"
        : "bg-slate-200 text-slate-600";
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${cls}`}>
      {status}
    </span>
  );
}

function RunEvaluationPanel({
  onCancel,
  onSuccess,
}: {
  onCancel: () => void;
  onSuccess: () => void;
}): JSX.Element {
  const datasetsFetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listEvalDatasets({ status: "active" }, signal),
    [],
  );
  const { data: datasets } = useApi<EvalDataset[]>(datasetsFetcher, []);
  const judgesFetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listJudges({ status: "active" }, signal),
    [],
  );
  const { data: judges } = useApi<Judge[]>(judgesFetcher, []);
  const [datasetId, setDatasetId] = useState("");
  const [label, setLabel] = useState("");
  const [scorers, setScorers] = useState<string[]>(DEFAULT_SCORERS);
  const [predictTarget, setPredictTarget] = useState<EvalPredictTarget>("llm");
  const [subjectRef, setSubjectRef] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggleScorer = (name: string): void => {
    setScorers((prev) =>
      prev.includes(name) ? prev.filter((s) => s !== name) : [...prev, name],
    );
  };

  const submit = async (): Promise<void> => {
    setSubmitting(true);
    setError(null);
    try {
      const payload: EvalRunCreatePayload = { dataset_id: datasetId, label, scorers };
      if (predictTarget !== "llm") {
        payload.predict_target = predictTarget;
        payload.subject_ref = subjectRef.trim();
      }
      const run = await caliberApi.createEvaluation(payload);
      void run;
      onSuccess();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "evaluation failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4">
      <h2 className="mb-3 text-sm font-semibold text-slate-900">Run evaluation</h2>
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label className="mb-1 block text-xs text-slate-500" htmlFor="eval-dataset">
            Test set
          </label>
          <select
            id="eval-dataset"
            aria-label="Test set"
            value={datasetId}
            onChange={(e) => setDatasetId(e.target.value)}
            className="w-full rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-900 outline-none focus:border-caliber-400 focus:ring-2 focus:ring-caliber-400/20"
          >
            <option value="">Select a test set…</option>
            {(datasets ?? []).map((ds) => (
              <option key={ds.dataset_id} value={ds.dataset_id}>
                {ds.name} (v{ds.version})
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="mb-1 block text-xs text-slate-500" htmlFor="eval-label">
            Label
          </label>
          <input
            id="eval-label"
            aria-label="Label"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="e.g. gpt-4o-mini baseline"
            className="w-full rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-900 outline-none focus:border-caliber-400 focus:ring-2 focus:ring-caliber-400/20"
          />
        </div>
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <div>
          <label className="mb-1 block text-xs text-slate-500" htmlFor="eval-target">
            What to score
          </label>
          <select
            id="eval-target"
            aria-label="What to score"
            data-testid="eval-predict-target"
            value={predictTarget}
            onChange={(e) => setPredictTarget(e.target.value as EvalPredictTarget)}
            className="w-full rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-900 outline-none focus:border-caliber-400 focus:ring-2 focus:ring-caliber-400/20"
          >
            <option value="llm">Model completion (generic)</option>
            <option value="prompt">Prompt version</option>
            <option value="skill">Skill</option>
            <option value="workflow">Workflow version</option>
          </select>
        </div>
        {predictTarget !== "llm" && (
          <div>
            <label className="mb-1 block text-xs text-slate-500" htmlFor="eval-subject">
              {predictTarget === "prompt"
                ? "Prompt (name@version)"
                : predictTarget === "workflow"
                  ? "Workflow version id"
                  : "Skill id"}
            </label>
            <input
              id="eval-subject"
              aria-label="Subject under test"
              data-testid="eval-subject-ref"
              value={subjectRef}
              onChange={(e) => setSubjectRef(e.target.value)}
              placeholder={
                predictTarget === "prompt"
                  ? "support-greeting@3"
                  : predictTarget === "workflow"
                    ? "WFV-…"
                    : "SK-…"
              }
              className="w-full rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-900 outline-none focus:border-caliber-400 focus:ring-2 focus:ring-caliber-400/20"
            />
          </div>
        )}
      </div>

      <div className="mt-3">
        <span className="mb-1 block text-xs text-slate-500">Graders</span>
        <div className="flex flex-wrap gap-2">
          {AVAILABLE_SCORERS.map((s) => (
            <label
              key={s.name}
              title={s.description}
              className={`inline-flex cursor-pointer items-center gap-1.5 rounded-xl border px-2.5 py-1.5 text-xs font-medium ${
                scorers.includes(s.name)
                  ? "border-caliber-300 bg-caliber-50 text-caliber-700"
                  : "border-slate-200 bg-white text-slate-600"
              }`}
            >
              <input
                type="checkbox"
                className="sr-only"
                checked={scorers.includes(s.name)}
                onChange={() => toggleScorer(s.name)}
              />
              {s.label}
            </label>
          ))}
        </div>

        {(judges ?? []).length > 0 && (
          <>
            <span className="mb-1 mt-3 block text-xs text-slate-500">
              Custom LLM judges
            </span>
            <div className="flex flex-wrap gap-2" data-testid="judge-scorer-options">
              {(judges ?? []).map((judge) => {
                const token = judgeScorerName(judge.judge_id);
                return (
                  <label
                    key={judge.judge_id}
                    title={judge.description || judge.instructions}
                    data-testid={`judge-scorer-${judge.judge_id}`}
                    className={`inline-flex cursor-pointer items-center gap-1.5 rounded-xl border px-2.5 py-1.5 text-xs font-medium ${
                      scorers.includes(token)
                        ? "border-caliber-300 bg-caliber-50 text-caliber-700"
                        : "border-slate-200 bg-white text-slate-600"
                    }`}
                  >
                    <input
                      type="checkbox"
                      className="sr-only"
                      checked={scorers.includes(token)}
                      onChange={() => toggleScorer(token)}
                    />
                    ⚖ {judge.name}
                  </label>
                );
              })}
            </div>
          </>
        )}
      </div>

      {error && <div className="mt-3 text-sm text-red-600">{error}</div>}

      <div className="mt-4 flex justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-xl px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100"
        >
          Cancel
        </button>
        <button
          type="button"
          disabled={
            submitting ||
            !datasetId ||
            scorers.length === 0 ||
            (predictTarget !== "llm" && !subjectRef.trim())
          }
          onClick={() => void submit()}
          className="inline-flex items-center gap-1.5 rounded-xl bg-caliber-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-caliber-700 disabled:opacity-50"
        >
          <Play className="h-3.5 w-3.5" />
          {submitting ? "Running…" : "Run"}
        </button>
      </div>
    </div>
  );
}
