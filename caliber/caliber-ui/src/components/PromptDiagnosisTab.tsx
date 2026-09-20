/**
 * Prompt Diagnosis tab — the per-prompt Workspace surface for "operator opens
 * a flagged item, sees why it was flagged" (`docs/two-week-alpha-plan.md`'s
 * journey, step 1-2).
 *
 * The verification queue (`GET /verification-queue`) and the refinement-job
 * pipeline (`GET /jobs`, `PipelineProgress`) both already exist and are fully
 * wired end to end; what was missing was a place in the UI that renders a
 * flagged item's own reason and links it to the job diagnosing it. This tab
 * fills that gap and hands off to the existing Calibration tab (candidate /
 * eval / Apply — see `PromptOptimizationTab`) rather than duplicating any of
 * that surface.
 *
 * Scoping: items are looked up by this prompt's `agent_id` — the same key
 * every verification-queue row is created with
 * (`caliber/src/caliber/routes/verification.py`). A linked job is found by
 * `RefinementJob.primary_item_id === item.item_id`, the FK the backend
 * itself uses to relate a job back to the item that started it
 * (`caliber/tests/fixtures/day1_seed_fixture.py::seed_flagged_job`). Verify
 * does not create a job for a general (not pre-linked) item — see
 * `routes/verification.py`'s own "Deliberately not built here" note — so a
 * freshly-verified item with no job yet is an expected, not broken, state.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { caliberApi } from "@/api/caliberApi";
import { apiErrorText } from "@/lib/apiErrors";
import { relativeTime } from "@/lib/time";
import { ListRow, ListRows } from "@/components/ListRow";
import { PipelineProgress } from "@/components/PipelineProgress";
import { SeverityBadge } from "@/components/SeverityBadge";
import { StatusBadge } from "@/components/StatusBadge";
import type { PromptInfo, RefinementJob, VerificationItem } from "@/api/types";

interface PromptDiagnosisTabProps {
  prompt: PromptInfo;
  /** Switches the parent Workspace to the Calibration stage (candidate/eval/Apply). */
  onOpenCalibration: () => void;
}

function readEvalOverall(job: RefinementJob): number | null {
  const results = job.eval_results;
  if (!results) return null;
  const candidate = results["candidate"];
  if (candidate && typeof candidate === "object") {
    const overall = (candidate as Record<string, unknown>)["overall"];
    if (typeof overall === "number") return overall;
  }
  return null;
}

function readGatePassed(job: RefinementJob): boolean | null {
  const results = job.eval_results;
  if (!results) return null;
  const gate = results["gate"];
  if (gate && typeof gate === "object") {
    const passed = (gate as Record<string, unknown>)["passed"];
    if (typeof passed === "boolean") return passed;
  }
  return null;
}

