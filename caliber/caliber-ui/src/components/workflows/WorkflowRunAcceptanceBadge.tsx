import type { WorkflowRun, WorkflowRunAcceptance } from "@/api/workflowTypes";

function tone(status: string): string {
  switch (status) {
    case "accepted":
      return "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800/70 dark:bg-emerald-950/40 dark:text-emerald-300";
    case "rejected":
    case "error":
      return "border-red-200 bg-red-50 text-red-700 dark:border-red-800/70 dark:bg-red-950/40 dark:text-red-300";
    default:
      return "border-slate-200 bg-slate-50 text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300";
  }
}

/** The badge text of a run's acceptance, or `null` when the run has none (nothing to judge, or it predates acceptance). */
export function workflowRunAcceptanceLabel(
  acceptance: WorkflowRunAcceptance | null | undefined,
): string | null {
  if (!acceptance) return null;
  switch (acceptance.status) {
    case "accepted":
      return "Acceptance: accepted";
    case "rejected":
      return "Acceptance: rejected";
    case "error":
      return "Acceptance: could not be judged";
    default:
      // A status this build does not know is shown as what it is, never as an acceptance.
      return `Acceptance: ${String(acceptance.status)}`;
  }
}

/** What the badge's tooltip says: the criteria that failed, or the code of why the run could not be judged. */
export function workflowRunAcceptanceTitle(
  acceptance: WorkflowRunAcceptance | null | undefined,
): string | null {
  if (!acceptance) return null;
  if (acceptance.status === "error") {
    return `The run's effects could not be judged (${acceptance.error_code ?? "unknown"}), so the run is not accepted.`;
  }
  const decisions = acceptance.decisions ?? [];
  const failed = decisions.flatMap((decision) =>
    decision.failed_criteria.map((criterion) => `${criterion} (${decision.candidate_id})`),
  );
  if (acceptance.status === "rejected") {
    return failed.length > 0
      ? `The downstream's own record did not confirm the effect. Failed criteria: ${failed.join(", ")}.`
      : "The downstream's own record could not be read as evidence of the effect.";
  }
  return `${decisions.length} effect${decisions.length === 1 ? "" : "s"} confirmed by the downstream's own record (${acceptance.criteria_version ?? "unversioned criteria"}).`;
}

export interface WorkflowRunAcceptanceBadgeProps {
  run: Pick<WorkflowRun, "acceptance">;
  dataTestId?: string;
}

export function WorkflowRunAcceptanceBadge({
  run,
  dataTestId,
}: WorkflowRunAcceptanceBadgeProps): JSX.Element | null {
  const label = workflowRunAcceptanceLabel(run.acceptance);
  if (!run.acceptance || !label) return null;
  return (
    <span
      data-testid={dataTestId}
      title={workflowRunAcceptanceTitle(run.acceptance) ?? undefined}
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-semibold ${tone(run.acceptance.status)}`}
    >
      {label}
    </span>
  );
}
