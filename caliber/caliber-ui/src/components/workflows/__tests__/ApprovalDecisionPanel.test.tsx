import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApprovalDecision } from "@/api/workflowTypes";
import { ApprovalDecisionPanel } from "@/components/workflows/ApprovalDecisionPanel";
import type { ApprovalOutcome } from "@/lib/approvalPanel";

import { APPROVAL, EVIDENCE, INTENT, POLICY, PROPOSAL, SECRET_VALUE, review } from "./approvalFixtures";

type Props = Parameters<typeof ApprovalDecisionPanel>[0];

function renderPanel(overrides: Partial<Props> = {}) {
  const onDecide = vi.fn<(decision: ApprovalDecision, reason?: string) => void>();
  const props: Props = {
    approval: APPROVAL,
    proposal: PROPOSAL,
    review: review(),
    onDecide,
    ...overrides,
  };
  const utils = render(<ApprovalDecisionPanel {...props} />);
  return { ...utils, onDecide, props };
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true, now: new Date("2026-11-01T13:48:30Z") });
});
afterEach(() => {
  vi.useRealTimers();
});

describe("ApprovalDecisionPanel: what the approver is deciding", () => {
  it("renders every field of the bound request from the fixture", () => {
    renderPanel();

    expect(
      screen.getByRole("heading", {
        name: "Approval request for cases/MORT-2026-000184/document-requests",
      }),
    ).toBeInTheDocument();
    const summary = screen.getByTestId("approval-summary");
    const text = summary.textContent ?? "";
    expect(text).toContain("RAP-1");
    expect(text).toContain("EP-1");
    expect(text).toContain("WR-1");
    expect(text).toContain("human_gate");
    expect(text).toContain("document-request");
    expect(text).toContain("version 1.0.0");
    expect(text).toContain("cases/MORT-2026-000184/document-requests");
    expect(text).toContain("Irreversible write");
    expect(text).toContain("Valid for version 7");
    expect(text).toContain("by editor_a, 2026-11-01 12:00 UTC");
    expect(text).toContain("needs 2 approvals from the caliber.approver role");
    expect(screen.getByTestId("approval-risk-badge")).toHaveTextContent("Risk class: Irreversible write");
    // The digests are shortened to 12 hex characters and carry the full value in their accessible name.
    expect(screen.getByLabelText(`Intent digest ${INTENT}`)).toHaveTextContent("sha256:3f9a1c0b77d2");
    expect(screen.getByLabelText(`Evidence digest ${EVIDENCE}`)).toHaveTextContent("sha256:eeeeeeeeeeee");
    expect(screen.getByLabelText(`Policy digest ${POLICY}`)).toHaveTextContent("sha256:5e7b");
    // Requested and expiry times are absolute (UTC), and the expiry has a countdown.
    expect(screen.getByText("2026-11-01 14:30 UTC").tagName).toBe("TIME");
    expect(screen.getByTestId("approval-countdown")).toHaveTextContent("expires in 41 min");
    expect(screen.getByTestId("approval-quorum")).toHaveTextContent("1 of 2 approved");
    expect(screen.getByText("approver_b")).toBeInTheDocument();
  });

  it("shows the argument names and kinds, never a value", () => {
    const rogue = { ...PROPOSAL, args: { api_key: SECRET_VALUE }, args_values: [SECRET_VALUE] };
    renderPanel({ proposal: rogue as typeof PROPOSAL });

    const args = screen.getByTestId("approval-args");
    expect(within(args).getByText(/document_type/)).toBeInTheDocument();
    expect(within(args).getByText("(string)")).toBeInTheDocument();
    expect(within(args).getByText("(unnamed field)")).toBeInTheDocument();
    expect(screen.getByText(/Values are not shown here; the intent digest pins them/)).toBeInTheDocument();
    expect(screen.getByText(/3 fields\./)).toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(SECRET_VALUE);
  });

  it("says when the argument list was shortened and when the proposal has no arguments", () => {
    const { unmount } = renderPanel({
      proposal: { ...PROPOSAL, args_field_count: 80, args_truncated: true },
    });
    expect(screen.getByText(/80 fields, list shortened/)).toBeInTheDocument();
    unmount();
    renderPanel({ proposal: { ...PROPOSAL, args_fields: [], args_field_count: 0 } });
    expect(screen.getByText("No arguments.")).toBeInTheDocument();
  });

  it("says the request is not versioned, and shows who is hidden from the effect", () => {
    const base = review();
    const { unmount } = renderPanel({
      review: { ...base, binding: { ...base.binding!, expected_resource_version: null } },
    });
    expect(screen.getByText("Not versioned")).toBeInTheDocument();
    unmount();
    renderPanel({ proposal: null });
    expect(
      screen.getByRole("heading", { name: "Approval request for node human_gate" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Hidden: deciding this request needs an approving role/)).toBeInTheDocument();
    expect(screen.queryByTestId("approval-args")).not.toBeInTheDocument();
  });

  it("copies the proposal id, and survives a refused clipboard", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const writeText = vi.fn().mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error("denied"));
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderPanel();
    const copy = screen.getByRole("button", { name: "Copy proposal id" });
    await user.click(copy);
    expect(writeText).toHaveBeenCalledWith("EP-1");
    expect(await screen.findByText("Copied")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(2_000);
    });
    expect(screen.getByRole("button", { name: "Copy proposal id" })).toHaveTextContent("Copy");
    await user.click(screen.getByRole("button", { name: "Copy proposal id" }));
    expect(writeText).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Copy proposal id" })).toHaveTextContent("Copy");
  });

  it("shows when it was requested but not by whom when the server withholds the requester", () => {
    const base = review();
    renderPanel({
      review: { ...base, binding: { ...base.binding!, requested_by: null } },
    });
    const text = screen.getByTestId("approval-summary").textContent ?? "";
    expect(text).toContain("Requested2026-11-01 12:00 UTC");
    expect(text).not.toContain("editor_a");
  });

  it("renders a legacy node-level request without the exact-intent rows", () => {
    renderPanel({ proposal: null, review: review({ binding: null }) });
    expect(screen.queryByLabelText(/Intent digest/)).not.toBeInTheDocument();
    expect(screen.queryByText("Expires at")).not.toBeInTheDocument();
    expect(screen.getByTestId("approval-quorum")).toBeInTheDocument();
  });

  it("marks the caller among the approvers and names who decided a decided request", () => {
    renderPanel({
      approval: { ...APPROVAL, status: "approved", decided_by: "approver_b" },
      review: review({
        quorum: { approvers: ["approver_b", "approver_a"], required: 2, remaining: 0 },
        state: { code: "already_decided", superseded_by: null },
      }),
    });
    expect(screen.getByText("approver_a (you)")).toBeInTheDocument();
    expect(screen.getByText("approved by approver_b")).toBeInTheDocument();
  });

  it("refreshes the countdown once a minute and keeps it out of the live regions", () => {
    renderPanel();
    const countdown = screen.getByTestId("approval-countdown");
    expect(countdown).toHaveTextContent("expires in 41 min");
    expect(countdown.closest("[aria-live], [role='alert'], [role='status']")).toBeNull();
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByTestId("approval-countdown")).toHaveTextContent("expires in 40 min");
    act(() => {
      vi.advanceTimersByTime(40 * 60_000);
    });
    expect(screen.getByTestId("approval-countdown")).toHaveTextContent("expires in less than a minute");
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByTestId("approval-countdown")).toHaveTextContent("expired");
  });
});

