/**
 * Shared Aria goal-plan rendering atoms.
 *
 * One source of truth for plan/step status colors, the step row, and the
 * mid-run interaction prompt, so the standalone Plans page and the inline
 * plan-in-chat card render identically. The orchestrator API and types live in
 * `@/api`; this module is presentation only.
 */

import type {
  AriaAutonomy,
  AriaInteraction,
  AriaInteractionAnswerPayload,
  AriaPlan,
  AriaPlanStatus,
  AriaPlanStep,
  AriaStepStatus,
} from "@/api/types";

const TERMINAL_PLAN_STATUSES: ReadonlySet<AriaPlanStatus> = new Set([
  "completed",
  "failed",
  "cancelled",
]);

/** A plan still worth showing in-thread (draft/approved/running/paused). */
export function isResumablePlanStatus(status: AriaPlanStatus): boolean {
  return !TERMINAL_PLAN_STATUSES.has(status);
}

/** The most recent still-open plan to restore inline (list arrives newest-first). */
export function pickResumablePlan(plans: AriaPlan[]): AriaPlan | null {
  return plans.find((p) => isResumablePlanStatus(p.status)) ?? null;
}

// Recommended default (review-then-run) first; raw enum values stay on the wire.
export const AUTONOMY_LEVELS: AriaAutonomy[] = ["approve_plan", "ask_each", "auto_guarded"];

/** Plain-language framing for the autonomy dial — "how often should Aria check in". */
export const AUTONOMY_LABELS: Record<AriaAutonomy, { label: string; hint: string }> = {
  approve_plan: {
    label: "Review the plan, then run",
    hint: "Approve the plan once; Aria runs it and only stops for risky (gated) steps.",
  },
  ask_each: {
    label: "Stop before each change",
    hint: "Aria pauses for your OK before every step that changes anything.",
  },
  auto_guarded: {
    label: "Run everything it safely can",
    hint: "Aria runs all non-gated steps without stopping; gated steps still pause.",
  },
};

export function PlanStatusBadge({ status }: { status: AriaPlanStatus }): JSX.Element {
  const cls: Record<AriaPlanStatus, string> = {
    draft: "bg-surface-200 text-gray-600",
    approved: "bg-mlflow-blue/10 text-mlflow-blue",
    running: "bg-amber-100 text-amber-700",
    paused: "bg-amber-100 text-amber-700",
    completed: "bg-emerald-100 text-emerald-700",
    failed: "bg-red-100 text-red-700",
    cancelled: "bg-gray-200 text-gray-500",
  };
  return (
    <span
      title={status}
      className={`text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${cls[status]}`}
    >
      {status}
    </span>
  );
}

export function StepStatusBadge({ status }: { status: AriaStepStatus }): JSX.Element {
  const cls: Record<AriaStepStatus, string> = {
    pending: "bg-surface-100 text-gray-500",
    blocked: "bg-surface-100 text-gray-400",
    running: "bg-amber-100 text-amber-700",
    waiting_input: "bg-violet-100 text-caliber-purple",
    waiting_job: "bg-amber-100 text-amber-700",
    done: "bg-emerald-100 text-emerald-700",
    failed: "bg-red-100 text-red-700",
    skipped: "bg-gray-200 text-gray-500",
  };
  return (
    <span
      title={status}
      className={`shrink-0 text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${cls[status]}`}
    >
      {status.replace(/_/g, " ")}
    </span>
  );
}

export function PlanStepRow({ step }: { step: AriaPlanStep }): JSX.Element {
  return (
    <li className="px-4 py-3 flex items-start gap-3">
      <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-surface-100 text-xs font-mono text-gray-600">
        {step.seq + 1}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="font-medium text-gray-900">{step.title || step.capability_key}</span>
          <code className="text-[11px] text-caliber-purple bg-violet-50 px-1.5 py-0.5 rounded">
            {step.capability_key}
          </code>
        </div>
        {step.depends_on.length > 0 && (
          <div className="text-xs text-gray-400 mt-0.5">
            depends on {step.depends_on.length} prior step(s)
          </div>
        )}
        {step.error && <div className="text-xs text-red-600 mt-0.5">{step.error}</div>}
      </div>
      <StepStatusBadge status={step.status} />
    </li>
  );
}

/**
 * A mid-run pause rendered as an actionable prompt. Handles the kinds the
 * executor raises today — `permission`/`confirm` (approve/deny, with accept
 * /reject labels for a below-gate confirm) and `choice` (one button per option).
 */
export function PlanInteractionPrompt({
  interaction,
  pending,
  onAnswer,
}: {
  interaction: AriaInteraction;
  pending: boolean;
  onAnswer: (payload: AriaInteractionAnswerPayload) => void;
}): JSX.Element {
  const isConfirm = interaction.kind === "confirm";
  const hasOptions = interaction.kind === "choice" && interaction.options.length > 0;
  const ev = interaction.evidence as { metric?: string; min?: number; value?: unknown };
  const hasGateEvidence = isConfirm && typeof ev.metric === "string" && ev.metric.length > 0;
  return (
    <div className="rounded-lg border border-violet-200 bg-violet-50 px-4 py-3">
      <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-caliber-purple">
        {isConfirm ? "Aria needs you to confirm" : "Aria needs your approval"}
        {interaction.required_scope && (
          <span className="font-mono normal-case text-[10px] bg-white/70 px-1.5 py-0.5 rounded">
            requires {interaction.required_scope}
          </span>
        )}
      </div>
      <p className="mt-1 text-sm text-gray-800">{interaction.prompt}</p>

      {/* Why it's asking — make the stakes legible, not just "Approve/Deny". */}
      {hasGateEvidence && (
        <div className="mt-2 rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-xs text-amber-800">
          <span className="font-semibold">Below quality gate.</span>{" "}
          <span className="font-mono">
            {ev.metric}: {String(ev.value)}
          </span>{" "}
          (needs ≥ {String(ev.min)}). Accepting keeps the result; rejecting skips the step.
        </div>
      )}
      {!isConfirm && interaction.required_scope && (
        <p className="mt-1.5 text-xs text-gray-500">
          Separation of duties — a different person with{" "}
          <span className="font-mono">{interaction.required_scope}</span> authority must
          approve this; you can&rsquo;t approve your own request.
        </p>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        {hasOptions ? (
          interaction.options.map((opt, i) => (
            <button
              key={i}
              type="button"
              disabled={pending}
              onClick={() => onAnswer({ value: opt.value, choice: opt.label })}
              className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
            >
              {opt.label}
            </button>
          ))
        ) : (
          <>
            <button
              type="button"
              disabled={pending}
              onClick={() => onAnswer({ approved: true })}
              className="text-sm font-medium text-white bg-emerald-600 px-3 py-1.5 rounded-md hover:bg-emerald-700 disabled:opacity-50"
            >
              {isConfirm ? "Accept" : "Approve"}
            </button>
            <button
              type="button"
              disabled={pending}
              onClick={() => onAnswer({ approved: false })}
              className="text-sm font-medium text-gray-700 bg-white border border-surface-200 px-3 py-1.5 rounded-md hover:bg-surface-50 disabled:opacity-50"
            >
              {isConfirm ? "Reject" : "Deny"}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
