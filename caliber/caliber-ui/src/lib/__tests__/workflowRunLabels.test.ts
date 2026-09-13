import { describe, expect, it } from "vitest";

import {
  approvalCheckpointKind,
  workflowRunApprovalBlockedLabel,
  workflowRunApprovalNoun,
  workflowRunApprovalPauseLabel,
  workflowRunApprovalRecordNoun,
  workflowRunApprovalRecordedLabel,
  workflowRunApprovalRejectedLabel,
  workflowRunApprovalSubject,
  workflowRunApprovalTitle,
  workflowRunAwaitingApprovalLabel,
  workflowRunCheckpointLabel,
  workflowRunCheckpointMarkerLabel,
  workflowRunLifecycleDetail,
  workflowRunLifecycleLabel,
  workflowRunLifecycleMessage,
  workflowRunLifecycleReason,
  workflowRunLifecycleSummary,
  workflowRunPendingApprovalChipLabel,
  workflowRunStatusBorderClass,
  workflowRunStatusFromEventType,
  workflowRunStatusFromStep,
  workflowRunStatusLabel,
  workflowRunStatusMessage,
  workflowRunStatusPhrase,
  workflowRunStatusRingClass,
  workflowRunStatusVerbPhrase,
} from "@/lib/workflowRunLabels";

describe("approvalCheckpointKind", () => {
  it("reads the kind off an object payload", () => {
    expect(approvalCheckpointKind({ kind: "human_approval" })).toBe(
      "human_approval",
    );
    expect(approvalCheckpointKind({ kind: "runtime_approval" })).toBe(
      "runtime_approval",
    );
  });

  it("reads a bare string value", () => {
    expect(approvalCheckpointKind("human_approval")).toBe("human_approval");
  });

  it("returns null for unrecognized kinds, arrays, and nullish input", () => {
    expect(approvalCheckpointKind({ kind: "wait_for_event" })).toBeNull();
    expect(approvalCheckpointKind("something_else")).toBeNull();
    expect(approvalCheckpointKind(["human_approval"])).toBeNull();
    expect(approvalCheckpointKind(null)).toBeNull();
    expect(approvalCheckpointKind(undefined)).toBeNull();
    expect(approvalCheckpointKind(42)).toBeNull();
  });
});

describe("workflowRunCheckpointLabel", () => {
  it("labels every known checkpoint kind", () => {
    expect(workflowRunCheckpointLabel("human_approval")).toBe("Human approval");
    expect(workflowRunCheckpointLabel("runtime_approval")).toBe(
      "Runtime approval",
    );
    expect(workflowRunCheckpointLabel("wait_for_event")).toBe("Wait for event");
    expect(workflowRunCheckpointLabel("wait_until")).toBe("Scheduled wait");
    expect(workflowRunCheckpointLabel("wait_event")).toBe("Wait event");
  });

  it("falls back to 'Checkpoint' for the checkpoint kind and unknown/null values", () => {
    expect(workflowRunCheckpointLabel("checkpoint")).toBe("Checkpoint");
    expect(workflowRunCheckpointLabel(null)).toBe("Checkpoint");
    expect(workflowRunCheckpointLabel("something_custom")).toBe(
      "something_custom",
    );
  });
});

describe("workflowRunCheckpointMarkerLabel", () => {
  it("uses marker-specific phrasing for known kinds", () => {
    expect(workflowRunCheckpointMarkerLabel("human_approval")).toBe(
      "Approval gate",
    );
    expect(workflowRunCheckpointMarkerLabel("runtime_approval")).toBe(
      "Runtime approval",
    );
    expect(workflowRunCheckpointMarkerLabel("wait_for_event")).toBe(
      "Event wait",
    );
    expect(workflowRunCheckpointMarkerLabel("wait_until")).toBe(
      "Scheduled wait",
    );
    expect(workflowRunCheckpointMarkerLabel("wait_event")).toBe("Resume gate");
  });

  it("falls back to workflowRunCheckpointLabel for anything else", () => {
    expect(workflowRunCheckpointMarkerLabel("checkpoint")).toBe("Checkpoint");
    expect(workflowRunCheckpointMarkerLabel(null)).toBe("Checkpoint");
  });
});