describe("ApprovalDecisionPanel: decisions", () => {
  it("offers exactly the responses the server lists, in the contract's order", () => {
    renderPanel({ review: review({ allowed_responses: ["approve", "request_changes", "reject"] }) });
    const actions = within(screen.getByTestId("approval-actions"));
    expect(actions.getAllByRole("button").map((button) => button.textContent)).toEqual([
      "Approve",
      "Request changes",
      "Reject",
    ]);
  });

  it("does not render a response the server does not list", () => {
    renderPanel();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request changes" })).not.toBeInTheDocument();
  });

  it("approves without a reason and sends no reason", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { onDecide } = renderPanel();
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecide).toHaveBeenCalledExactlyOnceWith("approve", undefined);
  });

  it("sends the trimmed reason with an approval", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { onDecide } = renderPanel();
    await user.type(screen.getByLabelText("Reason"), "  checked the evidence  ");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecide).toHaveBeenCalledExactlyOnceWith("approve", "checked the evidence");
  });

  it("calls onDecide with request_changes and its reason when the server lists it", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { onDecide } = renderPanel({
      review: review({ allowed_responses: ["approve", "request_changes", "reject"] }),
    });
    await user.click(screen.getByRole("button", { name: "Request changes" }));
    expect(onDecide).not.toHaveBeenCalled();
    expect(screen.getByTestId("approval-reason-error")).toHaveTextContent(
      "A reason is required to request changes. Type one in the reason box.",
    );
    await user.type(screen.getByLabelText("Reason"), "attach the pay statement");
    await user.click(screen.getByRole("button", { name: "Request changes" }));
    expect(onDecide).toHaveBeenCalledExactlyOnceWith("request_changes", "attach the pay statement");
  });

  it("requires a reason to reject, announces it before any call, and focuses the reason box", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { onDecide } = renderPanel();
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(onDecide).not.toHaveBeenCalled();
    const error = screen.getByTestId("approval-reason-error");
    expect(error).toHaveAttribute("role", "alert");
    expect(error).toHaveTextContent("A reason is required to reject.");
    const reason = screen.getByLabelText("Reason");
    expect(reason).toHaveFocus();
    expect(reason).toHaveAttribute("aria-invalid", "true");
    expect(reason.getAttribute("aria-describedby")).toContain(error.id);
    // Typing clears the message.
    await user.type(reason, "wrong case");
    expect(screen.getByTestId("approval-reason-error")).toBeEmptyDOMElement();
  });

  it("asks for confirmation inline before rejecting, with focus on the confirm button", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { onDecide } = renderPanel();
    await user.type(screen.getByLabelText("Reason"), "wrong case");
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(onDecide).not.toHaveBeenCalled();
    const confirm = screen.getByRole("button", { name: "Confirm reject" });
    expect(screen.getByText("Reject and fail this run?")).toBeInTheDocument();
    expect(confirm).toHaveFocus();
    await user.click(confirm);
    expect(onDecide).toHaveBeenCalledExactlyOnceWith("reject", "wrong case");
  });

  it("returns focus to Reject when the confirmation is cancelled", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { onDecide } = renderPanel();
    await user.type(screen.getByLabelText("Reason"), "wrong case");
    await user.click(screen.getByRole("button", { name: "Reject" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByTestId("approval-reject-confirm")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toHaveFocus();
    expect(onDecide).not.toHaveBeenCalled();
  });

  it("works from the keyboard alone, in visual order", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { onDecide } = renderPanel();
    // Heading holds focus on open; Tab reaches the focusable digests and copy button, then the reason, then Approve.
    expect(screen.getByTestId("approval-panel-heading")).toHaveFocus();
    const seen: string[] = [];
    for (let step = 0; step < 12; step += 1) {
      await user.tab();
      const active = document.activeElement as HTMLElement;
      seen.push(active.getAttribute("aria-label") || active.id || active.textContent || "");
      if (active.textContent === "Approve") break;
    }
    expect(seen[seen.length - 1]).toBe("Approve");
    expect(seen.some((item) => item.startsWith("Intent digest"))).toBe(true);
    expect(seen.indexOf("Approve")).toBeGreaterThan(seen.findIndex((item) => item.includes("-reason")));
    await user.keyboard("{Enter}");
    expect(onDecide).toHaveBeenCalledExactlyOnceWith("approve", undefined);
  });

  it("shows the in-flight decision as busy and makes the others aria-disabled, never natively disabled", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { onDecide } = renderPanel({ pending: "approve" });
    const approve = screen.getByRole("button", { name: "Approving…" });
    const reject = screen.getByRole("button", { name: "Reject" });
    expect(approve).toHaveAttribute("aria-busy", "true");
    expect(reject).toHaveAttribute("aria-disabled", "true");
    expect(reject).not.toBeDisabled();
    await user.click(reject);
    await user.click(approve);
    expect(onDecide).not.toHaveBeenCalled();
  });
});

