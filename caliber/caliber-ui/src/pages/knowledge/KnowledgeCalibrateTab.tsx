/**
 * Knowledge-base CALIBRATION surface (Phase K2).
 *
 * Runs a KB version against a test-question set and shows retrieval-quality
 * metrics (Recall@k, nDCG@k, Faithfulness, Answer-correctness) with a
 * baseline-vs-candidate compare and run history. Scoped to a single KB; kept
 * self-contained so it can later move into a per-KB workspace.
 *
 * Mirrors the run-history + baseline + per-row-detail + diff/compare pattern
 * from PromptRunsStage / ToolRunsStage / SkillRunsStage, adapted to retrieval
 * quality (per-question rows instead of per-case rows, four metrics instead of
 * a single score).
 */

import { Fragment, useCallback, useEffect, useMemo, useState } from "react";

import { caliberApi } from "@/api/caliberApi";
import type {
  KnowledgeBaseVersion,
  KnowledgeCalibrationMetrics,
  KnowledgeCalibrationQuestionResult,
  KnowledgeCalibrationRunDetail,
  KnowledgeCalibrationRunSummary,
  KnowledgeRetrievalMode,
} from "@/api/knowledgeTypes";
import type { EvalDataset } from "@/api/types";
import { useApiQuery } from "@/hooks/useApiQuery";

/** The four headline retrieval-quality metrics, in display order. */
const METRIC_KEYS = [
  "recall_at_k",
  "ndcg_at_k",
  "faithfulness",
  "answer_correctness",
] as const;
type MetricKey = (typeof METRIC_KEYS)[number];

const METRIC_LABELS: Record<MetricKey, string> = {
  recall_at_k: "Recall@k",
  ndcg_at_k: "nDCG@k",
  faithfulness: "Faithfulness",
  answer_correctness: "Answer correctness",
};

const RETRIEVAL_MODES: Array<{ id: KnowledgeRetrievalMode; label: string }> = [
  { id: "dense", label: "Dense" },
  { id: "hybrid", label: "Hybrid" },
  { id: "graph_hybrid", label: "GraphRAG hybrid" },
  { id: "age_graph", label: "Apache AGE" },
];

function retrievalModeLabel(mode: string): string {
  return RETRIEVAL_MODES.find((item) => item.id === mode)?.label ?? mode;
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString();
}

