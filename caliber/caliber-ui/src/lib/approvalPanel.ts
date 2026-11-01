/**
 * Pure helpers of the exact-intent approval panel (B06.5, #743): which requests the panel owns, how a digest and a
 * countdown read, and the one message and recovery step for every refusal.
 *
 * The messages are the ones of `docs/architecture/approval-ux-contract.md` ("Denial reasons") for its eight codes, and
 * the decision routes' own `reason_code` vocabulary for the rest. The server decides every refusal: nothing here infers
 * one. A message is chosen from a code the server sent, with values the server also sent (never from error text), so an
 * unexpected code falls back to a generic message instead of rendering text from a response body.
 */
import { ApiError } from "@/api/caliberApi";
import type {
  WorkflowApprovalStateCode,
  WorkflowRuntimeApproval,
} from "@/api/workflowTypes";

/** A request bound to one effect proposal: decided in the approval panel, not with a blind button. */
export function isBoundApproval(
  approval: Pick<WorkflowRuntimeApproval, "proposal_id"> | null | undefined,
): boolean {
  return Boolean(approval?.proposal_id);
}

/**
 * A bound request the panel should still show in full: waiting for a decision, or decided and waiting for dispatch.
 * Superseded and consumed requests are history and stay as the compact card.
 */
export function isLiveBoundApproval(approval: WorkflowRuntimeApproval): boolean {
  return (
    isBoundApproval(approval)
    && (approval.status === "pending" || approval.status === "approved")
    && !approval.superseded_by
    && !approval.consumed_at
  );
}

/**
 * Server timestamps are UTC, and a stored one is serialised without an offset ("2026-11-01T14:30:00"), which `Date` would
 * read as local time. A value with no `Z` or offset is UTC here, so the expiry and its countdown are right in any zone.
 */
export function parseServerTime(value: string): number {
  const hasZone = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(value);
  return new Date(hasZone ? value : `${value}Z`).getTime();
}

/** `sha256:` and the first 12 hex digits; anything that is not a digest is shown as received, bounded. */
export function shortDigest(digest: string): string {
  const match = /^sha256:([0-9a-f]{12})[0-9a-f]*$/.exec(digest);
  return match ? `sha256:${match[1]}` : digest.slice(0, 24);
}

const EFFECT_CLASS_LABELS: Record<string, string> = {
  read: "Read only",
  reversible_write: "Reversible write",
  irreversible_write: "Irreversible write",
  export: "Data export",
};

export function effectClassLabel(effectClass: string): string {
  return EFFECT_CLASS_LABELS[effectClass] ?? effectClass.replace(/_/g, " ");
}

/** Whether an effect class cannot be taken back; the badge is then drawn as a warning (text first, colour second). */
export function isIrreversibleEffect(effectClass: string): boolean {
  return effectClass === "irreversible_write" || effectClass === "export";
}

