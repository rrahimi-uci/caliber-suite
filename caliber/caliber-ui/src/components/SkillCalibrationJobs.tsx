import { useCallback, useEffect, useState } from "react";

import { caliberApi } from "@/api/caliberApi";
import type { RefinementJob } from "@/api/types";
import { PromptDiff } from "@/components/PromptDiff";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { relativeTime } from "@/lib/time";

/**
 * A skill's calibration runs, with the review and Apply step.
 *
 * Skill calibration queues the same refinement job a prompt does, but until now the page
 * could only say "Calibration queued": a skill candidate could be reviewed and applied only
 * through the API (alpha gap P1-20). This lists the skill's jobs, and for a candidate that
 * is ready it shows the diff, the evidence (badged when it is simulated) and an Apply
 * button. Rolling back stays in the version history on the Author stage.
 */

/** The hidden target a skill's calibration jobs are filed under. */
export function skillTargetAgentId(skillName: string): string {
  return `skill::${skillName}`;
}

interface ScoreBlock {
  overall: number | null;
}

interface EvidenceView {
  baseline: number | null;
  candidate: number | null;
  delta: number | null;
  gatePassed: boolean | null;
  gateReasons: string[];
  exampleCount: number | null;
  datasetId: string | null;
  simulated: boolean;
  provider: string | null;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function overallOf(value: unknown): ScoreBlock {
  return { overall: asNumber(asRecord(value).overall) };
}

export function evidenceView(job: RefinementJob): EvidenceView {
  const results = asRecord(job.eval_results);
  const gate = asRecord(results.gate);
  const reasons = Array.isArray(gate.reasons)
    ? gate.reasons.filter(
        (reason): reason is string => typeof reason === "string",
      )
    : [];
  return {
    baseline: overallOf(results.baseline).overall,
    candidate: overallOf(results.candidate).overall,
    delta: asNumber(asRecord(results.deltas).overall),
    gatePassed: typeof gate.passed === "boolean" ? gate.passed : null,
    gateReasons: reasons,
    exampleCount: asNumber(results.n_examples),
    datasetId:
      typeof results.eval_dataset_id === "string"
        ? results.eval_dataset_id
        : null,
    simulated: results.simulated === true,
    provider: typeof results.provider === "string" ? results.provider : null,
  };
}

function formatScore(value: number | null): string {
  return value === null ? "—" : value.toFixed(2);
}

function formatDelta(value: number | null): string {
  if (value === null) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}`;
}

function candidateText(job: RefinementJob): {
  baseline: string | null;
  candidate: string | null;
} {
  const candidate = asRecord(job.candidate);
  return {
    baseline:
      typeof candidate.baseline_content === "string"
        ? candidate.baseline_content
        : null,
    candidate: typeof candidate.content === "string" ? candidate.content : null,
  };
}

function statusVariant(
  status: string,
): "success" | "destructive" | "warning" | "secondary" {
  if (status === "applied") return "success";
  if (status === "failed" || status === "rejected") return "destructive";
  if (status === "candidate_ready") return "warning";
  return "secondary";
}

export function SkillCalibrationJobs({
  skillName,
  canOperate = true,
  reloadKey = 0,
  onApplied,
}: {
  skillName: string;
  canOperate?: boolean;
  /** Change this to reload the list, for example after a new run is queued. */
  reloadKey?: number;
  onApplied?: () => void;
}): JSX.Element {
  const agentId = skillTargetAgentId(skillName);
  const [jobs, setJobs] = useState<RefinementJob[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reviewing, setReviewing] = useState<string | null>(null);
  const [applying, setApplying] = useState(false);
  const [applied, setApplied] = useState<{
    jobId: string;
    version: unknown;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const data = await caliberApi.listJobs({ agent_id: agentId }, signal);
        if (signal?.aborted) return;
        setJobs(data);
        setLoadError(null);
      } catch (err) {
        if (signal?.aborted) return;
        setLoadError(
          err instanceof Error
            ? err.message
            : "Failed to load calibration runs",
        );
      }
    },
    [agentId],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
    // ``reloadKey`` is the caller's "something changed" signal, not a value read here.
  }, [load, reloadKey]);

  // A queued or running run is still moving; poll until every run has settled.
  const moving = jobs?.some((job) =>
    ["queued", "running"].includes(job.status),
  );
  useEffect(() => {
    if (!moving) return;
    const controller = new AbortController();
    const timer = setInterval(() => void load(controller.signal), 2000);
    return () => {
      clearInterval(timer);
      controller.abort();
    };
  }, [moving, load]);

  const sorted = [...(jobs ?? [])].sort((a, b) =>
    b.created_at.localeCompare(a.created_at),
  );

  const runApply = async (job: RefinementJob): Promise<void> => {
    setError(null);
    setApplying(true);
    try {
      const result = await caliberApi.applyJob(job.job_id);
      const details = asRecord(asRecord(result.promotion).details);
      setApplied({ jobId: job.job_id, version: details.new_version ?? null });
      setReviewing(null);
      await load();
      onApplied?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Apply failed");
    } finally {
      setApplying(false);
    }
  };

  return (
    <section
      data-testid="skill-calibration-jobs"
      className="rounded-2xl border border-slate-200/70 bg-white p-4 shadow-card"
    >
      <h3 className="text-sm font-semibold text-slate-800">Calibration runs</h3>
      <p className="mt-1 text-xs text-slate-500">
        A run ends at a candidate you review. Applying it makes the candidate
        the skill&apos;s content as a new version; roll back from the version
        history on the Author stage.
      </p>
      {(loadError || error) && (
        <div role="alert" className="mt-2 text-xs text-red-700">
          {error ?? loadError}
        </div>
      )}
      {applied && (
        <div
          role="status"
          data-testid="skill-apply-result"
          className="mt-2 rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800"
        >
          Applied {applied.jobId}
          {typeof applied.version === "number"
            ? ` — the skill is now version ${applied.version}`
            : ""}
          . Roll back from the version history on the Author stage.
        </div>
      )}
      {jobs !== null && sorted.length === 0 && (
        <p className="mt-2 text-xs text-slate-400">
          No calibration runs yet. Use Calibrate above to queue one.
        </p>
      )}
      <ul className="mt-3 space-y-3">
        {sorted.map((job) => {
          const view = evidenceView(job);
          const text = candidateText(job);
          const isOpen = reviewing === job.job_id;
          return (
            <li
              key={job.job_id}
              data-testid={`skill-job-${job.job_id}`}
              className="rounded-lg border border-slate-200 p-3 text-xs"
            >
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={statusVariant(job.status)}>{job.status}</Badge>
                <span className="font-mono">{job.job_id}</span>
                <span className="text-slate-500">
                  {job.current_stage} stage
                </span>
                {job.created_at && (
                  <span className="text-slate-400">
                    {relativeTime(job.created_at)}
                  </span>
                )}
                {job.error_message && (
                  <span className="text-red-700">{job.error_message}</span>
                )}
                {canOperate && job.status === "candidate_ready" && (
                  <Button
                    size="sm"
                    className="ml-auto"
                    data-testid={`skill-review-${job.job_id}`}
                    onClick={() => setReviewing(isOpen ? null : job.job_id)}
                  >
                    {isOpen ? "Hide review" : "Review & apply"}
                  </Button>
                )}
              </div>
              {isOpen && (
                <div
                  className="mt-3 space-y-3"
                  data-testid="skill-review-panel"
                >
                  {view.simulated && (
                    <p
                      role="status"
                      data-testid="skill-simulated-evaluation"
                      className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 font-semibold text-amber-900"
                    >
                      Simulated evaluation — not evidence. These scores are
                      canned by the {view.provider ?? "fake"} evaluator, not
                      measured. Do not apply this candidate on the strength of
                      them.
                    </p>
                  )}
                  <div className="grid grid-cols-3 gap-2">
                    {[
                      ["Baseline", formatScore(view.baseline)],
                      ["Candidate", formatScore(view.candidate)],
                      ["Change", formatDelta(view.delta)],
                    ].map(([label, value]) => (
                      <div
                        key={label}
                        className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2"
                      >
                        <div className="text-[11px] uppercase tracking-wide text-slate-500">
                          {label}
                        </div>
                        <div className="mt-0.5 text-base font-semibold text-slate-900">
                          {value}
                        </div>
                      </div>
                    ))}
                  </div>
                  <p className="text-slate-500">
                    {view.exampleCount !== null
                      ? `${view.exampleCount} evaluation examples`
                      : "Evaluation examples not recorded"}
                    {view.datasetId ? ` · ${view.datasetId}` : ""}
                    {view.gatePassed === null
                      ? ""
                      : view.gatePassed
                        ? " · gate passed"
                        : " · gate failed"}
                  </p>
                  {view.gateReasons.length > 0 && (
                    <ul className="list-disc pl-5 text-red-700">
                      {view.gateReasons.map((reason) => (
                        <li key={reason}>{reason}</li>
                      ))}
                    </ul>
                  )}
                  {text.candidate === null ? (
                    <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-red-700">
                      The candidate content is unavailable. Do not apply this
                      run until its candidate is inspected.
                    </div>
                  ) : (
                    <div data-testid="skill-candidate-diff">
                      <PromptDiff
                        baseline={text.baseline}
                        candidate={text.candidate}
                      />
                    </div>
                  )}
                  <div className="flex justify-end">
                    <Button
                      size="sm"
                      data-testid={`skill-apply-${job.job_id}`}
                      disabled={
                        applying ||
                        text.candidate === null ||
                        view.gatePassed === false
                      }
                      onClick={() => void runApply(job)}
                    >
                      {applying ? "Applying…" : "Apply to skill"}
                    </Button>
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
