# Approval UX contract: exact-intent approvals

What the approver sees, which responses are allowed, when each refusal is detected, and the message for every refusal. It is the contract for #523 task 5 (`caliber/caliber-ui/src/components/workflows/ApprovalDecisionPanel.tsx` and the API behind it); M06 (#548) extends it with timers and reassignment. The stored data is an `approval_request` record (`caliber/src/caliber/contracts/schemas/approval_request.v1.json`, ADR 0003); an example that validates against it is `caliber/tests/fixtures/contracts/approval_request/valid_ux_example.json`.

Two kinds of thing appear below and the difference matters. A **record field** is stored in `approval_request`, `approval_decision`, `proposal` or `policy_decision`. A **view field** is computed by the server when the panel opens, from records plus the connector and account lookups, and is not stored. The panel reads one view (`GET /workflow-runs/{run_id}/approvals/{approval_id}`, described under "Panel data") and nothing else.

## What exists today

- **Data.** `WorkflowRuntimeApproval` (`caliber-ui/src/api/workflowTypes.ts`): `runtime_approval_id`, `workflow_run_id`, `node_id`, `status` (`pending`, `approved`, `rejected`), `requested_at`, `decided_at`, `decided_by`, `decision_reason`, `policy_snapshot`. No proposal, intent, version, expiry or evidence.
- **UI.** `WorkflowRunRecoveryPanel.tsx` lists a run's approvals; the Approve and Reject buttons are in `pages/WorkflowDetail.tsx` and send only `runtime_approval_id`. There is no reason field and no diff. `WorkflowRunCheckpointPanel.tsx` shows checkpoints and does not render approvals; #523 task 5 therefore adds the panel to the run page (the recovery panel's approvals section), not to the checkpoint panel.
- **API** (prefix `/ajax-api/2.0/mlflow/caliber`): `GET /workflow-runs/{run_id}/approvals`; `POST /workflow-runs/{run_id}/approval/approve` and `.../approval/reject`, each with an optional `{"runtime_approval_id", "reason"}` (the model forbids other keys). Approve needs the node's `required_role` scope plus the `approve` project action; it records one approver per call, answers `status: pending` with `remaining_approvals` until the node's quorum is met, and when it is met sets the approval `approved` **and leaves the run `waiting_approval`**: continuing is a separate `POST .../resume`, which needs `caliber.operator`. For an explicit proposal-bound id, the route records the decision and leaves `consumed_at` null; the dispatcher owns exact-intent revalidation and consumption. Reject needs only the required role, fails the run at once (`error_code: approval_rejected`), and quorum and the self-decision rule do not apply to it.
- **Errors.** Routes raise `HTTPException` with a string `detail`; the platform's own mechanism for a machine-readable cause is `CaliberHTTPException(reason_code=...)`, rendered by `routes/_errors.py` as `{"detail", "status_code", "reason_code"}`.
- **Absent.** `request_changes`, version checks at decision time, a diff, and the richer approval view (#523 tasks 1–4). Expiry and supersession are enforced by the decision route and dispatcher recheck; a bound decision is consumed exactly once by the dispatch claim transaction.

## Decisions this contract makes for #523

1. **Machine-readable cause: `reason_code`, one vocabulary.** Every refusal below is a `CaliberHTTPException` whose `reason_code` is the code in the tables. #523 task 2 names its two expiry errors `{"error": "approval_expired"}` and `approval_superseded` and task 4 `approval_already_consumed`; they are replaced by the `reason_code` values `expired`, `superseded` and `already_consumed`, in the envelope above, so a client sees one shape.
2. **Approving continues the run.** When the quorum is met the platform resumes the run itself; an approver does not also need `caliber.operator` to press Resume. (Today's separate resume stays for retries from checkpoints.)
3. **`request_changes` closes the approval as `changes_requested`** and leaves the run `waiting_approval` until the proposer submits a new proposal (which supersedes through #523 task 2) or the run is cancelled. It needs no new run transition.
4. **Paths.** The decision routes are the nested ones named under "Allowed responses"; the old `.../approval/approve` and `.../approval/reject` stay as aliases for one release. `approval_id` is the existing `runtime_approval_id`; the `approval_request` record's `approval_request_id` has the same value.

## Panel data

One view model, built by the server for `GET /workflow-runs/{run_id}/approvals/{approval_id}`:

```
{ "approval_request": <the record>,
  "proposal": <the proposal record>,
  "policy_decision": <the decision recorded for this proposal, or null>,
  "current_state": <the target as the downstream shows it now, or null>,
  "current_state_error": <why it could not be read, or null>,
  "preview": {"before": {...}, "after": {...}} | null,
  "requested_by_type": "human" | "agent",
  "evidence_url": <link to the evidence package, or null>,
  "approvals": [<approver, time>...], "required_approvals": 2 }
```

`current_state` is read through the connector's query operation. `preview` is computed by the connector adapter from the proposal and `current_state` (what the target will look like after the effect); the adapter owns its semantics, because `proposal.args` is the call's arguments, not a resulting state. `requested_by_type` comes from the account's `principal_type` (ADR 0004). `evidence_url` is present once the evidence package exists (#538).

## Fields shown

| field | source | display |
| --- | --- | --- |
| Case | `proposal.target.resource` | The heading, from the resource name ("Approval request for cases/MORT-2026-000184/document-requests"). |
| Proposal id and intent hash | `approval_request.proposal_id`, `approval_request.intent_hash` | The id in monospace with a copy button; the hash as `sha256:` plus its first 12 hex characters, the full value on focus and in the accessible name. |
| Target connector and resource | `proposal.target.connector`, `proposal.target.resource` | Two labelled lines: "System" and "Resource". |
| Expected resource version | `approval_request.expected_resource_version` | "Valid for version 1"; "Not versioned" when `null`. The version in `current_state`, when read, is shown beside it, in warning colour and text when different. |
| Before and after | view `preview`, from `current_state` | A two-column table, "Now at the system" and "After this action"; changed rows are marked with the text "changes". When `current_state` is unreadable the panel says so (`current_state_error`) and offers Reject and Request changes only. |
| Evidence | `approval_request.evidence_hash`, view `evidence_url` | A link "Evidence (opens in a new tab)" when the URL exists; the hash as text. |
| Policy | `approval_request.policy_digest`, `policy_decision.reason_codes` | "Policy `sha256:3f9a…`: allowed because <reason codes>"; each reason code a chip with a plain-language label. |
| Risk class | `approval_request.risk_class` | A badge, text first ("Reversible write"). |
| Requested by | `approval_request.requested_by`, view `requested_by_type` | The principal and its type (person or agent). |
| Expires at | `approval_request.expires_at` | Absolute time with its offset and a countdown ("expires in 42 min"). |
| Approvals so far | view `approvals`, `required_approvals` | "1 of 2 approved", with the earlier approvers listed. |

## Allowed responses

`approval_request.allowed_responses` decides which buttons appear. A response not in the list is not rendered, and the API refuses it. Paths are under `/ajax-api/2.0/mlflow/caliber`.

| response | API call | body | resulting state |
| --- | --- | --- | --- |
| `approve` | `POST /workflow-runs/{run_id}/approvals/{approval_id}/approve` | `{"intent_hash": str, "reason": str \| null}` (`reason` optional) | Until the quorum is met the answer is 200 with `status: pending` and `remaining_approvals`; the run stays `waiting_approval`. When met, the approval is `approved` and the platform resumes the run. `intent_hash` must equal the request's. |
| `reject` | `POST /workflow-runs/{run_id}/approvals/{approval_id}/reject` | `{"reason": str}` (required in the UI, optional in the API) | The run becomes `failed` with `error_code: approval_rejected`. Irreversible: the UI asks for confirmation first. |
| `request_changes` | `POST /workflow-runs/{run_id}/approvals/{approval_id}/decision` | `{"decision": "request_changes", "reason": str}` (required) | The approval is `changes_requested`; the run stays `waiting_approval` until a new proposal supersedes it or the run is cancelled. |

Each decision becomes an `approval_decision` record (`approval_request_id`, `decision`, `decided_by`, `decided_at`, `reason`). The pressed button shows a busy state with `aria-busy`; the others become `aria-disabled` (never natively `disabled`, which drops focus) while a call is in flight.

## Denial reasons

Each code has a `reason_code`, an HTTP status, the point at which the platform detects it, one message and one UI state. **Detected** matters: at *decision* time the refusal comes back as the answer to the decision call; at *commit* time the decision was already recorded, and the approver learns of it from the refreshed view and a run event, not from the call. Messages use `{placeholders}` for values the server fills in.

| code | HTTP | detected | message | UI state |
| --- | --- | --- | --- | --- |
| `stale_version` | 409 | commit (the dispatcher compares the target's version, #523 task 3); at decision when the view's `current_state` already differs | "The case changed after this request was made (it was at version {expected} and is now at version {current}). A fresh approval request is needed." | Banner above the diff, the diff re-rendered against the current state, Approve `aria-disabled`, a "Ask for a new request" button. |
| `expired` | 409 | decision and commit | "This request expired at {expires_at}. Nothing was done. Ask the requester to send it again." | Read-only panel; the response buttons are replaced by the message. |
| `superseded` | 409 | decision and commit | "A newer request replaced this one{newer_request}. Open the newer request." (`{newer_request}` is " ({id})" once `superseded_by` exists) | Read-only panel with a link to the newer request when it exists. |
| `self_approval` | 403 | decision | "You started this run, so you cannot approve it. A second person must decide." | Approve `aria-disabled` with the reason as its description (`aria-describedby`); Reject and Request changes stay available. |
| `duplicate_approver` | 403 | decision | "You already approved this step. It needs {required} different approvers; {remaining} more is needed." | Approve `aria-disabled`, "{approved} of {required} approved" shown with the earlier approvers. |
| `role_revoked` | 403 (404 when the project is no longer visible to the user) | decision; also commit, when the approver lost the role between the decision and dispatch | "You no longer hold a role in this project that can approve this, so this decision {was_not_recorded}." | Read-only for this user, with a link to the project's access page. At commit the text says "was withdrawn" and the run event names it. |
| `fenced_owner` | 409 | **commit only**: the dispatcher or resume path finds the execution owner changed (ADR 0002, #536); never an answer to the decision call | "The system that was running this workflow changed owner after you approved. Nothing was done yet; the platform retries under the new owner and your approval still stands unless it expires." | Informational banner on the refreshed view (`role="status"`), no action needed, no re-approval offered. |
| `intent_mismatch` | 409 | decision (the sent `intent_hash` is not the request's, i.e. the panel is stale) and commit (the proposal changed after the request was made) | "What you reviewed no longer matches what would run (reviewed {reviewed}, now {current}). Nothing was approved." | Banner, Approve `aria-disabled`; the panel refetches and shows what changed by diffing the `args` it displayed before against the refetched ones (the record keeps only the hash); Request changes stays available. |

Other outcomes the panel must handle, so none is a surprise: `insufficient_scope` (403, "This step needs the {role} role and you hold {held}.", today's `record_approval` message; the panel is read-only for this user and the message is not `role_revoked`, which is about a role the user held), `run_not_waiting` (409, the run was cancelled or finished meanwhile; read-only panel), `already_decided` (409, the approval is no longer pending; shows who decided), `already_consumed` (409, #523 task 4, concurrent consumer), `approvals_disabled` (409, runtime approvals are off; the panel is not shown), `not_found` (404), a validation error (400; the field is named), an unauthenticated call (401, the session is renewed), and a **network failure or 5xx**: the outcome is unknown, so the panel refetches the view and shows the real state; it never retries the decision itself (a retry would answer `duplicate_approver`). A partial quorum is not an error: 200 with `status: pending`, shown as "{approved} of {required} approved". An agent approver above its autonomy ceiling is not refused: the request falls back to a human approver (ADR 0004 invariant 3).

The server decides; the UI never infers a denial.

## Accessibility

- **The panel is inline** in the run page, not a dialog: no modal, no focus trap, no Esc handling.
- **Keyboard order = visual order.** Heading, request summary (proposal, target, version), risk class, before and after, evidence and policy, approvals so far, the reason field, then the buttons in the order Approve, Request changes, Reject. Everything is operable with Tab and Enter or Space.
- **Focus on open** moves to the panel heading (`tabIndex=-1`; a screen reader announces "Approval request for cases/MORT-2026-000184/document-requests"). After a decision, focus moves to the result message; after a denial, to the banner or message, not to a button. The result is not also sent to the live region (it would be announced twice): the message element is the live region.
- **Live region.** One `aria-live="polite"` region carries the outcome ("Approved. 1 more approval needed.", "Rejected."); a blocking denial (`stale_version`, `intent_mismatch`, `expired`, `superseded`, `role_revoked`) uses `role="alert"`; `fenced_owner` uses `role="status"`.
- **Labels.** Each field is a `<dt>`/`<dd>` pair or a labelled control, so a screen reader says "Expected resource version: 1" in one breath; the intent hash has an accessible name with the full value; the diff is a table with column headers and a text "changes" marker; risk class and denial states are conveyed in text as well as colour; the evidence link says it opens in a new tab.
- **Disabled actions** use `aria-disabled="true"` with `aria-describedby` pointing at the reason, so they stay focusable and the reason is read.
- **Reason field.** Labelled; "Required" stated in text for Reject and Request changes; a missing value is announced before any call is made.
- **Reject** asks for confirmation inline ("Reject and fail this run?") with focus on the confirm button.
- **Countdown** updates at most once a minute and is outside the live region.

## Wireframe

```
+--------------------------------------------------------------------------------+
| Approval request for cases/MORT-2026-000184/document-requests  [Reversible write]|
| Request ar-ux-0001 · proposal pr-ux-0001 · intent sha256:3f9a1c0b77d2            |
| Requested by editor_a (person)                      Expires 14:30Z (in 42 min)  |
+--------------------------------------------------------------------------------+
| [banner slot: denial or status message, role=alert / role=status]               |
+--------------------------------------------------------------------------------+
| System      document-request                                                    |
| Resource    cases/MORT-2026-000184/document-requests                           |
| Version     valid for 1   (now at 1)                                            |
+--------------------------------------------------------------------------------+
| Before and after                                                                |
|  field              | Now at the system | After this action                     |
|  document_requests  | (none)            | pay_statement   changes               |
+--------------------------------------------------------------------------------+
| Evidence (opens in a new tab)      Policy sha256:5e7b… allowed: within_grant    |
| Approvals so far: 0 of 1 approved                                               |
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

The view endpoint and its model, the three response routes (and the old ones as aliases), `reason_code` for every code above (replacing task 2's and task 4's `{"error": ...}` names), the panel and its states, the request and decision records through `caliber.contracts.validate_record`, and a UI test per denial code and per other outcome that asserts the message and the disabled or read-only state. Out of scope here: the component and the API changes themselves.
