/**
 * The approval panel for an exact-intent request (B06.5, #743; `docs/architecture/approval-ux-contract.md`).
 *
 * It shows what the approver is deciding (the effect's target and class, who asked, the run and node, the pins that bind
 * the decision, when it expires, who has approved so far) and offers the decisions the server lists. It is presentational:
 * the server's view says whether each decision is allowed and why not, and this component renders those signals without
 * re-deriving any authorisation rule. A refusal is a code the server sent, shown with its one message and recovery step.
 *
 * No argument value is rendered: the view carries the names and kinds of the arguments only, and the intent digest is what
 * binds the values.
 *
 * Accessibility: the panel is inline (no dialog, no focus trap). Focus moves to the heading when the panel opens on a page
 * where nothing else holds focus, and to the result message after a decision or a refusal, never to a button. One polite
 * status element carries outcomes and one alert element carries blocking refusals; both are always in the document so the
 * announcement is reliable, and focus lands on whichever one has the text. A decision that is not available is
 * `aria-disabled` (so it stays focusable) and names its reason through `aria-describedby`. The countdown refreshes once a
 * minute and is outside both live regions.
 */
import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import type {
  ApprovalDecision,
  EffectProposal,
  WorkflowApprovalView,
  WorkflowRuntimeApproval,
} from "@/api/workflowTypes";
import {
  BLOCKING_DENIALS,
  countdownLabel,
  denialMessage,
  effectClassLabel,
  formatExpiry,
  isIrreversibleEffect,
  parseServerTime,
  shortDigest,
  type ApprovalOutcome,
  type DenialContext,
} from "@/lib/approvalPanel";

/** What the panel reads from the server's view besides the approval row and the proposal. */
export type ApprovalReview = Omit<WorkflowApprovalView, "approval" | "proposal">;

export interface ApprovalDecisionPanelProps {
  approval: Pick<
    WorkflowRuntimeApproval,
    | "runtime_approval_id"
    | "workflow_run_id"
    | "node_id"
    | "requested_at"
    | "decided_at"
    | "decided_by"
  > & { status: string };
  proposal: EffectProposal | null;
  review: ApprovalReview;
  /** Called with the decision and the trimmed reason (when one was typed). The panel never sends a principal. */
  onDecide: (decision: ApprovalDecision, reason?: string) => void;
  /** The decision whose call is in flight, if any. */
  pending?: ApprovalDecision | null;
  /** What the last decision call did. */
  outcome?: ApprovalOutcome | null;
  /** An in-page anchor to the newer request when this one was superseded and the newer one is on the page. */
  newerRequestHref?: string | null;
}

const DECISION_ORDER: ApprovalDecision[] = ["approve", "request_changes", "reject"];
const DECISION_LABEL: Record<ApprovalDecision, string> = {
  approve: "Approve",
  request_changes: "Request changes",
  reject: "Reject",
};
const MAX_REASON = 1000;
const MINUTE_MS = 60_000;

function useMinuteClock(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), MINUTE_MS);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

function Row({ term, children }: { term: string; children: ReactNode }): JSX.Element {
  return (
    <div className="grid gap-0.5 sm:grid-cols-[10rem_minmax(0,1fr)] sm:gap-3">
      <dt className="text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-500 dark:text-slate-400">
        {term}
      </dt>
      <dd className="min-w-0 break-words text-xs text-slate-800 dark:text-slate-100">{children}</dd>
    </div>
  );
}

function Digest({ label, value }: { label: string; value: string }): JSX.Element {
  // A group, not a bare `code`: the `code` role cannot carry a name, and the full digest has to be in the accessible name.
  return (
    <span
      role="group"
      tabIndex={0}
      title={value}
      aria-label={`${label} ${value}`}
      className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-700 focus:outline focus:outline-2 focus:outline-sky-500 dark:bg-slate-800 dark:text-slate-200"
    >
      <code>{shortDigest(value)}</code>
    </span>
  );
}