/** Coerce a possibly-null metric to a number, or null when undefined. */
function metricValue(
  metrics: KnowledgeCalibrationMetrics | null | undefined,
  key: MetricKey,
): number | null {
  if (!metrics) return null;
  const raw = metrics[key];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

function questionMetric(
  result: KnowledgeCalibrationQuestionResult,
  key: MetricKey,
): number | null {
  const raw = result[key];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

/** Percent string for a 0..1 metric, or an em-dash when undefined. */
function pct(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

function signedPct(value: number): string {
  return `${value >= 0 ? "+" : ""}${Math.round(value * 100)}%`;
}

function count(metrics: KnowledgeCalibrationMetrics, key: string): number {
  const raw = metrics[key];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : 0;
}

/** Headline score for a run: answer-correctness, else faithfulness, else nDCG. */
function headlineScore(metrics: KnowledgeCalibrationMetrics): number | null {
  return (
    metricValue(metrics, "answer_correctness") ??
    metricValue(metrics, "faithfulness") ??
    metricValue(metrics, "ndcg_at_k") ??
    metricValue(metrics, "recall_at_k")
  );
}

function verdictTone(verdict: string): string {
  if (verdict === "pass" || verdict === "passed")
    return "bg-emerald-100 text-emerald-700";
  if (verdict === "partial") return "bg-amber-100 text-amber-700";
  return "bg-red-100 text-red-700";
}

interface KnowledgeCalibrateTabProps {
  knowledgeBaseId: string;
  versions: KnowledgeBaseVersion[];
  /** Active version id from the KB, used to default the version selector. */
  activeVersionId: string | null;
}

export function KnowledgeCalibrateTab({
  knowledgeBaseId,
  versions,
  activeVersionId,
}: KnowledgeCalibrateTabProps): JSX.Element {
  // ── Run config ──────────────────────────────────────────────────────────
  const [versionId, setVersionId] = useState<string>("");
  const [evalDatasetId, setEvalDatasetId] = useState<string>("");
  const [retrievalMode, setRetrievalMode] =
    useState<KnowledgeRetrievalMode>("dense");
  const [topK, setTopK] = useState<number>(6);

  // ── Run history + viewed/baseline detail ────────────────────────────────
  const [history, setHistory] = useState<KnowledgeCalibrationRunSummary[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [viewedRunId, setViewedRunId] = useState<string | null>(null);
  const [viewedDetail, setViewedDetail] =
    useState<KnowledgeCalibrationRunDetail | null>(null);
  const [viewedLoading, setViewedLoading] = useState(false);
  const [baselineRunId, setBaselineRunId] = useState<string | null>(null);
  const [baselineDetail, setBaselineDetail] =
    useState<KnowledgeCalibrationRunDetail | null>(null);

  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [pinning, setPinning] = useState(false);
  const [pinError, setPinError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const datasetsQuery = useApiQuery<EvalDataset[]>(
    ["knowledge-bases", "calibrate", "eval-datasets"],
    (signal) => caliberApi.listEvalDatasets({ status: "active" }, signal),
  );
  const datasets = useMemo(() => datasetsQuery.data ?? [], [datasetsQuery.data]);

  // Load the KB detail once to discover the pinned baseline run (the list query
  // omits baseline_run_id — it lives on the KB detail schema).
  const kbDetailQuery = useApiQuery(
    ["knowledge-bases", knowledgeBaseId, "calibrate-detail"],
    (signal) => caliberApi.getKnowledgeBase(knowledgeBaseId, signal),
    { enabled: Boolean(knowledgeBaseId) },
  );
  useEffect(() => {
    setBaselineRunId(kbDetailQuery.data?.baseline_run_id ?? null);
  }, [kbDetailQuery.data?.baseline_run_id]);

  // Default the version selector to the active version (else newest completed,
  // else newest) whenever the KB / version set changes.
  useEffect(() => {
    if (versionId && versions.some((v) => v.knowledge_base_version_id === versionId)) {
      return;
    }
    const active = versions.find(
      (v) => v.knowledge_base_version_id === activeVersionId,
    );
    const completed = versions.find((v) => v.status === "completed");
    const next = active ?? completed ?? versions[0] ?? null;
    setVersionId(next?.knowledge_base_version_id ?? "");
  }, [versions, activeVersionId, versionId]);

  // Default the dataset selector to the first available test set.
  useEffect(() => {
    if (evalDatasetId && datasets.some((d) => d.dataset_id === evalDatasetId)) {
      return;
    }
    setEvalDatasetId(datasets[0]?.dataset_id ?? "");
  }, [datasets, evalDatasetId]);

  const refreshHistory = useCallback(
    async (signal?: AbortSignal) => {
      setLoadingHistory(true);
      try {
        const runs = await caliberApi.listKnowledgeBaseTestRuns(
          knowledgeBaseId,
          undefined,
          signal,
        );
        if (!signal?.aborted) setHistory(runs);
      } catch {
        if (!signal?.aborted) setHistory([]);
      } finally {
        if (!signal?.aborted) setLoadingHistory(false);
      }
    },
    [knowledgeBaseId],
  );

  useEffect(() => {
    const controller = new AbortController();
    void refreshHistory(controller.signal);
    return () => controller.abort();
  }, [refreshHistory]);

  // Default the viewed run to the latest saved run once history loads.
  useEffect(() => {
    if (viewedRunId || history.length === 0) return;
    setViewedRunId(history[0]!.test_run_id);
  }, [history, viewedRunId]);

  // Load the viewed run's full per-question detail when the selection changes.
  useEffect(() => {
    if (!viewedRunId) {
      setViewedDetail(null);
      return;
    }
    let cancelled = false;
    setViewedLoading(true);
    setExpanded(new Set());
    void caliberApi
      .getKnowledgeBaseTestRun(viewedRunId)
      .then((detail) => {
        if (!cancelled) setViewedDetail(detail);
      })
      .catch(() => {
        if (!cancelled) setViewedDetail(null);
      })
      .finally(() => {
        if (!cancelled) setViewedLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [viewedRunId]);

  // Lazily load the baseline run's detail (skipped when it is the viewed run).
  useEffect(() => {
    if (!baselineRunId || baselineRunId === viewedRunId) {
      setBaselineDetail(null);
      return;
    }
    let cancelled = false;
    void caliberApi
      .getKnowledgeBaseTestRun(baselineRunId)
      .then((detail) => {
        if (!cancelled) setBaselineDetail(detail);
      })
      .catch(() => {
        if (!cancelled) setBaselineDetail(null);
      });
    return () => {
      cancelled = true;
    };
  }, [baselineRunId, viewedRunId]);

  const selectedVersion = versions.find(
    (v) => v.knowledge_base_version_id === versionId,
  );
  const versionReady = selectedVersion?.status === "completed";
  const hasDatasets = datasets.length > 0;
  const canRun =
    Boolean(versionId) && Boolean(evalDatasetId) && versionReady && !running;

  const runCalibration = async () => {
    if (!versionId || !evalDatasetId) return;
    setRunning(true);
    setRunError(null);
    try {
      const summary = await caliberApi.calibrateKnowledgeBase(knowledgeBaseId, {
        version_id: versionId,
        eval_dataset_id: evalDatasetId,
        retrieval_mode: retrievalMode,
        top_k: topK,
      });
      await refreshHistory();
      setViewedRunId(summary.test_run_id);
    } catch (err) {
      setRunError(
        err instanceof Error ? err.message : "Calibration run failed",
      );
    } finally {
      setRunning(false);
    }
  };

  const pinAsBaseline = async (testRunId: string) => {
    setPinning(true);
    setPinError(null);
    try {
      const result = await caliberApi.setKnowledgeBaseBaseline(
        knowledgeBaseId,
        testRunId,
      );
      setBaselineRunId(result.baseline_run_id ?? testRunId);
    } catch (err) {
      setPinError(err instanceof Error ? err.message : "Failed to set baseline");
    } finally {
      setPinning(false);
    }
  };

  const toggleExpanded = (index: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  };

  const viewedIsBaseline =
    viewedRunId != null && viewedRunId === baselineRunId;
  const showComparison =
    baselineRunId != null &&
    !viewedIsBaseline &&
    viewedDetail != null &&
    baselineDetail != null;

  // Metric deltas (candidate − baseline) + regressions when comparing.
  const comparison = useMemo(() => {
    if (!showComparison || !viewedDetail || !baselineDetail) return null;
    const deltas = METRIC_KEYS.map((key) => {
      const cur = metricValue(viewedDetail.metrics, key);
      const base = metricValue(baselineDetail.metrics, key);
      const delta = cur !== null && base !== null ? cur - base : null;
      return { key, cur, base, delta };
    });
    // Align questions by text; flag rows whose verdict dropped to fail or whose
    // headline (correctness→faithfulness→ndcg→recall) score regressed.
    const baseByQuestion = new Map(
      baselineDetail.results.map((r) => [r.question, r]),
    );
    const regressions = viewedDetail.results
      .map((cur) => {
        const base = baseByQuestion.get(cur.question) ?? null;
        if (!base) return null;
        const curScore =
          questionMetric(cur, "answer_correctness") ??
          questionMetric(cur, "faithfulness") ??
          questionMetric(cur, "ndcg_at_k") ??
          questionMetric(cur, "recall_at_k");
        const baseScore =
          questionMetric(base, "answer_correctness") ??
          questionMetric(base, "faithfulness") ??
          questionMetric(base, "ndcg_at_k") ??
          questionMetric(base, "recall_at_k");
        const verdictDropped =
          (base.verdict === "pass" ||
            base.verdict === "passed" ||
            base.verdict === "partial") &&
          (cur.verdict === "fail" || cur.verdict === "failed");
        const scoreDropped =
          curScore !== null && baseScore !== null && curScore < baseScore;
        if (verdictDropped || scoreDropped) {
          return { cur, base, curScore, baseScore };
        }
        return null;
      })
      .filter(
        (
          row,
        ): row is {
          cur: KnowledgeCalibrationQuestionResult;
          base: KnowledgeCalibrationQuestionResult;
          curScore: number | null;
          baseScore: number | null;
        } => row !== null,
      );
    return { deltas, regressions };
  }, [showComparison, viewedDetail, baselineDetail]);

  const viewedMetrics = viewedDetail?.metrics ?? null;
  const results = viewedDetail?.results ?? [];

  return (
    <div className="space-y-5">
      {/* ── Run config ── */}
      <section
        data-testid="kb-calibrate-config"
        className="card overflow-hidden"
      >
        <div className="border-b border-slate-200/70 px-5 py-4">
          <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
            Calibrate retrieval quality
          </div>
          <h2 className="mt-2 text-lg font-semibold text-slate-900">
            Run a test set against this knowledge base
          </h2>
          <p className="mt-1 text-sm text-slate-500">
            Score a built version against a set of test questions to measure
            recall, ranking, faithfulness, and answer correctness — then pin a
            baseline to catch regressions.
          </p>
        </div>
        <div className="grid gap-4 px-5 py-5 md:grid-cols-2 xl:grid-cols-4">
          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-semibold text-slate-600">Version</span>
            <select
              data-testid="kb-calibrate-version"
              value={versionId}
              onChange={(event) => setVersionId(event.target.value)}
              className="rounded-md border border-slate-200 bg-white px-3 py-2 text-sm"
            >
              {versions.length === 0 && <option value="">No versions</option>}
              {versions.map((version) => (
                <option
                  key={version.knowledge_base_version_id}
                  value={version.knowledge_base_version_id}
                >
                  v{version.version_number}
                  {version.status === "completed" ? "" : ` (${version.status})`}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-semibold text-slate-600">
              Test set
            </span>
            <select
              data-testid="kb-calibrate-dataset"
              value={evalDatasetId}
              onChange={(event) => setEvalDatasetId(event.target.value)}
              disabled={!hasDatasets}
              className="rounded-md border border-slate-200 bg-white px-3 py-2 text-sm disabled:opacity-50"
            >
              {!hasDatasets && <option value="">No test sets</option>}
              {datasets.map((dataset) => (
                <option key={dataset.dataset_id} value={dataset.dataset_id}>
                  {dataset.name}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-semibold text-slate-600">
              Retrieval mode
            </span>
            <select
              data-testid="kb-calibrate-mode"
              value={retrievalMode}
              onChange={(event) =>
                setRetrievalMode(event.target.value as KnowledgeRetrievalMode)
              }
              className="rounded-md border border-slate-200 bg-white px-3 py-2 text-sm"
            >
              {RETRIEVAL_MODES.map((mode) => (
                <option key={mode.id} value={mode.id}>
                  {mode.label}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="text-xs font-semibold text-slate-600">Top-k</span>
            <input
              data-testid="kb-calibrate-topk"
              type="number"
              min={1}
              max={20}
              value={topK}
              onChange={(event) =>
                setTopK(
                  Math.max(1, Math.min(20, Number(event.target.value) || 1)),
                )
              }
              className="rounded-md border border-slate-200 bg-white px-3 py-2 text-sm"
            />
          </label>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200/70 px-5 py-4">
          <div className="text-xs text-slate-500">
            {!hasDatasets ? (
              <span>
                No test sets yet — create a test set of questions (with gold
                sources/answers) to calibrate against.
              </span>
            ) : selectedVersion && !versionReady ? (
              <span>
                Version v{selectedVersion.version_number} is{" "}
                {selectedVersion.status}. Wait for the build to finish before
                calibrating.
              </span>
            ) : (
              <span>
                Runs retrieval + judging per question on the server. This may
                take a moment for large test sets.
              </span>
            )}
          </div>
          <button
            type="button"
            data-testid="kb-calibrate-run"
            onClick={() => void runCalibration()}
            disabled={!canRun}
            className="inline-flex items-center gap-2 rounded-md bg-caliber-600 px-4 py-2 text-sm font-medium text-white hover:bg-caliber-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {running ? "Running calibration…" : "Run calibration"}
          </button>
        </div>
      </section>

      {runError && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {runError}
        </div>
      )}
      {pinError && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
          {pinError}
        </div>
      )}

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(300px,0.42fr)]">
        {/* ── Latest / selected run ── */}
        <div className="space-y-4">
          {viewedRunId == null && !loadingHistory ? (
            <div className="rounded-2xl border-2 border-dashed border-slate-200 bg-slate-50/60 px-6 py-12 text-center text-sm text-slate-400">
              No calibration runs yet. Configure a version and test set above,
              then run calibration to measure retrieval quality.
            </div>
          ) : (
            <section
              data-testid="kb-calibrate-run-results"
              className="card overflow-hidden"
            >
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200/70 px-5 py-4">
                <div className="flex items-center gap-2">
                  <h3 className="text-sm font-semibold text-slate-700">
                    {viewedRunId === history[0]?.test_run_id
                      ? "Latest run"
                      : "Selected run"}
                  </h3>
                  {viewedIsBaseline ? (
                    <span
                      data-testid="kb-calibrate-baseline-marker"
                      className="rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-blue-700 ring-1 ring-blue-200/60"
                    >
                      Baseline
                    </span>
                  ) : (
                    viewedRunId != null && (
                      <button
                        type="button"
                        data-testid="kb-calibrate-set-baseline"
                        onClick={() => void pinAsBaseline(viewedRunId)}
                        disabled={pinning}
                        className="rounded-md border border-blue-200 bg-white px-2.5 py-1 text-[11px] font-medium text-blue-700 hover:bg-blue-50 disabled:opacity-50"
                      >
                        {pinning ? "Setting…" : "Set as baseline"}
                      </button>
                    )
                  )}
                </div>
                {viewedDetail && (
                  <div className="text-xs text-slate-500">
                    {retrievalModeLabel(viewedDetail.retrieval_mode)} · top-
                    {viewedDetail.top_k} · {viewedDetail.test_set_size} question
                    {viewedDetail.test_set_size === 1 ? "" : "s"}
                  </div>
                )}
              </div>

              {viewedLoading || viewedDetail == null ? (
                <div className="px-5 py-8 text-xs text-slate-400">
                  Loading results…
                </div>
              ) : (
                <div className="space-y-5 px-5 py-5">
                  {/* Metric cards */}
                  <div
                    data-testid="kb-calibrate-metric-cards"
                    className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
                  >
                    {METRIC_KEYS.map((key) => (
                      <div
                        key={key}
                        data-testid={`kb-calibrate-metric-${key}`}
                        className="rounded-xl border border-slate-200 bg-slate-50/60 px-4 py-3"
                      >
                        <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                          {METRIC_LABELS[key]}
                        </div>
                        <div className="mt-1 text-2xl font-semibold text-slate-900">
                          {pct(metricValue(viewedMetrics, key))}
                        </div>
                      </div>
                    ))}
                  </div>

                  {/* Pass / partial / fail counts */}
                  {viewedMetrics && (
                    <div className="text-xs text-slate-500">
                      <span className="font-semibold text-emerald-600">
                        {count(viewedMetrics, "passed_count")} passed
                      </span>
                      {" · "}
                      <span className="font-semibold text-amber-600">
                        {count(viewedMetrics, "partial_count")} partial
                      </span>
                      {" · "}
                      <span className="font-semibold text-red-600">
                        {count(viewedMetrics, "failed_count")} failed
                      </span>
                    </div>
                  )}

                  {/* Per-question table */}
                  <div className="overflow-x-auto rounded-xl border border-slate-200">
                    <table
                      data-testid="kb-calibrate-question-table"
                      className="w-full text-left text-xs"
                    >
                      <thead className="bg-slate-50 text-[11px] uppercase tracking-wide text-slate-500">
                        <tr>
                          <th className="px-3 py-2 font-semibold">Question</th>
                          <th className="px-3 py-2 font-semibold">Recall</th>
                          <th className="px-3 py-2 font-semibold">nDCG</th>
                          <th className="px-3 py-2 font-semibold">Faithful.</th>
                          <th className="px-3 py-2 font-semibold">Correct.</th>
                          <th className="px-3 py-2 font-semibold">Verdict</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-100">
                        {results.map((result, index) => {
                          const open = expanded.has(index);
                          return (
                            <Fragment key={`q-${index}`}>
                              <tr
                                data-testid="kb-calibrate-question-row"
                                onClick={() => toggleExpanded(index)}
                                className="cursor-pointer hover:bg-slate-50/60"
                              >
                                <td className="max-w-[20rem] truncate px-3 py-2 text-slate-700">
                                  {result.question}
                                </td>
                                <td className="px-3 py-2 text-slate-600">
                                  {pct(questionMetric(result, "recall_at_k"))}
                                </td>
                                <td className="px-3 py-2 text-slate-600">
                                  {pct(questionMetric(result, "ndcg_at_k"))}
                                </td>
                                <td className="px-3 py-2 text-slate-600">
                                  {pct(questionMetric(result, "faithfulness"))}
                                </td>
                                <td className="px-3 py-2 text-slate-600">
                                  {pct(
                                    questionMetric(result, "answer_correctness"),
                                  )}
                                </td>
                                <td className="px-3 py-2">
                                  <span
                                    className={`rounded px-2 py-0.5 text-[11px] font-medium ${verdictTone(result.verdict)}`}
                                  >
                                    {result.verdict}
                                  </span>
                                </td>
                              </tr>
                              {open && (
                                <tr data-testid="kb-calibrate-question-detail">
                                  <td
                                    colSpan={6}
                                    className="bg-slate-50/70 px-3 py-3"
                                  >
                                    <div className="space-y-2 text-[11px] text-slate-600">
                                      <div>
                                        <span className="font-semibold text-slate-500">
                                          Retrieved sources:
                                        </span>{" "}
                                        {result.retrieved_sources.length > 0
                                          ? result.retrieved_sources.join(", ")
                                          : "—"}
                                      </div>
                                      <div>
                                        <span className="font-semibold text-slate-500">
                                          Gold sources:
                                        </span>{" "}
                                        {result.gold_sources.length > 0
                                          ? result.gold_sources.join(", ")
                                          : "—"}
                                      </div>
                                      <div>
                                        <span className="font-semibold text-slate-500">
                                          Answer:
                                        </span>{" "}
                                        {result.answer_error
                                          ? `Error: ${result.answer_error}`
                                          : result.answer || "—"}
                                      </div>
                                    </div>
                                  </td>
                                </tr>
                              )}
                            </Fragment>
                          );
                        })}
                        {results.length === 0 && (
                          <tr>
                            <td
                              colSpan={6}
                              className="px-3 py-6 text-center text-slate-400"
                            >
                              This run has no scored questions.
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </section>
          )}

          {/* ── Baseline compare: metric deltas + regressions ── */}
          {showComparison && comparison && (
            <section
              data-testid="kb-calibrate-comparison"
              className="card overflow-hidden border-blue-200"
            >
              <div className="border-b border-blue-200/70 bg-blue-50/40 px-5 py-3">
                <h3 className="text-sm font-semibold text-blue-900">
                  Vs. baseline
                </h3>
              </div>
              <div className="space-y-4 px-5 py-5">
                <div
                  data-testid="kb-calibrate-deltas"
                  className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
                >
                  {comparison.deltas.map(({ key, delta }) => (
                    <div
                      key={key}
                      data-testid={`kb-calibrate-delta-${key}`}
                      className="rounded-xl border border-slate-200 bg-white px-4 py-3"
                    >
                      <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                        {METRIC_LABELS[key]}
                      </div>
                      <div
                        className={`mt-1 text-lg font-semibold ${
                          delta === null
                            ? "text-slate-400"
                            : delta > 0
                              ? "text-emerald-700"
                              : delta < 0
                                ? "text-red-700"
                                : "text-slate-600"
                        }`}
                      >
                        {delta === null ? "—" : signedPct(delta)}
                      </div>
                    </div>
                  ))}
                </div>

                <div data-testid="kb-calibrate-regressions">
                  {comparison.regressions.length === 0 ? (
                    <div className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
                      No regressions — no question that scored higher in the
                      baseline dropped.
                    </div>
                  ) : (
                    <div className="space-y-2">
                      <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                        <span className="font-semibold">
                          {comparison.regressions.length} regression
                          {comparison.regressions.length === 1 ? "" : "s"}
                        </span>{" "}
                        — questions whose verdict or score dropped vs. the
                        baseline.
                      </div>
                      {comparison.regressions.map((row, index) => (
                        <div
                          key={`regression-${index}`}
                          data-testid="kb-calibrate-regression-row"
                          className="rounded-md border border-red-200 bg-white px-3 py-2 text-xs"
                        >
                          <div className="font-medium text-slate-700">
                            {row.cur.question}
                          </div>
                          <div className="mt-1 text-[11px] text-slate-500">
                            baseline {row.base.verdict} ({pct(row.baseScore)}) →
                            candidate {row.cur.verdict} ({pct(row.curScore)})
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </section>
          )}
        </div>

        {/* ── History ── */}
        <section
          data-testid="kb-calibrate-history"
          className="card h-fit overflow-hidden"
        >
          <div className="border-b border-slate-200/70 px-5 py-4">
            <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">
              Run history
            </div>
            <h3 className="mt-2 text-sm font-semibold text-slate-900">
              Past calibration runs
            </h3>
          </div>
          <div className="max-h-[40rem] divide-y divide-slate-100 overflow-y-auto">
            {loadingHistory && history.length === 0 ? (
              <div className="px-5 py-8 text-xs text-slate-400">
                Loading runs…
              </div>
            ) : history.length === 0 ? (
              <div className="px-5 py-8 text-xs text-slate-400">
                No runs yet.
              </div>
            ) : (
              history.map((run) => {
                const score = headlineScore(run.metrics);
                const selected = run.test_run_id === viewedRunId;
                return (
                  <button
                    key={run.test_run_id}
                    type="button"
                    data-testid="kb-calibrate-history-row"
                    onClick={() => setViewedRunId(run.test_run_id)}
                    className={`flex w-full items-start justify-between gap-3 px-5 py-3 text-left transition ${
                      selected ? "bg-caliber-50/50" : "hover:bg-slate-50/60"
                    }`}
                  >
                    <div className="min-w-0">
                      <div className="text-xs font-semibold text-slate-700">
                        {formatDate(run.completed_at ?? run.created_at)}
                      </div>
                      <div className="mt-0.5 truncate text-[11px] text-slate-400">
                        {retrievalModeLabel(run.retrieval_mode)}
                        {run.test_run_id === baselineRunId && (
                          <span className="ml-1 rounded-full bg-blue-50 px-1.5 py-0.5 text-[9px] font-semibold uppercase text-blue-700">
                            baseline
                          </span>
                        )}
                      </div>
                    </div>
                    <div className="text-right text-xs font-semibold text-slate-800">
                      {pct(score)}
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

export default KnowledgeCalibrateTab;
