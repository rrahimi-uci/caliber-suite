import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/caliberApi";
import type { WorkflowRuntimeApproval } from "@/api/workflowTypes";
import {
  BLOCKING_DENIALS,
  KNOWN_DENIAL_CODES,
  classifyDecisionError,
  countdownLabel,
  denialMessage,
  effectClassLabel,
  formatExpiry,
  isBoundApproval,
  isClosedState,
  isIrreversibleEffect,
  isLiveBoundApproval,
  parseServerTime,
  shortDigest,
} from "@/lib/approvalPanel";

const BASE: WorkflowRuntimeApproval = {
  runtime_approval_id: "RAP-1",
  workflow_run_id: "WR-1",
  project_id: null,
  node_id: "gate",
  status: "pending",
  requested_at: "2026-11-01T12:00:00",
  decided_at: null,
  decided_by: null,
  decision_reason: null,
  policy_snapshot: null,
};

describe("which requests the panel owns", () => {
  it("treats a request with a proposal id as bound and a legacy one as not", () => {
    expect(isBoundApproval({ proposal_id: "EP-1" })).toBe(true);
    expect(isBoundApproval({ proposal_id: null })).toBe(false);
    expect(isBoundApproval({})).toBe(false);
    expect(isBoundApproval(null)).toBe(false);
    expect(isBoundApproval(undefined)).toBe(false);
  });

  it.each([
    [{ proposal_id: "EP-1", status: "pending" as const }, true],
    [{ proposal_id: "EP-1", status: "approved" as const }, true],
    [{ proposal_id: "EP-1", status: "rejected" as const }, false],
    [{ proposal_id: "EP-1", status: "pending" as const, superseded_by: "RAP-2" }, false],
    [{ proposal_id: "EP-1", status: "approved" as const, consumed_at: "2026-11-01T13:00:00" }, false],
    [{ proposal_id: null, status: "pending" as const }, false],
  ])("is live for %o: %s", (overrides, expected) => {
    expect(isLiveBoundApproval({ ...BASE, ...overrides })).toBe(expected);
  });
});

describe("digests, classes and times", () => {
  it("shortens a digest to sha256: and 12 hex characters and bounds anything else", () => {
    expect(shortDigest(`sha256:${"ab".repeat(32)}`)).toBe("sha256:abababababab");
    expect(shortDigest("x".repeat(100))).toBe("x".repeat(24));
    expect(shortDigest("sha256:XYZ")).toBe("sha256:XYZ");
  });

  it("labels the four effect classes and passes an unknown one through as words", () => {
    expect(effectClassLabel("read")).toBe("Read only");
    expect(effectClassLabel("reversible_write")).toBe("Reversible write");
    expect(effectClassLabel("irreversible_write")).toBe("Irreversible write");
    expect(effectClassLabel("export")).toBe("Data export");
    expect(effectClassLabel("some_new_class")).toBe("some new class");
    expect(isIrreversibleEffect("irreversible_write")).toBe(true);
    expect(isIrreversibleEffect("export")).toBe(true);
    expect(isIrreversibleEffect("reversible_write")).toBe(false);
  });

  it("reads a stored time with no offset as UTC and honours an explicit one", () => {
    expect(parseServerTime("2026-11-01T14:30:00")).toBe(Date.UTC(2026, 10, 1, 14, 30));
    expect(parseServerTime("2026-11-01T14:30:00.250000")).toBe(Date.UTC(2026, 10, 1, 14, 30, 0, 250));
    expect(parseServerTime("2026-11-01T14:30:00Z")).toBe(Date.UTC(2026, 10, 1, 14, 30));
    expect(parseServerTime("2026-11-01T16:30:00+02:00")).toBe(Date.UTC(2026, 10, 1, 14, 30));
    expect(parseServerTime("2026-11-01T09:30:00-0500")).toBe(Date.UTC(2026, 10, 1, 14, 30));
    expect(Number.isNaN(parseServerTime("not a time"))).toBe(true);
  });

  it("formats an expiry in UTC and returns an unparseable value as received", () => {
    expect(formatExpiry("2026-11-01T14:30:00")).toBe("2026-11-01 14:30 UTC");
    expect(formatExpiry("2026-11-01T16:30:00+02:00")).toBe("2026-11-01 14:30 UTC");
    expect(formatExpiry("soon")).toBe("soon");
  });

  it.each([
    [-1, "expired"],
    [0, "expired"],
    [1, "expires in less than a minute"],
    [59_999, "expires in less than a minute"],
    [60_000, "expires in 1 min"],
    [42 * 60_000, "expires in 42 min"],
    [59 * 60_000 + 59_000, "expires in 59 min"],
    [60 * 60_000, "expires in 1 h"],
    [150 * 60_000, "expires in 2 h 30 min"],
    [47 * 3_600_000, "expires in 47 h"],
    [48 * 3_600_000, "expires in 2 days"],
    [10 * 24 * 3_600_000, "expires in 10 days"],
  ])("counts down %i ms as %s", (remaining, label) => {
    expect(countdownLabel(1_000_000 + remaining, 1_000_000)).toBe(label);
  });

  it("does not invent a countdown for a time that did not parse", () => {
    expect(countdownLabel(Number.NaN, 0)).toBe("no expiry time");
  });
});