describe("approval phrasing helpers", () => {
  it("workflowRunApprovalTitle distinguishes runtime approval from plain approval", () => {
    expect(workflowRunApprovalTitle("runtime_approval")).toBe(
      "Runtime approval",
    );
    expect(workflowRunApprovalTitle("human_approval")).toBe("Approval");
    expect(workflowRunApprovalTitle(null)).toBe("Approval");
  });

  it("workflowRunApprovalNoun", () => {
    expect(workflowRunApprovalNoun("runtime_approval")).toBe(
      "runtime approval",
    );
    expect(workflowRunApprovalNoun("human_approval")).toBe("approval");
    expect(workflowRunApprovalNoun(null)).toBe("approval");
  });

  it("workflowRunApprovalRecordNoun", () => {
    expect(workflowRunApprovalRecordNoun("runtime_approval")).toBe(
      "runtime approval record",
    );
    expect(workflowRunApprovalRecordNoun(null)).toBe("approval record");
  });

  it("workflowRunApprovalSubject covers all three branches", () => {
    expect(workflowRunApprovalSubject("runtime_approval")).toBe(
      "runtime approval gate",
    );
    expect(workflowRunApprovalSubject("human_approval")).toBe(
      "human approval step",
    );
    expect(workflowRunApprovalSubject(null)).toBe("approval step");
  });

  it("workflowRunAwaitingApprovalLabel and pending chip label", () => {
    expect(workflowRunAwaitingApprovalLabel("runtime_approval")).toBe(
      "Awaiting runtime approval",
    );
    expect(workflowRunAwaitingApprovalLabel(null)).toBe("Awaiting approval");
    expect(workflowRunPendingApprovalChipLabel("runtime_approval")).toBe(
      "pending runtime approval",
    );
    expect(workflowRunPendingApprovalChipLabel(null)).toBe(
      "pending approval",
    );
  });

  it("workflowRunApprovalRecordedLabel / RejectedLabel / BlockedLabel / PauseLabel", () => {
    expect(workflowRunApprovalRecordedLabel("runtime_approval")).toBe(
      "Runtime approval recorded",
    );
    expect(workflowRunApprovalRecordedLabel(null)).toBe("Approval recorded");
    expect(workflowRunApprovalRejectedLabel("runtime_approval")).toBe(
      "Runtime approval rejected",
    );
    expect(workflowRunApprovalRejectedLabel(null)).toBe("Approval rejected");
    expect(workflowRunApprovalBlockedLabel("runtime_approval")).toBe(
      "Runtime approval blocked",
    );
    expect(workflowRunApprovalBlockedLabel(null)).toBe(
      "Approval gate blocked",
    );
    expect(workflowRunApprovalPauseLabel("runtime_approval")).toBe(
      "Paused for runtime approval",
    );
    expect(workflowRunApprovalPauseLabel(null)).toBe("Paused for approval");
  });
});

describe("workflowRunStatusLabel / Phrase / VerbPhrase / Message", () => {
  it("labels every known status", () => {
    expect(workflowRunStatusLabel("blocked")).toBe("Blocked");
    expect(workflowRunStatusLabel("cancel_requested")).toBe(
      "Cancel requested",
    );
    expect(workflowRunStatusLabel("waiting_approval")).toBe(
      "Awaiting approval",
    );
  });

  it("falls back to title-cased words for an unknown status, and 'Unknown' for null", () => {
    expect(workflowRunStatusLabel("some_odd_status")).toBe(
      "Some Odd Status",
    );
    expect(workflowRunStatusLabel(null)).toBe("Unknown");
  });

  it("workflowRunStatusPhrase lowercases the label", () => {
    expect(workflowRunStatusPhrase("blocked")).toBe("blocked");
    expect(workflowRunStatusPhrase("waiting_approval")).toBe(
      "awaiting approval",
    );
  });

  it("workflowRunStatusVerbPhrase covers every known case and the default", () => {
    expect(workflowRunStatusVerbPhrase("blocked")).toBe("is blocked");
    expect(workflowRunStatusVerbPhrase("cancel_requested")).toBe(
      "has a cancel request pending",
    );
    expect(workflowRunStatusVerbPhrase("cancelled")).toBe("was cancelled");
    expect(workflowRunStatusVerbPhrase("completed")).toBe("completed");
    expect(workflowRunStatusVerbPhrase("expired")).toBe("expired");
    expect(workflowRunStatusVerbPhrase("failed")).toBe("failed");
    expect(workflowRunStatusVerbPhrase("queued")).toBe("is queued");
    expect(workflowRunStatusVerbPhrase("rejected")).toBe("was rejected");
    expect(workflowRunStatusVerbPhrase("resuming")).toBe("is resuming");
    expect(workflowRunStatusVerbPhrase("running")).toBe("is running");
    expect(workflowRunStatusVerbPhrase("waiting_approval")).toBe(
      "is awaiting approval",
    );
    expect(workflowRunStatusVerbPhrase("waiting_event")).toBe(
      "is waiting for event",
    );
    expect(workflowRunStatusVerbPhrase("mystery")).toBe(
      "has status mystery",
    );
    expect(workflowRunStatusVerbPhrase(null)).toBe("has status unknown");
  });

  it("workflowRunStatusMessage composes the run id and verb phrase", () => {
    expect(workflowRunStatusMessage("WR-1", "running")).toBe(
      "Run WR-1 is running.",
    );
    expect(workflowRunStatusMessage("WR-2", null)).toBe(
      "Run WR-2 has status unknown.",
    );
  });
});

