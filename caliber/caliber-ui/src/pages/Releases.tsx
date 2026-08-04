/**
 * Releases & Rollback — the cross-artifact "what's live, and what changed?" hub.
 *
 * A read-only control-plane view: a board of what's currently live across
 * artifacts, and a unified promotion/rollback/activation timeline. Per-artifact
 * rollback lives on each artifact's page; this aggregates the picture.
 *
 * Both aggregates are visibility-scoped server-side, so this page shows the same
 * rows the artifact workspaces would. It also renders an explicit error state:
 * without one, a failed aggregate query rendered as "Nothing deployed yet." —
 * an empty release board is indistinguishable from a broken one, and on this page
 * that reads as "nothing is in production".
 */
import { caliberApi } from "@/api/caliberApi";
import type {
  ReleaseOperation,
  ReleaseTimelineEvent,
  SystemEffect,
  WebhookDeadLetter,
} from "@/api/versioning";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  useApiMutation,
  useApiQuery,
  useInvalidate,
} from "@/hooks/useApiQuery";
import { relativeTime } from "@/lib/time";
import { useState } from "react";

const ACTION_LABEL: Record<string, string> = {
  promote_prompt: "Promote",
  rollback_prompt: "Roll back",
  promote_workflow: "Promote",
  rollback_workflow: "Roll back",
  activate_knowledge_base_version: "Activate",
  rollback_knowledge_base_version: "Roll back",
  rollback_skill: "Roll back",
};

function isRollback(action: string): boolean {
  return action.startsWith("rollback");
}

function queryErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return "unknown error";
}

function transition(event: ReleaseTimelineEvent): string {
  const d = event.details ?? {};
  const from = d.from_version ?? d.previous_active_version_id ?? null;
  const to = d.to_version ?? d.version_id ?? null;
  if (from != null && to != null) return `v${from} → v${to}`;
  if (to != null) return `→ v${to}`;
  return "";
}