describe("ApprovalDecisionPanel: authority is the server's signal", () => {
  it.each([
    ["self_approval", "You started this run, so you cannot approve it. A second person must decide."],
    [
      "duplicate_approver",
      "You already approved this step. It needs 2 different approvers; 1 more is needed.",
    ],
    [
      "insufficient_scope",
      "This step needs the caliber.approver role and your account does not hold it. Ask an administrator for the role, or ask someone who holds it to decide.",
    ],
    [
      "project_role_required",
      "Deciding an approval needs an approving role in this project (reviewer or owner) and you do not hold one. Ask a project owner.",
    ],
    [
      "queue_disabled",
      "Deciding approvals is switched off for this deployment: the workflow run queue is disabled. Ask an administrator to enable it, then reload.",
    ],
    [
      "approvals_disabled",
      "Runtime approvals are switched off for this deployment, so nothing can be decided here. Ask an administrator to enable them, then reload.",
    ],
    [
      "checkpointing_disabled",
      "Approving needs run checkpointing, which is switched off for this deployment. Rejecting is still possible. Ask an administrator to enable checkpointing to approve.",
    ],
  ])("disables Approve for %s, keeps it focusable and names the reason", async (code, message) => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { onDecide } = renderPanel({
      review: review({
        caller: {
          principal: "approver_a",
          approve: { allowed: false, reason_code: code },
          reject: { allowed: true, reason_code: null },
        },
      }),
    });
    const approve = screen.getByRole("button", { name: "Approve" });
    expect(approve).toHaveAttribute("aria-disabled", "true");
    expect(approve).not.toBeDisabled();
    const why = screen.getByTestId("approval-approve-why");
    expect(why).toHaveTextContent(`Approve is unavailable. ${message}`);
    expect(approve.getAttribute("aria-describedby")).toBe(why.id);
    approve.focus();
    expect(approve).toHaveFocus();
    await user.click(approve);
    await user.keyboard("{Enter}");
    expect(onDecide).not.toHaveBeenCalled();
    // Reject stays available: one reviewer refusing is a complete answer.
    expect(screen.getByRole("button", { name: "Reject" })).not.toHaveAttribute("aria-disabled");
  });

  it("does not decide authority itself: it honours an allowed signal for a request it would have blocked", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    // The requester is the caller and the policy says no self-approval, yet the server says allowed: the panel obeys.
    const base = review();
    const { onDecide } = renderPanel({
      review: {
        ...base,
        binding: { ...base.binding!, requested_by: "approver_a" },
        policy: { ...base.policy, allow_self_approval: false },
      },
    });
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecide).toHaveBeenCalledExactlyOnceWith("approve", undefined);
  });

  it("disables Reject for the caller the server says may not reject", () => {
    renderPanel({
      review: review({
        caller: {
          principal: "viewer_a",
          approve: { allowed: false, reason_code: "insufficient_scope" },
          reject: { allowed: false, reason_code: "insufficient_scope" },
        },
      }),
    });
    expect(screen.getByRole("button", { name: "Reject" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByTestId("approval-reject-why")).toBeInTheDocument();
  });
});