describe("workflowRunStatusBorderClass / RingClass", () => {
  it("returns the mapped classes for a known status", () => {
    expect(workflowRunStatusBorderClass("completed")).toContain(
      "emerald",
    );
    expect(workflowRunStatusRingClass("failed")).toContain("red");
  });

  it("falls back to the slate default for unknown/null status", () => {
    expect(workflowRunStatusBorderClass("unknown_status")).toContain(
      "slate",
    );
    expect(workflowRunStatusBorderClass(null)).toContain("slate");
    expect(workflowRunStatusRingClass(null)).toContain("slate");
  });
});

describe("workflowRunStatusFromEventType", () => {
  it("maps every known event type to a status", () => {
    expect(workflowRunStatusFromEventType("workflow.run.queued")).toBe(
      "queued",
    );
    expect(workflowRunStatusFromEventType("workflow.run.recovered")).toBe(
      "queued",
    );
    expect(workflowRunStatusFromEventType("workflow.run.started")).toBe(
      "running",
    );
    expect(
      workflowRunStatusFromEventType("workflow.run.node_started"),
    ).toBe("running");
    expect(
      workflowRunStatusFromEventType("workflow.run.waiting_approval"),
    ).toBe("waiting_approval");
    expect(
      workflowRunStatusFromEventType("workflow.run.waiting_event"),
    ).toBe("waiting_event");
    expect(workflowRunStatusFromEventType("workflow.run.resumed")).toBe(
      "queued",
    );
    expect(workflowRunStatusFromEventType("workflow.run.cancelled")).toBe(
      "cancelled",
    );
    expect(workflowRunStatusFromEventType("workflow.run.completed")).toBe(
      "completed",
    );
    expect(workflowRunStatusFromEventType("workflow.run.expired")).toBe(
      "expired",
    );
    expect(workflowRunStatusFromEventType("workflow.run.failed")).toBe(
      "failed",
    );
    expect(
      workflowRunStatusFromEventType("workflow.run.approval.rejected"),
    ).toBe("failed");
  });

  it("returns null for an unrecognized event type", () => {
    expect(workflowRunStatusFromEventType("workflow.run.mystery")).toBeNull();
  });
});

