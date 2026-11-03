---
audience:
  - operator
  - developer
  - evaluator
doc_type: runbook
product_area: operations
stability: beta
prerequisites:
  - A reference-profile deployment with PostgreSQL and the background owner loop
  - An operator token scoped to the project containing the run
  - The Beta scope and reference-profile pages
reviewed_on: 2026-11-03
version_applicability: current main branch docs contract
tags:
  - beta
  - intervention
  - approvals
  - evidence
  - recovery
---

# Beta intervention guide

Use this guide when a governed Beta run stops for a person, when an approval is no longer safe to consume, or when an
evidence archive must be inspected. It describes the installed Cookbook 22 journey and the real run-scoped API that the
SDK and CLI wrap. The Harborlight case is synthetic: it is a control-flow fixture, not a credit decision and not a
claim about model quality.

All HTTP paths below are relative to `/ajax-api/2.0/mlflow/caliber`. The server derives project and authorization scope
from the authenticated identity and run; never add a caller-supplied project to an evidence or approval request.

## The intervention decision tree

| State or symptom | Safe first action | Never do |
| --- | --- | --- |
| `waiting_approval` | Read the run's approvals, then inspect the exact approval view and proposal | Approve from the initiating editor or reconstruct the proposal from input text |
| Approval is expired, superseded, or already consumed | Re-read the approval view and create a new run if the request is still needed | Retry an old approval id or edit a stored approval/policy row |
| Run is approved but still waiting | Confirm the approval is bound to the same run and proposal, then use the operator resume action | Send the downstream request directly or click a second approval path |
| Run is accepted | Download and verify the evidence archive, then use read-only replay if needed | Treat a successful HTTP status as proof of downstream acceptance without the archive/receipt chain |
| Replay reports `matches_original: false` | Hold the result, compare the recorded observations and criteria version, and open a defect/review item | Re-run the live workflow to make the replay pass |
| Evidence returns `410 evidence_expired` | Retain the tombstone and export metadata; recover from the source run only if policy allows a new export | Recreate an expired archive under the old scope or claim the bytes are recoverable |

## 1. Inspect a run that is waiting for approval

UI: open **Workflows → Runs**, select the project, then open the run detail page. The run state must be
`waiting_approval`. The approval panel should show the pending runtime approval id and the project/run scope. For the
installed Cookbook 22 journey, the proposal describes one `request_missing_document` action for case
`MORT-2026-000184`, document `pay_statement`, expected version `1`.

API:

```bash
RUN_ID="run-..."
curl -sS -H "Authorization: Bearer $CALIBER_TOKEN" \
  "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/workflow-runs/$RUN_ID" | jq '.data | {workflow_run_id,status,project_id,acceptance}'

curl -sS -H "Authorization: Bearer $CALIBER_TOKEN" \
  "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/workflow-runs/$RUN_ID/approvals" | jq '.data'
```

The list response identifies `runtime_approval_id`. Use that id with the same `RUN_ID` to read the approval view:

```bash
APPROVAL_ID="runtime-approval-..."
curl -sS -H "Authorization: Bearer $CALIBER_TOKEN" \
  "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/workflow-runs/$RUN_ID/approvals/$APPROVAL_ID" \
  | jq '.data | {approval,binding,proposal,policy,quorum,state,allowed_responses,caller}'
```

The `proposal` is nested in this view. The current contract has no standalone `ProposalsAPI` or proposal route. The
`binding` pins the intent/evidence/policy digests and expected resource version; compare them before deciding.

SDK:

```python
approvals = caliber.workflows.runs.approvals(run_id)
runtime_approval_id = approvals[0].runtime_approval_id
view = caliber.workflows.runs.approval(run_id, runtime_approval_id)
assert view.proposal is not None
```

## 2. Reject an unsafe or stale approval

UI: from the approval panel, choose **Reject**, enter a concise reason that states the evidence or policy mismatch, and
confirm. The run must remain scoped to the same project and the approval must still be open. If the UI reports an
expired or superseded state, stop and start a new run instead of retrying.

API:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $CALIBER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"runtime_approval_id":"'"$APPROVAL_ID"'","reason":"the expected resource version is stale"}' \
  "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/workflow-runs/$RUN_ID/approval/reject" | jq '.data'
