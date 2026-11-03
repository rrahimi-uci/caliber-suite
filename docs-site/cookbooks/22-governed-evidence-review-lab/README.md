# Cookbook 22 · Governed Evidence Review

> **Beta path:** this cookbook uses the supported Beta profile described in
> [`docs/start/beta-scope.md`](../../../docs/start/beta-scope.md). It is a synthetic
> evidence-review scenario only; it never makes a mortgage, underwriting, approval, or
> denial decision.

Take one evidence-review request from a governed workflow run to a verified downstream
outcome. An editor admits a run for the synthetic Harborlight case, a scripted model proposes
one missing-document request, and the run stops at the approval boundary. A distinct approver
approves the exact proposal; an operator resumes the run; the connector commits exactly one
request and reads the changed case back. The evidence bundle joins the run, proposal, approval,
policy decision, effect receipt, and downstream request. A replay of the captured evidence is
matched and makes no outbound mutation.

The lab is deliberately narrow. The downstream is the existing D1 disposable system of record,
the case is the pinned fixture from B15.1, and the workflow uses the governed worker seams from
B05. This package does not create another route, duplicate the case fixture, or claim that an
external system is exactly-once beyond its documented idempotency and version precondition.

## What this lab demonstrates

- **Admission and isolation.** An editor in project A can start the evidence-review run; an
  editor in project B cannot read project A's run or causal-links response. An unknown run or
  project is returned as the same scoped `404` not-found contract.
- **A protected proposal.** The scripted model proposes exactly
  `request_missing_document(case_id, document_type)`. The editor sees the proposal but cannot
  approve it: the route returns `403` with `project_role_required`, and the downstream mutation
  count remains zero.
- **Two-person approval and controlled effect.** A distinct approver records the decision, the
  operator resumes the run, and the worker sends one `document-request` with the expected case
  version. The receipt is verified by querying the downstream, not by trusting the outbound
  response alone.
- **Downstream boundaries.** The same logical key is `201` on its first commit and `200` on a
  replay; a stale `If-Match` is `412` and changes nothing. An unbound or unknown verifier fails
  closed with the documented `connector_unbound` / `no_authoritative_verifier` refusal before a
  proposal is recorded.
- **Evidence and replay.** The run reaches `completed` with acceptance `accepted`; the evidence
  export contains the linked ids and the replay reports `matched` without sending a second
  request. The causal-links view has no missing or unrecorded links.

**You are done when:**

- the run is `waiting_approval` before the distinct approver decides;
- project A has exactly one committed proposal, one receipt, and one downstream document request;
- the case changes from version 1 to version 2 and the receipt is verified by read-back;
- the editor's approval attempt is refused, project B is isolated, and stale replay is `412`;
- export and offline replay preserve the same run/proposal/approval/effect ids and report `matched`.

## Prerequisites and setup

Read the [Beta scope](../../../docs/start/beta-scope.md) and [reference profile](../../../docs/operate/reference-profile.md)
before starting. The supported shape requires PostgreSQL, CSRF, the effect reconciler and
background tasks, a two-person approval policy, the registered `document_request` connector,
and a project-scoped downstream binding. The scenario harness supplies the disposable
Harborlight downstream and scripted model; do not point it at a customer system.

For the installed cookbook runner, use the lab registration after the dependent V01 and SDK
artifacts are present:

```bash
export CALIBER_OPERATOR_USERS=admin,lab22-approver
export CALIBER_APPROVER_USERS=admin,lab22-approver
scripts/cookbook-env.sh up lab22 --flags cookbooks
```

The authoritative profile settings and refusal text are in the Beta scope page. Do not copy
secrets or real provider credentials into a disposable lab stack.

## Dataset and sample files

There is no duplicate payload under this cookbook. The scenario loads the existing pinned fixture
at [`caliber/tests/fixtures/egi/case_000184.json`](../../../caliber/tests/fixtures/egi/case_000184.json)
through the Harborlight projection. Its source commit, SHA-256, MIT license, synthetic notice,
and non-credit-decision boundary are recorded in
[`caliber/tests/fixtures/egi/SOURCE.md`](../../../caliber/tests/fixtures/egi/SOURCE.md). The
contract is summarized in [`assets/README.md`](assets/README.md).

The only expected missing document is `pay_statement` for case `MORT-2026-000184` at version 1.
The connector request creates a document-request record and never changes any underwriting or
credit outcome.

## Configuration and environment

| Variable or binding | Lab value | Why |
| --- | --- | --- |
| `CALIBER_PROFILE` | `reference` | enables the supported Beta trust boundary; development is not the acceptance profile |
| `CALIBER_OPERATOR_USERS` | `admin,lab22-approver` | admits the worker lease and the operator resume path |
| `CALIBER_APPROVER_USERS` | `admin,lab22-approver` | gives only the distinct approver the platform approval scope |
| `CALIBER_BACKGROUND_TASKS_ENABLED` | `true` | runs the governed worker/reconciler loops |
| `CALIBER_EFFECT_RECONCILER_ENABLED` | `true` | verifies the downstream receipt after the effect |
| `CALIBER_AGENT_STEP_TOOL_BACKENDS` | registered tool → `document_request` | binds the governed step to the trusted connector |
| `CALIBER_EFFECT_CALLBACK_CONNECTORS` | project A → disposable Harborlight downstream | supplies the authoritative lookup by stable request key |
| `CALIBER_FEATURE_COOKBOOKS` | `true` | only needed to install the gallery recipe; it is not an authorization grant |