describe("workflowRunStatusFromStep", () => {
  it("returns null for a null/undefined step", () => {
    expect(workflowRunStatusFromStep(null)).toBeNull();
    expect(workflowRunStatusFromStep(undefined)).toBeNull();
  });

  it("reads waiting_event/waiting_approval off a detail prefix", () => {
    expect(
      workflowRunStatusFromStep({ detail: "waiting_event: my-event" }),
    ).toBe("waiting_event");
    expect(
      workflowRunStatusFromStep({ detail: "waiting_approval: gate-1" }),
    ).toBe("waiting_approval");
  });

  it("reads waiting_event/waiting_approval directly off status", () => {
    expect(workflowRunStatusFromStep({ status: "waiting_event" })).toBe(
      "waiting_event",
    );
    expect(workflowRunStatusFromStep({ status: "waiting_approval" })).toBe(
      "waiting_approval",
    );
  });

  it("infers waiting_event from a blocked wait_for_event/wait_until node", () => {
    expect(
      workflowRunStatusFromStep({
        status: "blocked",
        node_type: "wait_for_event",
      }),
    ).toBe("waiting_event");
    expect(
      workflowRunStatusFromStep({
        status: "blocked",
        node_type: "wait_until",
      }),
    ).toBe("waiting_event");
  });

  it("infers waiting_approval from a blocked human_approval node", () => {
    expect(
      workflowRunStatusFromStep({
        status: "blocked",
        node_type: "human_approval",
      }),
    ).toBe("waiting_approval");
  });

  it("returns null when blocked with an unrelated node type, or no match at all", () => {
    expect(
      workflowRunStatusFromStep({ status: "blocked", node_type: "http" }),
    ).toBeNull();
    expect(workflowRunStatusFromStep({ status: "running" })).toBeNull();
    expect(workflowRunStatusFromStep({})).toBeNull();
  });
});

describe("workflowRunLifecycleLabel", () => {
  it("uses summary-mode phrasing by default", () => {
    expect(workflowRunLifecycleLabel("workflow.run.queued")).toBe(
      "Run queued",
    );
    expect(workflowRunLifecycleLabel("workflow.run.recovered")).toBe(
      "Run recovered",
    );
    expect(workflowRunLifecycleLabel("workflow.run.started")).toBe(
      "Run started",
    );
    expect(
      workflowRunLifecycleLabel("workflow.run.waiting_event"),
    ).toBe("Waiting for event");
    expect(workflowRunLifecycleLabel("workflow.run.resumed")).toBe(
      "Run resumed",
    );
    expect(workflowRunLifecycleLabel("workflow.run.retried")).toBe(
      "Run retried",
    );
  });

  it("switches to marker-mode phrasing", () => {
    expect(
      workflowRunLifecycleLabel("workflow.run.queued", { mode: "marker" }),
    ).toBe("Queued");
    expect(
      workflowRunLifecycleLabel("workflow.run.recovered", {
        mode: "marker",
      }),
    ).toBe("Recovered");
    expect(
      workflowRunLifecycleLabel("workflow.run.node_started", {
        mode: "marker",
      }),
    ).toBe("Node started");
    expect(
      workflowRunLifecycleLabel("workflow.run.waiting_event", {
        mode: "marker",
      }),
    ).toBe("Paused for event");
    expect(
      workflowRunLifecycleLabel("workflow.run.resumed", { mode: "marker" }),
    ).toBe("Resumed");
    expect(
      workflowRunLifecycleLabel("workflow.run.retried", { mode: "marker" }),
    ).toBe("Retried");
  });

  it("delegates approval events to the approval-kind helpers", () => {
    expect(
      workflowRunLifecycleLabel("workflow.run.approval.approved"),
    ).toBe("Runtime approval recorded");
    expect(
      workflowRunLifecycleLabel("workflow.run.approval.rejected"),
    ).toBe("Runtime approval rejected");
    expect(
      workflowRunLifecycleLabel("workflow.run.waiting_approval", {
        approvalKind: "human_approval",
      }),
    ).toBe("Awaiting approval");
    expect(
      workflowRunLifecycleLabel("workflow.run.waiting_approval", {
        approvalKind: "human_approval",
        mode: "marker",
      }),
    ).toBe("Paused for approval");
  });

  it("defaults the waiting_approval approvalKind to null in both modes when omitted", () => {
    expect(workflowRunLifecycleLabel("workflow.run.waiting_approval")).toBe(
      "Awaiting approval",
    );
    expect(
      workflowRunLifecycleLabel("workflow.run.waiting_approval", {
        mode: "marker",
      }),
    ).toBe("Paused for approval");
  });

  it("covers the remaining terminal event labels", () => {
    expect(workflowRunLifecycleLabel("workflow.run.cancel_requested")).toBe(
      "Cancel requested",
    );
    expect(workflowRunLifecycleLabel("workflow.run.cancelled")).toBe(
      "Run cancelled",
    );
    expect(workflowRunLifecycleLabel("workflow.run.completed")).toBe(
      "Run completed",
    );
    expect(workflowRunLifecycleLabel("workflow.run.expired")).toBe(
      "Run expired",
    );
    expect(workflowRunLifecycleLabel("workflow.run.failed")).toBe(
      "Run failed",
    );
  });

  it("falls back to a humanized type label for an unrecognized event", () => {
    expect(workflowRunLifecycleLabel("workflow.run.node.custom_thing")).toBe(
      "Node Custom Thing",
    );
  });
});