export function PromptDiagnosisTab({
  prompt,
  onOpenCalibration,
}: PromptDiagnosisTabProps): JSX.Element {
  const agentId = prompt.agent_id;

  const [items, setItems] = useState<VerificationItem[]>([]);
  const [jobs, setJobs] = useState<RefinementJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedItemId, setSelectedItemId] = useState<string | null>(null);

  const [actionItemId, setActionItemId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setLoadError(null);
      try {
        const [itemList, jobList] = await Promise.all([
          // `status: "all"` -- without it, `GET /verification-queue`
          // defaults to `status=pending` server-side
          // (`routes/verification.py::list_items`), which would make this
          // tab permanently blind to exactly the items it exists to show a
          // diagnosis for once they've been verified (or dismissed / marked
          // duplicate): this is a diagnosis/history view scoped by agent,
          // not a pending-only action queue -- a verified item with a
          // linked job (this component's own primary case, per the module
          // docstring) must stay visible after verification.
          caliberApi.listVerificationItems(
            { agent_id: agentId, status: "all" },
            signal,
          ),
          caliberApi.listJobs({ agent_id: agentId }, signal),
        ]);
        if (signal?.aborted) return;
        const sorted = [...itemList].sort(
          (a, b) =>
            new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
        );
        setItems(sorted);
        setJobs(jobList.filter((job) => job.artifact_type === "prompt"));
        setSelectedItemId((prev) => {
          if (prev && sorted.some((item) => item.item_id === prev)) return prev;
          return sorted[0]?.item_id ?? null;
        });
      } catch (err) {
        if (!signal?.aborted) {
          setLoadError(apiErrorText(err, "Failed to load flagged items"));
        }
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [agentId],
  );

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  const selectedItem = useMemo(
    () => items.find((item) => item.item_id === selectedItemId) ?? null,
    [items, selectedItemId],
  );
  const linkedJob = useMemo(
    () =>
      selectedItem
        ? (jobs.find((job) => job.primary_item_id === selectedItem.item_id) ??
          null)
        : null,
    [jobs, selectedItem],
  );

  const runVerify = useCallback(
    async (itemId: string) => {
      setActionItemId(itemId);
      setActionError(null);
      try {
        await caliberApi.verifyItem(itemId);
        await refresh();
      } catch (err) {
        setActionError(apiErrorText(err, "Failed to verify item"));
      } finally {
        setActionItemId(null);
      }
    },
    [refresh],
  );

  const runDismiss = useCallback(
    async (itemId: string) => {
      setActionItemId(itemId);
      setActionError(null);
      try {
        await caliberApi.dismissItem(itemId);
        await refresh();
      } catch (err) {
        setActionError(apiErrorText(err, "Failed to dismiss item"));
      } finally {
        setActionItemId(null);
      }
    },
    [refresh],
  );

  if (loading && items.length === 0) {
    return (
      <div className="rounded-2xl border border-slate-200/70 bg-white p-8 text-center text-sm text-slate-500 shadow-card">
        Loading flagged items…
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
        {loadError}
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-8 py-12 text-center text-sm text-slate-500">
        No flagged items for this prompt yet. Items appear here once a trace
        is flagged for this prompt&apos;s agent and lands on the verification
        queue.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,20rem)_1fr]">
      <div>
        <h2 className="mb-2 text-sm font-semibold text-slate-700">
          Flagged items
        </h2>
        <ListRows testId="diagnosis-item-list">
          {items.map((item) => (
            <ListRow
              key={item.item_id}
              testId={`diagnosis-item-row-${item.item_id}`}
              onClick={() => setSelectedItemId(item.item_id)}
              className={
                item.item_id === selectedItemId
                  ? "bg-violet-50/70 dark:bg-violet-950/20"
                  : ""
              }
              title={item.category}
              subtitle={`${relativeTime(item.created_at)}`}
              columns={
                <div className="flex items-center gap-2">
                  <SeverityBadge severity={item.severity} />
                  <StatusBadge status={item.status} />
                </div>
              }
            />
          ))}
        </ListRows>
      </div>

      {selectedItem && (
        <div
          data-testid="diagnosis-item-detail"
          className="space-y-4 rounded-2xl border border-slate-200/70 bg-white p-5 shadow-card"
        >
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 className="text-sm font-semibold text-slate-900">
                Why this was flagged
              </h3>
              <p className="mt-0.5 text-xs text-slate-500">
                {selectedItem.category} · flagged{" "}
                {relativeTime(selectedItem.created_at)}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <SeverityBadge severity={selectedItem.severity} />
              <StatusBadge status={selectedItem.status} />
            </div>
          </div>

          <p className="whitespace-pre-wrap rounded-lg bg-slate-50 p-3 text-sm text-slate-700">
            {selectedItem.free_text}
          </p>

          <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-xs text-slate-500 sm:grid-cols-2">
            {selectedItem.trace_id && (
              <div>
                <dt className="inline font-medium text-slate-600">
                  Trace:{" "}
                </dt>
                <dd className="inline font-mono">{selectedItem.trace_id}</dd>
              </div>
            )}
            {selectedItem.session_id && (
              <div>
                <dt className="inline font-medium text-slate-600">
                  Session:{" "}
                </dt>
                <dd className="inline font-mono">
                  {selectedItem.session_id}
                </dd>
              </div>
            )}
            {selectedItem.artifact_ref && (
              <div>
                <dt className="inline font-medium text-slate-600">
                  Artifact:{" "}
                </dt>
                <dd className="inline font-mono">
                  {selectedItem.artifact_ref}
                </dd>
              </div>
            )}
            {selectedItem.verified_by && (
              <div>
                <dt className="inline font-medium text-slate-600">
                  Verified by:{" "}
                </dt>
                <dd className="inline">{selectedItem.verified_by}</dd>
              </div>
            )}
          </dl>

          {actionError && (
            <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {actionError}
            </div>
          )}

          {selectedItem.status === "pending" && (
            <div className="flex items-center gap-2 border-t border-slate-100 pt-3">
              <button
                type="button"
                data-testid="diagnosis-verify-btn"
                disabled={actionItemId === selectedItem.item_id}
                onClick={() => void runVerify(selectedItem.item_id)}
                className="rounded-lg bg-caliber-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-caliber-700 disabled:opacity-50"
              >
                {actionItemId === selectedItem.item_id
                  ? "Verifying…"
                  : "Verify"}
              </button>
              <button
                type="button"
                data-testid="diagnosis-dismiss-btn"
                disabled={actionItemId === selectedItem.item_id}
                onClick={() => void runDismiss(selectedItem.item_id)}
                className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-semibold text-slate-600 hover:bg-slate-50 disabled:opacity-50"
              >
                Dismiss
              </button>
            </div>
          )}

          <div className="border-t border-slate-100 pt-4">
            <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Diagnosis → candidate → eval
            </h4>
            {linkedJob ? (
              <div className="space-y-3" data-testid="diagnosis-linked-job">
                <PipelineProgress
                  currentStage={linkedJob.current_stage}
                  status={linkedJob.status}
                />
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
                  <span>
                    Job:{" "}
                    <span className="font-mono text-slate-700">
                      {linkedJob.job_id}
                    </span>
                  </span>
                  <span>
                    Status: <StatusBadge status={linkedJob.status} />
                  </span>
                  {readEvalOverall(linkedJob) !== null && (
                    <span>
                      Eval score:{" "}
                      <span className="font-semibold text-slate-700">
                        {(
                          (readEvalOverall(linkedJob) as number) * 100
                        ).toFixed(1)}
                        %
                      </span>
                    </span>
                  )}
                  {readGatePassed(linkedJob) !== null && (
                    <span
                      className={
                        readGatePassed(linkedJob)
                          ? "font-semibold text-emerald-600"
                          : "font-semibold text-red-600"
                      }
                    >
                      Gate {readGatePassed(linkedJob) ? "passed" : "failed"}
                    </span>
                  )}
                </div>
                {linkedJob.status === "candidate_ready" && (
                  <button
                    type="button"
                    data-testid="diagnosis-open-calibration-btn"
                    onClick={onOpenCalibration}
                    className="rounded-lg bg-caliber-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-caliber-700"
                  >
                    Review candidate in Calibration →
                  </button>
                )}
                {linkedJob.status !== "candidate_ready" && (
                  <button
                    type="button"
                    data-testid="diagnosis-open-calibration-btn"
                    onClick={onOpenCalibration}
                    className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-semibold text-slate-600 hover:bg-slate-50"
                  >
                    View in Calibration →
                  </button>
                )}
              </div>
            ) : (
              <div className="rounded-lg border border-dashed border-slate-200 bg-slate-50 p-3 text-xs text-slate-500">
                {selectedItem.status === "pending"
                  ? "No refinement job yet — verify this item, then start a calibration run from the Calibration tab."
                  : "No refinement job is linked to this item yet."}
                <div className="mt-2">
                  <button
                    type="button"
                    data-testid="diagnosis-open-calibration-btn"
                    onClick={onOpenCalibration}
                    className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 hover:bg-slate-50"
                  >
                    Go to Calibration →
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
