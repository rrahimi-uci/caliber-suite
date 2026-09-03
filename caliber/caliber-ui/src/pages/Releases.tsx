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
import type {
  ReleaseCandidate,
  ReleaseCandidateCreatePayload,
  ReleaseCriterion,
  ReleaseEvidence,
} from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  useApiMutation,
  useApiQuery,
  useInvalidate,
} from "@/hooks/useApiQuery";
import { relativeTime } from "@/lib/time";
import { useRef, useState } from "react";

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

const DEFAULT_CRITERIA = JSON.stringify(
  [
    {
      key: "quality",
      title: "Evaluation quality",
      weight: 70,
      score: 0,
      threshold: 0.9,
      blocking: true,
      evidence_refs: [],
    },
    {
      key: "human_review",
      title: "Human review completion",
      weight: 30,
      score: 0,
      threshold: 1,
      blocking: true,
      evidence_refs: [],
    },
  ],
  null,
  2,
);

function ReleaseSignoffFactory({
  canOperate,
  canAdmin,
}: {
  canOperate: boolean;
  canAdmin: boolean;
}): JSX.Element {
  const invalidate = useInvalidate();
  const candidates = useApiQuery(["release-candidates"], (signal) =>
    caliberApi.listReleaseCandidates(signal),
  );
  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [artifactType, setArtifactType] = useState("workflow");
  const [artifactRef, setArtifactRef] = useState("");
  const [versionRef, setVersionRef] = useState("");
  const [criteriaText, setCriteriaText] = useState(DEFAULT_CRITERIA);
  const [evidenceText, setEvidenceText] = useState("[]");
  const [requiredScore, setRequiredScore] = useState("0.8");
  const [rollbackRef, setRollbackRef] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const refresh = async (): Promise<void> => {
    await invalidate(["release-candidates"]);
  };
  const create = useApiMutation(
    (payload: ReleaseCandidateCreatePayload) => caliberApi.createReleaseCandidate(payload),
    { onSuccess: refresh },
  );

  const submit = async (): Promise<void> => {
    setFormError(null);
    try {
      const criteria = JSON.parse(criteriaText) as ReleaseCriterion[];
      const evidence = JSON.parse(evidenceText) as ReleaseEvidence[];
      if (!Array.isArray(criteria) || !Array.isArray(evidence)) {
        throw new Error("Criteria and evidence must be JSON arrays.");
      }
      await create.mutateAsync({
        name,
        artifact_type: artifactType,
        artifact_ref: artifactRef,
        version_ref: versionRef,
        criteria,
        evidence,
        required_score: Number(requiredScore),
        planned_action: { action: `promote_${artifactType}`, target: "prod" },
        rollback_target: { version_ref: rollbackRef, target: "prod" },
      });
      setShowCreate(false);
      setName("");
      setArtifactRef("");
      setVersionRef("");
      setRollbackRef("");
    } catch (error) {
      setFormError(queryErrorMessage(error));
    }
  };

  const listError = candidates.error;

  return (
    <section className="space-y-3" data-testid="release-signoff-factory">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-gray-700">Release signoff factory</h2>
          <p className="text-xs text-gray-500">
            Weighted evidence, blockers, waivers, accountable decisions, rollback targets, and Allure output.
          </p>
        </div>
        {canOperate && (
          <Button size="sm" onClick={() => setShowCreate((value) => !value)}>
            {showCreate ? "Cancel" : "New candidate"}
          </Button>
        )}
      </div>
      {showCreate && (
        <div className="grid gap-3 rounded-md border border-surface-200 p-4 md:grid-cols-2">
          <input data-testid="release-candidate-name" className="rounded border px-3 py-2 text-sm" placeholder="Release candidate name" value={name} onChange={(event) => setName(event.target.value)} />
          <select data-testid="release-candidate-type" className="rounded border px-3 py-2 text-sm" value={artifactType} onChange={(event) => setArtifactType(event.target.value)}>
            <option value="workflow">Workflow</option><option value="prompt">Prompt</option><option value="skill">Skill</option><option value="knowledge_base">Knowledge base</option>
          </select>
          <input data-testid="release-candidate-artifact" className="rounded border px-3 py-2 text-sm" placeholder="Artifact ID or registry name" value={artifactRef} onChange={(event) => setArtifactRef(event.target.value)} />
          <input data-testid="release-candidate-version" className="rounded border px-3 py-2 text-sm" placeholder="Candidate version" value={versionRef} onChange={(event) => setVersionRef(event.target.value)} />
          <input data-testid="release-candidate-rollback" className="rounded border px-3 py-2 text-sm" placeholder="Rollback version" value={rollbackRef} onChange={(event) => setRollbackRef(event.target.value)} />
          <input data-testid="release-candidate-required-score" className="rounded border px-3 py-2 text-sm" type="number" min="0" max="1" step="0.05" value={requiredScore} onChange={(event) => setRequiredScore(event.target.value)} />
          <textarea data-testid="release-candidate-criteria" className="rounded border p-3 font-mono text-xs md:col-span-2" rows={9} value={criteriaText} onChange={(event) => setCriteriaText(event.target.value)} />
          <textarea data-testid="release-candidate-evidence" className="rounded border p-3 font-mono text-xs md:col-span-2" rows={5} value={evidenceText} onChange={(event) => setEvidenceText(event.target.value)} placeholder='[{"evidence_type":"evaluation_run","evidence_ref":"EVR-...","label":"Sealed eval"}]' />
          {formError && <p className="text-sm text-red-700 md:col-span-2">{formError}</p>}
          <Button data-testid="release-candidate-create" className="md:col-span-2" disabled={create.isPending || !name || !artifactRef || !versionRef || !rollbackRef} onClick={() => void submit()}>
            Create and evaluate candidate
          </Button>
        </div>
      )}
      {listError && <p role="alert" className="text-sm text-red-700">{queryErrorMessage(listError)}</p>}
      <div className="space-y-2">
        {candidates.data?.map((candidate) => (
          <ReleaseCandidateCard
            key={candidate.candidate_id}
            candidate={candidate}
            canOperate={canOperate}
            canAdmin={canAdmin}
            onChanged={refresh}
          />
        ))}
      </div>
    </section>
  );
}