describe("workflowRunLifecycleSummary", () => {
  it("appends approval id + reason detail when present", () => {
    expect(
      workflowRunLifecycleSummary("workflow.run.approval.approved", {
        runtime_approval_id: "RA-1",
        reason: "looks good",
      }),
    ).toBe("Runtime approval recorded · RA-1 · looks good");
    expect(
      workflowRunLifecycleSummary("workflow.run.approval.rejected", {
        runtime_approval_id: "RA-2",
      }),
    ).toBe("Runtime approval rejected · RA-2");
  });

  it("falls back to the base label with no detail", () => {
    expect(
      workflowRunLifecycleSummary("workflow.run.approval.approved", {}),
    ).toBe("Runtime approval recorded");
    expect(
      workflowRunLifecycleSummary("workflow.run.approval.rejected", {}),
    ).toBe("Runtime approval rejected");
  });

  it("appends the recovery reason", () => {
    expect(
      workflowRunLifecycleSummary("workflow.run.recovered", {
        reason: "lease_expired",
      }),
    ).toBe("Run recovered · worker lease expired");
    expect(
      workflowRunLifecycleSummary("workflow.run.recovered", {}),
    ).toBe("Run recovered");
  });

  it("appends the resume event name", () => {
    expect(
      workflowRunLifecycleSummary("workflow.run.resumed", {
        event_name: "payment.captured",
      }),
    ).toBe("Run resumed · payment.captured");
    expect(workflowRunLifecycleSummary("workflow.run.resumed", {})).toBe(
      "Run resumed",
    );
  });

  it("appends node type + node id detail for node_started", () => {
    expect(
      workflowRunLifecycleSummary("workflow.run.node_started", {
        node_type: "http_request",
        node_id: "n1",
      }),
    ).toBe("Node started · Http Request · n1");
    expect(
      workflowRunLifecycleSummary("workflow.run.node_started", {}),
    ).toBe("Node started");
  });

  it("renders the retried-as message when a retried run id is present", () => {
    expect(
      workflowRunLifecycleSummary("workflow.run.retried", {
        retried_run_id: "WR-9",
      }),
    ).toBe("Retried as WR-9");
    expect(workflowRunLifecycleSummary("workflow.run.retried", {})).toBe(
      "Run retried",
    );
  });

  it("appends reason/error detail for cancel/cancelled/expired/failed", () => {
    expect(
      workflowRunLifecycleSummary("workflow.run.cancel_requested", {
        reason: "operator request",
      }),
    ).toBe("Cancel requested · operator request");
    expect(
      workflowRunLifecycleSummary("workflow.run.cancelled", {
        error: "boom",
      }),
    ).toBe("Run cancelled · boom");
    expect(
      workflowRunLifecycleSummary("workflow.run.expired", {}),
    ).toBe("Run expired");
    expect(
      workflowRunLifecycleSummary("workflow.run.failed", {
        reason: "lease_expired",
      }),
    ).toBe("Run failed · worker lease expired");
  });

  it("returns the base label for any other event type", () => {
    expect(
      workflowRunLifecycleSummary("workflow.run.completed", {}),
    ).toBe("Run completed");
  });
});