describe("the one message for each refusal", () => {
  it("renders the contract text of all eight codes with the values the server sent", () => {
    expect(denialMessage("stale_version", { expectedVersion: "1", currentVersion: "2" })).toBe(
      "The case changed after this request was made (it was at version 1 and is now at version 2). A fresh approval request is needed.",
    );
    expect(denialMessage("expired", { expiresAt: "2026-11-01T14:30:00" })).toBe(
      "This request expired at 2026-11-01 14:30 UTC. Nothing was done. Ask the requester to send it again.",
    );
    expect(denialMessage("superseded", { supersededBy: "RAP-2" })).toBe(
      "A newer request replaced this one (RAP-2). Open the newer request.",
    );
    expect(denialMessage("self_approval")).toBe(
      "You started this run, so you cannot approve it. A second person must decide.",
    );
    expect(denialMessage("duplicate_approver", { required: 3, remaining: 2 })).toBe(
      "You already approved this step. It needs 3 different approvers; 2 more are needed.",
    );
    expect(denialMessage("role_revoked")).toBe(
      "You no longer hold a role in this project that can approve this, so this decision was not recorded.",
    );
    expect(denialMessage("fenced_owner")).toMatch(/^The system that was running this workflow changed owner after you approved\./);
    expect(
      denialMessage("intent_mismatch", { reviewed: `sha256:${"a".repeat(64)}`, current: `sha256:${"b".repeat(64)}` }),
    ).toBe(
      "What you reviewed no longer matches what would run (reviewed sha256:aaaaaaaaaaaa, now sha256:bbbbbbbbbbbb). Nothing was approved.",
    );
  });

  it("does not invent a version or a time the server did not send", () => {
    expect(denialMessage("stale_version", {})).toContain("it was at version unknown and is no longer at that version");
    expect(denialMessage("expired", {})).toContain("expired at its expiry time");
    expect(denialMessage("superseded", {})).toBe("A newer request replaced this one. Open the newer request.");
    expect(denialMessage("intent_mismatch", {})).toContain("reviewed an earlier version, now a different one");
    expect(denialMessage("duplicate_approver", { required: 2, remaining: 1 })).toContain("1 more is needed");
  });

  it("names who decided a decided or rejected request, and omits the name when unknown", () => {
    expect(denialMessage("already_decided", { decidedBy: "approver_b" })).toContain("a decision from approver_b");
    expect(denialMessage("already_decided", {})).toContain("holds a decision and is waiting");
    expect(denialMessage("rejected", { decidedBy: "approver_b" })).toContain("rejected by approver_b");
    expect(denialMessage("rejected", {})).toContain("was rejected.");
  });

  it("has text for every known code and none for an unknown one", () => {
    for (const code of KNOWN_DENIAL_CODES) {
      expect(denialMessage(code, { requiredRole: "caliber.approver" }), code).toEqual(expect.any(String));
    }
    expect(denialMessage("not_a_code")).toBeNull();
    expect(denialMessage("insufficient_scope", { requiredRole: "caliber.admin" })).toContain("the caliber.admin role");
  });

  it("announces the five blocking refusals assertively and no other", () => {
    expect([...BLOCKING_DENIALS].sort()).toEqual([
      "expired",
      "intent_mismatch",
      "role_revoked",
      "stale_version",
      "superseded",
    ]);
  });

  it("closes a panel for every state but open", () => {
    expect(isClosedState("open")).toBe(false);
    expect(isClosedState("expired")).toBe(true);
    expect(isClosedState("already_consumed")).toBe(true);
  });
});