/**
 * One release candidate's status, actions, and signoff/waiver form.
 *
 * A dedicated component per candidate — not shared parent-level state indexed
 * by row — so each row's rationale/waiver inputs and in-flight mutations are
 * isolated by construction. The parent used to hold one `rationale`/
 * `waiverKey`/`waiverReason` triple for the *entire list*: typing a rationale
 * for one candidate silently changed what every other visible candidate's
 * "Sign off" button would submit, and clicking any row's button submitted
 * whichever text last landed in that shared state. See the regression test
 * `releases.test.tsx` ("keeps signoff and waiver state isolated per
 * candidate").
 *
 * Signoff and waiver both remove the control that was just clicked from the
 * DOM on success (the status moves past `signed_*`, or the criterion drops
 * out of `blockers`), so the browser's default focus-after-click has nowhere
 * to land. `rowRef` gives focus an explicit, persistent target to return to.
 */
function ReleaseCandidateCard({
  candidate,
  canOperate,
  canAdmin,
  onChanged,
}: {
  candidate: ReleaseCandidate;
  canOperate: boolean;
  canAdmin: boolean;
  onChanged: () => Promise<void>;
}): JSX.Element {
  const [rationale, setRationale] = useState("");
  const [waiverKey, setWaiverKey] = useState("");
  const [waiverReason, setWaiverReason] = useState("");
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const rowRef = useRef<HTMLDivElement>(null);

  const evaluate = useApiMutation(
    (target: ReleaseCandidate) => caliberApi.evaluateReleaseCandidate(target.candidate_id),
    { onSuccess: onChanged },
  );
  const signoff = useApiMutation(
    (input: { target: ReleaseCandidate; decision: "go" | "no_go" }) =>
      caliberApi.signoffReleaseCandidate(input.target.candidate_id, {
        decision: input.decision,
        rationale,
      }),
    {
      onSuccess: async () => {
        rowRef.current?.focus();
        await onChanged();
      },
    },
  );
  const waive = useApiMutation(
    (target: ReleaseCandidate) =>
      caliberApi.waiveReleaseCriterion(target.candidate_id, {
        criterion_key: waiverKey,
        reason: waiverReason,
      }),
    {
      onSuccess: async () => {
        rowRef.current?.focus();
        await onChanged();
      },
    },
  );
  const report = useApiMutation(
    (target: ReleaseCandidate) => caliberApi.generateReleaseAllureReport(target.candidate_id),
    {
      onSuccess: async (job) => {
        setActionMessage(`Allure report ${job.report_job_id} generated and retained.`);
        await onChanged();
      },
    },
  );

  const actionError = evaluate.error ?? signoff.error ?? waive.error ?? report.error;

  return (
    <div
      ref={rowRef}
      tabIndex={-1}
      data-testid={`release-candidate-${candidate.candidate_id}`}
      className="rounded-md border border-surface-200 p-3 focus:outline-none focus-visible:ring-2 focus-visible:ring-caliber-purple/50"
    >
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Badge variant="secondary">{candidate.status}</Badge>
        <span className="font-semibold text-gray-900">{candidate.name}</span>
        <span className="font-mono text-xs text-gray-500">{candidate.artifact_ref}@{candidate.version_ref}</span>
        <span className="ml-auto font-semibold">{Math.round((candidate.weighted_score ?? 0) * 100)}%</span>
      </div>
      <p className="mt-1 text-xs text-gray-500">
        {candidate.criteria.length} criteria · {candidate.evidence.length} evidence · {candidate.blockers.length} blockers · rollback {String(candidate.rollback_target.version_ref ?? "not set")}
      </p>
      {actionMessage && <p className="mt-1 text-xs text-emerald-700">{actionMessage}</p>}
      {actionError && <p role="alert" className="mt-1 text-sm text-red-700">{queryErrorMessage(actionError)}</p>}
      {canOperate && (
        <div className="mt-3 flex flex-wrap gap-2">
          <Button size="sm" variant="outline" onClick={() => evaluate.mutate(candidate)}>Re-evaluate</Button>
          <Button size="sm" variant="outline" onClick={() => report.mutate(candidate)}>Generate Allure</Button>
          {canAdmin && !candidate.status.startsWith("signed_") && (
            <>
              <input aria-label="Signoff rationale" className="min-w-64 rounded border px-2 py-1 text-xs" placeholder="Accountable decision rationale" value={rationale} onChange={(event) => setRationale(event.target.value)} />
              <Button size="sm" disabled={rationale.length < 8 || candidate.status !== "ready"} onClick={() => signoff.mutate({ target: candidate, decision: "go" })}>Sign off GO</Button>
              <Button size="sm" variant="outline" disabled={rationale.length < 8} onClick={() => signoff.mutate({ target: candidate, decision: "no_go" })}>Sign off NO-GO</Button>
            </>
          )}
        </div>
      )}
      {canAdmin && candidate.blockers.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          <input aria-label="Waiver criterion key" className="rounded border px-2 py-1 text-xs" placeholder="criterion key" value={waiverKey} onChange={(event) => setWaiverKey(event.target.value)} />
          <input aria-label="Waiver reason" className="min-w-72 rounded border px-2 py-1 text-xs" placeholder="Exception reason and compensating control" value={waiverReason} onChange={(event) => setWaiverReason(event.target.value)} />
          <Button size="sm" variant="outline" disabled={!waiverKey || waiverReason.length < 8} onClick={() => waive.mutate(candidate)}>Record waiver</Button>
        </div>
      )}
    </div>
  );
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

      <ReleaseSignoffFactory canOperate={canOperate} canAdmin={canAdmin} />

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
