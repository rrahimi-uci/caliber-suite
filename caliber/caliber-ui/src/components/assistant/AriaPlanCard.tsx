/**
 * Inline goal-plan card for the Aria chat panel.
 *
 * Renders the orchestrator's plan in the conversation and drives its lifecycle
 * without leaving chat: approve & run a draft, answer mid-run interactions
 * (permission / below-gate confirm), and observe async steps as a background
 * worker resumes them (the card polls while a step is parked). The standalone
 * Plans page remains the durable dashboard; this is the same plan, in-thread.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, caliberApi } from "@/api/caliberApi";
import type { AriaInteraction, AriaInteractionAnswerPayload, AriaPlanDetail } from "@/api/types";
import {
  AUTONOMY_LABELS,
  PlanInteractionPrompt,
  PlanStatusBadge,
  PlanStepRow,
} from "@/components/aria/planView";

const POLL_INTERVAL_MS = 4000;

export function AriaPlanCard({
  initialDetail,
  onChange,
}: {
  initialDetail: AriaPlanDetail;
  onChange?: (detail: AriaPlanDetail) => void;
}): JSX.Element {
  const [detail, setDetail] = useState<AriaPlanDetail>(initialDetail);
  const [interactions, setInteractions] = useState<AriaInteraction[]>([]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  // Adopt a freshly decomposed plan when the panel hands us a new one.
  useEffect(() => {
    setDetail(initialDetail);
    setError(null);
  }, [initialDetail]);

  const plan = detail.plan;
  const steps = detail.steps;
  const planId = plan.plan_id;
  const pendingInteractions = interactions.filter((i) => i.status === "pending");

  const apply = useCallback((next: AriaPlanDetail) => {
    setDetail(next);
    onChangeRef.current?.(next);
  }, []);

  const refreshInteractions = useCallback(async () => {
    try {
      setInteractions(await caliberApi.listAriaInteractions(planId));
    } catch {
      /* a transient interaction-list failure shouldn't break the card */
    }
  }, [planId]);

  // Load interactions on mount / plan change and whenever the plan pauses.
  useEffect(() => {
    void refreshInteractions();
  }, [refreshInteractions, plan.status]);

  const act = useCallback(
    async (fn: () => Promise<AriaPlanDetail>) => {
      setPending(true);
      setError(null);
      try {
        apply(await fn());
        await refreshInteractions();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "action failed");
      } finally {
        setPending(false);
      }
    },
    [apply, refreshInteractions],
  );

  // While running or parked on an async job, poll so the worker's progress
  // (and any new interaction) shows up in the thread without a manual refresh.
  const needsPoll =
    plan.status === "running" || steps.some((s) => s.status === "waiting_job");
  useEffect(() => {
    if (!needsPoll) return undefined;
    const id = setInterval(() => {
      caliberApi
        .pollAriaPlan(planId)
        .then(async (next) => {
          apply(next);
          await refreshInteractions();
        })
        .catch(() => {
          /* keep polling; a single failed tick is non-fatal */
        });
    }, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [needsPoll, planId, apply, refreshInteractions]);

  const isDraft = plan.status === "draft";
  const isResumable =
    (plan.status === "approved" || plan.status === "paused") &&
    pendingInteractions.length === 0;
  const hasSteps = steps.length > 0;

  const approveAndRun = (): void =>
    void act(async () => {
      await caliberApi.approveAriaPlan(planId);
      return caliberApi.executeAriaPlan(planId);
    });

  return (
    <section className="rounded-lg border border-violet-200 bg-white p-3 text-sm shadow-sm space-y-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-[10px] font-semibold uppercase tracking-wide text-caliber-purple">
            Plan
          </p>
          <p className="mt-0.5 text-sm font-semibold text-gray-900">{plan.goal}</p>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-gray-500">
            <PlanStatusBadge status={plan.status} />
            <span title={plan.autonomy}>
              {AUTONOMY_LABELS[plan.autonomy]?.label ?? plan.autonomy}
            </span>
            <span>
              {steps.length} step{steps.length !== 1 ? "s" : ""}
            </span>
          </div>
        </div>
        <Link
          to="/aria/plans"
          className="shrink-0 text-xs font-medium text-caliber-purple hover:underline"
        >
          Open in Plans →
        </Link>
      </div>

      {error && (
        <div className="rounded-md border border-red-200 bg-red-50 px-2.5 py-2 text-xs text-red-700">
          {error}
        </div>
      )}

      {hasSteps ? (
        <ol className="rounded-md border border-surface-200 divide-y divide-surface-100 overflow-hidden">
          {steps.map((step) => (
            <PlanStepRow key={step.step_id} step={step} />
          ))}
        </ol>
      ) : (
        <p className="rounded-md border border-surface-200 bg-surface-50 px-3 py-3 text-center text-xs text-gray-400">
          Aria found no platform steps for this goal — try naming a capability (e.g.
          a judge, review queue, or eval dataset).
        </p>
      )}

      {pendingInteractions.map((interaction) => (
        <PlanInteractionPrompt
          key={interaction.interaction_id}
          interaction={interaction}
          pending={pending}
          onAnswer={(payload: AriaInteractionAnswerPayload) =>
            void act(() => caliberApi.answerAriaInteraction(interaction.interaction_id, payload))
          }
        />
      ))}

      {plan.status === "completed" && (
        <p className="text-xs font-medium text-emerald-700">All steps complete.</p>
      )}

      {(isDraft || isResumable) && (
        <div className="flex items-center justify-end gap-2">
          {isDraft && (
            <button
              type="button"
              disabled={pending}
              onClick={() => void act(() => caliberApi.updateAriaPlan(planId, { status: "cancelled" }))}
              className="text-xs px-3 py-1.5 rounded-md text-gray-600 hover:bg-surface-100 disabled:opacity-50"
            >
              Cancel
            </button>
          )}
          {isDraft && hasSteps && (
            <button
              type="button"
              disabled={pending}
              onClick={approveAndRun}
              className="text-xs font-semibold text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
            >
              {pending ? "Running…" : "Approve & run"}
            </button>
          )}
          {isResumable && (
            <button
              type="button"
              disabled={pending}
              onClick={() => void act(() => caliberApi.executeAriaPlan(planId))}
              className="text-xs font-semibold text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
            >
              {plan.status === "paused" ? "Resume" : "Execute"}
            </button>
          )}
        </div>
      )}
    </section>
  );
}