describe("classifying a failed decision call", () => {
  const apiError = (status: number, body: Record<string, unknown> | null) =>
    new ApiError(status, "detail", body ? ({ detail: "detail", status_code: status, ...body } as never) : null);

  it.each([
    ["already_consumed"],
    ["superseded"],
    ["expired"],
    ["approval_not_current"],
    ["already_decided"],
  ])("maps the 409 %s to its refusal", (code) => {
    expect(classifyDecisionError(apiError(409, { reason_code: code }))).toEqual({ kind: "denied", code });
  });

  it("maps 503 approval_busy to the retry message and any other 5xx to an unknown outcome", () => {
    expect(classifyDecisionError(apiError(503, { reason_code: "approval_busy" }))).toEqual({
      kind: "denied",
      code: "approval_busy",
    });
    expect(classifyDecisionError(apiError(503, null))).toEqual({ kind: "unknown" });
    expect(classifyDecisionError(apiError(500, { reason_code: "approval_busy" }))).toEqual({ kind: "unknown" });
    expect(classifyDecisionError(apiError(502, null))).toEqual({ kind: "unknown" });
  });

  it("treats a network failure and a non-API error as an unknown outcome", () => {
    expect(classifyDecisionError(apiError(0, null))).toEqual({ kind: "unknown" });
    expect(classifyDecisionError(new TypeError("Failed to fetch"))).toEqual({ kind: "unknown" });
    expect(classifyDecisionError(undefined)).toEqual({ kind: "unknown" });
  });

  it("maps a 403 with no known code to a generic refusal and 404 to not found", () => {
    expect(classifyDecisionError(apiError(403, null))).toEqual({ kind: "denied", code: "forbidden" });
    expect(classifyDecisionError(apiError(404, null))).toEqual({ kind: "denied", code: "not_found" });
    expect(classifyDecisionError(apiError(403, { reason_code: "self_approval" }))).toEqual({
      kind: "denied",
      code: "self_approval",
    });
  });

  it("never renders response text: an unknown reason code on a 409 is a plain failure", () => {
    expect(classifyDecisionError(apiError(409, { reason_code: "totally_new", detail: "secret detail" }))).toEqual({
      kind: "failed",
      status: 409,
    });
  });

  it("names the field of a validation error and bounds its text", () => {
    const long = "x".repeat(500);
    expect(
      classifyDecisionError(
        apiError(400, { errors: [{ loc: ["body", "reason"], msg: "too long", type: "value_error" }] }),
      ),
    ).toEqual({ kind: "invalid", message: "reason: too long" });
    const bounded = classifyDecisionError(
      apiError(422, { errors: [{ loc: [], msg: long, type: "value_error" }] }),
    );
    expect(bounded.kind === "invalid" && bounded.message.length).toBe(200);
    expect(classifyDecisionError(apiError(400, null))).toEqual({
      kind: "invalid",
      message: "the request was not accepted",
    });
  });
});