The exact binding syntax is deployment-specific and belongs to the reference-profile page. A
missing binding must refuse closed; it must not silently fall back to an unverified HTTP call.

## Run it through the UI

The browser journey is supplied by B15.3b (`22-governed-evidence-review.spec.ts`) and is run by
the cookbook runner once that dependent artifact is present.

1. Sign in as the editor in project A and open **Workflows**. Start the installed evidence-review
   workflow for the Harborlight case. Confirm the run pauses at **Waiting for approval** and the
   proposal names `request_missing_document` with the case id, `pay_statement`, and expected
   version `1`.
2. Attempt **Approve** as the editor. The control is disabled or the route returns `403`
   `project_role_required`; the downstream request ledger remains empty.
3. Sign in as `lab22-approver`, inspect the exact proposal diff, and approve it. Resume the run as
   `admin` or the configured operator. The run reaches **Completed** with acceptance **Accepted**.
4. Open the run's **Evidence** view. Confirm the run, proposal, approval, decision, receipt and
   downstream request ids are linked, with no missing or unrecorded links. Download the evidence
   export.
5. Open **Replay** for the exported bundle. Confirm the result is `matched` and the downstream
   ledger still has one mutation.
6. Switch to project B and confirm the same run is not readable. Try the stale version path and
   confirm the downstream responds `412` with no second request.

## Run it through the SDK

The maintained installed example is the next package step after the V01 scenario and browser
journey. Run it from the repository root with an administrator token:

```bash
scripts/cookbook-run-all.sh --labs 22 --api-only
```

Or run the example directly when it is installed:

```bash
cd sdk/caliber-sdk
../../caliber/.venv/bin/python -m examples.cookbooks.cookbook_22_governed_evidence_review
```

The script must assert the same public ids through API/SDK/CLI surfaces: submit → proposal →
approval → resume → verified receipt → export → replay. It must also exercise the editor denial,
project-B isolation, unknown/stale downstream paths, and the one-mutation ledger. It must use the
scripted provider and local fixture; ordinary validation never calls a live model or customer
system.

## Verify it worked

| Claim | Where to look | What proves it |
| --- | --- | --- |
| Approval is a real boundary | run status and approval route | `waiting_approval`; editor `403`; zero downstream mutations before approval |
| One controlled effect | proposal/receipt tables and downstream | one committed proposal, one verified receipt, case version 2, one request key |
| Downstream replay is safe | downstream POST with the same key | first `201`, replay `200`, same request id and no extra mutation |
| Stale writers fail closed | downstream POST with old `If-Match` | `412`, current case unchanged, no extra request |
| Tenant/project isolation holds | project B run and causal-links reads | scoped `404`; project B cannot approve or mutate project A's run |
| Evidence is complete | export and causal-links response | same ids across sections; `missing_links=[]`, `unrecorded_links=[]` |
| Offline replay is non-mutating | replay result and downstream ledger | `matched`; ledger remains at one mutation |

## Trust boundary and limitations

- The Harborlight record is synthetic training data and is not a credit decision. The scenario
  requests a missing document only.
- The worker sends a protected action only after admission, policy decision, distinct approval,
  operator resume, and connector binding. A project role alone does not grant platform approval.
- `201`/`200` idempotency and `412` version fencing are guarantees of the disposable D1
  downstream contract. They are not a universal exactly-once guarantee for arbitrary external
  systems.
- Evidence export is a record of the linked persisted rows and captured observations. It does not
  make an unverified connector authoritative, and replay never sends an outbound mutation.
- The scenario uses a scripted model for deterministic CI. It proves the governance and evidence
  path, not the quality of a real provider's document classification.

## Common errors and troubleshooting

| You see | Why | Fix |
| --- | --- | --- |
| `403 project_role_required` while approving | the initiating editor cannot approve the protected effect | sign in as the distinct approver with the project role and `caliber.approver` scope |
| scoped `404` for the run or causal links | the caller is in the other project or the id is unknown | select project A and use the run id returned by admission |
| `connector_unbound` | project A has no configured downstream binding | configure the reference-profile connector binding; do not retry blindly |
| `no_authoritative_verifier` | the target has no trusted read-back verifier | use the registered `document_request` connector or expect the fail-closed refusal |
| `412` on the document request | `If-Match` is older than the current case version | refresh the case and submit the current version; do not force the write |
| replay reports `changed` or sends a request | the bundle, commit, or source digest differs | use the exact captured export; a changed source is a new run, not a replay |

## Cleanup and reset

The scenario owns only its disposable project, users, run, evidence and local downstream. Use the
example's cleanup option when available, then remove the disposable stack:

```bash
scripts/cookbook-env.sh down lab22
rm -rf .tmp/cookbook-env/lab22
```

Do not delete the shared `caliber/tests/fixtures/egi` fixture; it is the single provenance-pinned
source for the Beta EGI scenarios.
