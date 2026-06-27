/**
 * Aria Plans — goal-plans the assistant decomposes into capability steps.
 *
 * Implemented surface: decompose a goal into a draft plan, inspect the step
 * DAG, tune autonomy, approve, execute or resume, and answer mid-run
 * interactions. Mirrors the Review Queues chrome (list + detail coordinator).
 */

import { useCallback, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, caliberApi } from "@/api/caliberApi";
import type { AriaAutonomy } from "@/api/types";
import { CopyButton } from "@/components/CopyButton";
import {
  AUTONOMY_LABELS,
  AUTONOMY_LEVELS,
  PlanInteractionPrompt,
  PlanStatusBadge,
  PlanStepRow,
} from "@/components/aria/planView";
import { useApi } from "@/hooks/useApi";
import { relativeTime } from "@/lib/time";

export function AriaPlans(): JSX.Element {
  const [openPlanId, setOpenPlanId] = useState<string | null>(null);
  if (openPlanId) {
    return <PlanDetail planId={openPlanId} onBack={() => setOpenPlanId(null)} />;
  }
  return <PlanList onOpen={setOpenPlanId} />;
}

/* -------------------------------------------------------------------------- */

function PlanList({ onOpen }: { onOpen: (id: string) => void }): JSX.Element {
  const fetcher = useCallback((signal: AbortSignal) => caliberApi.listAriaPlans(null, signal), []);
  const { data, error, loading, refresh } = useApi(fetcher, []);
  const [goal, setGoal] = useState("");
  const [autonomy, setAutonomy] = useState<AriaAutonomy>("approve_plan");
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const decompose = async (): Promise<void> => {
    if (!goal.trim()) return;
    setSubmitting(true);
    setActionError(null);
    try {
      const detail = await caliberApi.createAriaPlan({ goal: goal.trim(), autonomy });
      setGoal("");
      refresh();
      onOpen(detail.plan.plan_id);
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "failed to decompose goal");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <div className="flex items-center gap-2 text-sm text-gray-500 mb-4">
        <Link to="/" className="hover:text-gray-700">
          Dashboard
        </Link>
        <Chevron />
        <span className="text-gray-900 font-medium">Plans</span>
      </div>

      <div className="mb-6">
        <h1 className="text-xl font-semibold text-gray-900">Aria Plans</h1>
        <p className="text-sm text-gray-500 mt-0.5">
          Hand Aria a goal; it decomposes it into a plan of platform steps you can
          review and approve before anything runs.
        </p>
      </div>

      <div className="mb-6 bg-white rounded-lg border border-surface-200 p-4">
        <label className="text-xs text-gray-500 block mb-1">Goal</label>
        <textarea
          aria-label="Goal"
          className="w-full border border-surface-200 rounded-md px-3 py-2 text-sm h-20 focus:ring-1 focus:ring-caliber-purple focus:border-caliber-purple"
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          placeholder="e.g. Create a faithfulness judge and a review queue for the support agent."
        />
        <div className="mt-3 flex items-center justify-between">
          <label className="text-xs text-gray-500 flex items-center gap-2">
            Autonomy
            <select
              aria-label="Autonomy"
              className="border border-surface-200 rounded-md px-2 py-1 text-sm bg-white"
              value={autonomy}
              onChange={(e) => setAutonomy(e.target.value as AriaAutonomy)}
            >
              {AUTONOMY_LEVELS.map((a) => (
                <option key={a} value={a}>
                  {AUTONOMY_LABELS[a].label}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            disabled={!goal.trim() || submitting}
            onClick={() => void decompose()}
            className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
          >
            {submitting ? "Decomposing…" : "Decompose goal"}
          </button>
        </div>
        {actionError && <div className="mt-2 text-sm text-red-600">{actionError}</div>}
      </div>

      {error && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error.message}
        </div>
      )}

      <div className="bg-white rounded-lg border border-surface-200 overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 uppercase tracking-wide border-b border-surface-200 bg-surface-50">
              <th className="text-left font-medium px-4 py-3">Goal</th>
              <th className="text-left font-medium px-4 py-3">Status</th>
              <th className="text-left font-medium px-4 py-3">Autonomy</th>
              <th className="text-left font-medium px-4 py-3">Steps</th>
              <th className="text-left font-medium px-4 py-3">Updated</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-surface-100">
            {loading && !data && (
              <tr>
                <td colSpan={5} className="px-4 py-10 text-center text-sm text-gray-500">
                  Loading…
                </td>
              </tr>
            )}
            {data && data.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-10 text-center text-sm text-gray-500">
                  No plans yet — decompose a goal above.
                </td>
              </tr>
            )}
            {(data ?? []).map((plan) => (
              <tr key={plan.plan_id} className="hover:bg-surface-50">
                <td className="px-4 py-3">
                  <button
                    type="button"
                    onClick={() => onOpen(plan.plan_id)}
                    className="font-medium text-gray-900 hover:text-caliber-purple hover:underline text-left line-clamp-2 max-w-lg"
                  >
                    {plan.goal}
                  </button>
                </td>
                <td className="px-4 py-3">
                  <PlanStatusBadge status={plan.status} />
                </td>
                <td className="px-4 py-3 text-gray-600 text-xs">
                  {AUTONOMY_LABELS[plan.autonomy]?.label ?? plan.autonomy}
                </td>
                <td className="px-4 py-3 text-gray-600">{plan.step_count ?? "—"}</td>
                <td className="px-4 py-3 text-xs text-gray-500">
                  {relativeTime(plan.updated_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

/* -------------------------------------------------------------------------- */

function PlanDetail({
  planId,
  onBack,
}: {
  planId: string;
  onBack: () => void;
}): JSX.Element {
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.getAriaPlan(planId, signal),
    [planId],
  );
  const { data, error, loading, refresh } = useApi(fetcher, [planId]);
  const interactionsFetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listAriaInteractions(planId, signal),
    [planId],
  );
  const { data: interactions, refresh: refreshInteractions } = useApi(interactionsFetcher, [
    planId,
  ]);
  const [pending, setPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const plan = data?.plan;
  const steps = data?.steps ?? [];
  const isDraft = plan?.status === "draft";
  const isExecutable =
    plan?.status === "approved" || plan?.status === "paused" || plan?.status === "running";
  const pendingInteractions = (interactions ?? []).filter((i) => i.status === "pending");

  const run = async (fn: () => Promise<unknown>): Promise<void> => {
    setPending(true);
    setActionError(null);
    try {
      await fn();
      refresh();
      refreshInteractions();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "action failed");
    } finally {
      setPending(false);
    }
  };

  return (
    <>
      <div className="flex items-center gap-2 text-sm text-gray-500 mb-4">
        <button type="button" onClick={onBack} className="hover:text-gray-700">
          Plans
        </button>
        <Chevron />
        <span className="inline-flex items-center gap-1">
          <span className="text-gray-900 font-medium font-mono">{plan?.plan_id ?? planId}</span>
          <CopyButton value={plan?.plan_id ?? planId} label="Copy plan ID" />
        </span>
      </div>

      {error && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error.message}
        </div>
      )}
      {loading && !data && <div className="text-sm text-gray-500">Loading…</div>}

      {plan && (
        <>
          <div className="mb-5 flex items-start justify-between gap-4">
            <div>
              <h1 className="text-lg font-semibold text-gray-900 max-w-2xl">{plan.goal}</h1>
              <div className="mt-1 flex items-center gap-3 text-xs text-gray-500">
                <PlanStatusBadge status={plan.status} />
                <span title={plan.autonomy}>
                  Autonomy: {AUTONOMY_LABELS[plan.autonomy]?.label ?? plan.autonomy}
                </span>
                <span>{steps.length} steps</span>
              </div>
            </div>
            {isDraft && (
              <div className="flex items-center gap-2">
                <select
                  aria-label="Autonomy"
                  disabled={pending}
                  className="border border-surface-200 rounded-md px-2 py-1.5 text-sm bg-white"
                  value={plan.autonomy}
                  onChange={(e) =>
                    void run(() =>
                      caliberApi.updateAriaPlan(planId, {
                        autonomy: e.target.value as AriaAutonomy,
                      }),
                    )
                  }
                >
                  {AUTONOMY_LEVELS.map((a) => (
                    <option key={a} value={a}>
                      {AUTONOMY_LABELS[a].label}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  disabled={pending}
                  onClick={() => void run(() => caliberApi.updateAriaPlan(planId, { status: "cancelled" }))}
                  className="text-sm px-3 py-1.5 rounded-md text-gray-600 hover:bg-surface-100 disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  disabled={pending}
                  onClick={() => void run(() => caliberApi.approveAriaPlan(planId))}
                  className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
                >
                  Approve plan
                </button>
              </div>
            )}
            {isExecutable && pendingInteractions.length === 0 && (
              <button
                type="button"
                disabled={pending}
                onClick={() => void run(() => caliberApi.executeAriaPlan(planId))}
                className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
              >
                {plan.status === "paused" ? "Resume" : "Execute"}
              </button>
            )}
          </div>

          {actionError && (
            <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
              {actionError}
            </div>
          )}

          {pendingInteractions.map((interaction) => (
            <div key={interaction.interaction_id} className="mb-4">
              <PlanInteractionPrompt
                interaction={interaction}
                pending={pending}
                onAnswer={(payload) =>
                  void run(() =>
                    caliberApi.answerAriaInteraction(interaction.interaction_id, payload),
                  )
                }
              />
            </div>
          ))}

          <div className="bg-white rounded-lg border border-surface-200 overflow-hidden">
            <div className="px-4 py-2 text-xs font-semibold uppercase tracking-wide text-gray-500 border-b border-surface-200 bg-surface-50">
              Plan steps
            </div>
            {steps.length === 0 ? (
              <div className="px-4 py-8 text-center text-sm text-gray-400">
                Aria found no platform steps for this goal — try naming a capability
                (e.g. a judge or a review queue).
              </div>
            ) : (
              <ol className="divide-y divide-surface-100">
                {steps.map((step) => (
                  <PlanStepRow key={step.step_id} step={step} />
                ))}
              </ol>
            )}
          </div>
        </>
      )}
    </>
  );
}

function Chevron(): JSX.Element {
  return (
    <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M9 18l6-6-6-6" />
    </svg>
  );
}