```

SDK:

```python
caliber.workflows.runs.decide(
    run_id,
    runtime_approval_id=runtime_approval_id,
    decision="reject",
    reason="the expected resource version is stale",
)
```

`request_changes` can appear in `allowed_responses` as policy metadata, but it is not an executable decision route in
this release. A client must not invent `/approval/request-changes`.

## 3. Approve the exact proposal as a distinct person

UI: sign in as the project approver, not the initiating editor. Re-open the same run and approval, compare the proposal
resource and arguments with the request, then choose **Approve**. A self-approval must display
`403 project_role_required` and must not create a downstream mutation.

API:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $APPROVER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"runtime_approval_id":"'"$APPROVAL_ID"'","reason":"the exact synthetic request is authorized"}' \
  "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/workflow-runs/$RUN_ID/approval/approve" | jq '.data'
```

SDK:

```python
approved = approver.workflows.runs.decide(
    run_id,
    runtime_approval_id=runtime_approval_id,
    decision="approve",
    reason="the exact synthetic request is authorized",
)
```

An approval response may be a partial-quorum acknowledgement. It does not resume the run automatically. Check the
approval state again and use the operator action only after the required decision is recorded.

## 4. Resume and verify the accepted outcome

UI: as the operator, choose **Resume** on the approved run. Watch the run detail state move through `queued` or
`running` to `completed`; the acceptance panel must say `accepted`. The run must show one governed effect and one
receipt. If a connector is unbound or has no authoritative verifier, the run refuses closed before a proposal is
created; that is an expected safety result.

API:

```bash
curl -sS -X POST -H "Authorization: Bearer $CALIBER_TOKEN" \
  "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/workflow-runs/$RUN_ID/resume" | jq '.data'

curl -sS -H "Authorization: Bearer $CALIBER_TOKEN" \
  "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/workflow-runs/$RUN_ID" \
  | jq '.data | {status,acceptance}'

curl -sS -H "Authorization: Bearer $CALIBER_TOKEN" \
  "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/workflow-runs/$RUN_ID/causal-links?limit=200" \
  | jq '.data.effects'
```

The causal links should connect proposal, approval, decision, effect and receipt. A `201` first downstream commit,
`200` same-key replay and `412` stale `If-Match` are downstream fixture assertions, not permission to bypass the
dispatcher.

## 5. Read and verify the evidence package

UI: on the run detail page choose **Evidence → Download**. Keep the archive with the run id, package digest and
authorization scope. Do not upload it to an unapproved service.

API:

```bash
curl -sS -D evidence.headers -o evidence.zip \
  -H "Authorization: Bearer $CALIBER_TOKEN" \
  "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/workflow-runs/$RUN_ID/evidence"
```

The route returns a deterministic ZIP for the server-authorized view. The SDK leaves the bytes available to the
server-free archive verifier:

```python
archive = caliber.workflows.runs.evidence(run_id)
assert archive[:2] == b"PK"
```

An expired archive returns `410` with `reason: evidence_expired` and a tombstone. Keep that tombstone as evidence of
retention enforcement; do not recreate the old archive under the same scope.

## 6. Replay without another mutation

UI: choose **Evidence → Replay** only when the browser journey exposes the replay control. The result is a read-only
comparison and must display the server's `matches_original` value.

CLI:

```bash
caliberctl workflow run replay "$RUN_ID"
```

Exit 0 means `matches_original: true`; exit 1 means the comparison ran and found a mismatch. A refusal or malformed
archive is an API/verification failure, not a match.

API and SDK:

```bash
curl -sS -X POST -H "Authorization: Bearer $CALIBER_TOKEN" \
  "$CALIBER_BASE_URL/ajax-api/2.0/mlflow/caliber/workflow-runs/$RUN_ID/replay" | jq '.data | {workflow_run_id,matches_original,criteria_version,decisions}'
```

```python
report = caliber.workflows.runs.replay(run_id)
assert report.matches_original
```

Replay does not call a model, dispatcher, downstream or live provider. A mismatch requires investigation of the pinned
manifest, recorded observations and criteria version; starting a new live run is a separate, explicitly reviewed action.

## Operational records

Record the following with every intervention: UTC timestamp, deployment/candidate SHA, project and run id, approval id,
proposal id, decision principal and reason, response/error code, evidence archive/package digest, replay result, and
whether any downstream mutation was observed. Redact payload values and credentials. The records are evidence of an
operator decision, not a replacement for the independent Beta security review or the release decision in #613.
