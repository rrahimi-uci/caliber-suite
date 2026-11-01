/**
 * Loads the server's view of one bound approval request and wires the approval panel to the real decision routes
 * (B06.5, #743).
 *
 * The view is the only source of what the panel shows and of what the caller may do. A decision sends the request id and
 * an optional reason to the decision route and nothing else: who decides is the authenticated principal. After any
 * answer, success or refusal, the view is fetched again so the panel shows what the server now says; a network failure or
 * a server error leaves the outcome unknown, so it is refetched and never retried here (a retry could answer
 * `duplicate_approver`). Nothing in a response is logged.
 */
import { useCallback, useState } from "react";
import { caliberApi } from "@/api/caliberApi";
import type {
  ApprovalDecision,
  WorkflowApprovalPartialAck,
  WorkflowRun,
  WorkflowRuntimeApproval,
} from "@/api/workflowTypes";
import { useApiMutation, useApiQuery, useInvalidate } from "@/hooks/useApiQuery";
import { classifyDecisionError, type ApprovalOutcome } from "@/lib/approvalPanel";
import { ApprovalDecisionPanel } from "./ApprovalDecisionPanel";

/** What a successful decision call returns: the run, or the acknowledgement while the quorum is still open. */
export type ApprovalDecisionResult = WorkflowRun | WorkflowApprovalPartialAck;

export function isPartialAck(result: ApprovalDecisionResult): result is WorkflowApprovalPartialAck {
  return "remaining_approvals" in result;
}

/** How often an open request is re-read, so an expiry or a decision by someone else shows up without a reload. */
const VIEW_REFRESH_MS = 30_000;

interface BoundApprovalReviewProps {
  approval: WorkflowRuntimeApproval;
  /** Ids of the other approvals on the page, to link a superseded request to its replacement. */
  knownApprovalIds?: readonly string[];
  /** Called after a decision that the server accepted, with its result, so the page can refresh the run. */
  onDecided?: (result: ApprovalDecisionResult, decision: ApprovalDecision) => void;
}

export function BoundApprovalReview({
  approval,
  knownApprovalIds = [],
  onDecided,
}: BoundApprovalReviewProps): JSX.Element {
  const invalidate = useInvalidate();
  const runId = approval.workflow_run_id;
  const approvalId = approval.runtime_approval_id;
  const viewKey = ["workflow-run-approval-view", runId, approvalId] as const;
  const view = useApiQuery(
    viewKey,
    (signal) => caliberApi.getWorkflowRunApprovalView(runId, approvalId, signal),
    {
      refetchInterval: (query) =>
        query.state.data?.state.code === "open" ? VIEW_REFRESH_MS : false,
    },
  );
  const [outcome, setOutcome] = useState<ApprovalOutcome | null>(null);
  const [pending, setPending] = useState<ApprovalDecision | null>(null);

  const refresh = useCallback((): void => {
    void invalidate(["workflow-run-approval-view", runId, approvalId]);
    void invalidate(["workflow-run-approvals", runId]);
    void invalidate(["workflow-run-events", runId]);
  }, [invalidate, runId, approvalId]);

  const decide = useApiMutation(
    (variables: { decision: "approve" | "reject"; reason?: string }) =>
      caliberApi.decideWorkflowRunApproval(runId, variables.decision, {
        runtime_approval_id: approvalId,
        reason: variables.reason,
      }),
    {
      onSuccess: (result, variables) => {
        setPending(null);
        if (variables.decision === "reject") {
          setOutcome({ kind: "rejected" });
        } else {
          setOutcome({
            kind: "approved",
            remaining: isPartialAck(result) ? result.remaining_approvals : null,
          });
        }
        refresh();
        onDecided?.(result, variables.decision);
      },
      onError: (error) => {
        setPending(null);
        setOutcome(classifyDecisionError(error));
        refresh();
      },
    },
  );

  if (view.isLoading) {
    return (
      <div role="status" data-testid="approval-view-loading" className="text-xs text-slate-500">
        Loading the approval request…
      </div>
    );
  }
  if (view.isError || !view.data) {
    const status = view.error?.status;
    return (
      <div
        role="alert"
        data-testid="approval-view-error"
        className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800 dark:border-red-800 dark:bg-red-950/30 dark:text-red-200"
      >
        {status === 404
          ? "This approval request is not available to you or no longer exists. Refresh the run."
          : "The approval request could not be loaded, so nothing can be decided here. Refresh to try again."}
      </div>
    );
  }

  const { approval: record, proposal, ...review } = view.data;
  const supersededBy = review.state.superseded_by;
  return (
    <ApprovalDecisionPanel
      approval={{ ...approval, ...record }}
      proposal={proposal}
      review={review}
      pending={pending}
      outcome={outcome}
      newerRequestHref={
        supersededBy && knownApprovalIds.includes(supersededBy)
          ? `#approval-panel-${supersededBy}`
          : null
      }
      onDecide={(decision, reason) => {
        if (decision === "request_changes") {
          // No route records it yet and the server never lists it; refuse here rather than invent a call.
          setOutcome({ kind: "failed", status: 501 });
          return;
        }
        setOutcome(null);
        setPending(decision);
        decide.mutate({ decision, reason });
      }}
    />
  );
}