function denialContext(
  approval: ApprovalDecisionPanelProps["approval"],
  review: ApprovalReview,
): DenialContext {
  return {
    expiresAt: review.binding?.expires_at ?? null,
    expectedVersion: review.binding?.expected_resource_version ?? null,
    supersededBy: review.state.superseded_by ?? review.binding?.superseded_by ?? null,
    required: review.quorum.required,
    remaining: review.quorum.remaining,
    approved: review.quorum.approvers.length,
    requiredRole: review.policy.required_role,
    decidedBy: approval.decided_by,
  };
}

export function ApprovalDecisionPanel({
  approval,
  proposal,
  review,
  onDecide,
  pending = null,
  outcome = null,
  newerRequestHref = null,
}: ApprovalDecisionPanelProps): JSX.Element {
  const ids = useId();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const statusRef = useRef<HTMLDivElement>(null);
  const alertRef = useRef<HTMLDivElement>(null);
  const reasonRef = useRef<HTMLTextAreaElement>(null);
  const rejectRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);
  const nowMs = useMinuteClock();
  const [reason, setReason] = useState("");
  const [reasonError, setReasonError] = useState<string | null>(null);
  const [confirmingReject, setConfirmingReject] = useState(false);

  const binding = review.binding;
  const ctx = denialContext(approval, review);
  const open = review.state.code === "open";
  const busy = pending !== null;
  const heading = proposal
    ? `Approval request for ${proposal.resource}`
    : `Approval request for node ${approval.node_id}`;
  const expiresAtMs = binding ? parseServerTime(binding.expires_at) : Number.NaN;
  const approvedCount = review.quorum.approvers.length;

  // Focus the heading when the panel opens, unless the user is already somewhere on the page.
  useEffect(() => {
    const active = document.activeElement;
    if (!active || active === document.body) {
      headingRef.current?.focus({ preventScroll: true });
    }
  }, []);

  // After a decision or a refusal focus moves to the message, not to a button.
  useEffect(() => {
    if (!outcome) return;
    const blocking = outcome.kind === "denied" && BLOCKING_DENIALS.has(outcome.code);
    (blocking ? alertRef.current : statusRef.current)?.focus();
    if (outcome.kind === "approved" || outcome.kind === "rejected") {
      setReason("");
      setConfirmingReject(false);
    }
  }, [outcome]);

  useEffect(() => {
    if (confirmingReject) confirmRef.current?.focus();
  }, [confirmingReject]);

  const abilityOf = (decision: ApprovalDecision): { allowed: boolean; reason: string | null } => {
    if (decision === "request_changes") {
      // No route records it yet; the server lists only the decisions it accepts, so this is never offered today.
      return { allowed: false, reason: null };
    }
    const ability = review.caller[decision];
    if (ability.allowed) return { allowed: true, reason: null };
    return {
      allowed: false,
      reason: ability.reason_code ? denialMessage(ability.reason_code, ctx) : null,
    };
  };

  const decide = (decision: ApprovalDecision): void => {
    if (busy) return;
    const ability = abilityOf(decision);
    if (!ability.allowed && decision !== "request_changes") return;
    const trimmed = reason.trim();
    if ((decision === "reject" || decision === "request_changes") && !trimmed) {
      setReasonError(
        `A reason is required to ${decision === "reject" ? "reject" : "request changes"}. Type one in the reason box.`,
      );
      setConfirmingReject(false);
      reasonRef.current?.focus();
      return;
    }
    setReasonError(null);
    if (decision === "reject" && !confirmingReject) {
      setConfirmingReject(true);
      return;
    }
    setConfirmingReject(false);
    onDecide(decision, trimmed || undefined);
  };

  const outcomeText = ((): { text: string; blocking: boolean } | null => {
    if (!outcome) return null;
    switch (outcome.kind) {
      case "approved":
        return {
          text:
            outcome.remaining && outcome.remaining > 0
              ? `Approved. ${outcome.remaining} more ${outcome.remaining === 1 ? "approval" : "approvals"} needed.`
              : "Approved. The quorum is met. The run stays paused until someone with the operator role resumes it.",
          blocking: false,
        };
      case "rejected":
        return { text: "Rejected. The run was failed.", blocking: false };
      case "denied":
        return {
          text:
            denialMessage(outcome.code, ctx)
            ?? "The server refused this decision. The panel was refreshed; nothing was recorded.",
          blocking: BLOCKING_DENIALS.has(outcome.code),
        };
      case "invalid":
        return {
          text: `The decision was not accepted (${outcome.message}). Nothing was recorded.`,
          blocking: false,
        };
      case "unknown":
        return {
          text: "We could not confirm whether your decision was recorded. The panel was refreshed to show the current state; check it before deciding again.",
          blocking: false,
        };
      case "failed":
        return {
          text: `The decision failed (HTTP ${outcome.status}). Nothing was recorded. Refresh the run and try again.`,
          blocking: false,
        };
    }
  })();

  const stateMessage = open ? null : denialMessage(review.state.code, ctx);
  const stateBlocking = BLOCKING_DENIALS.has(review.state.code);
  const quorumText = `${approvedCount} of ${review.quorum.required} approved`;

  const decisions = DECISION_ORDER.filter((decision) => review.allowed_responses.includes(decision));
  const reasonRequiredNote =
    decisions.includes("reject") || decisions.includes("request_changes")
      ? "Required to reject or request changes. Optional to approve."
      : "Optional.";

  return (
    <section
      id={`approval-panel-${approval.runtime_approval_id}`}
      aria-labelledby={`${ids}-heading`}
      data-testid="approval-decision-panel"
      className="space-y-3 rounded-xl border border-slate-200 bg-white p-4 text-slate-800 shadow-sm dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h3
          id={`${ids}-heading`}
          ref={headingRef}
          tabIndex={-1}
          data-testid="approval-panel-heading"
          className="min-w-0 break-words text-sm font-bold focus:outline focus:outline-2 focus:outline-sky-500"
        >
          {heading}
        </h3>
        {proposal && (
          <span
            data-testid="approval-risk-badge"
            className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ring-1 ${
              isIrreversibleEffect(proposal.effect_class)
                ? "bg-amber-50 text-amber-900 ring-amber-300 dark:bg-amber-950/40 dark:text-amber-100 dark:ring-amber-700"
                : "bg-slate-50 text-slate-700 ring-slate-300 dark:bg-slate-800 dark:text-slate-200 dark:ring-slate-600"
            }`}
          >
            <span className="sr-only">Risk class: </span>
            {effectClassLabel(proposal.effect_class)}
          </span>
        )}
      </div>

      <div
        ref={alertRef}
        role="alert"
        tabIndex={-1}
        data-testid="approval-alert"
        className={
          (outcomeText?.blocking || (stateMessage && stateBlocking))
            ? "rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-900 dark:border-red-800 dark:bg-red-950/40 dark:text-red-100"
            : undefined
        }
      >
        {outcomeText?.blocking
          ? outcomeText.text
          : stateMessage && stateBlocking
            ? stateMessage
            : null}
        {review.state.code === "superseded" && newerRequestHref ? (
          <>
            {" "}
            <a className="font-semibold underline" href={newerRequestHref}>
              Go to the newer request
            </a>
          </>
        ) : null}
      </div>

      <div
        ref={statusRef}
        role="status"
        aria-live="polite"
        tabIndex={-1}
        data-testid="approval-status"
        className={
          (outcomeText && !outcomeText.blocking) || (stateMessage && !stateBlocking)
            ? "rounded-lg border border-slate-300 bg-slate-50 px-3 py-2 text-xs text-slate-800 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-100"
            : undefined
        }
      >
        {outcomeText && !outcomeText.blocking
          ? outcomeText.text
          : stateMessage && !stateBlocking
            ? stateMessage
            : null}
      </div>

      <dl className="space-y-2" data-testid="approval-summary">
        <Row term="Request">
          <code className="font-mono text-[11px]">{approval.runtime_approval_id}</code>
          {binding && (
            <>
              {" "}
              <span className="text-slate-500 dark:text-slate-400">· proposal</span>{" "}
              <code className="font-mono text-[11px]">{binding.proposal_id}</code>{" "}
              <CopyButton label="Copy proposal id" value={binding.proposal_id} />
            </>
          )}
        </Row>
        <Row term="Run and node">
          <code className="font-mono text-[11px]">{approval.workflow_run_id}</code>
          {" · node "}
          <code className="font-mono text-[11px]">{approval.node_id}</code>
        </Row>
        {binding && (
          <Row term="Intent digest">
            <Digest label="Intent digest" value={binding.intent_hash} />
            <span className="ml-2 text-[11px] text-slate-500 dark:text-slate-400">
              binds every argument value
            </span>
          </Row>
        )}
        {proposal ? (
          <>
            <Row term="System">
              {proposal.connector}{" "}
              <span className="text-slate-500 dark:text-slate-400">version {proposal.connector_version}</span>
            </Row>
            <Row term="Resource">
              <code className="font-mono text-[11px]">{proposal.resource}</code>
            </Row>
            <Row term="Effect class">{effectClassLabel(proposal.effect_class)}</Row>
            <Row term="Arguments">
              {proposal.args_fields.length === 0 ? (
                "No arguments."
              ) : (
                <ul className="list-disc pl-4" data-testid="approval-args">
                  {proposal.args_fields.map((field, index) => (
                    <li key={`${field.name ?? "field"}-${index}`}>
                      {field.name ?? "(unnamed field)"}{" "}
                      <span className="text-slate-500 dark:text-slate-400">({field.kind})</span>
                    </li>
                  ))}
                </ul>
              )}
              <p className="mt-1 text-[11px] text-slate-500 dark:text-slate-400">
                {proposal.args_field_count} {proposal.args_field_count === 1 ? "field" : "fields"}
                {proposal.args_truncated ? ", list shortened" : ""}. Values are not shown here; the intent
                digest pins them.
              </p>
            </Row>
          </>
        ) : binding ? (
          <Row term="Effect">
            Hidden: deciding this request needs an approving role in the project. The digests below still
            identify what it binds.
          </Row>
        ) : null}
        {binding && (
          <>
            <Row term="Version">
              {binding.expected_resource_version
                ? `Valid for version ${binding.expected_resource_version}`
                : "Not versioned"}
            </Row>
            <Row term="Evidence digest">
              <Digest label="Evidence digest" value={binding.evidence_hash} />
            </Row>
            <Row term="Policy">
              <Digest label="Policy digest" value={binding.policy_digest} />{" "}
              <span className="text-[11px] text-slate-500 dark:text-slate-400">
                needs {review.policy.approval_count}{" "}
                {review.policy.approval_count === 1 ? "approval" : "approvals"} from the{" "}
                {review.policy.required_role} role
              </span>
            </Row>
            <Row term="Requested">
              {binding.requested_by ? `by ${binding.requested_by}, ` : ""}
              {formatExpiry(approval.requested_at)}
            </Row>
            <Row term="Expires at">
              <time dateTime={binding.expires_at}>{formatExpiry(binding.expires_at)}</time>{" "}
              <span data-testid="approval-countdown" className="text-slate-500 dark:text-slate-400">
                ({countdownLabel(expiresAtMs, nowMs)})
              </span>
            </Row>
          </>
        )}
        <Row term="Approvals so far">
          <span data-testid="approval-quorum">{quorumText}</span>
          {approvedCount > 0 && (
            <ul className="mt-1 list-disc pl-4">
              {review.quorum.approvers.map((approver) => (
                <li key={approver}>
                  {approver}
                  {approver === review.caller.principal ? " (you)" : ""}
                </li>
              ))}
            </ul>
          )}
        </Row>
        {approval.decided_by && (
          <Row term="Decided">
            {approval.status} by {approval.decided_by}
          </Row>
        )}
      </dl>

      {open ? (
        <div className="space-y-2" data-testid="approval-actions">
          <div>
            <label
              htmlFor={`${ids}-reason`}
              className="block text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-500 dark:text-slate-400"
            >
              Reason
            </label>
            <p id={`${ids}-reason-note`} className="text-[11px] text-slate-500 dark:text-slate-400">
              {reasonRequiredNote}
            </p>
            <textarea
              id={`${ids}-reason`}
              ref={reasonRef}
              value={reason}
              maxLength={MAX_REASON}
              rows={2}
              onChange={(event) => {
                setReason(event.target.value);
                if (reasonError) setReasonError(null);
              }}
              aria-invalid={reasonError ? true : undefined}
              aria-describedby={
                reasonError ? `${ids}-reason-note ${ids}-reason-error` : `${ids}-reason-note`
              }
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-xs text-slate-900 focus:outline focus:outline-2 focus:outline-sky-500 dark:border-slate-600 dark:bg-slate-950 dark:text-slate-100"
            />
            <p
              id={`${ids}-reason-error`}
              role="alert"
              data-testid="approval-reason-error"
              className="text-[11px] font-semibold text-red-700 dark:text-red-300"
            >
              {reasonError}
            </p>
          </div>

          {confirmingReject ? (
            <div
              role="group"
              aria-label="Confirm rejection"
              data-testid="approval-reject-confirm"
              className="flex flex-wrap items-center gap-2 rounded-lg border border-red-300 bg-red-50 px-3 py-2 dark:border-red-800 dark:bg-red-950/40"
            >
              <span className="text-xs font-semibold text-red-900 dark:text-red-100">
                Reject and fail this run?
              </span>
              <button
                type="button"
                ref={confirmRef}
                aria-busy={pending === "reject" ? true : undefined}
                aria-disabled={busy ? true : undefined}
                onClick={() => decide("reject")}
                className="rounded-lg bg-red-600 px-3 py-1.5 text-xs font-semibold text-white focus:outline focus:outline-2 focus:outline-offset-2 focus:outline-red-700 aria-disabled:cursor-not-allowed aria-disabled:opacity-60"
              >
                {pending === "reject" ? "Rejecting…" : "Confirm reject"}
              </button>
              <button
                type="button"
                aria-disabled={busy ? true : undefined}
                onClick={() => {
                  if (busy) return;
                  setConfirmingReject(false);
                  rejectRef.current?.focus();
                }}
                className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 focus:outline focus:outline-2 focus:outline-sky-500 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100"
              >
                Cancel
              </button>
            </div>
          ) : null}

          <div className="flex flex-wrap items-start gap-2">
            {decisions.map((decision) => {
              const ability = abilityOf(decision);
              const inFlight = pending === decision;
              const unavailable = !ability.allowed && decision !== "request_changes";
              const hintId = `${ids}-${decision}-why`;
              return (
                <div key={decision} className="max-w-xs">
                  <button
                    type="button"
                    ref={decision === "reject" ? rejectRef : undefined}
                    data-testid={`approval-${decision.replace("_", "-")}`}
                    aria-disabled={unavailable || (busy && !inFlight) ? true : undefined}
                    aria-busy={inFlight ? true : undefined}
                    aria-describedby={unavailable && ability.reason ? hintId : undefined}
                    onClick={() => decide(decision)}
                    className={`rounded-lg px-3 py-1.5 text-xs font-semibold focus:outline focus:outline-2 focus:outline-offset-2 focus:outline-sky-500 aria-disabled:cursor-not-allowed aria-disabled:opacity-50 ${
                      decision === "approve"
                        ? "bg-emerald-600 text-white"
                        : decision === "reject"
                          ? "border border-red-300 bg-red-50 text-red-800 dark:border-red-800 dark:bg-red-950/40 dark:text-red-100"
                          : "border border-slate-300 bg-white text-slate-800 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-100"
                    }`}
                  >
                    {inFlight
                      ? decision === "approve"
                        ? "Approving…"
                        : decision === "reject"
                          ? "Rejecting…"
                          : "Sending…"
                      : DECISION_LABEL[decision]}
                  </button>
                  {unavailable && ability.reason ? (
                    <p
                      id={hintId}
                      data-testid={`approval-${decision.replace("_", "-")}-why`}
                      className="mt-1 text-[11px] text-slate-600 dark:text-slate-300"
                    >
                      {DECISION_LABEL[decision]} is unavailable. {ability.reason}
                    </p>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
      ) : null}
    </section>
  );
}

function CopyButton({ label, value }: { label: string; value: string }): JSX.Element {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      aria-label={label}
      onClick={() => {
        void (async () => {
          try {
            await navigator.clipboard.writeText(value);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 2000);
          } catch {
            // Clipboard access can be refused; the id stays selectable text beside the button.
          }
        })();
      }}
      className="rounded border border-slate-300 px-1.5 py-0.5 text-[10px] font-semibold text-slate-600 focus:outline focus:outline-2 focus:outline-sky-500 dark:border-slate-600 dark:text-slate-300"
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}