describe("workflowRunLifecycleDetail", () => {
  it("covers every approved/rejected id+reason combination", () => {
    expect(
      workflowRunLifecycleDetail("workflow.run.approval.approved", {
        runtime_approval_id: "RA-1",
        reason: "ok",
      }),
    ).toBe("Runtime approval RA-1 approved: ok");
    expect(
      workflowRunLifecycleDetail("workflow.run.approval.approved", {
        runtime_approval_id: "RA-1",
      }),
    ).toBe("Runtime approval RA-1 approved");
    expect(
      workflowRunLifecycleDetail("workflow.run.approval.approved", {
        reason: "ok",
      }),
    ).toBe("Runtime approval approved: ok");
    expect(
      workflowRunLifecycleDetail("workflow.run.approval.approved", {}),
    ).toBe("Runtime approval approved");

    expect(
      workflowRunLifecycleDetail("workflow.run.approval.rejected", {
        runtime_approval_id: "RA-2",
        reason: "no",
      }),
    ).toBe("Runtime approval RA-2 rejected: no");
    expect(
      workflowRunLifecycleDetail("workflow.run.approval.rejected", {
        runtime_approval_id: "RA-2",
      }),
    ).toBe("Runtime approval RA-2 rejected");
    expect(
      workflowRunLifecycleDetail("workflow.run.approval.rejected", {
        reason: "no",
      }),
    ).toBe("Runtime approval rejected: no");
    expect(
      workflowRunLifecycleDetail("workflow.run.approval.rejected", {}),
    ).toBe("Runtime approval rejected");
  });

  it("covers every recovered worker/reason combination", () => {
    expect(
      workflowRunLifecycleDetail("workflow.run.recovered", {
        worker_id: "W-1",
        reason: "lease_expired",
      }),
    ).toBe("Recovered by W-1: worker lease expired");
    expect(
      workflowRunLifecycleDetail("workflow.run.recovered", {
        worker_id: "W-1",
      }),
    ).toBe("Recovered by W-1");
    expect(
      workflowRunLifecycleDetail("workflow.run.recovered", {
        reason: "lease_expired",
      }),
    ).toBe("Recovered: worker lease expired");
    expect(workflowRunLifecycleDetail("workflow.run.recovered", {})).toBe(
      "Run recovered",
    );
  });

  it("returns resumed / retried detail only when the payload carries it", () => {
    expect(
      workflowRunLifecycleDetail("workflow.run.resumed", {
        event_name: "evt",
      }),
    ).toBe("Resume event evt");
    expect(workflowRunLifecycleDetail("workflow.run.resumed", {})).toBeNull();
    expect(
      workflowRunLifecycleDetail("workflow.run.retried", {
        retried_run_id: "WR-3",
      }),
    ).toBe("Retried as WR-3");
    expect(workflowRunLifecycleDetail("workflow.run.retried", {})).toBeNull();
  });

  it("uses the payload reason over the fallback for cancel/cancelled", () => {
    expect(
      workflowRunLifecycleDetail(
        "workflow.run.cancel_requested",
        { reason: "payload reason" },
        { cancelReason: "fallback reason" },
      ),
    ).toBe("Cancel requested: payload reason");
    expect(
      workflowRunLifecycleDetail(
        "workflow.run.cancel_requested",
        {},
        { cancelReason: "fallback reason" },
      ),
    ).toBe("Cancel requested: fallback reason");
    expect(
      workflowRunLifecycleDetail("workflow.run.cancel_requested", {}),
    ).toBeNull();

    expect(
      workflowRunLifecycleDetail(
        "workflow.run.cancelled",
        {},
        { cancelReason: "fallback" },
      ),
    ).toBe("Cancelled: fallback");
    expect(
      workflowRunLifecycleDetail("workflow.run.cancelled", {}),
    ).toBeNull();
  });

  it("uses the payload detail over the failure fallback for expired/failed", () => {
    expect(
      workflowRunLifecycleDetail(
        "workflow.run.expired",
        {},
        { failureDetail: "timeout" },
      ),
    ).toBe("Expired: timeout");
    expect(
      workflowRunLifecycleDetail("workflow.run.expired", {}),
    ).toBeNull();
    expect(
      workflowRunLifecycleDetail(
        "workflow.run.failed",
        { error: "stack trace" },
        { failureDetail: "fallback" },
      ),
    ).toBe("Failure: stack trace");
    expect(
      workflowRunLifecycleDetail("workflow.run.failed", {}),
    ).toBeNull();
  });

  it("returns null for any other event type", () => {
    expect(
      workflowRunLifecycleDetail("workflow.run.completed", {}),
    ).toBeNull();
  });
});

