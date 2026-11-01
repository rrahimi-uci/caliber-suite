import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { WorkflowRun, WorkflowRuntimeApproval } from "@/api/workflowTypes";
import { WorkflowRunRecoveryPanel } from "@/components/workflows/WorkflowRunRecoveryPanel";
import { server } from "@/test/server";
import { render, screen, userEvent, waitFor, within } from "@/test/utils";

import { APPROVAL, view } from "./approvalFixtures";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const RUN = {
  workflow_run_id: "WR-1",
  workflow_id: "WF-1",
  status: "waiting_approval",
  current_node_id: "human_gate",
  created_at: "2026-11-01T12:00:00",
  summary: {},
} as unknown as WorkflowRun;

const LEGACY: WorkflowRuntimeApproval = {
  ...APPROVAL,
  runtime_approval_id: "RA-LEGACY",
  proposal_id: null,
  expires_at: null,
};

describe("the recovery panel's approvals", () => {
  it("opens the exact-intent panel for a live bound request and keeps the compact card for the rest", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/approvals/RAP-1`, () =>
        HttpResponse.json({ data: view() }),
      ),
    );
    const history: WorkflowRuntimeApproval = {
      ...APPROVAL,
      runtime_approval_id: "RAP-OLD",
      status: "approved",
      superseded_by: "RAP-1",
    };
    render(<WorkflowRunRecoveryPanel run={RUN} approvals={[APPROVAL, LEGACY, history]} />);

    const live = screen.getByTestId("workflow-run-recovery-approval-RAP-1");
    expect(await within(live).findByTestId("approval-decision-panel")).toBeInTheDocument();
    // A legacy node-level request and a superseded one stay compact cards: no panel, no extra view fetch.
    const legacy = screen.getByTestId("workflow-run-recovery-approval-RA-LEGACY");
    expect(within(legacy).queryByTestId("approval-decision-panel")).not.toBeInTheDocument();
    expect(within(legacy).getByText("RA-LEGACY")).toBeInTheDocument();
    const old = screen.getByTestId("workflow-run-recovery-approval-RAP-OLD");
    expect(within(old).queryByTestId("approval-decision-panel")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("approval-decision-panel")).toHaveLength(1);
  });

  it("keeps the panel, and the refusal it announced, when the list then marks the request superseded", async () => {
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/approvals/RAP-1`, () =>
        HttpResponse.json({ data: view({ state: { code: "superseded", superseded_by: "RAP-2" } }) }),
      ),
    );
    const { rerender } = render(<WorkflowRunRecoveryPanel run={RUN} approvals={[APPROVAL]} />);
    expect(await screen.findByTestId("approval-decision-panel")).toBeInTheDocument();

    // The next poll of the list says the request was replaced: it would no longer be live, but its panel stays.
    rerender(
      <WorkflowRunRecoveryPanel
        run={RUN}
        approvals={[{ ...APPROVAL, status: "approved", superseded_by: "RAP-2" }]}
      />,
    );
    expect(screen.getByTestId("approval-decision-panel")).toBeInTheDocument();
    expect(await screen.findByText(/A newer request replaced this one \(RAP-2\)/)).toBeInTheDocument();

    // A different run starts again from the list's own idea of which requests are live.
    rerender(
      <WorkflowRunRecoveryPanel
        run={{ ...RUN, workflow_run_id: "WR-2" }}
        approvals={[{ ...APPROVAL, workflow_run_id: "WR-2", status: "approved", superseded_by: "RAP-2" }]}
      />,
    );
    expect(screen.queryByTestId("approval-decision-panel")).not.toBeInTheDocument();
  });

  it("hands an accepted decision to the page so it can refresh the run", async () => {
    const user = userEvent.setup();
    const onApprovalDecided = vi.fn();
    server.use(
      http.get(`${API_BASE}/workflow-runs/WR-1/approvals/RAP-1`, () =>
        HttpResponse.json({ data: view() }),
      ),
      http.post(`${API_BASE}/workflow-runs/WR-1/approval/approve`, () =>
        HttpResponse.json({
          data: {
            workflow_run_id: "WR-1",
            runtime_approval_id: "RAP-1",
            status: "pending",
            approvals: 1,
            required_approvals: 2,
            remaining_approvals: 1,
          },
        }),
      ),
    );
    render(
      <WorkflowRunRecoveryPanel
        run={RUN}
        approvals={[APPROVAL]}
        onApprovalDecided={onApprovalDecided}
      />,
    );
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    await waitFor(() => expect(onApprovalDecided).toHaveBeenCalledTimes(1));
    expect(onApprovalDecided.mock.calls[0]![0]).toMatchObject({ remaining_approvals: 1 });
    expect(onApprovalDecided.mock.calls[0]![1]).toBe("approve");
  });
});
