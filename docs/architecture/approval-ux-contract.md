# Approval UX contract: exact-intent approvals

What the approver sees, which responses are allowed, and the message for every refusal. It is the contract for #523 task 5 (`caliber/caliber-ui/src/components/workflows/ApprovalDecisionPanel.tsx` and the API behind it); M06 (#548) extends it with timers and reassignment. The data is an `approval_request` record (`caliber/src/caliber/contracts/schemas/approval_request.v1.json`, ADR 0003); the example that validates against it is `caliber/tests/fixtures/contracts/approval_request/valid_ux_example.json`.

## What exists today

The platform has node-level approvals only: `WorkflowRunCheckpointPanel.tsx` lists a run's `WorkflowRuntimeApproval` rows (`runtime_approval_id`, `node_id`, `status` of `pending`, `approved` or `rejected`, `requested_at`, `decided_at`, `decided_by`, `decision_reason`, `policy_snapshot`) and has no proposal, no diff and no intent. The API is `GET /ajax-api/2.0/mlflow/caliber/workflow-runs/{run_id}/approvals`, `POST .../workflow-runs/{run_id}/approval/approve` and `POST .../approval/reject`, each taking an optional `{"runtime_approval_id", "reason"}`. Approve records one approver per call and answers `pending` with `remaining_approvals` until the node's quorum is met; reject fails the run at once with `error_code: approval_rejected` (one authorized refusal is complete, and quorum and the self-decision rule do not apply to it). There is no `request_changes`, no expiry and no version check. The contract below is the target; the paths it names are the target paths and #523 decides whether the old ones stay as aliases.

## Fields shown

The panel opens on one `approval_request` and the `proposal` it names. Every field comes from a schema field, so the panel shows nothing the record does not state.

| field | source (schema field) | display |
| --- | --- | --- |
| Proposal id and intent hash | `approval_request.proposal_id`, `approval_request.intent_hash` | The id in monospace with a copy button; the hash shortened to its first 12 characters with the full value on focus and in the accessible name. A hash that differs from the proposal's `intent_hash` is never shown as approvable (`intent_mismatch`). |
| Target connector and resource | `proposal.target.connector`, `proposal.target.resource` | Two labelled lines: "System" and "Resource". |
| Expected resource version | `approval_request.expected_resource_version` | "Valid for version 1"; "Not versioned" when `null`. The current downstream version is shown beside it once read, in warning colour when different. |
| Before and after | `proposal.args` against the current downstream state, read through the connector's query operation | A two-column diff: "Now at the system" and "After this action", changed rows marked with text ("changes"), not colour alone. When the current state cannot be read the panel says so and offers Reject and Request changes only. |
| Evidence | `approval_request.evidence_hash` | A link "Evidence (12 items)" to the evidence package whose digest is the hash, opened in a new tab; the hash is shown as text. |
| Policy | `approval_request.policy_digest`, `policy_decision.reason_codes` of the decision for this proposal | "Policy 3f9a1c… allowed this because: <reason codes>". Each reason code is a chip with a plain-language label. |
| Risk class | `approval_request.risk_class` | A badge ("Irreversible write"), text first. |
| Requested by | `approval_request.requested_by` | The principal and its type (person or agent). |
| Expires at | `approval_request.expires_at` | Absolute time and a countdown ("expires in 42 min"); after expiry the panel shows `expired`. |

## Allowed responses

`approval_request.allowed_responses` decides which buttons appear. A response not in the list is not rendered, and the API refuses it.

| response | API call | body | resulting run status |
| --- | --- | --- | --- |
| `approve` | `POST /workflow-runs/{run_id}/approvals/{approval_id}/approve` | `{"intent_hash": str, "reason": str}` | The run stays `waiting_approval` until the quorum is met (the answer carries `remaining_approvals`); when met, the approval is `approved` and the run resumes. The `intent_hash` is required and must equal the request's. |
| `reject` | `POST /workflow-runs/{run_id}/approvals/{approval_id}/reject` | `{"reason": str}` | The run becomes `failed` with `error_code: approval_rejected` (as today). A reason is required in the UI and optional in the API. |
| `request_changes` | `POST /workflow-runs/{run_id}/approvals/{approval_id}/decision` | `{"decision": "request_changes", "reason": str}` | The approval is closed as `superseded` and the run returns to the proposing step with the reason attached, so a new proposal and a new approval request follow. A reason is required. |

Each decision becomes an `approval_decision` record (`approval_request_id`, `decision`, `decided_by`, `decided_at`, `reason`). The button that was pressed shows a busy state; the other buttons are disabled while a call is in flight.

## Denial reasons