const EIGHT_DENIALS: Array<[string, string, "alert" | "status"]> = [
  [
    "stale_version",
    "The case changed after this request was made (it was at version 7 and is no longer at that version). A fresh approval request is needed.",
    "alert",
  ],
  [
    "expired",
    "This request expired at 2026-11-01 14:30 UTC. Nothing was done. Ask the requester to send it again.",
    "alert",
  ],
  ["superseded", "A newer request replaced this one. Open the newer request.", "alert"],
  ["self_approval", "You started this run, so you cannot approve it. A second person must decide.", "status"],
  [
    "duplicate_approver",
    "You already approved this step. It needs 2 different approvers; 1 more is needed.",
    "status",
  ],
  [
    "role_revoked",
    "You no longer hold a role in this project that can approve this, so this decision was not recorded.",
    "alert",
  ],
  [
    "fenced_owner",
    "The system that was running this workflow changed owner after you approved. Nothing was done yet; the platform retries under the new owner and your approval still stands unless it expires.",
    "status",
  ],
  [
    "intent_mismatch",
    "What you reviewed no longer matches what would run (reviewed an earlier version, now a different one). Nothing was approved.",
    "alert",
  ],
];

describe("ApprovalDecisionPanel: refusals", () => {
  it.each(EIGHT_DENIALS)(
    "renders the contract message for %s and announces it as %s",
    (code, message, kind) => {
      renderPanel({ outcome: { kind: "denied", code } });
      const region = screen.getByTestId(kind === "alert" ? "approval-alert" : "approval-status");
      expect(region).toHaveTextContent(message);
      expect(region).toHaveAttribute("role", kind);
      expect(region).toHaveFocus();
      const other = screen.getByTestId(kind === "alert" ? "approval-status" : "approval-alert");
      expect(other).toBeEmptyDOMElement();
    },
  );

  it.each([
    ["already_decided", /already holds a decision/],
    ["already_consumed", /already used to run the action/],
    ["approval_not_current", /not for the step the run is waiting at/],
    ["run_not_waiting", /no longer waiting for approval/],
    ["not_found", /no longer exists for this run/],
    ["forbidden", /The server refused this decision/],
    ["approval_busy", /being decided or dispatched right now. Nothing was decided. Wait a moment/],
  ])("renders the recovery text for %s", (code, pattern) => {
    renderPanel({ outcome: { kind: "denied", code } });
    expect(screen.getByTestId("approval-status")).toHaveTextContent(pattern);
  });

  it("falls back to a generic message for a code it has no text for, never to response text", () => {
    renderPanel({ outcome: { kind: "denied", code: "something_new" } });
    expect(screen.getByTestId("approval-status")).toHaveTextContent(
      "The server refused this decision. The panel was refreshed; nothing was recorded.",
    );
  });

  it.each<[ApprovalOutcome, RegExp]>([
    [{ kind: "approved", remaining: 1 }, /^Approved\. 1 more approval needed\.$/],
    [{ kind: "approved", remaining: 3 }, /^Approved\. 3 more approvals needed\.$/],
    [{ kind: "approved", remaining: null }, /quorum is met\. The run stays paused until someone with the operator role resumes it/],
    [{ kind: "rejected" }, /^Rejected\. The run was failed\.$/],
    [{ kind: "invalid", message: "reason: too long" }, /not accepted \(reason: too long\)\. Nothing was recorded/],
    [{ kind: "unknown" }, /could not confirm whether your decision was recorded/],
    [{ kind: "failed", status: 418 }, /failed \(HTTP 418\)/],
  ])("announces the outcome %o politely and focuses the message", (outcome, pattern) => {
    renderPanel({ outcome });
    const status = screen.getByTestId("approval-status");
    expect(status).toHaveTextContent(pattern);
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveFocus();
  });

  it("clears the reason after an accepted decision but keeps it after a refusal", () => {
    const { rerender, props } = renderPanel();
    const reason = screen.getByLabelText("Reason") as HTMLTextAreaElement;
    // Seed a reason through the DOM value and an input event.
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      setter.call(reason, "because");
      reason.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(reason.value).toBe("because");
    rerender(<ApprovalDecisionPanel {...props} outcome={{ kind: "denied", code: "approval_busy" }} />);
    expect((screen.getByLabelText("Reason") as HTMLTextAreaElement).value).toBe("because");
    rerender(<ApprovalDecisionPanel {...props} outcome={{ kind: "rejected" }} />);
    expect((screen.getByLabelText("Reason") as HTMLTextAreaElement).value).toBe("");
  });
});

