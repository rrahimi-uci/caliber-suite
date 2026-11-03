---
audience:
  - developer
  - operator
  - decision-maker
doc_type: concept
product_area: docs
stability: beta
prerequisites:
  - A reading of the reference profile page, if you intend to run the supported shape
reviewed_on: 2026-11-01
version_applicability: current main branch docs contract
tags:
  - beta
  - scope
  - reference-profile
  - capabilities
  - limitations
---

# Beta scope: the supported profile and its evidence

> **What this page is.** It says which parts of CALIBER the Beta work supports, which it does not, and which scenario
> tests the support claim rests on. It describes `main`; it does not announce a release, and the Beta release gate
> (#613) is a separate decision. The [alpha scope](alpha-scope.md) and [alpha install](alpha-install.md) pages stay
> as the record of the `0.1.0a1` alpha and are not rewritten here. The claims below separate deterministic synthetic
> evidence from real-provider quality evidence and keep preview or unsupported surfaces explicit.

## What "supported" means here

An adapter is **supported** only when `GET /ajax-api/2.0/mlflow/caliber/capabilities` reports it as `supported` in
`data.adapters` for the deployment you are looking at. That report is derived from the running configuration and from
the scenario tests cited below (`caliber.features.SUPPORT_EVIDENCE`); no setting says "supported", and an adapter with no
evidence entry cannot be. The rest of this page is a reading of that report, not a second source for it: a contract test
(`caliber/tests/test_beta_scope_contract.py`) fails when the table, the evidence or the quoted messages below stop
matching the code.

Supported does **not** mean any of these:

- It is not a release label (nothing here says a Beta is out), a service level or a promise about upgrades, performance or
  hosting.
- It does not grant authority. A supported adapter is still behind the authorization, approval and admission checks of
  its own route and the effect dispatcher.
- It is not a claim about every integration, connector or MCP backend. Only the two adapters in the matrix below can be
  supported; every other path is preview, unavailable or refused, and says so.

Every adapter reports exactly one status:

| status | meaning |
| --- | --- |
| `supported` | the reference profile admits it, the deployment is configured as the cited scenarios were, and the live code still matches what they exercised |
| `preview` | it can run, but nothing in the supported shape covers it; this is the best an MCP server or an OpenAPI integration can be, and every adapter is here outside the reference profile |
| `unavailable` | it cannot run in this deployment; the report names the setting that says so, or `profile` when no setting does |

## The supported shape

The supported shape is the [reference profile](../operate/reference-profile.md): `CALIBER_PROFILE=reference`, started from a
configuration that passes every rule on that page (PostgreSQL, CSRF on, no development fallbacks, the egress policy on, no
MCP allowlist entries, no unlisted plugin, a confined file root, two-person approval and Apply). A process that breaks a
rule refuses to start and lists every violation. The default profile, `development`, is unchanged and supports nothing.

On top of the profile, each adapter needs a little more:

| adapter | also needs | variable |
| --- | --- | --- |
| `document_request_connector` | the background loops on, and the effect reconciler on (both default on) | `CALIBER_BACKGROUND_TASKS_ENABLED`, `CALIBER_EFFECT_RECONCILER_ENABLED` |
| `release_evaluation` | the release area on, its evaluator on, the background loops on, and a real evaluation provider | `CALIBER_FEATURE_RELEASE_CANDIDATES`, `CALIBER_WORKSPACE_RELEASE_EVALUATOR_ENABLED`, `CALIBER_BACKGROUND_TASKS_ENABLED`, `CALIBER_EVAL_PROVIDER=mlflow` |

These are the settings the report itself checks. Neither adapter is on after the [alpha install](alpha-install.md): that
install runs the development profile, with the release area and the integrations area off, so its report is `preview`
for the connector and `unavailable` for the rest; with the release area, its evaluator and the integrations area all on it is `preview`
for all four (the matrix below). The workflow queue (`CALIBER_WORKFLOW_RUN_QUEUE_ENABLED`) is not a
condition of either claim.

**`supported` is not a statement that a binding exists.** The report does not read the operator's wiring, and the reference
profile does not require it. For a protected action to actually reach the document-request connector from a workflow run,
the operator also sets:

| setting | what it does | if it is missing |
| --- | --- | --- |
| `CALIBER_AGENT_STEP_TOOL_BACKENDS` | maps a registered tool, by its registry identity, to a backend (`document_request`) so an agent bound to it runs as a governed step | empty (the default): no agent is governed, and the connector is reached by nothing |
| `CALIBER_EFFECT_CALLBACK_CONNECTORS` | binds the connector to the downstream `base_url` the effect is sent to and the reconciler asks, per project | no binding: `connector_unbound`, refused before anything is claimed, and nothing is sent |

A deployment can therefore report `document_request_connector` as `supported` and still send nothing, because no tool is
mapped or no connector is bound. Read the [worker's trusted inputs](../operate/reference-profile.md#the-worker-binds-the-governed-path-1051)
before relying on it.

## The Beta vertical slice

The Beta claim is deliberately narrow: an admitted run may propose one protected synthetic document request; a person
decides the exact proposal; the operator resumes the run; the dispatcher commits at most one downstream mutation; the
platform reads the result back, records acceptance, exports scope-bound evidence and replays that evidence without a
live provider or another write. The maintained proof is V01 in
[`caliber/tests/egi_scenarios/test_v01_governed_effect.py`](../../caliber/tests/egi_scenarios/test_v01_governed_effect.py)
and the installed identity of the journey is Cookbook 22.

The surfaces are the same contract, viewed by different callers:

| Surface | Beta path | Evidence boundary |
| --- | --- | --- |
| HTTP/API | `POST /workflow-runs`, run-scoped approval reads and `/workflow-runs/{run_id}/approval/{approve,reject}`, `resume`, `evidence`, and `replay` | Run and project scope are derived by the server; a caller cannot supply a broader evidence view. |
| Python SDK | `WorkflowRunsAPI.approvals()`, `approval()`, `decide()`, `resume()`, `evidence()`, and `replay()` | The proposal is nested in `WorkflowApprovalView`; replay exposes the server's `matches_original` field. |
| CLI | `caliberctl workflow run replay RUN_ID` | The command is read-only and exits nonzero when `matches_original` is false. |
| UI | Cookbook installation, run/approval surfaces and the corresponding browser journey | UI coverage must use the installed Cookbook 22 artifact; it must not seed a parallel manifest or downstream. |

There is no standalone `ProposalsAPI` or `ApprovalsAPI` on the current server. `request_changes` may be present in an
approval view's `allowed_responses` metadata, but it is not a decision route. Documentation, SDK examples and browser
tests must not invent either surface.

## Evidence tiers and non-claims

| Tier | What it establishes | What it does not establish |
| --- | --- | --- |
| Deterministic synthetic | Control flow, refusal semantics, project isolation, idempotency, receipt linkage, acceptance and offline replay for the Harborlight fixture | Customer credit quality, provider quality, availability, or general-purpose connector support |
| Bounded real-provider | Quality measurements on a domain-approved held-out corpus, with provider/version, threshold, cost cap and abort rule recorded | A replacement for the governed-effect safety proof or an authorization to broaden scope |
| Independent review | A reviewer not involved in implementation examines the Beta boundary, injection/refusal behavior and evidence integrity | A fact that can be substituted by CI, self-review, or an agent's own test run |

The repository's ordinary CI is deterministic and offline. A passing synthetic scenario is required engineering evidence,
but it is not a real-provider result and does not close the independent-review or staffing dependencies in #608 and #851.
The release decision remains #613; until its required evidence and review are recorded, this page is a scope contract,
not a release announcement.

## Supported, preview and unsupported behavior

Supported in the reference profile means the exact V01 control path is exercised and the required refusal and identity
checks remain green. Preview means an API or UI surface may exist but the Beta support evidence does not cover it.
Unsupported means the reference profile refuses it before an effect is claimed. In particular:

- `document_request` is the supported protected connector for the synthetic vertical slice when its tool and project
  binding are configured.
- Listed HTTP verifiers may settle their own bounded connector path; this does not promote generic HTTP to a supported
  Beta adapter.
- Protected registered-tool, OpenAPI and MCP effects without an authoritative verifier refuse closed with
  `no_authoritative_verifier`. Read-class operations remain subject to their own read policy.
- Runnable exports, ungoverned Python/host-file effects, unlisted plugins, unconfined file roots and MCP entries in the
  reference profile remain refused. A workflow that cannot provide an execution-owner lease and governed context does
  not silently fall back to an unreviewed write.

The [reference profile](../operate/reference-profile.md), [intervention guide](../operate/intervention-guide.md), and
[Beta feature coverage matrix](../reports/beta-feature-coverage-matrix.md) are the operational, recovery and traceability
companions to this scope page. This consolidated scope package is the deliverable tracked by **#877** and remains
subject to the release decision in **#613**.

## The support matrix

| adapter id | what it is | best status it can reach | reference profile, configured as above | development profile (the default) |
| --- | --- | --- | --- | --- |
| `document_request_connector` | the trusted `document_request` transport that the effect dispatcher sends an approved protected action through | `supported` | `supported` | `preview` (`not_reference_profile`) |
| `release_evaluation` | the workspace release evaluator that executes queued evaluation attempts of a release | `supported` | `supported` | `preview` (`not_reference_profile`), or `unavailable` while the release area or the evaluator is off |
| `mcp_servers` | MCP servers registered with the gateway | `preview` | `unavailable` (`mcp_not_in_reference_profile`): the profile lists no MCP command or host, so no server is reachable | `preview` (`no_support_evidence`), or `unavailable` while `CALIBER_FEATURE_INTEGRATIONS` is off |
| `openapi_integrations` | imported OpenAPI integrations and the tools published from them | `preview` | `preview` (`no_support_evidence`) | `preview` (`no_support_evidence`), or `unavailable` while `CALIBER_FEATURE_INTEGRATIONS` is off |

The four rows are the four adapters the capability report lists, in that order. A closed area flag wins over everything
else: the adapter is `unavailable` with the same `feature_disabled` code and text as the `404` of its routes. The full
resolution order, one rule per row, is in
[Capability exposure](../operate/reference-profile.md#capability-exposure-what-is-supported-b15-support1-755).

### Evidence for each supported adapter

The report lists these scenario test ids in `evidence` for a supported adapter, and lists nothing for any other status.
Each id is a test function on `main`; the contract test fails on one that does not exist. Paths are relative to `caliber/`.
All of them are collected by the CI job `egi-scenarios-postgres` ("EGI scenarios (PostgreSQL)", `pytest tests/egi_scenarios`,
defined in [`.github/workflows/ci.yml`](https://github.com/rrahimi-uci/caliber-suite/blob/main/.github/workflows/ci.yml)),
but they do not all use a database. The last column says which database each one runs on, and the contract test derives it
from the test sources: `dialect-parameterised` is a scenario with a SQLite and a PostgreSQL parameter, and only its
PostgreSQL parameter runs on PostgreSQL; `no database` uses only the HTTP downstream fixture; `SQLite from the models` builds a
SQLite database with `Base.metadata.create_all`, never PostgreSQL.

| adapter id | scenario | what it demonstrates | database |
| --- | --- | --- | --- |
| `document_request_connector` | `tests/egi_scenarios/test_connector_capabilities.py::test_stable_key_mutates_once` | the same idempotency key sent twice commits once at the downstream | `no database` |
| `document_request_connector` | `tests/egi_scenarios/test_connector_capabilities.py::test_stale_precondition_returns_412_and_no_mutation` | a write bound to a version that is not current is refused `412` and changes nothing | `no database` |
| `document_request_connector` | `tests/egi_scenarios/test_connector_capabilities.py::test_query_by_key_after_lost_response` | after a response is lost, the request is found by its key at the downstream | `no database` |
| `document_request_connector` | `tests/egi_scenarios/test_b04_dispatcher.py::test_dispatcher_uses_canonical_approval_and_registered_connector` | on a reference-profile app, an approval recorded over the decision route and a real execution-owner lease reach the registered connector once, with an outbox row and a receipt | `dialect-parameterised` |
| `document_request_connector` | `tests/egi_scenarios/test_v07_restart_exactly_once.py::test_kill_after_downstream_commit` | a dispatcher killed after the downstream committed leaves one mutation, found by key and not sent again | `dialect-parameterised` |
| `release_evaluation` | `tests/egi_scenarios/test_v13_release_evaluation.py::test_evaluator_crash_recovers` | after an evaluator crash, the provider runs it already observed stay on the evaluation and only the recovered claim settles it | `SQLite from the models` |
| `release_evaluation` | `tests/egi_scenarios/test_v13_release_evaluation.py::test_worker_records_provider_run_before_completion_crash` | a provider run is recorded before completion, so a crash between the two loses no run identifier | `SQLite from the models` |
| `release_evaluation` | `tests/egi_scenarios/test_v13_release_evaluation.py::test_recovery_before_provider_result_keeps_attempt_history` | a crash before the provider returns still burns the old claim number and keeps the attempt history | `SQLite from the models` |

Two pins keep the claim tied to the code it was shown on: for the connector, the admission record
`caliber.profile.REFERENCE_CONNECTORS` (implementation, capability digest and configuration digest, checked live), and for
the evaluator, the implementation `WorkspaceReleaseEvaluator` and evidence schema version `1`. Changing either withdraws the
claim (`unavailable`) until a reviewer confirms the scenarios again.

What the evidence does not cover, stated so the word "supported" is not read wider than it is:

- **The connector scenarios run against a purpose-built downstream fixture** (a case record with document requests,
  started by the tests), not against a customer system. They show the platform's side of the contract: a stable key, a
  precondition, a read-back by key. A downstream that does not behave like that fixture is outside the claim.
- **The release-evaluation scenarios use a scripted evaluation provider.** They prove the claim, recovery and fenced
  completion of an attempt, not the quality of a real provider's scores. With `CALIBER_EVAL_PROVIDER=mlflow` the evaluator
  calls the real provider and reports `supported`; with `fake` it reports `preview` because the scores are simulated.
- **Only two of the five connector scenarios, and none of the release-evaluation scenarios, run on PostgreSQL.**
  `test_b04_dispatcher.py` (a migrated reference-profile app) and `test_v07_restart_exactly_once.py` have a PostgreSQL
  parameter. The three `test_connector_capabilities.py` scenarios use no database: they show the downstream's stable key,
  precondition and read-back by key, not the platform's persistence. The release-evaluation scenarios build a SQLite
  database from the models (no migrations), without a reference-profile app or the capability route, so they show the
  evaluator's claim, recovery and fencing logic, not PostgreSQL concurrency or lease behaviour, and not the migrated schema.
- **No scenario here proves the whole loop for one workflow end to end under the capability report.** The end-to-end
  governed run (approve, resume, one send, a receipt read back by key) is shown by
  `tests/egi_scenarios/test_b05_governed_worker_wiring.py::test_a_proposal_is_approved_over_http_resumed_and_sent_exactly_once`,
  which is evidence for the controls described below but is not one of the pinned `evidence` entries. Adding it is a
  reviewed change to `SUPPORT_EVIDENCE`, not a documentation edit.

## What the worker reaches in the supported shape

Support of an adapter is not the same as every path to it being open. In the reference profile the workflow-run worker
sends a protected action only through a connector whose outcome the platform can read back by key. Today that is:

| node or tool | what the reference profile does | evidence (a control, not a support claim) |
| --- | --- | --- |
| a governed agent step bound to a tool the operator mapped to `document_request` (`CALIBER_AGENT_STEP_TOOL_BACKENDS`), with the connector bound to a downstream for the project (`CALIBER_EFFECT_CALLBACK_CONNECTORS`) | proposes, parks `waiting_approval`, and after a different person approves and an operator resumes, sends once and reads the receipt back by key | `tests/egi_scenarios/test_b05_governed_worker_wiring.py::test_a_proposal_is_approved_over_http_resumed_and_sent_exactly_once` |
| a webhook or API-request node whose exact URL the operator listed in `CALIBER_EFFECT_HTTP_VERIFIERS` | sends to the generic `http` connector, which is reachable at those URLs only; the `http` connector is not one of the four adapters and the report claims nothing for it | `tests/egi_scenarios/test_b05_http_unknown_settlement.py::test_a_listed_webhook_is_sent_once_and_its_2xx_is_confirmed_by_the_lookup` |
| a webhook or API-request node whose URL nobody listed | refused before a proposal is recorded: `HTTP effect refused: no_authoritative_verifier` | `tests/egi_scenarios/test_b05_http_unknown_settlement.py::test_a_webhook_whose_url_is_not_listed_is_refused_before_a_proposal_exists` |
| a protected (non-read) registered-tool, OpenAPI or MCP call | refused before a proposal is recorded (a read-class call, such as a `GET`, `HEAD` or `OPTIONS` of a read row or a read-class registered or MCP tool, still runs inline): `registered tool effect refused: no_authoritative_verifier`, `OpenAPI effect refused: no_authoritative_verifier`, `MCP effect refused: no_authoritative_verifier` (these connectors have no lookup by key the platform can call; one shared check produces the sentence, and the tests show it for the HTTP label and the `registered_tool` verifier) | `tests/test_governed_worker_wiring.py::test_a_workflow_node_is_refused_before_anything_is_recorded_when_its_backend_has_no_verifier`, `tests/test_governed_worker_wiring.py::test_the_receipt_verifier_of_a_context_never_allows_an_unverified_connector` |

Everything the inventory marks as running code, or writing to the host or a database with no dispatcher in front of it, is
refused in the reference profile with `unsupported_in_reference_profile` before anything runs: the `python_code`,
`external_app` and `output_folder` nodes, an administrator's registered Python tool, runnable exports, the standalone
tool-sandbox service and the host-file built-ins
([Refused paths](../operate/reference-profile.md#refused-paths-in-the-reference-profile-b047-774),
[Executable code](../operate/reference-profile.md#executable-code-is-refused-b124a-754)). An installed optimizer plugin
whose distribution is not in `CALIBER_PLUGIN_ALLOWLIST`, and `CALIBER_WORKFLOW_FILE_ROOT=unconfined`, stop the process from
starting; a `folder_input` node, or a `file_input` node without a managed file, reads only inside the configured file root,
and with none configured it is refused (`output_folder` is refused outright, see above)
([Plugins and host paths](../operate/reference-profile.md#plugins-and-host-paths-b124b-763)).

| refusal | scenario that starts a real reference process or app and shows it |
| --- | --- |
| a code node, an administrator's Python tool, a refused path | `tests/egi_scenarios/test_b12_code_refusal.py::test_authoring_a_code_node_is_refused_by_validate_compile_and_publish`, `tests/egi_scenarios/test_b04_refusals.py::test_runnable_code_exports_are_refused_in_the_reference_profile` |
| an MCP allowlist entry | `tests/egi_scenarios/test_b12_mcp_startup_rule.py::test_an_unlisted_mcp_entry_refuses_startup_before_serving` |
| an unlisted plugin, an unconfined file root | `tests/egi_scenarios/test_b12_plugin_refusal.py::test_an_unlisted_installed_plugin_refuses_startup_and_is_never_imported`, `tests/egi_scenarios/test_b12_plugin_refusal.py::test_an_unconfined_file_root_refuses_startup` |

## Opt-in, disabled and unavailable: the exact answers

A feature flag says an area is on, never that it is supported, and turning one on promotes nothing. Each flag is read once
at startup, so restart after changing it.

A route of a disabled area answers `404` with `reason_code: feature_disabled` and the variable to set; the same sentence is the
adapter's `message` in the capability report. Each row below is what the report says, word for word, and the recovery is
the variable in `disabled_by` (`profile` means no setting changes the answer).

**A disabled area (the default install).** `CALIBER_FEATURE_RELEASE_CANDIDATES` unset:

```text
GET /ajax-api/2.0/mlflow/caliber/releases/candidates
404 {"detail": "Release-candidate signoff and workspace releases is disabled in this deployment. Set CALIBER_FEATURE_RELEASE_CANDIDATES=true to enable it.", "status_code": 404, "reason_code": "feature_disabled"}

release_evaluation.message  (reason_code feature_disabled, disabled_by CALIBER_FEATURE_RELEASE_CANDIDATES)
Release-candidate signoff and workspace releases is disabled in this deployment. Set CALIBER_FEATURE_RELEASE_CANDIDATES=true to enable it.

mcp_servers.message and openapi_integrations.message  (reason_code feature_disabled, disabled_by CALIBER_FEATURE_INTEGRATIONS)
MCP servers and OpenAPI integrations is disabled in this deployment. Set CALIBER_FEATURE_INTEGRATIONS=true to enable it.
```

**Not the reference profile (a development deployment).** Recovery: start a process with `CALIBER_PROFILE=reference` from a
configuration that passes it; nothing else promotes the adapter.

```text
document_request_connector.message  (reason_code not_reference_profile, disabled_by CALIBER_PROFILE)
OpenAPI document-request connector is preview in this deployment: it is supported only under CALIBER_PROFILE=reference. Set CALIBER_PROFILE=reference and start from a configuration that passes the profile to use it as a supported path.

release_evaluation.message  (reason_code not_reference_profile, disabled_by CALIBER_PROFILE)
Workspace release evaluation is preview in this deployment: it is supported only under CALIBER_PROFILE=reference. Set CALIBER_PROFILE=reference and start from a configuration that passes the profile to use it as a supported path.
```

**A reference process whose configuration breaks the profile.** Normally such a process refuses to start, so this is the
answer of a configuration the report is asked about, with the codes the profile checks (not a configured value):

```text
reference profile refused (1 violation):
- csrf_enabled is false; set CALIBER_CSRF_ENABLED=true for the reference profile [csrf_disabled]

document_request_connector.message  (reason_code reference_profile_violations, disabled_by profile)
OpenAPI document-request connector is unavailable: this configuration breaks the reference profile (csrf_disabled). Change the settings named in the startup refusal.

release_evaluation.message  (reason_code reference_profile_violations, disabled_by profile)
Workspace release evaluation is unavailable: this configuration breaks the reference profile (csrf_disabled). Change the settings named in the startup refusal.
```

**A pin no longer matches the code.** The claim is withdrawn until a reviewer confirms the scenarios again; no setting
changes it (`disabled_by` is `profile`):

```text
document_request_connector.message  (reason_code connector_capability_mismatch; connector_not_admitted if the record is gone)
OpenAPI document-request connector is unavailable: the registered transport is not the one the reference profile admits (connector_capability_mismatch).

release_evaluation.message  (reason_code evaluator_evidence_mismatch)
Workspace release evaluation is unavailable: the evaluator or its evidence shape is not the one its scenario evidence covers (evaluator_evidence_mismatch).
```

**The release path, one condition at a time.** Recovery is the variable in `disabled_by`. (`CALIBER_EVAL_PROVIDER=auto`, the
Docker stack's default, is resolved when the settings load: to `mlflow` when a real language-model provider is configured and
to `fake` otherwise, so the report never sees `auto`.)

```text
release_evaluation.message  (release area on, evaluator off: evaluator_disabled)
Workspace release evaluation is unavailable in this deployment. Set CALIBER_WORKSPACE_RELEASE_EVALUATOR_ENABLED=true to enable it.

release_evaluation.message  (CALIBER_EVAL_PROVIDER=fake: simulated_provider)
Workspace release evaluation is preview while its scores are simulated. Set CALIBER_EVAL_PROVIDER=mlflow so the evaluation is a real one.

release_evaluation.message  (CALIBER_EVAL_PROVIDER set to anything but mlflow or fake, a typo or a padded value for example: eval_provider_unrecognized)
Workspace release evaluation is preview: CALIBER_EVAL_PROVIDER is not a provider this build supports. Set CALIBER_EVAL_PROVIDER=mlflow so the evaluation is a real one.

release_evaluation.message  (CALIBER_BACKGROUND_TASKS_ENABLED=false: background_tasks_disabled)
Workspace release evaluation is unavailable: the release evaluator runs in a background loop and CALIBER_BACKGROUND_TASKS_ENABLED is false. Set CALIBER_BACKGROUND_TASKS_ENABLED=true to enable it.
```

**The connector, one condition at a time:**

```text
document_request_connector.message  (CALIBER_BACKGROUND_TASKS_ENABLED=false: background_tasks_disabled)
OpenAPI document-request connector is unavailable: the effect reconciler runs in a background loop and CALIBER_BACKGROUND_TASKS_ENABLED is false. Set CALIBER_BACKGROUND_TASKS_ENABLED=true to enable it.

document_request_connector.message  (CALIBER_EFFECT_RECONCILER_ENABLED=false: reconciler_disabled)
OpenAPI document-request connector is unavailable: the effect reconciler is off, so an effect whose outcome was never recorded stays blocked. Set CALIBER_EFFECT_RECONCILER_ENABLED=true to enable it.
```

A `supported` adapter's `message` and the capability block it sits in:

```text
document_request_connector.message
OpenAPI document-request connector is supported: the reference profile admits this transport by its pinned implementation, capability record and configuration.

release_evaluation.message
Workspace release evaluation is supported: the evaluator and its evidence shape match the scenario evidence.
```

**MCP servers and OpenAPI integrations.** With the integrations area on, the reference profile reports MCP servers as
unavailable and OpenAPI integrations as preview; nothing promotes either:

```text
mcp_servers.message  (reference profile, area on: mcp_not_in_reference_profile, disabled_by profile)
MCP servers are unavailable under the reference profile: it lists no MCP command or host, so no server is reachable.

mcp_servers.message and openapi_integrations.message  (development profile, area on: no_support_evidence, disabled_by profile)
MCP servers are preview: no scenario evidence supports them in the reference profile, and no setting changes that.
OpenAPI integrations are preview: no scenario evidence supports them in the reference profile, and no setting changes that.
```

The capability report reads, for a client or the web app, `data.adapters.<adapter id>` with `status`, `kind`, `label`,
`reason_code`, `disabled_by`, `message` and `evidence`. Read it instead of probing for a `404`. `data.features` is
unchanged. The web app's navigation renders these values (#771): a `Supported`, `Preview` or `Unavailable` chip with the `message` above
as its description on MCP Servers, OpenAPI Integrations and Release candidates, and a "Support status" panel that lists all four adapters,
including the connector, which has no page. An unavailable adapter that is listed is a disabled item, and anything the server does not
report as supported reads as `Preview`. A visible entry grants no authority; see
[how the web app renders it](../operate/reference-profile.md#capability-exposure-what-is-supported-b15-support1-755).

## Known limits

These are true of `main` today and are deliberate; each one that has an owner names it.

- **The worker dispatches only two connectors.** In the reference profile a production run reaches `document_request`
  (through a governed agent step) and `http` (through a webhook or API-request node at operator-listed URLs). The
  `registered_tool`, `openapi_http` and `mcp` connectors have no lookup by key the platform can call, so a protected
  (non-read) call that would use one is refused `no_authoritative_verifier` before its proposal is recorded; read-class
  calls still run inline. A protected call the *model* chooses is proposed (in a governed agent step) or refused (in every
  other governed loop), never run directly; a plan a caller builds by hand, with no governed context, is not governed
  ([Model-selected tool calls](../operate/reference-profile.md#model-selected-tool-calls-b046-768)).
- **A permanent unknown has a narrow operator exit.** A downstream that never shows a request and cannot prove its
  absence leaves the proposal `unknown` until an execution-owner admin supplies an evidence reference and attests
  `failed` through the #1054 endpoint. The action is audited, may not attest `committed`, and wakes a parked run;
  the [recovery runbook](../operate/recovery-runbook.md) defines the contract.
- **An unknown webhook outcome fails the run.** A webhook or API-request node whose effect ends `unknown` (a timeout
  after the send, a 5xx, a 2xx the lookup cannot see yet) fails its run; unlike a governed agent step it does not park.
  The reconciler settles the proposal by the listed lookup without sending again, but settling does not resume the failed
  run. Read the proposal before running such a workflow again: a new run is a new effect with a new key. Settling the
  proposal of an ended run, and withdrawing an approved decision that was never consumed, are #1074. A run that is woken
  after a settlement finds its one effect already `committed` and fails with `HTTP effect was already recorded as committed;
  not run again`, because the response of the original send was never durable.
- **A revoked approver, a lost lease or an unbound connector fails the run** with the proposal left `proposed` (nothing was
  sent); settling that proposal is #1074.
- **A tool node's own `requires_approval` gate still asks first**, and a governed effect then asks the exact-intent
  question, so such a node is approved twice.
- **A versioned proposal is sent only through `document_request`.** The platform has no read of its own for another
  connector's target, so a versioned proposal for any other connector is `version_unverifiable`.
- **The `http` read-back is the operator's endpoint.** A `lookup_url` that is not `https` is refused at startup
  (`http_lookup_cleartext`) because the lookup carries no credentials and its answer commits the effect; a `404` from it is
  never proof that an effect did not happen.
- **Acceptance gates completion for `document-request` effects only.** In the reference profile a run is `completed` only
  when every `document-request` effect it proposed has an `accepted` decision (#791): the platform reads the downstream's
  own record of the case when the run finishes, and a run whose effect was committed but whose recorded state is wrong, or
  could not be read, is `failed` (`acceptance_rejected`). An effect of another connector has no criteria set, so a run with
  only such effects carries no acceptance information, and runs that finished before this are not relabelled. A document
  request must carry the case version it was proposed against, or its run is rejected.
- **Fifteen protected-effect rows are not routed.** The inventory marks them `dispatch`, but no connector routes them yet,
  and the matrix records them as `unrouted` instead of claiming otherwise. They are not refused in the reference profile
  either, because their reference-profile behaviour has not been decided:

| inventory rows | what they are | owner |
| --- | --- | --- |
| EP-009, EP-010, EP-013, EP-051, EP-059 | knowledge-build runs, review-queue enqueue (a platform-database write), output-bucket and object-store writes | #1086 |
| EP-070, EP-071, EP-072, EP-075, EP-076, EP-077 | prompt and release publishing | #1087 |
| EP-017, EP-081, EP-090 | the synchronous run route, Aria's `run_workflow` tool, and a path with no production caller | #1088 |
| EP-034 | Aria's published-tool call | #1063 |

- **Generated Python can still reach a client.** The runnable export routes answer `409` in the reference profile, but the
  generated text is sealed into the compiled bundle and can leave through compile, get and publish. That is part of #1088;
  do not read the two `409`s as a guarantee that no generated Python reaches a client.
- **Governed effects outside the worker's own graph.** Synchronous version runs and the sub-workflows of a worker run
  have no execution-owner lease and bind no governed context, so the reference profile refuses their protected nodes (and
  an agent in them that can reach a mapped tool); the development profile runs them on the legacy guarded path.
  Evaluation, gate and Aria previews are different: they run as previews, with protected tools stubbed, so that refusal
  does not apply. Refinement and calibration replays are not previews: an agent bound to a mapped tool (or handing off to
  one) is refused in the replay, so evaluating or calibrating such a workflow scores an error in the reference profile
  until the replay is given a manifest without that agent.
- **A governed effect inside a fan-out, loop, error boundary, handoff or `parallel` branch** cannot park the run: the run is
  refused up front (agents) or fails (effects).
- **The capability `adapters` map is an owner-confirmable decision.** Which adapters can be `supported`, and which scenarios
  pin them, is `caliber.features.ADAPTERS` and `SUPPORT_EVIDENCE`. Adding an adapter, or promoting `http` or a
  registered-tool path, is a reviewed change to those tables with its own scenario evidence, and then a change to this
  page; the owner confirms that decision, and nothing in this page or in a setting makes it.
- **MCP servers stay out of the reference profile.** The profile lists no MCP command or host
  (`caliber.profile.REFERENCE_MCP_ALLOWLIST` is empty), including the first-party PostgreSQL servers; adding an entry is an
  ADR 0001 change.
- **Not enforced by the process.** The single tenant and region, the object store, at least two projects, origin rules, the
  database role and cookie flags are stated in ADR 0001 and listed under
  [Not enforced yet](../operate/reference-profile.md#not-enforced-yet); a configuration that passes the profile is the
  minimum for the supported shape, not the whole of it.
- **Bounded historical replay is a read-only comparison, not the Beta evidence package.** B09.3 records immutable,
  project- and principal-scoped model/read observations with content digests; B09.4 adds
  `POST /workflow-runs/{run_id}/replay`, which returns HTTP 200 only for a bounded, supported historical comparison.
  Missing, tampered, foreign, malformed, unsupported or over-bound observations refuse with no live fallback. It does not
  claim that the B14 evidence package exists; see the [reference-profile replay notes](../operate/reference-profile.md#bounded-historical-replay-b094-805).

## What stays from the alpha

The alpha pages describe `0.1.0a1` and stay as written, except where a sentence there contradicted `main` and was
corrected. Everything the alpha scope says about the default install still holds: the default profile supports no
adapter, the preview areas stay behind their flags (Aria ships on), and enabling a flag accepts that area's preview
behaviour and promotes nothing. What the Beta adds is the per-adapter answer above, the reference profile, and the refusal
codes, none of which changes a default.

## Related

- [Reference profile](../operate/reference-profile.md) for the settings, the refusal catalogue and the resolution order of
  the capability report.
- [Reference profile recovery runbook](../operate/recovery-runbook.md) for an unknown effect, a lost owner or a backlog.
- [Alpha scope and known limitations](alpha-scope.md) and [Alpha install](alpha-install.md), the alpha record.
- [REST API reference](../api/reference.md) for `GET /capabilities`.