export function Releases(): JSX.Element {
  const invalidate = useInvalidate();
  const [recoveryError, setRecoveryError] = useState<string | null>(null);
  const meQuery = useApiQuery(["me"], (signal) => caliberApi.getMe(signal));
  const canOperate =
    (meQuery.data?.scopes ?? []).includes("caliber.operator") ||
    (meQuery.data?.is_admin ?? false);
  const canAdmin =
    (meQuery.data?.scopes ?? []).includes("caliber.admin") ||
    (meQuery.data?.is_admin ?? false);
  const liveQuery = useApiQuery(["releases-live"], () =>
    caliberApi.listReleasesLive(),
  );
  const timelineQuery = useApiQuery(["releases-timeline"], () =>
    caliberApi.listReleasesTimeline({ limit: 100 }),
  );
  const operationsQuery = useApiQuery(
    ["release-operations"],
    () => caliberApi.listReleaseOperations(),
    { enabled: canOperate },
  );
  const effectsQuery = useApiQuery(
    ["system-effects"],
    () => caliberApi.listSystemEffects(),
    { enabled: canOperate },
  );
  const deadLettersQuery = useApiQuery(
    ["webhook-dead-letters"],
    () => caliberApi.listWebhookDeadLetters(),
    { enabled: canOperate },
  );
  const refreshRecovery = async (): Promise<void> => {
    await Promise.all([
      invalidate(["release-operations"]),
      invalidate(["system-effects"]),
      invalidate(["webhook-dead-letters"]),
      invalidate(["releases-live"]),
      invalidate(["releases-timeline"]),
    ]);
  };
  const reconcile = useApiMutation(
    () => caliberApi.reconcileReleaseOperations(),
    {
      onSuccess: refreshRecovery,
    },
  );
  const resolveRelease = useApiMutation(
    (input: {
      operation: ReleaseOperation;
      action: "retry" | "abandon";
      reason?: string;
    }) =>
      caliberApi.resolveReleaseOperation(input.operation.operation_id, {
        action: input.action,
        reason: input.reason,
      }),
    { onSuccess: refreshRecovery },
  );
  const resolveEffect = useApiMutation(
    (input: {
      effect: SystemEffect;
      resolution: "retry" | "skip";
      reason: string;
    }) =>
      caliberApi.resolveSystemEffect(input.effect.effect_key, {
        resolution: input.resolution,
        reason: input.reason,
      }),
    { onSuccess: refreshRecovery },
  );
  const replayDeadLetter = useApiMutation(
    (row: WebhookDeadLetter) =>
      caliberApi.replayWebhookDeadLetter(row.dead_letter_id),
    { onSuccess: refreshRecovery },
  );
  const acknowledgeDeadLetter = useApiMutation(
    (input: { row: WebhookDeadLetter; note: string }) =>
      caliberApi.acknowledgeWebhookDeadLetter(
        input.row.dead_letter_id,
        input.note,
      ),
    { onSuccess: refreshRecovery },
  );

  const runRecovery = async (action: () => Promise<unknown>): Promise<void> => {
    setRecoveryError(null);
    try {
      await action();
    } catch (error) {
      setRecoveryError(queryErrorMessage(error));
    }
  };

  const incompleteOperations = (operationsQuery.data ?? []).filter((row) =>
    ["prepared", "applying", "reconcile_required"].includes(row.status),
  );

  return (
    <div className="space-y-6 p-6" data-testid="releases-page">
      <div>
        <h1 className="text-xl font-semibold text-gray-900">
          Releases &amp; Rollback
        </h1>
        <p className="mt-0.5 text-sm text-gray-500">
          What&apos;s live across artifacts, and the recent promotion/rollback
          history.
        </p>
      </div>

      <section>
        <h2 className="mb-2 text-sm font-semibold text-gray-700">
          What&apos;s live now
        </h2>
        <div
          data-testid="releases-live-board"
          className="overflow-hidden rounded-md border border-surface-200"
        >
          {liveQuery.isLoading && (
            <div className="p-3 text-sm text-gray-400">Loading…</div>
          )}
          {liveQuery.isError && (
            <div
              data-testid="releases-live-error"
              className="p-3 text-sm text-red-700"
              role="alert"
            >
              Could not load what&apos;s live:{" "}
              {queryErrorMessage(liveQuery.error)}. This is a load failure, not
              an empty release board.
            </div>
          )}
          {!liveQuery.isError &&
            liveQuery.data &&
            liveQuery.data.length === 0 && (
              <div className="p-3 text-sm text-gray-400">
                Nothing deployed yet in your visible projects.
              </div>
            )}
          {liveQuery.data?.map((row) => (
            <div
              key={`${row.artifact_type}:${row.artifact_id}`}
              data-testid={`releases-live-${row.artifact_id}`}
              className="flex items-center gap-3 border-b border-surface-100 px-3 py-2 text-sm last:border-0"
            >
              <Badge variant="secondary">{row.artifact_type}</Badge>
              <span className="font-medium text-gray-900">
                {row.artifact_name ?? row.artifact_id}
              </span>
              <span className="font-mono text-xs text-gray-500">
                {row.version_id}
              </span>
              <span className="ml-auto text-xs text-gray-400">
                {row.since ? `since ${relativeTime(row.since)}` : ""}
                {row.by ? ` · ${row.by}` : ""}
              </span>
            </div>
          ))}
        </div>
      </section>

      {canOperate && (
        <section className="space-y-3" data-testid="release-recovery-console">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold text-gray-700">
                Recovery console
              </h2>
              <p className="text-xs text-gray-500">
                Incomplete releases, indeterminate effects, and undelivered
                webhooks.
              </p>
            </div>
            <Button
              size="sm"
              variant="outline"
              disabled={reconcile.isPending}
              onClick={() =>
                void runRecovery(() => reconcile.mutateAsync(undefined))
              }
            >
              Reconcile releases
            </Button>
          </div>
          {recoveryError && (
            <div
              role="alert"
              className="text-sm text-red-700"
              data-testid="recovery-error"
            >
              Recovery action failed: {recoveryError}
            </div>
          )}
          {(operationsQuery.isError ||
            effectsQuery.isError ||
            deadLettersQuery.isError) && (
            <div role="alert" className="text-sm text-red-700">
              One or more recovery queues could not be loaded.
            </div>
          )}

          <div className="rounded-md border border-surface-200 p-3">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500">
              Incomplete releases ({incompleteOperations.length})
            </h3>
            {incompleteOperations.length === 0 ? (
              <p className="mt-2 text-sm text-gray-400">
                No incomplete release operations.
              </p>
            ) : (
              <ul className="mt-2 space-y-2">
                {incompleteOperations.map((row) => (
                  <li
                    key={row.operation_id}
                    data-testid={`release-operation-${row.operation_id}`}
                    className="flex flex-wrap items-center gap-2 text-sm"
                  >
                    <Badge
                      variant={
                        row.status === "prepared" ? "warning" : "destructive"
                      }
                    >
                      {row.status}
                    </Badge>
                    <span className="font-medium">
                      {row.resource_name}@{row.target_name}
                    </span>
                    <span className="font-mono text-xs text-gray-500">
                      {row.version_before == null
                        ? "none"
                        : `v${row.version_before}`}{" "}
                      → v{row.version_after}
                    </span>
                    {row.last_error && (
                      <span className="text-xs text-red-700">
                        {row.last_error}
                      </span>
                    )}
                    {row.status === "prepared" && (
                      <span className="ml-auto flex gap-1">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            void runRecovery(() =>
                              resolveRelease.mutateAsync({
                                operation: row,
                                action: "retry",
                              }),
                            )
                          }
                        >
                          Retry exact intent
                        </Button>
                        <Button
                          size="sm"
                          variant="destructive"
                          onClick={() => {
                            const reason = window.prompt(
                              "Why is this pre-effect release being abandoned?",
                            );
                            if (reason?.trim())
                              void runRecovery(() =>
                                resolveRelease.mutateAsync({
                                  operation: row,
                                  action: "abandon",
                                  reason: reason.trim(),
                                }),
                              );
                          }}
                        >
                          Abandon
                        </Button>
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="grid gap-3 lg:grid-cols-2">
            <RecoveryQueue
              title={`Indeterminate effects (${effectsQuery.data?.effects.length ?? 0})`}
              empty="No effects need a decision."
            >
              {effectsQuery.data?.effects.map((effect) => (
                <li
                  key={effect.effect_key}
                  data-testid={`system-effect-${effect.effect_key}`}
                  className="space-y-1 text-sm"
                >
                  <div className="font-medium">
                    {effect.workflow_run_id} · {effect.node_id}
                  </div>
                {canAdmin ? (
                  <div className="flex gap-1">
                    {(["retry", "skip"] as const).map((resolution) => (
                      <Button
                        key={resolution}
                        size="sm"
                        variant="outline"
                        onClick={() => {
                          const reason = window.prompt(
                            `Reason to ${resolution} this effect?`,
                          );
                          if (reason?.trim())
                            void runRecovery(() =>
                              resolveEffect.mutateAsync({
                                effect,
                                resolution,
                                reason: reason.trim(),
                              }),
                            );
                        }}
                      >
                        {resolution}
                      </Button>
                    ))}
                  </div>
                ) : (
                  <div className="text-xs text-gray-500">
                    Admin scope is required to resolve effects.
                  </div>
                )}
                </li>
              ))}
            </RecoveryQueue>
            <RecoveryQueue
              title={`Webhook dead letters (${deadLettersQuery.data?.open_count ?? 0})`}
              empty="No webhook deliveries need recovery."
            >
              {deadLettersQuery.data?.dead_letters.map((row) => (
                <li
                  key={row.dead_letter_id}
                  data-testid={`dead-letter-${row.dead_letter_id}`}
                  className="space-y-1 text-sm"
                >
                  <div className="font-medium">
                    {row.event_type} · {row.kind}
                  </div>
                  <div className="text-xs text-red-700">{row.reason}</div>
                  <div className="flex gap-1">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={!row.has_event}
                      onClick={() =>
                        void runRecovery(() =>
                          replayDeadLetter.mutateAsync(row),
                        )
                      }
                    >
                      Replay
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => {
                        const note = window.prompt(
                          "Why is this dead letter being acknowledged without delivery?",
                        );
                        if (note?.trim())
                          void runRecovery(() =>
                            acknowledgeDeadLetter.mutateAsync({
                              row,
                              note: note.trim(),
                            }),
                          );
                      }}
                    >
                      Acknowledge
                    </Button>
                  </div>
                </li>
              ))}
            </RecoveryQueue>
          </div>
        </section>
      )}

      <section>
        <h2 className="mb-2 text-sm font-semibold text-gray-700">Timeline</h2>
        <ul data-testid="releases-timeline" className="space-y-1">
          {timelineQuery.isLoading && (
            <li className="text-sm text-gray-400">Loading…</li>
          )}
          {timelineQuery.isError && (
            <li
              data-testid="releases-timeline-error"
              className="text-sm text-red-700"
              role="alert"
            >
              Could not load the release timeline:{" "}
              {queryErrorMessage(timelineQuery.error)}.
            </li>
          )}
          {!timelineQuery.isError &&
            timelineQuery.data &&
            timelineQuery.data.length === 0 && (
              <li className="text-sm text-gray-400">
                No promotions or rollbacks yet in your visible projects.
              </li>
            )}
          {timelineQuery.data?.map((event) => {
            const overridden = Boolean(event.details?.overridden);
            return (
              <li
                key={event.log_id}
                data-testid={`releases-event-${event.log_id}`}
                className="flex items-center gap-2 rounded border border-surface-100 px-3 py-1.5 text-sm"
              >
                <Badge
                  variant={isRollback(event.action) ? "warning" : "default"}
                >
                  {ACTION_LABEL[event.action] ?? event.action}
                </Badge>
                <span className="text-gray-600">{event.entity_type}</span>
                <span className="font-medium text-gray-900">
                  {event.entity_id}
                </span>
                <span className="font-mono text-xs text-gray-500">
                  {transition(event)}
                </span>
                {overridden && (
                  <Badge
                    variant="destructive"
                    data-testid={`releases-overridden-${event.log_id}`}
                  >
                    gate overridden
                  </Badge>
                )}
                <span className="ml-auto text-xs text-gray-400">
                  {event.actor}
                  {event.timestamp ? ` · ${relativeTime(event.timestamp)}` : ""}
                </span>
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
}

function RecoveryQueue({
  title,
  empty,
  children,
}: {
  title: string;
  empty: string;
  children: React.ReactNode;
}): JSX.Element {
  const hasChildren = Array.isArray(children)
    ? children.length > 0
    : Boolean(children);
  return (
    <div className="rounded-md border border-surface-200 p-3">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500">
        {title}
      </h3>
      {hasChildren ? (
        <ul className="mt-2 space-y-2">{children}</ul>
      ) : (
        <p className="mt-2 text-sm text-gray-400">{empty}</p>
      )}
    </div>
  );
}