describe("ApprovalDecisionPanel: a request that cannot be decided is read-only", () => {
  it.each([
    ["expired", "alert", /expired at 2026-11-01 14:30 UTC\. Nothing was done/],
    ["superseded", "alert", /A newer request replaced this one/],
    ["already_decided", "status", /already holds a decision/],
    ["rejected", "status", /was rejected/],
    ["already_consumed", "status", /already used to run the action/],
    ["approval_not_current", "status", /not for the step the run is waiting at/],
    ["run_not_waiting", "status", /no longer waiting for approval/],
  ] as const)("shows %s with its message and no decision controls", (code, kind, pattern) => {
    renderPanel({ review: review({ state: { code, superseded_by: null } }) });
    expect(screen.getByTestId(kind === "alert" ? "approval-alert" : "approval-status")).toHaveTextContent(
      pattern,
    );
    expect(screen.queryByTestId("approval-actions")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Reason")).not.toBeInTheDocument();
  });

  it("links a superseded request to its replacement when the replacement is on the page", () => {
    renderPanel({
      review: review({ state: { code: "superseded", superseded_by: "RAP-2" } }),
      newerRequestHref: "#approval-panel-RAP-2",
    });
    expect(screen.getByTestId("approval-alert")).toHaveTextContent("A newer request replaced this one (RAP-2).");
    expect(screen.getByRole("link", { name: "Go to the newer request" })).toHaveAttribute(
      "href",
      "#approval-panel-RAP-2",
    );
  });

  it("names the newer request without a link when it is not on the page", () => {
    renderPanel({ review: review({ state: { code: "superseded", superseded_by: "RAP-2" } }) });
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByTestId("approval-alert")).toHaveTextContent("(RAP-2)");
  });
});

describe("ApprovalDecisionPanel: focus on open", () => {
  it("moves focus to the heading when nothing else holds focus", () => {
    renderPanel();
    expect(screen.getByTestId("approval-panel-heading")).toHaveFocus();
    expect(screen.getByTestId("approval-panel-heading")).toHaveAttribute("tabindex", "-1");
  });

  it("does not take focus from a control the user is already using", () => {
    const outside = document.createElement("input");
    document.body.appendChild(outside);
    outside.focus();
    renderPanel();
    expect(outside).toHaveFocus();
    outside.remove();
  });
});
