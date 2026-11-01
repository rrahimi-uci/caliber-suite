import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { WorkflowApprovalView } from "@/api/workflowTypes";
import { BoundApprovalReview } from "@/components/workflows/BoundApprovalReview";
import { server } from "@/test/server";
import { render, screen, userEvent, waitFor } from "@/test/utils";

import { APPROVAL, view } from "./approvalFixtures";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const RUN = `${API_BASE}/workflow-runs/WR-1`;

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

interface Calls {
  views: number;
  posts: Array<{ path: string; body: Record<string, unknown> }>;
}

/** Serve a sequence of views (the last one repeats) and record every decision call. */
function serve(
  views: WorkflowApprovalView[],
  decisions: { approve?: () => Response; reject?: () => Response } = {},
): Calls {
  const calls: Calls = { views: 0, posts: [] };
  server.use(
    http.get(`${RUN}/approvals/RAP-1`, () => {
      const index = Math.min(calls.views, views.length - 1);
      calls.views += 1;
      return HttpResponse.json({ data: views[index] });
    }),
    http.post(`${RUN}/approval/:decision`, async ({ request, params }) => {
      calls.posts.push({
        path: String(params["decision"]),
        body: (await request.json()) as Record<string, unknown>,
      });
      const handler = decisions[params["decision"] as "approve" | "reject"];
      return handler ? handler() : HttpResponse.json({ detail: "unexpected" }, { status: 500 });
    }),
  );
  return calls;
}

const ack = () =>
  HttpResponse.json({
    data: {
      workflow_run_id: "WR-1",
      runtime_approval_id: "RAP-1",
      status: "pending",
      approvals: 1,
      required_approvals: 2,
      remaining_approvals: 1,
    },
  });

const run = (status: string) =>
  HttpResponse.json({
    data: { workflow_run_id: "WR-1", workflow_id: "WF-1", status, current_node_id: "human_gate" },
  });

const refusal = (status: number, reason_code?: string) =>
  HttpResponse.json(
    { detail: "static detail", status_code: status, ...(reason_code ? { reason_code } : {}) },
    { status },
  );

