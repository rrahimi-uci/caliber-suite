/**
 * Dashboard — CALIBER operational landing surface.
 *
 * Surfaces key fleet metrics, current operational pressure, and recent
 * activity so operators can triage and act without jumping across tabs.
 */

import { useCallback, useMemo, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { caliberApi } from "@/api/caliberApi";
import type { RefinementJob } from "@/api/types";
import { useDashboardSummaryContext } from "@/components/DashboardSummaryContext";
import { useApi } from "@/hooks/useApi";
import {
  humanizeWorkflowCalibrationLabel as humanizeLabel,
  workflowCalibrationView,
} from "@/lib/workflowCalibration";
import { relativeTime } from "@/lib/time";

type Tone = "slate" | "blue" | "emerald" | "amber" | "red" | "violet";

const TONE_CLASSES: Record<Tone, { panel: string; text: string; bar: string; chip: string }> = {
  slate: {
    panel: "border-slate-200 bg-slate-50",
    text: "text-slate-700",
    bar: "bg-slate-400",
    chip: "border-slate-200 bg-slate-50 text-slate-700",
  },
  blue: {
    panel: "border-blue-200/70 bg-blue-50",
    text: "text-blue-700",
    bar: "bg-blue-500",
    chip: "border-blue-200/70 bg-blue-50 text-blue-700",
  },
  emerald: {
    panel: "border-emerald-200/70 bg-emerald-50",
    text: "text-emerald-700",
    bar: "bg-emerald-500",
    chip: "border-emerald-200/70 bg-emerald-50 text-emerald-700",
  },
  amber: {
    panel: "border-amber-200/70 bg-amber-50",
    text: "text-amber-700",
    bar: "bg-amber-500",
    chip: "border-amber-200/70 bg-amber-50 text-amber-700",
  },
  red: {
    panel: "border-red-200/70 bg-red-50",
    text: "text-red-700",
    bar: "bg-red-500",
    chip: "border-red-200/70 bg-red-50 text-red-700",
  },
  violet: {
    panel: "border-violet-200/70 bg-violet-50",
    text: "text-violet-700",
    bar: "bg-violet-500",
    chip: "border-violet-200/70 bg-violet-50 text-violet-700",
  },
};

type ActivityType = "job" | "workflow";

interface ActivityItem {
  id: string;
  type: ActivityType;
  title: string;
  detail: string;
  tone: Tone;
  /** Where the row links to, or `null` for a non-clickable row. */
  href: string | null;
  timestamp: string;
}

interface WorkflowDeliveryItem {
  jobId: string;
  workflowId: string;
  agentId: string;
  status: RefinementJob["status"];
  objective: string | null;
  judgeEnabled: boolean;
  candidateSummary: string | null;
  targetAlias: string | null;
  gateBlocked: boolean;
  lowConfidence: boolean;
  tone: Tone;
  href: string;
  updatedAt: string;
}

interface WorkflowDeliverySummary {
  totalCalibrations: number;
  activeCount: number;
  activeWorkflowCount: number;
  candidateReadyCount: number;
  gateBlockedCount: number;
  lowConfidenceCount: number;
  judgeEnabledCount: number;
  items: WorkflowDeliveryItem[];
}

type WorkflowCalibrationData = NonNullable<ReturnType<typeof workflowCalibrationView>>;

export function Dashboard(): JSX.Element {
  const { data, error, loading, refresh } = useDashboardSummaryContext();

  const jobsState = useApi(
    useCallback((signal: AbortSignal) => caliberApi.listJobs({}, signal), []),
  );

  const activeRefinements = (data?.jobs_running ?? 0) + (data?.jobs_queued ?? 0);
  const standardVerification = Math.max(
    0,
    (data?.verification_pending ?? 0) - (data?.verification_pending_critical ?? 0),
  );
  const openReviewWork = (data?.verification_pending ?? 0) + activeRefinements;
  const agentCoverage = percentOf(data?.agents_enabled ?? 0, data?.agents_total ?? 0);
  const assistantSlo = data?.assistant_slo;
  const executionRate = assistantSlo?.execution_success_rate ?? 0;
  const publishRate = assistantSlo?.publish_success_rate ?? 0;

  const recentActivity = useMemo(() => {
    return buildRecentActivity({
      jobs: jobsState.data ?? [],
    });
  }, [jobsState.data]);
  const workflowDelivery = useMemo(
    () => buildWorkflowDeliverySummary(jobsState.data ?? []),
    [jobsState.data],
  );

  const refreshAll = (): void => {
    refresh();
    jobsState.refresh();
  };

  const activityLoading = jobsState.loading;
  const activityError = jobsState.error?.message;

  return (
    <div className="space-y-6" data-testid="dashboard-page">
      <section className="overflow-hidden rounded-lg border border-slate-200 bg-white shadow-card">
        <div className="flex flex-wrap items-start justify-between gap-4 border-b border-slate-100 px-5 py-4">
          <div>
            <div className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">
              CALIBER Operations
            </div>
            <h1 className="mt-2 text-3xl font-semibold tracking-tight text-slate-900">Dashboard</h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-500">
              Central command surface for verification load, refinement throughput, and
              fleet reliability.
            </p>
          </div>
          <div className="flex items-center gap-2 text-xs text-slate-500">
            {data && <span>Updated {relativeTime(data.generated_at)}</span>}
            <button
              type="button"
              onClick={refreshAll}
              disabled={loading || activityLoading}
              className="inline-flex items-center gap-2 rounded-md border border-slate-200 bg-white px-3 py-1.5 font-medium text-slate-700 shadow-sm transition hover:border-slate-300 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <RefreshIcon spinning={loading || activityLoading} />
              Refresh
            </button>
          </div>
        </div>

        <div className="grid gap-4 px-5 py-5 md:grid-cols-2 xl:grid-cols-4">
          <SummaryCard
            label="Open work"
            value={loading && !data ? "..." : openReviewWork.toLocaleString()}
            detail="Verification + refinement"
            tone={openReviewWork > 0 ? "amber" : "emerald"}
          />
          <SummaryCard
            label="Verification"
            value={loading && !data ? "..." : String(data?.verification_pending ?? 0)}
            detail={
              data
                ? `${data.verification_pending_critical} critical · ${standardVerification} standard`
                : "Pending verification items"
            }
            tone={(data?.verification_pending_critical ?? 0) > 0 ? "red" : "blue"}
          />
          <SummaryCard
            label="Refinements"
            value={loading && !data ? "..." : String(activeRefinements)}
            detail={data ? `${data.jobs_running} running · ${data.jobs_queued} queued` : "Job activity"}
            tone={activeRefinements > 0 ? "blue" : "slate"}
          />
          <SummaryCard
            label="Fleet coverage"
            value={loading && !data ? "..." : `${agentCoverage}%`}
            detail={data ? `${data.agents_enabled}/${data.agents_total} agents enabled` : "Agent posture"}
            tone={agentCoverage >= 80 ? "emerald" : "amber"}
          />
        </div>
      </section>

      {error && <ErrorBanner message={error.message} onRetry={refreshAll} />}

      <section className="grid gap-5 xl:grid-cols-[minmax(0,1.05fr)_minmax(360px,0.95fr)]">
        <Panel
          title="Operational Status"
          subtitle="Current pressure across the delivery lifecycle"
          action={<PanelLink to="/workflows">Open workflows</PanelLink>}
        >
          <div className="space-y-4">
            <MetricBar
              label="Verification queue"
              value={data?.verification_pending ?? 0}
              max={Math.max(1, data?.verification_pending ?? 0, data?.jobs_completed ?? 0)}
              tone={(data?.verification_pending_critical ?? 0) > 0 ? "red" : "blue"}
              detail={`${data?.verification_pending_critical ?? 0} critical items`}
            />
            <MetricBar
              label="Active refinement"
              value={activeRefinements}
              max={Math.max(1, activeRefinements, data?.jobs_completed ?? 0)}
              tone={activeRefinements > 0 ? "violet" : "slate"}
              detail={`${data?.jobs_running ?? 0} running · ${data?.jobs_queued ?? 0} queued`}
            />
            <MetricBar
              label="Completed runs"
              value={data?.jobs_completed ?? 0}
              max={Math.max(1, data?.jobs_completed ?? 0)}
              tone={(data?.jobs_failed ?? 0) > 0 ? "red" : "emerald"}
              detail={`${data?.jobs_failed ?? 0} failed · ${data?.jobs_rejected ?? 0} rejected`}
            />
          </div>

          <div className="mt-6 grid gap-3 rounded-lg border border-slate-200 bg-slate-50 p-4 sm:grid-cols-2">
            <ReliabilityTile
              label="Execution success"
              value={executionRate}
              detail={assistantSlo ? `${assistantSlo.executions_completed}/${assistantSlo.executions_total} completed` : "Awaiting signal"}
              tone={executionRate >= 0.9 ? "emerald" : executionRate >= 0.75 ? "amber" : "red"}
            />
            <ReliabilityTile
              label="Publish success"
              value={publishRate}
              detail={assistantSlo ? `${assistantSlo.publish_success}/${assistantSlo.publish_total} published` : "Awaiting signal"}
              tone={publishRate >= 0.9 ? "emerald" : publishRate >= 0.75 ? "amber" : "red"}
            />
          </div>
        </Panel>

        <Panel
          title="Recent Activity"
          subtitle="Latest jobs and workflow calibrations"
          action={<PanelLink to="/workflows">Open workflows</PanelLink>}
        >
          {activityError && (
            <div className="mb-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {activityError}
            </div>
          )}

          {activityLoading && recentActivity.length === 0 ? (
            <SkeletonRows rows={5} />
          ) : recentActivity.length === 0 ? (
            <div className="rounded-md border border-slate-200 bg-slate-50 px-4 py-6 text-sm text-slate-500">
              No recent events yet.
            </div>
          ) : (
            <div className="space-y-2">
              {recentActivity.map((item) => {
                const body = (
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <StatusChip tone={item.tone}>{activityLabel(item.type)}</StatusChip>
                        <span className="text-sm font-medium text-slate-900">{item.title}</span>
                      </div>
                      <p className="mt-1 truncate text-xs text-slate-500">{item.detail}</p>
                    </div>
                    <span className="shrink-0 text-[11px] text-slate-400">{relativeTime(item.timestamp)}</span>
                  </div>
                );
                const key = `${item.type}-${item.id}`;
                return item.href ? (
                  <Link
                    key={key}
                    to={item.href}
                    className="block rounded-md border border-slate-200 bg-white px-3 py-3 transition hover:border-slate-300 hover:bg-slate-50"
                  >
                    {body}
                  </Link>
                ) : (
                  <div
                    key={key}
                    className="block rounded-md border border-slate-200 bg-white px-3 py-3"
                  >
                    {body}
                  </div>
                );
              })}
            </div>
          )}
        </Panel>
      </section>

      <Panel
        title="Workflow Delivery Lane"
        subtitle="Workflow calibrations in flight, gate blockers, and judge coverage across recent runs"
        action={<PanelLink to="/workflows">Open workflows</PanelLink>}
      >
        <div data-testid="workflow-delivery-panel">
          {workflowDelivery.totalCalibrations === 0 ? (
            <div className="rounded-md border border-slate-200 bg-slate-50 px-4 py-6 text-sm text-slate-500">
              No workflow calibration jobs have been recorded yet.
            </div>
          ) : (
            <>
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                <CompactMetricTile
                  label="Active calibrations"
                  value={workflowDelivery.activeCount}
                  detail={`${workflowDelivery.activeWorkflowCount} workflows in flight`}
                  tone={workflowDelivery.activeCount > 0 ? "violet" : "slate"}
                />
                <CompactMetricTile
                  label="Candidate ready"
                  value={workflowDelivery.candidateReadyCount}
                  detail="Calibrations with a candidate ready to apply"
                  tone={workflowDelivery.candidateReadyCount > 0 ? "amber" : "emerald"}
                />
                <CompactMetricTile
                  label="Gate blocked"
                  value={workflowDelivery.gateBlockedCount}
                  detail="Active calibrations with blocker reasons"
                  tone={workflowDelivery.gateBlockedCount > 0 ? "red" : "emerald"}
                />
                <CompactMetricTile
                  label="LLM judge"
                  value={workflowDelivery.judgeEnabledCount}
                  detail="Active calibrations using provider-backed scoring"
                  tone={workflowDelivery.judgeEnabledCount > 0 ? "blue" : "slate"}
                />
              </div>

              <div className="mt-5">
                <div className="mb-2 flex items-center justify-between gap-3">
                  <div className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-500">
                    Recent workflow calibrations
                  </div>
                  <div className="text-[11px] text-slate-400">
                    {workflowDelivery.totalCalibrations} tracked
                  </div>
                </div>
                <div className="space-y-2">
                  {workflowDelivery.items.map((item) => (
                    <Link
                      key={item.jobId}
                      to={item.href}
                      className="block rounded-md border border-slate-200 bg-white px-3 py-3 transition hover:border-slate-300 hover:bg-slate-50"
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <StatusChip tone={item.tone}>Workflow</StatusChip>
                            <span className="font-mono text-xs font-semibold text-violet-700">
                              {item.workflowId}
                            </span>
                            <span className="text-sm font-medium text-slate-900">
                              {humanizeState(item.status)}
                            </span>
                            {item.objective && (
                              <StatusChip tone="slate">{humanizeLabel(item.objective)}</StatusChip>
                            )}
                            {item.judgeEnabled && <StatusChip tone="blue">LLM judge</StatusChip>}
                            {item.gateBlocked && <StatusChip tone="red">Gate blocked</StatusChip>}
                            {item.lowConfidence && <StatusChip tone="amber">Low confidence</StatusChip>}
                          </div>
                          <p className="mt-1 text-xs text-slate-500">
                            {[
                              item.agentId,
                              item.targetAlias ? `alias ${item.targetAlias}` : null,
                              item.candidateSummary,
                            ]
                              .filter(Boolean)
                              .join(" · ")}
                          </p>
                        </div>
                        <span className="shrink-0 text-[11px] text-slate-400">
                          {relativeTime(item.updatedAt)}
                        </span>
                      </div>
                    </Link>
                  ))}
                </div>
              </div>
            </>
          )}
        </div>
      </Panel>

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <ActionTile to="/prompts" title="Prompts" detail="Version + calibrate" tone="violet" />
        <ActionTile to="/tools" title="Tools" detail="Registry" tone="blue" />
        <ActionTile to="/skills" title="Skills" detail="Compose behavior" tone="emerald" />
        <ActionTile to="/workflows" title="Workflows" detail="Build + run" tone="violet" />
      </section>
    </div>
  );
}

// Backwards-compatible export while the route/tests migrate to Dashboard naming.
export const Overview = Dashboard;

function buildRecentActivity({
  jobs,
}: {
  jobs: RefinementJob[];
}): ActivityItem[] {
  const items: ActivityItem[] = [];

  for (const job of jobs.slice(0, 8)) {
    const calibration = workflowCalibrationView(job);
    if (calibration) {
      items.push({
        id: job.job_id,
        type: "workflow",
        title: `${calibration.workflowId} · ${humanizeState(job.status)}`,
        detail: buildWorkflowActivityDetail(job, calibration),
        tone: workflowTone(job.status, calibration.gatePassed === false),
        // Workflow calibration now lives on the workflow detail page.
        href: `/workflows/${encodeURIComponent(calibration.workflowId)}`,
        timestamp: job.updated_at,
      });
      continue;
    }
    items.push({
      id: job.job_id,
      type: "job",
      title: `${job.agent_id} · ${job.status}`,
      detail: `${job.current_stage} stage · ${job.artifact_type}`,
      tone: job.status === "failed" || job.status === "rejected" ? "red" : job.status === "candidate_ready" ? "amber" : "violet",
      // Generic refinement jobs are surfaced inline on their owning surface,
      // so this row is informational.
      href: null,
      timestamp: job.updated_at,
    });
  }

  return items
    .sort((a, b) => toTimestamp(b.timestamp) - toTimestamp(a.timestamp))
    .slice(0, 10);
}

function buildWorkflowDeliverySummary(jobs: RefinementJob[]): WorkflowDeliverySummary {
  const rows = jobs
    .map<WorkflowDeliveryItem | null>((job) => {
      const calibration = workflowCalibrationView(job);
      if (!calibration) return null;
      return {
        jobId: job.job_id,
        workflowId: calibration.workflowId,
        agentId: job.agent_id,
        status: job.status,
        objective: calibration.objective,
        judgeEnabled: calibration.judgeEnabled,
        candidateSummary: workflowCandidateSummary(calibration),
        targetAlias: calibration.targetAlias,
        gateBlocked: calibration.gatePassed === false,
        lowConfidence: calibration.lowConfidence,
        tone: workflowTone(job.status, calibration.gatePassed === false),
        href: `/workflows/${encodeURIComponent(calibration.workflowId)}`,
        updatedAt: job.updated_at,
      };
    })
    .filter((row): row is WorkflowDeliveryItem => row !== null)
    .sort((a, b) => toTimestamp(b.updatedAt) - toTimestamp(a.updatedAt));

  const activeRows = rows.filter((row) => isActiveWorkflowStatus(row.status));
  const activeWorkflowIds = new Set(activeRows.map((row) => row.workflowId));
  const displayRows = (activeRows.length > 0 ? activeRows : rows).slice(0, 5);

  return {
    totalCalibrations: rows.length,
    activeCount: activeRows.length,
    activeWorkflowCount: activeWorkflowIds.size,
    candidateReadyCount: activeRows.filter((row) => row.status === "candidate_ready").length,
    gateBlockedCount: activeRows.filter((row) => row.gateBlocked).length,
    lowConfidenceCount: activeRows.filter((row) => row.lowConfidence).length,
    judgeEnabledCount: activeRows.filter((row) => row.judgeEnabled).length,
    items: displayRows,
  };
}

function buildWorkflowActivityDetail(
  job: RefinementJob,
  calibration: WorkflowCalibrationData,
): string {
  return [
    job.agent_id,
    calibration.objective ? `objective ${humanizeLabel(calibration.objective)}` : null,
    calibration.judgeEnabled ? "LLM judge" : "structural scoring",
    workflowCandidateSummary(calibration),
    calibration.gatePassed === false
      ? "gate blocked"
      : job.status === "candidate_ready"
        ? "candidate ready to apply"
        : null,
    calibration.lowConfidence ? "low confidence" : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

function workflowCandidateSummary(calibration: WorkflowCalibrationData): string | null {
  if (calibration.maxCandidates !== null) {
    return `${calibration.candidates.length}/${calibration.maxCandidates} candidates`;
  }
  if (calibration.candidates.length > 0) {
    return `${calibration.candidates.length} candidates`;
  }
  if (calibration.winnerId) {
    return `winner ${calibration.winnerId}`;
  }
  return null;
}

function workflowTone(status: string, gateBlocked: boolean): Tone {
  if (status === "failed" || status === "rejected" || gateBlocked) return "red";
  if (status === "candidate_ready") return "amber";
  return "violet";
}

function isActiveWorkflowStatus(status: string): boolean {
  return status === "queued" || status === "running" || status === "candidate_ready";
}

function toTimestamp(value: string): number {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function SummaryCard({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail: string;
  tone: Tone;
}): JSX.Element {
  const styles = TONE_CLASSES[tone];
  return (
    <div className={`rounded-lg border px-4 py-3 ${styles.panel}`}>
      <div className="text-xs font-medium text-slate-500">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tracking-tight ${styles.text}`}>{value}</div>
      <div className="mt-1 text-xs text-slate-500">{detail}</div>
    </div>
  );
}

function Panel({
  title,
  subtitle,
  action,
  children,
}: {
  title: string;
  subtitle: string;
  action?: ReactNode;
  children: ReactNode;
}): JSX.Element {
  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-card">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-100 px-5 py-4">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
          <p className="mt-1 text-xs leading-5 text-slate-500">{subtitle}</p>
        </div>
        {action}
      </div>
      <div className="p-5">{children}</div>
    </section>
  );
}

function PanelLink({ to, children }: { to: string; children: ReactNode }): JSX.Element {
  return (
    <Link
      to={to}
      className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 transition hover:border-caliber-200 hover:text-caliber-purple"
    >
      {children}
      <ChevronIcon />
    </Link>
  );
}

function MetricBar({
  label,
  value,
  max,
  detail,
  tone,
}: {
  label: string;
  value: number;
  max: number;
  detail: string;
  tone: Tone;
}): JSX.Element {
  const styles = TONE_CLASSES[tone];
  const width = Math.max(4, Math.min(100, Math.round((value / Math.max(1, max)) * 100)));
  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-medium text-slate-900">{label}</div>
          <div className="text-xs text-slate-500">{detail}</div>
        </div>
        <div className={`text-sm font-semibold ${styles.text}`}>{value.toLocaleString()}</div>
      </div>
      <div className="h-2 rounded-full bg-slate-100">
        <div className={`h-full rounded-full ${styles.bar}`} style={{ width: `${value <= 0 ? 0 : width}%` }} />
      </div>
    </div>
  );
}

function ReliabilityTile({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: number;
  detail: string;
  tone: Tone;
}): JSX.Element {
  const styles = TONE_CLASSES[tone];
  const clamped = clampRate(value);
  return (
    <div className="rounded-md border border-slate-200 bg-white px-3 py-3">
      <div className="text-xs font-medium text-slate-500">{label}</div>
      <div className={`mt-1 text-xl font-semibold ${styles.text}`}>{formatRate(clamped)}</div>
      <div className="mt-1 text-[11px] text-slate-500">{detail}</div>
    </div>
  );
}

function CompactMetricTile({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: number;
  detail: string;
  tone: Tone;
}): JSX.Element {
  const styles = TONE_CLASSES[tone];
  return (
    <div className={`rounded-md border px-3 py-3 ${styles.panel}`}>
      <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tracking-tight ${styles.text}`}>
        {value.toLocaleString()}
      </div>
      <div className="mt-1 text-[11px] text-slate-500">{detail}</div>
    </div>
  );
}

function StatusChip({ tone, children }: { tone: Tone; children: ReactNode }): JSX.Element {
  const styles = TONE_CLASSES[tone];
  return (
    <span className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold ${styles.chip}`}>
      {children}
    </span>
  );
}

function ActionTile({
  to,
  title,
  detail,
  tone,
}: {
  to: string;
  title: string;
  detail: string;
  tone: Tone;
}): JSX.Element {
  const styles = TONE_CLASSES[tone];
  return (
    <Link
      to={to}
      className="group rounded-lg border border-slate-200 bg-white p-4 transition hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-card-hover"
    >
      <div className="text-xs font-semibold uppercase tracking-wide text-slate-400">{detail}</div>
      <div className={`mt-2 text-lg font-semibold ${styles.text}`}>{title}</div>
      <div className="mt-3 text-xs text-slate-500 transition group-hover:text-slate-700">Open workspace</div>
    </Link>
  );
}

function ErrorBanner({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}): JSX.Element {
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="font-medium">Failed to load dashboard</div>
          <div className="mt-0.5 text-xs text-red-600">{message}</div>
        </div>
        <button
          type="button"
          onClick={onRetry}
          className="rounded-md border border-red-200 bg-white px-2.5 py-1 text-xs font-semibold text-red-700 transition hover:bg-red-100"
        >
          Retry
        </button>
      </div>
    </div>
  );
}

function SkeletonRows({ rows }: { rows: number }): JSX.Element {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="h-14 animate-pulse rounded bg-slate-100" />
      ))}
    </div>
  );
}

function activityLabel(type: ActivityType): string {
  if (type === "workflow") return "Workflow";
  return "Job";
}

function humanizeState(value: string): string {
  const label = value.replaceAll("_", " ");
  return `${label.charAt(0).toUpperCase()}${label.slice(1)}`;
}

function formatRate(value: number): string {
  return `${Math.round(clampRate(value) * 100)}%`;
}

function clampRate(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

function percentOf(part: number, total: number): number {
  if (total <= 0) return 0;
  return Math.round((part / total) * 100);
}

function RefreshIcon({ spinning }: { spinning: boolean }): JSX.Element {
  return (
    <svg
      className={`h-3.5 w-3.5 ${spinning ? "animate-spin" : ""}`}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
    >
      <path d="M21 12a9 9 0 1 1-2.64-6.36" />
      <path d="M21 3v6h-6" />
    </svg>
  );
}

function ChevronIcon(): JSX.Element {
  return (
    <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <path d="M9 18l6-6-6-6" />
    </svg>
  );
}
