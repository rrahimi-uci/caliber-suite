/** Fixtures for the approval panel tests: the server's view of one bound approval request (B06.5, #743). */
import type {
  EffectProposal,
  WorkflowApprovalView,
  WorkflowRuntimeApproval,
} from "@/api/workflowTypes";

export const INTENT = `sha256:${"3f9a1c0b77d2".padEnd(64, "a")}`;
export const EVIDENCE = `sha256:${"e".repeat(64)}`;
export const POLICY = `sha256:${"5e7b".padEnd(64, "c")}`;
// A secret-looking literal that must never reach the screen. It is split so that the source holds no key-shaped token.
export const SECRET_VALUE = ["sk", "live", "SECRET", "VALUE", "0123456789"].join("-");

export const PROPOSAL: EffectProposal = {
  proposal_id: "EP-1",
  connector: "document-request",
  connector_version: "1.0.0",
  effect_class: "irreversible_write",
  resource: "cases/MORT-2026-000184/document-requests",
  state: "proposed",
  args_fields: [
    { name: "document_type", kind: "string" },
    { name: "customer", kind: "object" },
    { name: null, kind: "number" },
  ],
  args_field_count: 3,
  args_truncated: false,
};

export const APPROVAL: WorkflowRuntimeApproval = {
  runtime_approval_id: "RAP-1",
  workflow_run_id: "WR-1",
  project_id: "PRJ-1",
  node_id: "human_gate",
  status: "pending",
  requested_at: "2026-11-01T12:00:00",
  decided_at: null,
  decided_by: null,
  decision_reason: null,
  policy_snapshot: null,
  proposal_id: "EP-1",
  expires_at: "2026-11-01T14:30:00",
  superseded_by: null,
  consumed_at: null,
};

export function view(overrides: Partial<WorkflowApprovalView> = {}): WorkflowApprovalView {
  return {
    approval: {
      runtime_approval_id: "RAP-1",
      workflow_run_id: "WR-1",
      project_id: "PRJ-1",
      node_id: "human_gate",
      status: "pending",
      requested_at: "2026-11-01T12:00:00",
      decided_at: null,
      decided_by: null,
      decision_reason: null,
    },
    binding: {
      proposal_id: "EP-1",
      intent_hash: INTENT,
      evidence_hash: EVIDENCE,
      policy_digest: POLICY,
      expected_resource_version: "7",
      expires_at: "2026-11-01T14:30:00",
      requested_by: "editor_a",
      superseded_by: null,
      consumed_at: null,
    },
    proposal: PROPOSAL,
    policy: {
      required_role: "caliber.approver",
      approval_count: 2,
      timeout_behavior: "block",
      allow_self_approval: false,
    },
    quorum: { approvers: ["approver_b"], required: 2, remaining: 1 },
    state: { code: "open", superseded_by: null },
    allowed_responses: ["approve", "reject"],
    caller: {
      principal: "approver_a",
      approve: { allowed: true, reason_code: null },
      reject: { allowed: true, reason_code: null },
    },
    ...overrides,
  };
}

/** The `review` part of a view (everything but the approval row and the proposal), as the panel takes it. */
export function review(overrides: Partial<WorkflowApprovalView> = {}) {
  const { approval: _approval, proposal: _proposal, ...rest } = view(overrides);
  void _approval;
  void _proposal;
  return rest;
}