Every refusal has a stable code, an HTTP status, one message and one UI state. The message names the cause and the next step; it never says only "not allowed".

| code | HTTP | message | UI state |
| --- | --- | --- | --- |
| `stale_version` | 409 | "The case changed after this request was made (it was at version 1 and is now at version 2). Review the new state; a fresh approval request is needed." | Banner (error) above the diff, Approve disabled, the diff re-rendered against the current state, a "Ask for a new request" button. |
| `expired` | 409 | "This request expired at 14:30. Nothing was done. Ask the requester to send it again." | The panel is read-only; all response buttons are replaced by the message; focus stays on the message. |
| `superseded` | 409 | "A newer request replaced this one (request ar-0002). Open the newer request." | Read-only panel with a link to the newer request. |
| `self_approval` | 403 | "You started this run, so you cannot approve it. A second person must decide." | Approve disabled with the reason as its description; Reject and Request changes stay available. |
| `duplicate_approver` | 403 | "You already approved this step. This step needs 2 different approvers; 1 more is needed." | Approve disabled, the count "1 of 2 approved" shown, the earlier approval listed. |
| `role_revoked` | 403 | "You no longer hold the approver role in this project, so this decision was not recorded." | The panel becomes read-only for this user; a link to the project's access page. |
| `fenced_owner` | 409 | "The system that was running this workflow lost ownership while you were reviewing. Your decision was not applied; reload to see where the run stands." | Banner (warning), all buttons disabled, a "Reload" button that refetches the run. |
| `intent_mismatch` | 409 | "What you reviewed no longer matches what would run (intent 3f9a1c… against 77b0de…). Nothing was approved." | Banner (error), Approve disabled, the changed fields highlighted with text markers; Request changes stays available. |

The server decides; the UI never infers a denial. The response body for any of these is `{"detail": <message>, "status_code": <http>, "code": <code>}`, the API's existing error envelope plus `code`.

## Accessibility

- **Keyboard order.** Heading, request summary (proposal, target, version), risk class, diff, evidence and policy, then the response buttons in the order Approve, Request changes, Reject, then the reason field. Everything is reachable with Tab and operable with Enter or Space; Esc closes the panel only when no call is in flight.
- **Focus on open.** Focus moves to the panel heading (`tabIndex=-1`, so a screen reader announces "Approval request for case MORT-2026-000184"). On a denial, focus moves to the banner or message, not to a button.
- **Live region.** One `aria-live="polite"` region announces the outcome ("Approved. 1 more approval needed", "Rejected", or the denial message); an `expired` or `fenced_owner` message uses `role="alert"`.
- **Labels.** Each field is a `<dt>`/`<dd>` pair or a labelled control so a screen reader says "Expected resource version: 1" in one breath; the intent hash has an accessible name with the full value; the diff is a table with column headers "Now at the system" and "After this action" and a text "changes" marker in changed rows; risk class and denial states are conveyed in text as well as colour.
- **Reason field.** Required for Reject and Request changes: it is labelled, its requirement stated in text ("Required"), and a missing value is announced with the live region before the call is made.
- **Countdown.** The countdown updates at most once a minute and is not in the live region, so it does not interrupt.

## Wireframe

```
+--------------------------------------------------------------------------------+
| Approval request for case MORT-2026-000184                       [Reversible write]|
| Request ar-0001 · proposal pr-0001 · intent 3f9a1c0b77d2       Expires in 42 min |
| Requested by editor_a (person)                                                  |
+--------------------------------------------------------------------------------+
| System      document-request                                                    |
| Resource    cases/MORT-2026-000184/document-requests                           |
| Version     valid for 1   (now at 1)                                            |
+--------------------------------------------------------------------------------+
| Before and after                                                                |
|  field              | Now at the system | After this action     |               |
|  missing_documents  | pay_statement     | pay_statement         |               |
|  document_requests  | (none)            | pay_statement  changes |              |
+--------------------------------------------------------------------------------+
| Evidence  [Evidence (12 items)]        Policy 3f9a1c… allowed: within_grant     |
+--------------------------------------------------------------------------------+
| Reason (required to reject or request changes)                                  |
| [____________________________________________________________________]        |
|                                                                                 |
|  [ Approve ]   [ Request changes ]   [ Reject ]                                 |
+--------------------------------------------------------------------------------+
| (live region) Approved. 1 more approval needed.                                 |
+--------------------------------------------------------------------------------+
```

## What #523 builds from this

The panel and its states for the eight denial codes, the three response calls, the request and decision records through `caliber.contracts.validate_record`, and a UI test per denial code that asserts the message and the disabled or read-only state above. Out of scope here: the component and the API changes themselves.