describe("BoundApprovalReview", () => {
  it("loads the server's view and shows what is being decided", async () => {
    serve([view()]);
    render(<BoundApprovalReview approval={APPROVAL} />);
    expect(screen.getByTestId("approval-view-loading")).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", {
        name: "Approval request for cases/MORT-2026-000184/document-requests",
      }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("approval-quorum")).toHaveTextContent("1 of 2 approved");
  });

  it("sends the request id and the reason and nothing else, then shows the quorum progress", async () => {
    const user = userEvent.setup();
    const onDecided = vi.fn();
    const calls = serve([view(), view()], { approve: ack });
    render(<BoundApprovalReview approval={APPROVAL} onDecided={onDecided} />);
    await screen.findByTestId("approval-decision-panel");

    await user.type(screen.getByLabelText("Reason"), "looks right");
    await user.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(onDecided).toHaveBeenCalledTimes(1));
    expect(calls.posts).toEqual([
      { path: "approve", body: { runtime_approval_id: "RAP-1", reason: "looks right" } },
    ]);
    // Who decides is the authenticated principal: no principal, role or intent field is ever sent.
    expect(Object.keys(calls.posts[0]!.body).sort()).toEqual(["reason", "runtime_approval_id"]);
    expect(onDecided.mock.calls[0]![1]).toBe("approve");
    expect(await screen.findByText("Approved. 1 more approval needed.")).toBeInTheDocument();
    expect(screen.getByTestId("approval-status")).toHaveFocus();
    await waitFor(() => expect(calls.views).toBeGreaterThanOrEqual(2));
  });

  it("reports a completed quorum from the run the server returns", async () => {
    const user = userEvent.setup();
    const onDecided = vi.fn();
    serve([view()], { approve: () => run("waiting_approval") });
    render(<BoundApprovalReview approval={APPROVAL} onDecided={onDecided} />);
    await screen.findByTestId("approval-decision-panel");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/quorum is met\. The run stays paused/)).toBeInTheDocument();
    expect(onDecided).toHaveBeenCalledWith(expect.objectContaining({ status: "waiting_approval" }), "approve");
  });

  it("rejects only after the reason and the confirmation", async () => {
    const user = userEvent.setup();
    const calls = serve([view()], { reject: () => run("failed") });
    render(<BoundApprovalReview approval={APPROVAL} />);
    await screen.findByTestId("approval-decision-panel");

    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(calls.posts).toEqual([]);
    await user.type(screen.getByLabelText("Reason"), "wrong case");
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(calls.posts).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Confirm reject" }));

    expect(await screen.findByText("Rejected. The run was failed.")).toBeInTheDocument();
    expect(calls.posts).toEqual([
      { path: "reject", body: { runtime_approval_id: "RAP-1", reason: "wrong case" } },
    ]);
  });

  it("shows a superseded refusal assertively, focuses it and refetches the view", async () => {
    const user = userEvent.setup();
    const onDecided = vi.fn();
    const superseded = view({ state: { code: "superseded", superseded_by: "RAP-2" } });
    const calls = serve([view(), superseded], { approve: () => refusal(409, "superseded") });
    render(<BoundApprovalReview approval={APPROVAL} knownApprovalIds={["RAP-1", "RAP-2"]} onDecided={onDecided} />);
    await screen.findByTestId("approval-decision-panel");

    await user.click(screen.getByRole("button", { name: "Approve" }));

    const alert = await screen.findByText(/A newer request replaced this one \(RAP-2\)/);
    expect(alert.closest("[role='alert']")).toHaveFocus();
    expect(onDecided).not.toHaveBeenCalled();
    // The refreshed view is read-only and links the newer request that is on the page.
    expect(await screen.findByRole("link", { name: "Go to the newer request" })).toHaveAttribute(
      "href",
      "#approval-panel-RAP-2",
    );
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(calls.views).toBeGreaterThanOrEqual(2);
  });

  it.each([
    ["expired", /expired at 2026-11-01 14:30 UTC/, "alert"],
    ["already_decided", /already holds a decision/, "status"],
    ["already_consumed", /already used to run the action/, "status"],
    ["approval_not_current", /not for the step the run is waiting at/, "status"],
  ])("shows the 409 %s refusal and keeps nothing decided", async (code, pattern, region) => {
    const user = userEvent.setup();
    const calls = serve([view()], { approve: () => refusal(409, code) });
    render(<BoundApprovalReview approval={APPROVAL} />);
    await screen.findByTestId("approval-decision-panel");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() =>
      expect(screen.getByTestId(`approval-${region}`)).toHaveTextContent(pattern),
    );
    expect(calls.posts).toHaveLength(1);
  });

  it("tells the approver a 503 is a retry hint, keeps the buttons live and retries only on the next press", async () => {
    const user = userEvent.setup();
    let busy = true;
    const calls = serve([view()], {
      approve: () => (busy ? refusal(503, "approval_busy") : ack()),
    });
    render(<BoundApprovalReview approval={APPROVAL} />);
    await screen.findByTestId("approval-decision-panel");

    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(
      await screen.findByText(/being decided or dispatched right now\. Nothing was decided\. Wait a moment/),
    ).toBeInTheDocument();
    expect(calls.posts).toHaveLength(1);
    const approve = screen.getByRole("button", { name: "Approve" });
    expect(approve).not.toHaveAttribute("aria-disabled");

    busy = false;
    await user.click(approve);
    expect(await screen.findByText("Approved. 1 more approval needed.")).toBeInTheDocument();
    expect(calls.posts).toHaveLength(2);
  });

  it("does not retry after a server error: the outcome is unknown, so it refetches the view", async () => {
    const user = userEvent.setup();
    const recorded = view({
      state: { code: "already_decided", superseded_by: null },
      quorum: { approvers: ["approver_b", "approver_a"], required: 2, remaining: 0 },
    });
    const calls = serve([view(), recorded], { approve: () => HttpResponse.json({ detail: "boom" }, { status: 500 }) });
    render(<BoundApprovalReview approval={APPROVAL} />);
    await screen.findByTestId("approval-decision-panel");

    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(await screen.findByText(/could not confirm whether your decision was recorded/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("approval-quorum")).toHaveTextContent("2 of 2 approved"));
    expect(calls.posts).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("shows why a refused decision was refused by refetching the signals", async () => {
    const user = userEvent.setup();
    const blocked = view({
      caller: {
        principal: "approver_a",
        approve: { allowed: false, reason_code: "duplicate_approver" },
        reject: { allowed: true, reason_code: null },
      },
    });
    serve([view(), blocked], { approve: () => refusal(403) });
    render(<BoundApprovalReview approval={APPROVAL} />);
    await screen.findByTestId("approval-decision-panel");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/The server refused this decision/)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Approve" })).toHaveAttribute("aria-disabled", "true"),
    );
    expect(screen.getByTestId("approval-approve-why")).toHaveTextContent("You already approved this step");
  });

  it("explains an approval that is not available to the caller or not loadable", async () => {
    server.use(http.get(`${RUN}/approvals/RAP-1`, () => refusal(404)));
    const { unmount } = render(<BoundApprovalReview approval={APPROVAL} />);
    expect(await screen.findByTestId("approval-view-error")).toHaveTextContent(
      "not available to you or no longer exists",
    );
    unmount();
    server.use(http.get(`${RUN}/approvals/RAP-1`, () => refusal(500)));
    render(<BoundApprovalReview approval={APPROVAL} />);
    expect(await screen.findByTestId("approval-view-error")).toHaveTextContent(
      "could not be loaded, so nothing can be decided here",
    );
  });

  it("refuses request_changes locally because no route records it", async () => {
    const user = userEvent.setup();
    const calls = serve([view({ allowed_responses: ["approve", "request_changes", "reject"] })]);
    render(<BoundApprovalReview approval={APPROVAL} />);
    await screen.findByTestId("approval-decision-panel");
    await user.type(screen.getByLabelText("Reason"), "attach the statement");
    await user.click(screen.getByRole("button", { name: "Request changes" }));
    expect(await screen.findByText(/failed \(HTTP 501\)/)).toBeInTheDocument();
    expect(calls.posts).toEqual([]);
  });
});