/** "expires in 42 min": coarse on purpose, since it is refreshed once a minute and sits outside the live region. */
export function countdownLabel(expiresAtMs: number, nowMs: number): string {
  const remainingMs = expiresAtMs - nowMs;
  if (!Number.isFinite(remainingMs)) return "no expiry time";
  if (remainingMs <= 0) return "expired";
  const minutes = Math.floor(remainingMs / 60_000);
  if (minutes < 1) return "expires in less than a minute";
  if (minutes < 60) return `expires in ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) {
    const rest = minutes % 60;
    return rest === 0 ? `expires in ${hours} h` : `expires in ${hours} h ${rest} min`;
  }
  return `expires in ${Math.floor(hours / 24)} days`;
}

/** An absolute time with its offset, for the record ("2026-11-01 14:30 UTC"); the raw value when it does not parse. */
export function formatExpiry(value: string): string {
  const time = parseServerTime(value);
  if (Number.isNaN(time)) return value;
  const iso = new Date(time).toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;
}

/** The values a message may name. All of them come from the server's view, never from an error body. */
export interface DenialContext {
  expiresAt?: string | null;
  expectedVersion?: string | null;
  currentVersion?: string | null;
  supersededBy?: string | null;
  required?: number;
  remaining?: number;
  approved?: number;
  requiredRole?: string | null;
  decidedBy?: string | null;
  reviewed?: string | null;
  current?: string | null;
}

/** Every code the panel can show a message for: the contract's eight, then the routes' own and the ability reasons. */
export type ApprovalDenialCode =
  | "stale_version"
  | "expired"
  | "superseded"
  | "self_approval"
  | "duplicate_approver"
  | "role_revoked"
  | "fenced_owner"
  | "intent_mismatch"
  | "insufficient_scope"
  | "project_role_required"
  | "queue_disabled"
  | "approvals_disabled"
  | "checkpointing_disabled"
  | "already_decided"
  | "rejected"
  | "already_consumed"
  | "approval_not_current"
  | "run_not_waiting"
  | "approval_busy"
  | "not_found"
  | "forbidden";

/** Refusals that block the approver outright. They are announced assertively (`role="alert"`); the rest politely. */
export const BLOCKING_DENIALS: ReadonlySet<string> = new Set([
  "stale_version",
  "intent_mismatch",
  "expired",
  "superseded",
  "role_revoked",
]);

export const KNOWN_DENIAL_CODES: ReadonlySet<string> = new Set<ApprovalDenialCode>([
  "stale_version",
  "expired",
  "superseded",
  "self_approval",
  "duplicate_approver",
  "role_revoked",
  "fenced_owner",
  "intent_mismatch",
  "insufficient_scope",
  "project_role_required",
  "queue_disabled",
  "approvals_disabled",
  "checkpointing_disabled",
  "already_decided",
  "rejected",
  "already_consumed",
  "approval_not_current",
  "run_not_waiting",
  "approval_busy",
  "not_found",
  "forbidden",
]);

function version(value: string | null | undefined): string {
  return value && value.trim() ? value : "unknown";
}

/** The message and recovery step for a refusal; `null` for a code the panel has no text for. */
export function denialMessage(code: string, ctx: DenialContext = {}): string | null {
  switch (code) {
    case "stale_version":
      // The current version is only known when the server names it; otherwise say what is known without inventing it.
      return ctx.currentVersion
        ? `The case changed after this request was made (it was at version ${version(ctx.expectedVersion)} and is now at version ${ctx.currentVersion}). A fresh approval request is needed.`
        : `The case changed after this request was made (it was at version ${version(ctx.expectedVersion)} and is no longer at that version). A fresh approval request is needed.`;
    case "expired":
      return `This request expired at ${ctx.expiresAt ? formatExpiry(ctx.expiresAt) : "its expiry time"}. Nothing was done. Ask the requester to send it again.`;
    case "superseded":
      return `A newer request replaced this one${ctx.supersededBy ? ` (${ctx.supersededBy})` : ""}. Open the newer request.`;
    case "self_approval":
      return "You started this run, so you cannot approve it. A second person must decide.";
    case "duplicate_approver": {
      const required = ctx.required ?? 0;
      const remaining = ctx.remaining ?? 0;
      return `You already approved this step. It needs ${required} different approvers; ${remaining} more ${remaining === 1 ? "is" : "are"} needed.`;
    }
    case "role_revoked":
      return "You no longer hold a role in this project that can approve this, so this decision was not recorded.";
    case "fenced_owner":
      return "The system that was running this workflow changed owner after you approved. Nothing was done yet; the platform retries under the new owner and your approval still stands unless it expires.";
    case "intent_mismatch":
      return `What you reviewed no longer matches what would run (reviewed ${ctx.reviewed ? shortDigest(ctx.reviewed) : "an earlier version"}, now ${ctx.current ? shortDigest(ctx.current) : "a different one"}). Nothing was approved.`;
    case "insufficient_scope":
      return `This step needs the ${ctx.requiredRole ?? "required"} role and your account does not hold it. Ask an administrator for the role, or ask someone who holds it to decide.`;
    case "project_role_required":
      return "Deciding an approval needs an approving role in this project (reviewer or owner) and you do not hold one. Ask a project owner.";
    case "queue_disabled":
      return "Deciding approvals is switched off for this deployment: the workflow run queue is disabled. Ask an administrator to enable it, then reload.";
    case "approvals_disabled":
      return "Runtime approvals are switched off for this deployment, so nothing can be decided here. Ask an administrator to enable them, then reload.";
    case "checkpointing_disabled":
      return "Approving needs run checkpointing, which is switched off for this deployment. Rejecting is still possible. Ask an administrator to enable checkpointing to approve.";
    case "already_decided":
      return `This request already holds a decision${ctx.decidedBy ? ` from ${ctx.decidedBy}` : ""} and is waiting for the system to act on it. It cannot be decided again and nothing more is needed from you.`;
    case "rejected":
      return `This request was rejected${ctx.decidedBy ? ` by ${ctx.decidedBy}` : ""}. Nothing was or will be done. Ask the requester for a new request if the action is still needed.`;
    case "already_consumed":
      return "This approval was already used to run the action. It cannot be decided again. Ask the requester for a new request if the action is needed again.";
    case "approval_not_current":
      return "This request is not for the step the run is waiting at. Open the run's current approval to decide it.";
    case "run_not_waiting":
      return "This run is no longer waiting for approval (it was cancelled, finished or moved on). Nothing can be decided here.";
    case "approval_busy":
      return "This request is being decided or dispatched right now. Nothing was decided. Wait a moment, then press the button again.";
    case "not_found":
      return "This approval request no longer exists for this run. Refresh the run to see its current approval.";
    case "forbidden":
      return "The server refused this decision. The panel was refreshed to show the reason; nothing was recorded.";
    default:
      return null;
  }
}

/**
 * What the last decision call did, in the terms the panel renders. `unknown` is a network failure or a server error: the
 * decision may or may not have been recorded, so the panel refetches and never retries by itself.
 */
export type ApprovalOutcome =
  | { kind: "approved"; remaining: number | null }
  | { kind: "rejected" }
  | { kind: "denied"; code: string }
  | { kind: "invalid"; message: string }
  | { kind: "unknown" }
  | { kind: "failed"; status: number };

const MAX_FIELD_MESSAGE = 200;

/** Classify a failed decision call from its status and `reason_code`; the response text is never rendered as it came. */
export function classifyDecisionError(error: unknown): ApprovalOutcome {
  if (!(error instanceof ApiError)) return { kind: "unknown" };
  const status = error.status;
  const code = error.body?.reason_code;
  if (status === 503 && code === "approval_busy") return { kind: "denied", code: "approval_busy" };
  if (status === 0 || status >= 500) return { kind: "unknown" };
  if (code && KNOWN_DENIAL_CODES.has(code)) return { kind: "denied", code };
  if (status === 403) return { kind: "denied", code: "forbidden" };
  if (status === 404) return { kind: "denied", code: "not_found" };
  if (status === 400 || status === 422) {
    const first = error.body?.errors?.[0];
    const field = first?.loc?.[first.loc.length - 1];
    const text = first
      ? `${field ? `${field}: ` : ""}${first.msg}`
      : "the request was not accepted";
    return { kind: "invalid", message: text.slice(0, MAX_FIELD_MESSAGE) };
  }
  return { kind: "failed", status };
}

/** Whether a state code means nobody can decide this request now (the panel is read-only). */
export function isClosedState(code: WorkflowApprovalStateCode): boolean {
  return code !== "open";
}