describe("workflowRunLifecycleReason", () => {
  it("prefers reason over error, and humanizes lease_expired", () => {
    expect(
      workflowRunLifecycleReason({ reason: "lease_expired", error: "x" }),
    ).toBe("worker lease expired");
    expect(workflowRunLifecycleReason({ error: "boom" })).toBe("boom");
    expect(workflowRunLifecycleReason({})).toBeNull();
  });

  it("returns null for a non-object payload (array, primitive, nullish)", () => {
    expect(workflowRunLifecycleReason(["reason"])).toBeNull();
    expect(workflowRunLifecycleReason("plain string")).toBeNull();
    expect(workflowRunLifecycleReason(null)).toBeNull();
    expect(workflowRunLifecycleReason(undefined)).toBeNull();
  });

  it("ignores a blank/whitespace-only reason string", () => {
    expect(workflowRunLifecycleReason({ reason: "   " })).toBeNull();
  });
});

describe("workflowRunLifecycleMessage", () => {
  it("covers every explicit event-type branch", () => {
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.queued", {}),
    ).toBe("Run WR-1 queued.");
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.recovered", {
        reason: "lease_expired",
      }),
    ).toBe("Run WR-1 recovered and re-queued: worker lease expired.");
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.recovered", {}),
    ).toBe("Run WR-1 recovered and re-queued.");
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.started", {}),
    ).toBe("Run WR-1 started.");
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.node_started", {}),
    ).toBe("Run WR-1 is running.");
    expect(
      workflowRunLifecycleMessage(
        "WR-1",
        "workflow.run.approval.approved",
        {},
      ),
    ).toBe("Approval recorded for WR-1.");
    expect(
      workflowRunLifecycleMessage(
        "WR-1",
        "workflow.run.approval.rejected",
        { reason: "bad" },
      ),
    ).toBe("Runtime approval rejected for WR-1: bad.");
    expect(
      workflowRunLifecycleMessage(
        "WR-1",
        "workflow.run.approval.rejected",
        {},
      ),
    ).toBe("Runtime approval rejected for WR-1.");
    expect(
      workflowRunLifecycleMessage(
        "WR-1",
        "workflow.run.waiting_approval",
        {},
        { approvalKind: "runtime_approval" },
      ),
    ).toBe("Run WR-1 is awaiting runtime approval.");
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.waiting_approval", {}),
    ).toBe("Run WR-1 is awaiting approval.");
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.waiting_event", {}),
    ).toBe("Run WR-1 is waiting for event.");
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.resumed", {}),
    ).toBe("Run WR-1 resumed.");
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.retried", {
        retried_run_id: "WR-2",
      }),
    ).toBe("Run WR-1 retried as WR-2.");
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.retried", {}),
    ).toBe("Run WR-1 was retried.");
    expect(
      workflowRunLifecycleMessage(
        "WR-1",
        "workflow.run.cancel_requested",
        { reason: "operator" },
      ),
    ).toBe("Run WR-1 has a cancel request pending: operator.");
    expect(
      workflowRunLifecycleMessage(
        "WR-1",
        "workflow.run.cancel_requested",
        {},
      ),
    ).toBe("Run WR-1 has a cancel request pending.");
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.cancelled", {
        reason: "operator",
      }),
    ).toBe("Run WR-1 was cancelled: operator.");
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.cancelled", {}),
    ).toBe("Run WR-1 was cancelled.");
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.completed", {}),
    ).toBe("Run WR-1 completed.");
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.expired", {
        error: "timeout",
      }),
    ).toBe("Run WR-1 expired: timeout.");
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.expired", {}),
    ).toBe("Run WR-1 expired.");
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.failed", {
        error: "boom",
      }),
    ).toBe("Run WR-1 failed: boom.");
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.failed", {}),
    ).toBe("Run WR-1 failed.");
  });

  it("falls back to a humanized type label when the event type is unrecognized", () => {
    expect(
      workflowRunLifecycleMessage("WR-1", "workflow.run.node_started2", {}),
    ).toBe("Run WR-1 node started2.");
  });

  it("falls back to the humanized type label when no status can be derived either", () => {
    expect(
      workflowRunLifecycleMessage("WR-1", "totally.custom_event", {}),
    ).toBe("Run WR-1 totally custom event.");
  });
});
