# CALIBER repository-wide product and architecture review

**Review date:** 2026-07-27

**Reviewed baseline:** clean repository state at
`e4f9cb90134358f93bd6a484010a9a3d29d988db` plus the complete reviewed working
tree on 2026-07-27. The working tree contains coordinated but uncommitted product,
runtime, deployment, documentation, and test changes. This report therefore calls
them **reviewed-workspace changes**, not a new committed release.

**Product target used for scoring:** a self-hosted, single-organization platform
for trusted developers and technical operators. Enterprise-suite requirements are
explicitly out of scope: organization/team administration, SSO/SCIM, multi-tenant
isolation, compliance certification/evidence, segregation of duties, enterprise
collaboration, and multi-region/high-availability guarantees. Their absence is
documented only as a scope boundary and does **not** reduce the maturity score.

This exclusion does not remove baseline production requirements. Any
network-reachable deployment still needs real authentication, safe secrets, protection
from command execution and SSRF, effect-safe Preview and workflow-target evaluation,
reliable retries, trustworthy release evidence, authenticated published APIs,
rollback, constrained filesystem/object-storage capabilities, and actionable
monitoring.

**Scope:** React frontend, Starlette APIs, database model, workflow compiler/runtime,
background workers, evaluation and refinement systems, storage, observability,
deployment assets, tests, product documentation, all 16 numbered cookbooks, and
their generated training material.

## How to read this report

This document records the original audit history and the present independent
implementation review. It is ordered conclusions-first so historical diagnostics
cannot be mistaken for current behavior.

| If you want | Read |
| --- | --- |
| The verdict and why | [Executive summary](#executive-summary), [Overall maturity assessment](#overall-maturity-assessment) |
| What is true *now*, after accepted fixes | [Current implementation state](#current-implementation-state-after-this-independent-pass) |
| What was actually run, and what that proves | [Verification](#verification) |
| The findings themselves | [§1 Critical correctness and security findings](#critical-correctness-and-security-findings) (C1–C10) and §2–§11 |
| What to do about them, in order | [§12 Prioritized roadmap](#12-prioritized-roadmap), [§13 Implementation sequence](#13-concrete-implementation-sequence) |
| Cookbook-by-cookbook status | [Appendix A](#appendix-a--cookbook-continuity) |
| Superseded verification history | [Appendix B](#appendix-b--verification-history-and-superseded-passes) |

Findings carry an explicit state: **[Remediated in the reviewed workspace]**,
**[Partly remediated]**, or no marker for open. Scores are risk-adjusted reviewer
judgements, not test coverage.

## Executive summary

> **Can CALIBER realistically enable developers to build, test, evaluate, deploy,
> and operate production-grade AI agent systems with a predominantly
> low-code/no-code experience?**

**Not yet for production-grade end-to-end operation—even with enterprise readiness
removed from scope. Yes for a predominantly low-code build, test, inspect, and
controlled self-hosted execution path.** CALIBER is now more than a canvas or an
API collection: the reviewed workspace closes several concrete no-code bridges
that the previous report correctly identified. It is a credible **low-code agent
engineering studio and lifecycle control plane**, but it still cannot carry a user
from idea to production without security, release, and operations engineering.

The strongest shipped capabilities are real, not mock UI:

- a typed model with 29 registered node kinds plus manifest/configuration support
  for GraphRAG retrieval, structured ports/output schemas, and manual, event, and
  cron triggers;
- a polished graph editor with 13 starting templates, safe import/clone preflight,
  dependency validation, and a mounted project/workspace selector;
- durable workflow versions, validation, preview, queued execution, checkpoints,
  retry/resume, run events, tool-call details, memory inspection, artifacts, and
  trace-oriented debugging;
- standalone Agent configuration list/detail/preflight/history pages plus
  `/me`-gated admin create/edit/status/delete controls and non-admin read-only UX,
  while honestly stopping short of immutable agent versions or runtime agent testing;
- content-pinned managed project files selectable in Workflow Studio and resolved
  across direct preview, evaluation, deploy-gate, queued, synchronous, and
  published-service queued execution;
- typed Aria input interactions, schema validation, explicit step skipping, and
  result-to-input references for the shipped capability registry;
- fail-closed MCP command/host policy, runtime/deployment preflight, readiness UX,
  and three operational first-party database MCP sidecars (deployable for the
  self-hosted/dev stack, but not production-safe with their default shared
  control-plane database credential); and
- substantial prompt, skill, knowledge-base, dataset, judge, review-queue,
  evaluation, gateway, audit, and observability surfaces, backed by a broad test
  suite.

The implementation work was directionally rational and materially improved the
product. It also has clear limits. The production claim is still blocked by
correctness and security defects, not merely by missing polish:

1. **The shipped login is a client-side `admin/admin` demo and the backend trusts a
   browser-supplied identity header.** There is no shipped server-validated
   session/token or enforced trusted-proxy boundary. Built-in enterprise SSO is
   not required for the scoped target.
2. **MCP secret readback is contained, but secret management is not complete.**
   Literal leaves in MCP `env`, `headers`, and `auth_config` are now write-only in
   API responses, UI edits, MCP history, and generic audit list/export; safe
   environment references remain visible and PATCH can preserve hidden values.
   Literals are still stored as ordinary JSON for runtime use, however, with no
   durable encrypted/reference-backed resolver, deployment binding, rotation, or
   revocation lifecycle.
3. **Resource scoping remains inconsistent.** This pass scopes service management,
   Test Set example browsing, and review-item add/submit through their visible
   parents, but multiple other detail and mutation routes still use unscoped
   primary-key lookups. Under the single-organization target this remains an API
   integrity and accidental wrong-resource mutation risk.
4. **The workflow deploy gate now fails closed on missing, empty, or archived data,
   orders bounded samples deterministically, and invokes Preview.** It still uses a
   fake agent executor and measures only successful completion—not expected output,
   judge quality, regression, cost, or latency. Moreover, `prod` does not require a
   quality gate or pending human promotion by default (`GATED_ALIASES` is empty).
   This is safer containment, not release evidence; MCP's separate external-boundary
   rule still defaults to `prod`.
5. **Ordinary Preview refuses unisolated capability nodes, with one important new
   safe path.** A `file_input` carrying a content-pinned managed project-file
   snapshot is now allowed and verified by project, row ID, object version, size,
   metadata digest, and actual byte digest. Legacy host paths, folders, buckets,
   Python, MCP-resource, external-app, webhook, and API-request nodes remain
   refused. Normal live runs still lack a universal effect broker, outbound egress
   control, and an effect ledger.
6. **Human-approval behavior is internally inconsistent.** The scoped product does
   not require enterprise role/quorum/segregation-of-duties workflows, but the UI
   exposes those controls without enforcing them. Timeout behavior and queued versus
   synchronous execution also disagree. Unsupported controls should be removed or
   made truthful.
7. **New one-click workflow services are now authenticated by default, and the UI
   can create, list, copy once, and revoke scoped bearer tokens.** Parent workflow
   visibility is checked on service/token administration and audit actors identify
   the matched token. Studio now downloads the generated OpenAPI document through
   a parent-scoped CALIBER-authenticated route while the external document remains
   bearer-gated. Legacy or explicitly public services remain possible, and there is
   still no rate-limit, quota, CORS, secret-vault, or production identity boundary.
8. **Registered extension code is not isolated from the control plane.** Registered
   tools, their test path, and external-app entrypoints import and invoke installed
   Python callables in-process. The local source-code sandbox now adds POSIX CPU,
   address-space, file-size and descriptor limits, private work directories,
   process-group termination, AST restrictions, empty environment, and output/
   wall-time caps. That is useful containment, not a container/VM/kernel boundary.
9. **The previous arbitrary-stdio MCP RCE chain is remediated in the reviewed
   workspace, but the production boundary remains operator-dependent.** Every MCP
   test/invocation now applies an exact executable/module or host allowlist and a
   sanitized environment; prod deployment and runtime paths require an external
   boundary. Three database presets use non-root, read-only, capability-dropped
   sidecars. However, their default `CALIBER_MCP_POSTGRES_URL` is the same `caliber`
   database/credential used by the control plane, so an allowed DDL/DML tool can
   still damage CALIBER state. The sidecar host list is operator attestation, remote
   HTTPS does not itself constrain the remote service, and root-only dependency
   inspection does not recursively prove every deployed subworkflow dependency.
10. **Operations stop at observability.** There are useful traces and metrics, but no
   alert policies, configurable SLOs, continuous evaluation, drift monitoring,
   incident workflow, detailed agent/workflow health, trustworthy queue/worker
   readiness, or demonstrated single-instance failure recovery. Lease recovery
   also restarts an interrupted workflow from the beginning without an effect
   ledger or platform idempotency key, so a crash can duplicate external side
   effects.

The latest work also adds safe new-workflow import/clone semantics, first-class
managed files, typed Aria execution, routed Agent configuration, MCP readiness and
sidecars, a hardened local tool subprocess, queued file binding, synchronous HITL
rejection instead of silent pass-through, and runtime MCP preflight. These are real
closures. They do **not** create a secret vault, general extension sandbox, complete
agent lifecycle, evidence-grade deployment gate, continuous evaluation service, or
production operations system.

Evaluation-row tags and scorer/model/target identity are now visible, weighted and
raw values are explained, incomplete rows are excluded from scorer aggregates, and
all-zero effective weights return a controlled 400 before prediction. Users can
browse the set active as of a dataset version while separately inspecting additions,
and baseline choices require the same dataset version and scorer suite; target,
subject, and model identity are disclosed but not enforced as compatibility gates.
Remaining evaluation limits include synchronous/truncated execution, mutable
judge/provider inputs, no cryptographic evidence bundle, no durable coverage schema,
and no continuous quality/cost/latency gate.

The scope-adjusted assessment is now **3.1/5**. The increase is justified by
working product paths, not by counting code: Agents, import/clone, managed files,
typed Aria, and deployable first-party MCP are no longer absent. The score remains
risk-capped by spoofable identity, inconsistent authorization, in-process
extensions, normal-run egress/effect risks, completion-only release evidence, and
incomplete operations.

The correct product label is **late alpha / early-beta candidate for trusted
self-hosted technical teams**, not production-ready. Current defaults and several
control-plane paths must not be presented as production-safe behavior.

## Overall maturity assessment

Scale used here: **0 absent, 1 prototype, 2 partial, 3 usable with material gaps,
4 strong, 5 production-complete**. Scores are reviewer judgments, not test
coverage, and the overall score is risk-adjusted rather than an arithmetic mean.

| Dimension | Score | Assessment |
| --- | ---: | --- |
| Visual workflow composition | **4.1/5** | Broad typed primitives, templates, validation, managed-file selection, and a strong graph editor. Some advanced fields still require JSON or code-like expressions. |
| Prompt, skill, tool, agent, and knowledge engineering | **3.7/5** | Prompts, skills, and KBs are deep; routed Agent configuration and preflight now exist. Reusable custom tools still require importable Python, and Agent history is audit-backed rather than immutable/versioned. |
| Developer debugging and run inspection | **4.0/5** | Best part of the product: run graph, events, checkpoints, retries, tool calls, memory, outputs, artifacts, and trace views. Trace-ID persistence is repaired in the reviewed workspace; deterministic replay remains incomplete. |
| Testing and evaluation | **2.9/5** | Real datasets, judges, weighted scorecards, compatible baselines, calibration, active-as-of browsing, and some regression gates. There is still no durable large async eval, immutable run bundle, continuous eval, CI product-quality gate, or cost/latency gate. |
| Deployment and release management | **2.5/5** | Versions, aliases, rollback, authenticated API publishing, MCP deployment preflight, and first-party sidecars exist. `prod` does not require the fake/completion-only quality gate or human promotion by default; deployment-scoped secrets and trustworthy evidence remain absent. |
| Operations and monitoring | **2.7/5** | Useful traces/metrics, token/cost/latency summaries, SSE, audit, system services, and one precise API/database health poll. Actionable alerts, deep readiness, queue/worker operations, drift, and failure recovery remain incomplete. |
| Platform UX | **3.7/5** | Agents, import/clone, project selection, managed files, typed Aria forms, MCP readiness, evaluation, and service-token states are discoverable. Fragmented lifecycle idioms, secrets/alerts/benchmark gaps, raw IDs, and giant workspaces remain material debt. |
| Production safety and access control | **2.2/5** | Arbitrary stdio execution is now fail-closed, managed files are pinned/scoped, service auth defaults are safer, and Preview containment improved. Identity remains spoofable, literals remain stored, normal-run effects/egress are unbrokered, and registered extensions execute in-process. |
| Architecture and operability | **3.1/5** | Typed domain/runtime, durable SQL state, managed-file protocol, MCP policy/sidecars, storage/event abstractions, and extensive tests are strengths. Transitive dependency policy, at-least-once effects, in-process extensions, hard-coded modes, and scoping defects remain. |
| End-to-end low-code/no-code lifecycle | **3.2/5** | A user can build, clone/import, bind managed documents, collect typed Aria inputs, test, debug, inspect evidence, and publish an authenticated API predominantly in the UI. Production security, release evidence, and operations still require manual engineering. |

**Risk-adjusted overall: 3.1/5, late alpha / early-beta candidate for trusted
self-hosted teams.** Enterprise readiness is not scored; baseline production
safety is. The arithmetic mean is higher than the reported overall, but the 2.2/5
safety dimension and 2.5/5 release dimension cap the judgment: spoofable identity,
unbrokered effects, in-process extensions, and false release evidence remain
blockers even though the workflow IDE is strong.

## Current implementation state after this independent pass

### Accepted remediation and actual closure

This table records what is now true **and** the remaining contract boundary. It is
not a list of fully closed product areas.

| Area | Verified current behavior | Residual limit | Evidence / regression coverage |
| --- | --- | --- | --- |
| Agent configuration workspace | `/agents` and `/agents/:agentId` now provide searchable inventory, admin-scoped registration/mutation, detail/edit, enable/disable, skill-reference and experiment-binding preflight, audit-backed revision history, and permanent deletion. Both pages fetch `/me`, hide create/edit/status/delete controls from non-admins, and explain the read-only state | These records configure the refinement fleet, not a complete agent lifecycle. There is no immutable version, restore/rollback, soft archive, runtime test, deployment, or health view. Permission-aware rendering fixes the earlier 403-discovery UX, but `/me` still rests on C1's spoofable demo identity and is not a trustworthy production identity boundary | `src/pages/Agents.tsx`, `src/pages/AgentDetail.tsx`, `src/pages/__tests__/agents.test.tsx`, scoped agent route changes |
| Workflow import and clone | Inventory actions import YAML/JSON or clone a selected saved version. Preview validates graph/tool/skill/KB/dataset/MCP/subworkflow dependencies and every managed-file snapshot against the selected visible project row/ref/digest/size/object version, rejects inline secrets/unresolved dependencies, then creates a fresh ID, actor-derived owner/project scope, and v1 draft | Dependencies and valid managed-file refs remain links; they are not copied/remapped. MLflow prompt aliases are explicitly unverified, and portable cross-project import still needs a dependency/file/secret mapping-and-copy flow | `components/workflows/WorkflowImportDialog.tsx`, `routes/workflows.py`, `tests/test_routes_workflow_import.py`, `src/pages/__tests__/workflows.test.tsx` |
| Managed project files | Object Store can copy an object into the active File Directory; Workflow Studio selects the resulting immutable snapshot. Direct preview, workflow evaluation, deploy-gate replay, queued execution (including published-service invocation), and synchronous execution verify project, row ID, ref, object version, metadata digest, byte digest and size before use; queued `input_files` materialize in the worker | Managed snapshots require a project. Binding is still absent from calibration/refinement, standalone export runtime, and transient assistant-draft execution; it does not propagate into nested child manifests or cover dataset-manifest refs. `input_files` is request-time materialization, not dynamic graph mapping, and managed refs do not support folders or streaming. Legacy host-path and bucket/folder effects remain unbrokered for live runs | `workflows/file_tools.py`, `storage/service.py`, `routes/object_store.py`, `routes/workflow_versions.py`, `orchestrator/workflow_run_worker.py`, `routes/evaluations.py`, `routes/services.py`, `workflows/promoter.py`, associated file/runtime tests |
| Aria typed execution | Missing capability inputs pause the plan with a schema-driven form; answers merge into the step, validate the registry schema, then re-enter the normal risk gate. The same form offers **Skip step**: the UI submits `approved:false`, the backend marks the step skipped, and execution resumes. Deterministic `$from_step` references connect declared outputs such as a created queue ID to a later step, and async calibration can still park/poll/resume | The heuristic planner still selects capabilities by literal domain words, supports only the small registered capability set, and asks for arrays/objects as JSON. It does not discover trace/workflow/agent IDs or author arbitrary workflows/prompts/tools | `assistant/capabilities.py`, `assistant/plans.py`, `assistant/executor.py`, `components/aria/planView.tsx`, Aria backend/UI tests |
| MCP execution policy and first-party DB integrations | Test, discovery, calibration, direct invocation, queued/synchronous runtime, promotion and promotion approval apply the same command/host/discovered-tool policy. Stdio is fail-closed to configured executable/Python target allowlists; runtime exposes readiness/blockers. PostgreSQL, pgvector, and AGE presets target separate hardened Compose sidecars | The default sidecar URL uses the same `caliber` database and credential as the control plane; container isolation cannot stop an allowed SQL/DDL tool from damaging that database. Production needs a separate database and least-privilege role. Five external presets need operator provisioning; sidecar trust is operator attestation, rate limits are process-local, and child inspection is not transitive | `mcp_policy.py`, `mcp_gateway.py`, `routes/mcp_servers.py`, `routes/workflow_deployments.py`, `deploy/caliber/compose.yaml`, MCP tests |
| Local source-code sandbox | Python source-tool execution now adds private workdirs, empty environment, `-I`, AST private/dunder rejection, POSIX CPU/address-space/file/descriptor limits, byte-capped output, hard timeout, and process-group termination | It remains same-host subprocess containment and explicitly is not a production sandbox. Registered tools, registered-tool tests, and external-app entrypoints still execute in-process | `tool_sandbox/service.py`, `tool_sandbox/_runner.py`, sandbox tests |
| Evaluation visibility | Non-admin list no longer crashes: `owner_column()` supports `created_by`. Detail now resolves **through** `get_visible()`, so list and detail share the same default visibility predicate — a project header alone no longer unlocks another owner's run, and the creator's project-scoped rows outside the active project are no longer readable | List can additionally apply its explicit `only=<tier>` view filter. Scoping still depends on the client-supplied `X-CALIBER-Project` header and the demo identity of C1; that is an identity problem, not a default-predicate problem | `tests/test_scoping.py`, `tests/test_routes_evaluations_visibility.py` (incl. real create → list → detail round trips) |
| Dataset versions | Evaluation creation rejects future versions. `version=N` remains “added in N”; `as_of_version=N` uses the same active-membership predicate as evaluation/restore, with mutual-exclusion and range validation. The UI presents the paginated snapshot view, keeps later-retired members read-only, and makes restore explicit | The browser defaults to a 500-row page, evaluation has lower caps, and no cryptographic run/content digest or pre-truncation inventory proves exhaustive identity | `tests/test_routes_evaluations_reproducibility.py`, `tests/test_routes_eval_datasets.py`, `src/pages/__tests__/eval-dataset-detail.test.tsx` |
| Weights/tags/evidence | Loading and persisted rows retain both. Evaluation Detail renders tags, judge labels, prediction and error/incomplete state, target/subject/model, coverage, and an explicit weighted-versus-raw legend | Tags still lack grouped slice metrics; coverage is UI-derived from immutable rows rather than a durable aggregate schema | `tests/test_eval_scorecard_weighting.py`, `tests/test_routes_evaluations_reproducibility.py`, `src/pages/__tests__/evaluations.test.tsx` |
| Partial scorer failure and zero weights | Every failing scorer is reported; healthy raw scores remain for diagnosis, but an incomplete row has score 0, cannot pass, and is excluded from all per-scorer aggregates. A non-empty all-zero effective-weight set returns 400 before prediction | Aggregate schemas still do not persist valid-row/weight denominators for future asynchronous consumers | `tests/test_eval_scorecard_weighting.py`, `tests/test_routes_evaluations_reproducibility.py`, `tests/test_assistant_agent_tools.py` |
| Baseline comparison | The UI restricts baselines to successful runs with the same dataset/version and scorer suite, and discloses target, subject, and model identity | It does not reject a target/subject/model mismatch or compare threshold/sampling policy, and remains an ad hoc UI comparison rather than a controlled release gate | `src/pages/__tests__/evaluations.test.tsx` |
| Workflow trace linkage | Queued and synchronous runs persist `result.mlflow_trace_id`; the run trace panel and trace-to-run lookup can resolve it | Replay is not pinned to all resolved artifact/provider/configuration versions | `tests/test_workflow_run_trace_linkage.py` |
| MCP and provider secret readback | Provider reads return presence/fingerprint. MCP literal leaves are a write-only sentinel in list/detail/history/audit export; PATCH preserves unchanged leaves and safe env references remain visible. The UI omits unchanged sensitive maps and removes an obsolete token-env mapping when token auth is disabled | MCP literals still exist in ordinary DB JSON for runtime use; URI/command arguments are outside this three-field contract; no encrypted/reference-backed secret lifecycle, rotation, revocation, or consumer graph exists | `tests/test_mcp_servers.py`, `tests/test_mcp_servers_routes.py`, `src/pages/__tests__/mcp-servers.test.tsx` |
| Workflow service publishing | New services default to `auth_required=true`; the publish UI sends that choice, manages one-time bearer tokens, warns on explicit/legacy public state, scopes management through the parent workflow, and records the actual token/anonymous actor. OpenAPI download uses a parent-scoped authenticated Studio route without weakening the external bearer-gated route | Explicit public endpoints and legacy rows remain possible; no rate limits, quotas, CORS policy, durable vault, or trustworthy platform identity | `tests/test_routes_services.py`, `tests/test_cov90_routes_services.py`, `src/pages/__tests__/workflow-service-tab.test.tsx` |
| Review queue submission | Add/submit resolve the visible parent, archived queues reject writes, answer types/options are enforced, and an atomic pending→submitting claim prevents duplicate concurrent writeback; a failed external write restores pending | A crash can strand `submitting`; external success followed by local DB failure has no cross-system idempotency key; `reviewers`/`assigned_to` remain descriptive rather than enforced | `tests/test_routes_review_queues.py` |
| Preview and deploy-gate containment | Each `execute()` preflights its current IR before tracing/interpreting. Nine unconditionally blocked dedicated node types plus legacy host-path `file_input` fail closed; content-pinned managed `file_input` is the scoped exception. Deploy gates use Preview, deterministic ordering, and fail closed for missing/empty/archived datasets | Registered tools explicitly allowed in Preview and knowledge queries keep existing policy. The gate still uses a fake executor and completion-only metric, normal runs lack an effect broker, and nested managed-file binding is incomplete | `tests/test_workflow_preview_preflight.py`, `tests/test_workflow_promoter.py`, managed-file runtime tests |
| Shell health | AppShell owns one health-query observer and passes the result to Sidebar and TopBar; visible labels and ARIA text say only “API + database reachable,” and sustained polling is regression-tested | `/health` remains API/database liveness only, not worker, scheduler, queue, storage, MLflow, or provider readiness | `src/components/__tests__/sidebar-health-footer.test.tsx` |
| Test/CI isolation | Each test process/worker receives unique temporary MLflow SQLite and artifact roots; async trace export is disabled and roots are cleaned. Security audit bootstraps `setuptools>=83`, gitleaks is not skipped after an earlier failure, and the helper is invoked in an isolated CI environment | These changes repair the harness and workflow definition; they do not by themselves prove a new green remote CI run | `tests/conftest.py`, `tests/test_test_harness_isolation.py`, `.github/workflows/ci.yml`, `scripts/run-supported-python-security-audit.sh` |
| Unknown route UX | The wildcard renders a real Not Found view with a dashboard link | It is a client-rendered route boundary, not evidence of server HTTP-404 behavior | `src/pages/__tests__/app-shell-e2e.test.tsx` |
| Cookbook prose | Generated 03/08/10 material better reflects shipped side-effect, workflow-target evaluation, and alignment paths | Source/generated documentation still conflicts in the places catalogued in §10 | Generated artifact diff plus existing documentation checks |

Large product decisions remain open: identity, durable reference secrets,
systematic resource authorization, a real effect broker for normal/live execution,
evidence-grade release gates, canonical HITL, isolated extensions, transitive
dependency/capability policy, idempotent side effects, production topology, and
operations. Arbitrary stdio command execution is no longer an open default-path
finding; maintaining its fail-closed policy is now a regression requirement.

### Recommendation decision ledger

Every recommendation in the prior report was treated as a hypothesis. “Useful” is
not equivalent to “complete,” and “important” is not automatically small enough for
this pass.

| Change or decision | Verdict | Reviewer assessment |
| --- | --- | --- |
| Add routed Agent list/detail/configuration controls | **Accepted with a narrower product label** | The pages close the missing inventory/configuration workflow and clearly disclose that preflight is not runtime proof and history is not versioning. They must not be relabeled as a full standalone agent build/test/deploy/rollback lifecycle. |
| Import or clone by trusting source identity, files, or dependencies | **Rejected and replaced** | The implementation generates a fresh ID, derives owner/project from context, creates only a new workflow plus v1 draft, rejects inline secrets/unresolved dependencies, verifies managed snapshots in the selected project, and leaves prompt aliases visibly unverified. Valid refs stay linked rather than being silently copied. |
| Copy all workflow dependencies during clone/import | **Deferred** | Silent copying would break provenance and secret/alias semantics. Current link-preserving import plus explicit dependency mapping is correct; a future portable bundle needs typed mapping and conflict policy. |
| First-class content-pinned managed files | **Accepted on the explicitly bound runtime paths** | Logical ref plus row ID, object version, size and digest verification is a substantial improvement over host paths. Direct preview/evaluation/gate, queued/synchronous execution, and queued published-service invocation are covered; the alternate-runner, nested-manifest, dataset-ref, dynamic-mapping, folder, and streaming boundaries in C9 prevent declaring universal runtime parity. |
| Typed Aria inputs, step skipping, and step-output references | **Accepted and implemented** | Schema-driven collection, validation, an explicit backend-backed **Skip step** action, and durable sibling references close the empty-input and unwanted-step failures for the registered capabilities. Literal-keyword planning, JSON-heavy complex fields, and the small capability catalog remain product limits. |
| Treat a stdio executable allowlist as a production sandbox | **Rejected** | All invocation paths should retain the allowlist and sanitized environment, but local stdio remains containment only. Production aliases correctly require an external boundary. |
| Treat an allowlisted sidecar hostname as independently proven isolation | **Rejected as an overclaim** | The shipped DB sidecars themselves have rational Compose controls, but `managed_sidecar_hosts` is operator attestation. The UI/report must state that fact and retain deployment/runtime preflight. |
| First-party PostgreSQL/pgvector/AGE MCP sidecars | **Accepted for deployable self-hosted/dev connectivity** | They make three catalog paths operational in shipped Compose and correct the earlier `localhost` collision. They are not production-safe with the default shared control-plane DB/credential; use a separate least-privilege target. The other five entries remain operator work. |
| Add resource limits to the local source sandbox | **Accepted as hardening** | POSIX limits, AST restrictions and process-group kill reduce accidental and common abuse. They do not justify executing untrusted code without a container/VM/kernel boundary and do not cover registered in-process extensions. |
| Silently pass approval nodes in synchronous execution | **Rejected and replaced** | A synchronous path cannot persist/checkpoint the queued approval protocol, so it now fails before starting and directs callers to queued execution. This is truthful until one canonical approval engine serves both paths. |
| Complete-row per-scorer aggregates while retaining healthy raw row scores | **Accepted and implemented** | Throwing away valid diagnostics has no value; allowing them into per-scorer aggregates creates survivorship bias. Raw values remain visible, the row fails, and headline overall/pass-rate denominators conservatively penalize it as zero. |
| Equal-weight fallback when every explicit weight is zero | **Rejected and replaced** | It contradicts zero-as-exclusion and silently rewrites displayed weights. The API now rejects the run before prediction with a controlled input error. |
| Persist a new scorer-coverage schema immediately | **Deferred, not rejected** | Current immutable rows let the UI derive truthful coverage without a migration. A durable denominator belongs with the future asynchronous/immutable evaluation schema. |
| Separate active-as-of browsing from additions history | **Accepted and implemented** | This preserves the public `version=N` contract while exposing the same active-membership semantics evaluation/restore consume. The UI remains paginated rather than claiming an exhaustive snapshot. |
| Render tags, scorer identities, errors, weight semantics, target identity, and safer baseline choices | **Accepted and implemented** | Dataset version/scorer compatibility plus disclosed target identity improves interpretation without overclaiming a fully controlled comparison. Full target-policy matching and slice analytics remain deferred. |
| Make service tokens admin-only | **Rejected** | Operators already publish services; requiring a separate global admin for the token needed to use that service makes the UI path incoherent. Operator scope plus parent workflow visibility is the consistent current contract. |
| Default new services to auth and expose token lifecycle | **Accepted and implemented** | This closes an unsafe default without pretending the bearer-token subsystem replaces platform identity, quotas, or a secret vault. |
| Let Studio navigate directly to the bearer-gated external OpenAPI URL | **Rejected and replaced** | A normal browser link cannot attach the one-time service bearer token and returned 401. Studio now fetches the same shared spec through CALIBER auth and downloads a Blob; the external route keeps its bearer contract. |
| MCP API/audit redaction with PATCH preservation | **Accepted as containment** | It stops known readback while preserving runtime compatibility. Replacing stored literals with a durable resolver remains Critical work, not something this patch can honestly claim. |
| Preview preflight refusal for unisolated dedicated nodes | **Accepted as containment** | Blocking before any node in the current IR runs is safer and testable. A nested child is checked on child entry, after safe parent work may have occurred. A partial per-node mock would risk hidden mixed live/dry behavior; a capability broker remains the architectural fix. |
| Invent an arbitrary quality scorer for deploy gates | **Rejected** | No product-defined judge or expected-output contract exists in this gate. The defensible patch is fail-closed deterministic Preview containment; the gate remains completion-only and cannot authorize production. |
| Review state/type/concurrency checks | **Accepted and implemented** | They prevent overwrite and common duplicate writeback without introducing enterprise quorum/SoD machinery. Cross-system exactly-once semantics remain deferred. |
| Implement organization/SSO/SCIM/multi-tenant/compliance/quorum features | **Rejected as out of scope** | Their absence is not scored. Misleading role/quorum UI still must be removed or made truthful; baseline identity and authorization remain mandatory. |
| Full identity, secret vault, effect broker, extension sandbox, production topology, and continuous operations in this patch | **Deferred** | These are valid Critical/High roadmap items, but implementing them as incidental refactors would be speculative, high-risk, and architecturally dishonest. |

### Remaining gaps after this pass

Deliberately still open, in rough priority order:

1. **Identity and resource scoping (C1/C3).** Every visibility fix above still
   rests on a self-asserted `X-CALIBER-User` header and a client-supplied
   `X-CALIBER-Project`. The eval filter is now internally consistent; the
   boundary it enforces is not yet trustworthy.
2. **Evaluation evidence completeness.** Add immutable resolved run bundles,
   durable denominators/slices, asynchronous full-dataset execution, and
   cost/latency/continuous quality policies.
3. **Deep readiness probes (§6).** `/health` remains API + database only even
   though the shell now labels and polls that narrow signal correctly.
4. **Evidence immutability.** No content digest or pre-truncation inventory on an
   evaluation run, so a pinned run is reproducible by convention, not by proof.
5. **Complete backend and remote CI proof.** The converged workspace now has green
   frontend, browser, marked-integration, static-analysis, build, package, and
   Compose checks. Its all-extras Python 3.12 xdist run was stopped after 5,024
   passes, 8 skips, and 6 timing-sensitive failures; all six failures passed in a
   serial rerun, but that is not a completed broad-suite or coverage result. There
   is also no new GitHub Actions result proving artifact upload, dependency audit,
   gitleaks, packaging, and the matrix together.
6. **Transitive execution policy.** Named direct execution paths now bind managed
   files and run MCP preflight consistently, but alternate runners remain outside
   the file binder and child subworkflows resolve live aliases without receiving a
   child resolver/tool set. Deployment and MCP deletion inspection are also
   root-manifest-only.
7. **Release, HITL, isolation, and topology.** Deploy gates remain completion-only,
   manifest approval semantics remain inconsistent, extensions execute in-process,
   and production roles/recovery are incomplete.

## Verification

### Current converged-workspace verification status (2026-07-27)

The Agent, import/clone, managed-file, Aria, MCP, database-sidecar, sandbox,
runtime-approval, and documentation tracks were validated together. Frontend,
browser, marked-integration, static-analysis, build, package, and Compose checks
are green. The broad backend xdist run was not allowed to masquerade as green: it
became pathologically slow and was stopped after 5,024 passes, 8 skips, and 6
failures. Each of those six failures then passed serially, which indicates
parallel contention/timing sensitivity but does not prove the uncompleted suite or
its coverage gate.

Current source inventory was recounted directly: **40** registered route modules,
**316** literal `Route(...)` declarations, **60** top-level models, **28** lazy
routed components, **29** workflow component kinds, **13** workflow templates,
**13** router operators, **266** backend test files, **110** frontend unit/spec
files, and **8** Playwright spec files. Inventory is not a pass result.

| Check | Current-pass status | Evidence boundary |
| --- | --- | --- |
| Full supported-Python backend suite and coverage gate | **Incomplete; not claimed green** | An all-extras Python 3.12 xdist run was stopped after **5,024 passed / 8 skipped / 6 failed**. The exact six failures—two compensation, prompt-lookup timeout, event-bus fanout, workflow refinement, and calibration cases—then passed serially (**6 passed in 27.25s**). No fresh complete-suite coverage gate is claimed. |
| Marked integration suite, including real database MCP sidecars | **Passed with one optional collection skip** | **9 passed / 1 skipped / 5,094 deselected** against live PostgreSQL/pgvector/AGE stdio sidecars. The skip is collection of the absent optional DSPy optimizer module, not a failed connector behavior. |
| Frontend Vitest, TypeScript, ESLint, and production build | **Passed** | **110 files / 1,490 tests passed**; TypeScript, ESLint, and the production Vite build also passed. |
| Browser E2E with MinIO/PostgreSQL/AGE/MCP dependencies | **Passed with AGE opt-in skipped** | **23 passed / 1 skipped** using isolated SQLite plus the documented MinIO dependency. The AGE-specific spec remains intentionally gated by `CALIBER_EXPECT_AGE`. |
| Ruff, format, mypy, package build, and Compose validation | **Passed** | Ruff check/format, strict mypy (**273 source files**), wheel build, three shipped Compose validations, and diff checks passed. A fresh dependency audit, gitleaks scan, and remote GitHub Actions matrix were **not run** and are not implied by this row. |

Historical verified results and focused track results are retained in Appendix B
with their dates/boundaries. The current evidence proves the named checks only; it
does not convert the interrupted backend run into an all-green release candidate.

Still outside this review's execution evidence: real paid LLM/provider calls,
external MCP services, sustained load/soak or multi-process failover, formal browser
accessibility, and penetration testing. Passing future tests will prove encoded
behavior, not the production safety or lifecycle completeness assessed here.

## Review method and evidence boundary

This review distinguishes five states:

- **Shipped:** routed UI, API, persistence, and runtime behavior compose into a
  usable path.
- **Partial:** useful behavior ships, but a required lifecycle step or policy is
  missing.
- **API-only:** backend capability exists without a reachable product workflow.
- **Dormant:** code exists but current constants/defaults make it unreachable or
  unenforced.
- **Absent:** no implementation was found.

The audit inspected:

- the 28 lazy product routes/components in `caliber/caliber-ui/src/App.tsx`,
  including the new Agent inventory/detail routes, plus login redirects and
  wildcard handling, the workflow canvas/inspector/
  debugger, assistant shell, API client, and workspace state;
- the centralized backend registry of 40 route modules and 316 literal
  `Route(...)` declarations under `caliber/src/caliber/routes/`;
- 60 top-level SQLAlchemy domain models in `caliber/src/caliber/db/models.py`;
- workflow manifest, compiler, component catalog, promoter, interpreter, run
  worker, scheduler, memory, tool sandbox, and service publishing paths;
- evaluation, refinement, judge, review, audit, scoping, authentication, secret,
  storage, event, and observability paths;
- the standalone Docker/Compose deployment and the MLflow plugin entry point;
- repository tests and CI configuration; and
- product claims, architecture documents, roadmap, competitive analysis, and all
  cookbook workflows.

This was a repository-grounded architecture/product audit, not a penetration test,
load test, accessibility assessment, or real-provider benchmark. Passing unit
tests are evidence that implemented contracts behave as encoded; they are not
evidence that the encoded contract is product-complete or secure.

## Screens, pages, and components reviewed

| Product area | Reviewed surfaces | Result |
| --- | --- | --- |
| Shell and access | Login, App shell, sidebar, provider banner, local auth, route boundaries | Visually complete demo shell; not a production identity boundary. |
| Overview | Dashboard summary, live event badges, fleet coverage, and assistant execution/publish ratios | Useful high-level reliability entry point; not a configurable SLO/error-budget or per-agent/workflow health dashboard. |
| Prompts | Inventory/workspace, builder, playground, tests, calibration, versions, rollback, bindings | Deep and usable; version/governance semantics differ from other artifacts. |
| Skills | Inventory/workspace, wizard, render/trigger tests, packages, calibration, versions, bindings | Strong authoring path; routed and in-place experiences diverge. |
| Tools | Registry, wizard, detail, schema builder, sandbox, calibration, versions | Good registry UX around an implementation that must already exist in Python. |
| Agents | Inventory, create form, detail/edit, skill/config preflight, audit history, enable/disable/delete | A useful refinement-fleet configuration workspace. The pages use `/me` to hide admin-only mutations and explain read-only access; C1's untrusted demo identity still limits the security value of that gating. It is not immutable versioning, runtime testing, deployment, rollback, or health. |
| MCP | Catalog, setup, discovery, playground, policies, tests, calibration, readiness/blockers | Rich surface with write-only containment and deploy-aware policy. Three database presets run as shipped dev/self-hosted sidecars; production needs a separate least-privilege database. Five external presets require operator provisioning, and literal storage lacks a durable secret lifecycle. |
| Files/storage | Object Store, active File Directory, project selector, immutable import, managed file-input picker, queued inputs, folder/bucket nodes | A content-pinned project-file path composes across the explicitly bound preview/evaluation/gate/queued/synchronous paths, including service-triggered queued runs. Alternate runners, nested/dataset refs, dynamic mapping, folders, and streams remain incomplete. |
| Knowledge/RAG | KB inventory/editor, sources, builds, chunks, query playground, GraphRAG/AGE, calibration, versions | One of the strongest artifact workspaces; provider/storage readiness remains operator-managed. |
| Workflow Studio | Inventory/templates, import/clone dialog, dependency preflight, React Flow editor, managed-file picker, inspector, code view, versions, detail graph | Strong low-code composition and reuse environment; linked dependencies are not a portable bundle. |
| Workflow runtime | Preview, queued/synchronous runs, managed files, events, approvals, checkpoints, retry/resume, memory, artifacts, trace/debug panels | Deep runtime UX. Managed file input is safely previewable; other blocked effects are not simulated. Synchronous approval now refuses rather than bypasses, but canonical HITL, deterministic replay, transitive child binding, live-run isolation, and duplicate-side-effect risks remain. |
| Workflow deployment | Versions, navigation-hidden/deep-linkable deployments and promotions, service publishing, tokens, patches | Core primitives and secure new-service default exist; release gates/default aliases and evidence still bypass production governance. |
| Test Sets | Dataset inventory/detail, additions history, active-as-of snapshot, restore, trace import, MLflow sync | Useful curation; bulk/splits, immutable content digests, exhaustive snapshot proof, and grouped slice semantics remain incomplete. |
| Evaluation | Evaluations, detail scorecards, judges, alignment, review queues | Valuable ad hoc evaluation; not a continuous or release-grade evaluation system. |
| Aria | Assistant panel, plans, typed input interactions, step references, async job polling, drafts, approval/publish flows | Registered capability plans can now collect and execute missing inputs. Literal-keyword planning, small capability breadth, JSON-heavy complex inputs, and lack of discovery keep it guided rather than autonomous. |
| Observability | Trace search/detail/compare, metrics charts, Allure link, system services | Good inspection; lacks alert-to-action operations. |
| Gateway | Endpoints, guardrails, pricing, usage | Useful control-plane visibility; it does not by itself make CALIBER deployment secure or scalable. |
| Release/review controls | Audit log/export, review queues, Releases | Review state/type/concurrency and MCP audit safety improved; release evidence, recovery/idempotency, and advertised workflow-approval behavior remain incomplete. Formal enterprise signoff is excluded. |
| Settings | Assistant, provider, services/runtime inventory, versioning, Allure | Mostly an environment-backed inventory. Provider keys are now write-only in the browser, but updates are process-local and there is no secret lifecycle. |

No production page was found for **Secrets, Alerts/SLO administration, Benchmarks,
or detailed agent/workflow health**. Agents now have routed pages, and
`WorkspaceSelector` is mounted in the TopBar with project creation/selection.
Missing organization/team/role-administration pages are excluded from this
assessment.

## 1. Overall product completeness

### Lifecycle closure

| Lifecycle stage | What works | What prevents a predominantly no-code production path |
| --- | --- | --- |
| Idea and design | Aria chat/plans with typed input collection, prompt builder, workflow templates, Agent configuration, component guidance | Aria's heuristic is literal-keyword based and covers a small capability registry; there is no guided cookbook/solution installer or full standalone agent authoring lifecycle. |
| Build | Visual workflows, safe import/clone, prompts, skills, KBs, managed files, schemas, MCP/API/webhook nodes | Reusable tool implementation needs Python packaging; clone/import links rather than bundles dependencies; complex schemas/conditions/Aria fields still use JSON or expressions. |
| Test | Preview, managed-file preflight, local source sandbox, component test runs, datasets, judges, workflow eval | Preview safely reads pinned managed files and refuses other known unisolated nodes, but there is no effect-broker simulation, workflow assertion suite, server-authoritative component record, full-dataset async runner, or reusable suite policy. |
| Evaluate | Weighted row scorecards, custom judges, safer baselines, active-as-of snapshots, alignment, review queues, prompt/workflow refinement gates | Synchronous caps, no immutable resolved run bundle or grouped slice UI, no cost/latency, no scheduled/continuous eval, no CI product-quality gate, and mutable judges. |
| Deploy | Publish versions, alias deployments, rollback, authenticated workflow HTTP service, token UI, MCP preflight, and three first-party database sidecars | A single environment is acceptable, but the fake/completion-only gate is not mandatory for `prod`, linked dependencies/evidence are weak, and DB sidecars default to the control-plane database/credential. Formal multi-party approval is excluded. |
| Operate | SSE updates, trace detail/compare, tokens/cost/latency, logs/events, audit, health, coarse fleet/success ratios | Trace linkage is repaired; actionable alerts, trustworthy health, queue/worker visibility, effect idempotency, recovery evidence, and incident diagnosis remain incomplete. Multi-region HA is excluded. |
| Control | Four scopes, audit rows, review queues, MCP command/host/tool policy, runtime/deployment preflight, and a dormant workflow promotion state machine | Spoofable identity, stored MCP literals, inconsistent resource scoping, root-only transitive dependency checks, misleading HITL controls, and observational release evidence remain. Organization/membership governance is excluded. |

The product therefore has **feature breadth without lifecycle closure**. The typical
successful path is currently “build and inspect in CALIBER; finish security,
packaging, deployment, and operations outside CALIBER.”

### Critical correctness and security findings

#### C1 — authentication is a local demo, not a security boundary

- `caliber/caliber-ui/src/auth/localAuth.ts:7-10,38-71` defines `admin/admin`, stores the
  asserted identity in local storage, and maps it to `@local-admin`.
- `caliber/caliber-ui/src/pages/Login.tsx:103-121` validates that credential only in the
  browser.
- `caliber/caliber-ui/src/api/caliberApi.ts:369-389` sends the identity as
  `X-CALIBER-User`.
- `caliber/src/caliber/auth.py:64-120` trusts that header and assigns four global
  scopes from configuration lists. When the header is absent, `current_user()`
  falls back to `CALIBER_DEV_USER` (`auth.py:82-88,227-233`). Its own module notes
  that DB-backed assignment is future work (`auth.py:20-24`).
- `deploy/caliber/compose.yaml:27-40` publishes port 5001 directly and defaults the
  dev user, admin, approver, and operator to `@local-admin`.

CALIBER can be placed behind a trusted identity proxy, but the shipped default does
not include or enforce one. Because Compose defaults the dev user and every
privileged list to `@local-admin`, a direct API request with **no identity header**
is also an admin in the default stack. A production deployment must neither trust
clients to self-assert the header nor enable this fallback.

#### C2 — MCP readback containment without a durable secret system — **[Partly remediated]**

- **[Remediated from baseline `b9d8e786e`]** The baseline implementation resolved
  and returned full OpenAI and Anthropic keys and populated browser password fields.
  The reviewed workspace instead returns only presence and a masked fingerprint
  (`routes/settings.py:1321-1370`); the browser fields are write-only and cleared
  after a successful save (`Settings.tsx:621-766`), and regression tests assert
  that secret values are absent from the response.
- Provider updates still mutate only the running process environment
  (`routes/settings.py:1373-1393`). They are not a durable, restart-safe secret
  resolver and expose no clear, rotate, revoke, or deployment-binding workflow.
- MCP `env`, `headers`, and `auth_config` remain ordinary JSON fields at rest for
  runtime compatibility (`db/models.py`). This pass stops known readback:
  `mcp_secrets.py` recursively replaces literal leaves with
  `__CALIBER_WRITE_ONLY__`, preserves exact `${VAR}` and valid `*_env_var`
  references, and lets PATCH preserve an existing hidden leaf. POST or a PATCH
  path with no stored leaf rejects the sentinel rather than persisting it.
- MCP list/detail/create/update responses, MCP history, and new update/delete audit
  details use that sanitizer. Generic audit list plus JSON/CSV export sanitize
  historical MCP rows on read, containing records written before this change.
  `McpServers.tsx` explains the write-only contract and omits unchanged sensitive
  mappings instead of sending masked display values back as credentials.
- `secrets.py:1-39,52-148` supports environment and file sources only. Manifest
  `secret_refs` are metadata, not a complete per-run injection/rotation system.

The accepted change is valuable containment, not a vault. Existing and new literals
still reside in ordinary database JSON and are materialized for the runtime. There
is no encrypted/reference-backed resolver, deployment scoping, clear/rotate/revoke
workflow, consumer graph, or migration of stored literals. Credentials embedded in
URI strings or command arguments also sit outside the three-field redaction
contract. Safe production use still requires that lifecycle and independent
log/trace/error-boundary validation.

#### C3 — row-level authorization is inconsistent

The repository has a useful visibility helper, but route adoption is incomplete.
Examples verified in this review:

- project lists are owner-filtered, but `_require_project()` is a bare
  `session.get` (`routes/projects.py:275-279`); project detail, update, list files,
  create folder, and upload use that unscoped path. Download/delete additionally
  verify file-to-project linkage, but still do not verify project ownership
  (`:374-559`);
- workflow runs carry `project_id` and `tenant_id`, but `_get_run_or_404()` is a
  bare lookup (`routes/workflow_runs.py:268-272`); manifest, events, traces,
  checkpoints, cancel, retry, approve, and reject paths reuse it (`:826-1011,
  1354-1527`);
- workflow-version lookup is also a bare primary-key read
  (`routes/workflow_versions.py:363-367`), and version list/create/update/publish/
  preview/run/restore paths do not establish workflow/project ownership. Deployment
  list/promote/rollback use bare workflow IDs, while promotion approve/reject fetch
  by promotion ID without project scoping (`routes/workflow_deployments.py:73-106,
  229-312`). Known IDs can therefore cross project boundaries in the core release
  control plane. **[Partly remediated]** Service publish/read/delete and token CRUD
  now resolve the parent workflow through `get_visible()` before touching nested
  state; the broader deployment/version paths remain open;
- tool list/version paths apply visibility, while some direct detail/source/update
  paths start from an unrestricted primary-key lookup;
- evaluation creation resolves dataset, skill, workflow version, and judge IDs with
  unscoped lookups, so a known ID can cross project boundaries
  (`routes/evaluations.py:209-286,351-391`);
- **[Partly remediated]** Test Set example browsing and review add/submit now resolve
  the visible parent. Review submission enforces active/pending state and claims the
  row atomically. Other nested dataset mutation/restore/sync and review
  administration paths still need the systematic route inventory; and
- judge test/alignment and several nested dataset routes still bypass the scoped
  parent lookup (`routes/judges.py:229-338`).

Multi-tenancy is excluded from the product target, so these routes are not scored
as tenant-isolation failures. They remain serious resource-integrity and
access-control defects for a single organization with multiple developers, and can mutate
or expose the wrong workflow/project when an ID is known or associated incorrectly.

#### C4 — non-admin evaluation list/detail visibility — **[Remediated in the reviewed workspace]**

At baseline `b9d8e786e`, `apply_visibility_filter()` assumed an `owner` column even
though `CaliberEvalRun` uses `created_by`. The non-admin list path therefore raised
`AttributeError` before SQL execution, and detail used a separate hand-written
predicate whose ownership/project semantics differed from list. Default-admin tests
hid both defects.

Current code resolves ownership through `owner_column()` and makes detail query
through the same `get_visible()`/`apply_visibility_filter()` predicate used by list
(`db/scoping.py:30-75,78-136`; `routes/evaluations.py:289-331`). Negative tests now
cover a same-project/different-owner row and the creator's row outside the active
project. Real route tests also exercise non-admin create → list → detail both with
and without an active project (`tests/test_routes_evaluations_visibility.py:159-327`).

The earlier version of this report alleged that a route-created project-less run
fell through to the model's `visibility="project"` default and became unlistable.
That claim was false: both baseline and current `_persist_eval_run()` explicitly set
`visibility="project" if project_id else "user"` (`routes/evaluations.py:441-485`).
The new round-trip test is useful regression coverage, but it disproves rather than
repairs that alleged hole.

This closes C4's default list/detail parity defect. It does **not** make the boundary
trustworthy: identity/project headers remain client-asserted under C1, and evaluation
creation still resolves datasets, skills, workflow versions, and judges through the
unscoped lookups catalogued in C3.

#### C5 — workflow deployment gates remain false quality evidence — **[Partly remediated]**

- `routes/workflow_deployments.py:93-140` calls `promote()` without the live
  configuration or an executor.
- `workflows/promoter.py:1544-1573` therefore calls `build_executor(None)`, which
  selects `FakeWorkflowExecutor` (`:176-190`). The candidate is not evaluated with
  the production agent provider.
- `evaluate_deploy_gates()` now fails closed with 0% for missing, empty, or archived
  datasets. It orders active examples by creation time plus example ID before
  applying the bounded sample, so repeated evaluation selects the same rows.
- For non-empty data it counts only `run.status == "completed"` (`:1328-1345`). It
  does not compare output to expected values, call a judge, measure regression,
  cost, or latency.
- The Inspector exposes `min_overall_delta` and `max_tone_regression`, but this gate
  reads only `min_pass_rate` (`Inspector.tsx:3404-3443`;
  `workflows/promoter.py:1333-1335`), making two configured controls decorative.
- The gate now calls `execute(..., preview=True)`. The runtime preflight described in
  C9 means a graph containing a dedicated unisolated capability node fails before
  any node runs rather than replaying that effect.

These are correct fail-closed containment changes. They do **not** turn completion
into quality evidence: the executor is still fake, expected values/judges are not
scored, configured `min_overall_delta`/`max_tone_regression` remain unused, no
cost/latency evidence is captured, and `GATED_ALIASES` is empty by default, so
`prod` does not require the gate or pending approval. MCP production-isolation
preflight is a separate rule and does not make this quality evidence. The gate must
not authorize a production release.

#### C6 — human-in-the-loop policy is represented but not enforced

The manifest and inspector expose `required_role`, `approval_count`, and
`timeout_behavior` (`workflows/manifest.py:721-727`;
`caliber/caliber-ui/src/components/workflows/Inspector.tsx:5568-5631`). At runtime:

- the interpreter only checks whether a node ID is in an approved set and labels
  the MVP path as pass-through (`workflows/runtime.py:5921-5936`);
- the worker creates exactly one request with a hard-coded
  `{"timeout_behavior":"block"}` snapshot
  (`orchestrator/workflow_run_worker.py:1790-1817`); and
- decision routes require the global operator scope, not the node's configured
  role, quorum, timeout, assignment, or separation of duties
  (`routes/workflow_runs.py:1354-1451`).
- `requires_approval` tools selected *inside an Agent node* do not enter that
  approval queue: fake/default and model-chosen execution mark them `_gated` and
  skip them (`workflows/runtime.py:1429-1458,1951-1977`). Only an explicit IR Tool
  node can block/resume through the approval checkpoint (`:2167-2170,5118-5125`).
  Thus the same label means “skipped” for an agent-bound tool and “approvable” for
  an explicit tool node.

The UI currently promises controls that the server does not honor. Under the
revised scope CALIBER need not implement enterprise quorum/SoD machinery; the
smaller correct fix is to remove those controls and provide one consistent,
authorized reviewer plus enforced timeout behavior.

The synchronous execution path does not enable runtime approvals at all, so the
same manifest can pause in the queued path and pass through an approval node in a
synchronous path. Approval semantics must be canonical and path-independent.

**[Partly remediated outside the workflow interpreter]** Review Queue submission
now rejects archived queues, non-pending items, unknown/wrongly typed answers, and
duplicate/concurrent claims; failed MLflow writeback returns the item to pending.
This fixes ordinary review-record correctness, but it does not enforce the queue's
`reviewers`/`assigned_to`, and a process crash can strand `submitting` or an external
success followed by local commit failure can still duplicate work. Those residuals
need recovery/idempotency, not enterprise quorum machinery.

#### C7 — workflow service publishing is auth-on by default — **[Remediated for the narrow default]**

- New service schemas, model defaults, and the publish route now default
  `auth_required` to true. `WorkflowDetail.tsx` publishes explicitly with auth,
  lists tokens, creates a token whose plaintext is shown once, supports copy and
  revoke, and clearly warns when a legacy/explicit service is public.
- Service publish/read/delete and token CRUD resolve the visible parent workflow.
  Token lifecycle uses operator scope because the same operator can publish the
  service; requiring a global admin only for its credential would break the product
  path without improving the current parent boundary.
- Authenticated invocation audit records identify the matched token ID, never the
  plaintext/hash. Explicit public invocation is recorded as anonymous rather than
  inventing a token actor.
- The Studio OpenAPI action no longer navigates to the external bearer-gated URL
  without a token. A CALIBER-authenticated, parent-visible internal route and the
  external service route share one spec builder; the browser downloads the internal
  response as JSON, while external callers still need the service bearer token.

This closes the unsafe new-service default and missing token UX. It does not upgrade
legacy public services, prevent an authorized operator from deliberately publishing
public, or provide rate limits, quotas, CORS policy, a secret vault, or trustworthy
platform authentication. C1 therefore still limits the security value of the token
boundary.

#### C8 — registered extension code bypasses the subprocess sandbox

- The workflow runtime resolves a registered tool by importing its Python module and
  returning the callable for direct execution (`workflows/runtime.py:2291-2312`;
  `workflows/tools.py:193-227`).
- The Tool test-run path describes itself as sandbox-isolated, but imports the module
  and invokes `wrapped(**tool_input)` in the web process
  (`routes/tools.py:401-480`).
- External-app nodes let a workflow operator type any
  `package.module:callable` in the Inspector (`Inspector.tsx:2096-2109`), then
  import and invoke that installed entrypoint in-process without an allowlist
  (`workflows/runtime.py:2561-2598,2666-2714`).
- In contrast, `python_code` workflow nodes and Aria-authored source tools are wired
  to `LocalSubprocessToolSandbox`. The reviewed workspace uses a temporary
  directory, `python -I`, an empty environment, private/dunder AST rejection,
  POSIX CPU/address-space/file-size/open-file limits, byte-capped output, a hard
  timeout, and process-group termination.
- That service explicitly states that production needs container/VM/kernel
  isolation (`tool_sandbox/service.py:34-42,95-105`), and Compose deploys no
  separate sandbox service.

This requires fully trusted workflow authors as well as administrator-controlled
installed packages; an explicit allowlisted entrypoint registry is absent. The local
subprocess is useful containment for the two integrated paths, but the mixed
execution model is incompatible with untrusted workflow authors/extension code and is
not a production-grade sandbox boundary.

#### C9 — Preview refuses known unisolated capability nodes — **[Partly remediated]**

`execute(..., preview=True)` scans the complete compiled IR before creating a trace
or entering the interpreter. It refuses `folder_input`, `input_bucket`,
`output_bucket`, `output_folder`, `mcp_resource`, `python_code`, `external_app`,
`webhook`, `api_request`, and any legacy host-path `file_input`. Scanning dormant as
well as reachable nodes is intentionally fail-closed.

**[New safe exception]** A `file_input` containing an immutable managed-file
snapshot is permitted. Direct preview, workflow evaluation, deploy-gate replay,
queued execution, and synchronous execution construct a project-scoped resolver
that verifies the logical ref, project, row ID, object version, recorded digest,
actual byte digest, and size. Published-service invocation enqueues through that
same managed-file-aware worker path. `extract_document` is overridden to parse
verified bytes in a private temporary file without leaking its host path. Queued
request `input_files` are materialized by the worker rather than being ignored.

This closes cookbook 04's root-workflow host-path bridge and preserves honest
containment for other effects. It is not a universal effect architecture:

- registered tools retain the explicit `allow_in_preview` contract and knowledge
  queries can still execute; knowledge builds keep their separate preview-skip;
- calibration/refinement, standalone export runtime, and transient assistant-draft
  execution do not construct this binder. Nested child manifests do not receive a
  resolver/tool override, and dataset-manifest refs are outside the protocol; child
  alias and MCP dependency inspection also remain runtime/transitive concerns;
- request `input_files` are materialized before a run, not dynamically mapped into
  graph ports. Managed refs represent whole-file snapshots; folder refs and
  streaming are not supported;
- normal queued/synchronous/service runs still let process-accessible local paths,
  author-selected storage namespaces under process credentials, and installed
  external code execute without a central capability broker;
- webhook/API manifests still accept arbitrary destinations and the default sender
  lacks scheme/domain/IP policy, private/link-local/metadata blocking, DNS-rebinding
  defense, and network-level containment; and
- retries/lease recovery still lack an effect ledger, so normal mutations remain
  at-least-once.

The architectural requirement remains a common effect broker with managed refs,
per-deployment capabilities, centralized egress policy, audit, budgets, and
idempotency. The platform must not relabel “Preview refused” as “the workflow was
safely evaluated.”

#### C10 — arbitrary stdio MCP host-command execution — **[Remediated; boundary caveats remain]**

The prior default-path RCE finding was valid for the earlier implementation but is
not current behavior:

- every test/discovery/invocation calls `execution_readiness()`/`stdio_launch()`;
  an executable must match `CALIBER_MCP_STDIO_COMMAND_ALLOWLIST`, and use of the
  CALIBER Python interpreter additionally requires an allowlisted `-m` module or
  absolute script;
- protected environment keys cannot be overridden, the child receives a sanitized
  environment and safe `PATH`, no shell, and normally a private working directory;
- remote transports require exact host allowlisting and reject embedded URI
  credentials; OAuth was removed from the accepted schema because it is not
  implemented;
- promotion, promotion approval, queued submission, worker start, and synchronous
  execution preflight the exact manifest snapshot and discovered tool policy;
- production aliases require a boundary classified as remote HTTPS or an
  operator-attested managed sidecar; and
- Compose ships PostgreSQL, pgvector, and AGE MCP servers as separate non-root,
  read-only, capability-dropped, resource-limited sidecars on an internal network,
  with an explicit in-network database URL.

This rationally closes “choose `/bin/sh` through the API”. It does not prove a
general production MCP sandbox. Five catalog presets remain blocked unless an
operator deliberately provisions and allowlists them. More importantly, the three
DB sidecars default to the same `caliber` Postgres database/credential as the
control plane. A read-only container root is irrelevant to an allowed `execute_sql`,
DDL, or DML operation against that database; production use requires a separate
target database and least-privilege role. Sidecar membership is a hostname
attestation, external HTTPS is not proof of the remote workload, rate limits are
process-local, and deployment/deletion preflight does not recursively walk
subworkflow dependencies. C1 also remains critical independently: spoofed admin
identity can mutate MCP configuration within the allowlisted boundary.

## 2. Workflow Builder

### Capability assessment

| Capability | State | Evidence-based assessment |
| --- | --- | --- |
| Agent nodes | **Shipped** | Agent nodes support inline/registered prompts, tools, skills, handoffs, memory/session, model settings, and output schemas. Deployed nodes sync into the backend Agent Fleet. |
| Standalone agent creation/management | **Partial, routed** | Inventory/detail/preflight/history plus admin-scoped create/edit/enable/delete ship. `/me`-aware pages hide those mutations for non-admins and show read-only guidance, although C1 means the identity itself is still untrusted. It remains a refinement-fleet configuration record, not immutable versions, runtime testing, deployment, rollback, or health. |
| Prompt creation/engineering | **Strong** | Builder, templates, variables, playground, test history, baseline, calibration, bindings, aliases, and rollback are substantial. |
| Skill creation/engineering | **Strong** | Wizard, content, trigger/render tests, scenarios, packages, calibration, bindings, and skill versions are present. |
| Reusable tool creation | **Partial/code-required** | The wizard requires a Python dotted module and callable already importable by the runtime (`ToolWizard.tsx:254-296`). Schemas and tests are low-code; implementation and packaging are not. |
| Tool sandboxing | **Partial/unsafe for extensions** | `python_code` and Aria source-tool drafts use a resource-limited, AST-restricted local subprocess. Registered tools, their tests, and external-app entrypoints still execute imported Python in-process; the local backend is not container/VM/kernel isolation. |
| MCP integration | **Partial, materially improved** | Registration, discovery, invocation, per-tool policy/rate controls, tests, calibration, write-only containment, runtime/deployment preflight, and readiness UX exist. Three database presets run in first-party sidecars; five external presets require operator provisioning. The DB sidecars' default shared control-plane database/credential is dev-only, secrets lack a durable lifecycle, and child inspection is incomplete. |
| File/folder/object-storage nodes | **Managed file shipped on named paths; legacy effects partial** | Object Store → File Directory → immutable managed `file_input` composes in preview/eval/deploy-gate/queued/sync paths, including service-triggered queued runs. Queued `input_files` bind at worker start but are not dynamic graph mappings. Calibration/refinement, standalone export, assistant drafts, nested child manifests, and dataset-manifest refs remain outside the binder; folders/streams and legacy effects remain incomplete. |
| Knowledge/RAG | **Strong** | Ingestion, chunking, embeddings, dense/hybrid retrieval, GraphRAG, Apache AGE, query playground, builds, calibration, versions, and workflow nodes ship. |
| Structured outputs | **Shipped, developer-oriented** | Agent/workflow output schemas and JSON validation exist, but agent output-schema authoring remains a raw JSON textarea. Tool input/output schemas have a visual builder. Published services collapse the validated input object into one string copied to every Start port and do not validate runtime output against their advertised schema. |
| Multi-agent orchestration | **Shipped** | Handoffs, parallel fan-out/join, session-scoped per-agent-node memory, handoff context, subworkflows, and multi-agent templates are real. This is not an arbitrary shared multi-agent state store. |
| Conditional logic | **Shipped** | Ordered router branches and fallback behavior execute in the runtime. The visual IF field/operator/value builder has 13 operators; it lacks typed field/value pickers and nested AND/OR groups. |
| Parallel execution | **Shipped** | Parallel, join-all/join-any, and bounded `for_each` concurrency are implemented. |
| Loops | **Shipped** | `for_each` and bounded loop nodes execute with caps and stop conditions. Stop expressions are still code-like. |
| Human-in-the-loop | **Partial/misleading** | Explicit approval/tool nodes can pause/resume, which is enough for a one-reviewer target. Review Queue state/type/concurrency correctness improved, but workflow role/quorum/assignment controls remain unenforced, timeout/path behavior differs, and an approval-required tool selected inside an Agent is skipped rather than queued. |
| Wait/event resume | **Shipped** | Durable `wait_until` and named external-event resume are integrated with checkpoints and run actions. |
| Scheduling | **Partial** | Per-workflow manual/event/cron triggers, cron/timezone fields, next-run preview, target deployment, an Enabled toggle, and the scheduler ship. There is no central schedule inventory/calendar, execution history, backfill, overlap/missed-run policy, or independently deployed scheduler role. |
| Versioning | **Inconsistent** | Workflows have mutable drafts plus immutable published versions, diff, restore, aliases, and rollback. Prompt/skill/tool/KB/test-set idioms differ; some artifacts lack governed promotion or true rollback. |
| Optimization | **Partial** | Provider code supports MetaPrompt, SkillMetaPrompt, GEPA, DSPy BootstrapFewShot, and DSPyMIPRO; selection reaches four names and prompt UI options expose MetaPrompt/GEPA. README claims of nine optimizers are not current behavior. |
| Workflow reuse | **Useful partial** | Subworkflows, node copy/duplicate, restore-as-draft, safe clone-as-new, YAML/JSON import with dependency preflight, and YAML/Python export exist. Import preserves links rather than copying/remapping dependencies; no portable bundle or reusable custom-node library exists. |

### Builder strengths

The builder is not a thin canvas. `workflows/component_catalog.py:51-80` registers 29 typed
node types and `Workflows.tsx:48-205` offers 13 templates, including single agent,
multi-agent handoff, guarded pipeline, parallel fan-out, HITL, batch/refinement
loops, dense/GraphRAG/AGE workflows, event resume, and blank canvas. The inspector
covers runtime defaults, caching, triggers, memory, handoffs, guardrails, deployment
gates, tools, and data mappings. Canvas multi-select, duplicate, snapping, minimap,
and run-state overlays make this credible for developer use.

### Builder gaps that break no-code claims

1. A custom reusable Tool is a registry reference to shipped Python, not an artifact
   a user can fully implement and package in the UI; executing it is also not
   isolated from the API/workflow process.
2. MCP is deployment-aware, but only the three first-party database presets are
   turnkey in shipped Compose. GitHub/Ollama/Playwright need `npx`, MinIO needs an
   external container/service, and the Hugging Face command remains operator work;
   the default policy correctly blocks these rather than pretending they work.
3. Managed project files close the common uploaded-document path on the named
   bound surfaces, but not calibration/refinement, standalone export, transient
   assistant drafts, nested child manifests, dataset-manifest refs, dynamic graph
   mappings, folders, or streams. Deployment-scoped storage permissions also remain
   absent.
4. Aria now collects and validates typed inputs and connects declared step outputs,
   but the heuristic planner remains literal-keyword based, complex fields use JSON,
   and the capability set cannot author arbitrary prompts, tools, agents, or
   workflows.
5. Import/clone is link-preserving rather than portable: users still need a mapping
   workflow for secrets, managed files, prompt aliases, and unavailable artifacts.
6. Agent output JSON Schema, loop-stop expressions, and field transformations beyond
   direct port mapping remain developer-oriented. A type-aware connect/map popover
   already exists; the router builder needs typed values and composable nested
   predicates rather than replacement.

## 3. Developer experience

| Developer task | State | Assessment |
| --- | --- | --- |
| Build workflows | **Strong** | Templates, graph manipulation, inspector, validation, code view, quick-add/connect/map, preview, publish. |
| Debug workflows | **Strong** | Node status, step inputs/outputs, errors, logs/events, tool calls, traces, artifacts, checkpoints, lineage. |
| Replay executions | **Partial** | Retry, checkpoint resume, event resume, and trace replay visualization exist. There is no guaranteed deterministic replay pinned to workflow draft, prompt alias/content, skill, judge, provider, tool, configuration, or secret versions. |
| Inspect intermediate state | **Strong** | Per-node event payloads, port snapshots, current node, outputs, error summaries, and checkpoint state are available. |
| View memory evolution | **Partial** | Run detail exposes current persisted per-node conversation histories, counts, latest turns, clear actions, and a short transcript window. It has no mutation timeline, historical snapshots, diff, or rollback, so it is memory inspection rather than true evolution analysis. |
| Trace tool calls | **Strong** | Arguments, outputs, duration, tokens/cost where available, and trace/span views are surfaced. |
| Understand execution graph | **Strong** | Authored and replay graphs are first-class. |
| Compare runs | **Partial** | Observability can compare two traces and eval detail can compare aggregate scores. Workflow detail lacks a general run-versus-run output/state/metric diff. |
| Clone workflows | **Shipped with linked dependencies** | Users can clone a selected saved version into a new workflow/v1 draft after dependency preflight. Referenced artifacts stay linked rather than being copied or remapped. |
| Reuse components | **Partial** | Prompts, skills, tools, KBs, and subworkflows are reusable. No governed component bundle/custom node marketplace exists. |
| Import/export | **Partial but discoverable** | Workflow inventory imports YAML/JSON with inline-secret rejection, graph/dependency/managed-file preflight, fresh identity/ownership, and v1 draft creation. An unlinked version page exports YAML/Python. Valid refs are linked rather than copied; no portable cross-project mapping/copy bundle exists, and skill ZIP export/folder import remains asymmetric. |
| Publish an API | **Partial, auth-on by default** | Service/OpenAPI/status endpoints and one-time bearer-token create/list/revoke UI exist; explicit/legacy public state is warned. Rate limits, CORS, quotas, durable secret storage, and a trustworthy platform identity remain absent. |
| Preview safely | **Fail-closed plus managed files** | Preview safely resolves content-pinned project files and refuses the IR for other known unisolated dedicated nodes. Registered-tool/knowledge-query policy, child managed binding, and lack of a real effect broker mean this is not universal isolated simulation. |

### Trace-link correctness defect — **[Remediated]**

At baseline `b9d8e786e`, `WorkflowRunResult` carried and populated both
`mlflow_run_id` and `mlflow_trace_id`, but the async worker and synchronous route
persisted only the run ID. The in-app span viewer and trace-to-run lookup read
`CaliberWorkflowRun.trace_id`, so the integrated trace panel stayed empty and
by-trace lookup could not resolve a workflow run despite a real trace.

The reviewed workspace assigns `result.mlflow_trace_id` in both the queued worker
(`orchestrator/workflow_run_worker.py:1700-1709`) and synchronous route
(`routes/workflow_versions.py:945-955`). Four new tests cover
the queued and synchronous paths and both dependent endpoints; all four fail
against the pre-fix code.

### Frontend maintainability and data-loss risk

Several product workspaces are monoliths:

- `KnowledgeBases.tsx`: about 9,006 lines;
- `Prompts.tsx`: about 6,620 lines;
- `components/workflows/Inspector.tsx`: about 6,225 lines;
- `WorkflowEditor.tsx`: about 5,237 lines; and
- `WorkflowDetail.tsx`: about 4,788 lines.

This raises review, state-coupling, and regression costs. More seriously, workflow
editor unmount autosave suppresses failures (`WorkflowEditor.tsx:3020-3065`), so a
navigation race can lose work without a blocking warning or recoverable local draft.

## 4. Testing and evaluation

### What genuinely works

- Versioned Test Sets with examples, trace-derived examples, restore, and MLflow
  synchronization. Dataset-file models/storage helpers exist, but no route or Test
  Set attachment UI was found, so attachments are not counted as shipped.
- Generic evaluation targets for model, prompt, skill, and compiled workflow
  versions. Workflow targets pass the runtime's preview flag, but that flag does
  not contain every integration side effect.
- Deterministic scorers, custom LLM judges, per-row evidence, aggregate scorecards,
  pass/fail summaries, and ad hoc baseline deltas.
- Prompt refinement with pinned datasets, candidate-versus-baseline regression
  checks, persisted regression runs, and rejection before candidate readiness.
- Workflow refinement/calibration that compiles and replays baseline and candidate,
  scores deltas, and rejects a candidate before `candidate_ready` when its gate
  fails (`orchestrator/workflow_stages.py:546-769`;
  `workflows/refinement.py:376-486`).
- Prompt/skill/tool/KB test histories, baselines, and calibration workspaces.
- Judge playground and manual human-alignment statistics, including Cohen's kappa.
- Review queues with MLflow writeback.

Regression testing is therefore **not absent**. Enforced gates exist in prompt and
workflow refinement/calibration. The workflow path, however, defaults to fake/
structural execution and scoring without a real configured provider, resolves the
current active dataset examples rather than a pinned dataset version, and ends at
an operator-applied candidate. Generic ad hoc evaluation and the separate direct
deployment smoke gate have different contracts; none forms one consistent release
policy across artifacts.

### Gaps and correctness problems

| Requirement | Finding |
| --- | --- |
| Unit testing | Component sandboxes and test cases exist, but there is no first-class workflow node/assertion suite with fixtures, mocks, setup/teardown, and suite-level policy. |
| Dataset evaluation | Real, but synchronous; defaults to 50 examples and caps workflow targets at 20 (`routes/evaluations.py:71-78,393-399`). Active-as-of snapshots are now browsable, but the evaluation form still does not expose all truncation/threshold policy. Workflow targets containing a known unisolated dedicated node now fail Preview before execution rather than performing the effect; that is safe refusal, not successful evaluation. |
| Regression testing | Prompt and workflow refinement/calibration enforce candidate gates, but the workflow path defaults to structural/fake evidence unless a real provider is configured and is not dataset-version-pinned. Prompt promotion verdicts remain advisory/operator-supplied; the separate direct deployment “gate” is only a fake-executor completion check. |
| Benchmark management | Benchmark worksheet CRUD APIs and a frontend helper library exist, but no routed UI uses them and no server-side benchmark runner executes them. They should not be counted as shipped benchmark management. |
| Prompt evaluation | Deepest evaluation surface. The public claims still overstate optimizer breadth and promotion uniformity. |
| Offline evaluation | Possible with deterministic/fake/local components, but generic evaluation explicitly requires a real configured provider. No reproducible offline bundle is presented. |
| Continuous evaluation | **Absent.** No schedule, production sampling policy, drift detector, alert, or automated feedback-to-eval monitor was found. |
| Quality metrics | Scorers and judges are useful. A row with any scorer error cannot pass, every failure is retained, its healthy raw scores remain diagnostic, and it is excluded from each per-scorer aggregate. Its row score/overall contribution is zero and it remains in the overall/pass-rate denominator, making incomplete evidence a conservative penalty rather than survivorship. Evaluation Detail shows the error and derived coverage. A durable aggregate denominator/completeness schema is still absent. |
| Cost/token/latency metrics in eval | **Absent from generic eval records.** These exist in observability but are not joined into scorecards or release gates. |
| Failure analysis | Per-row failure evidence is useful, but generic eval rows are not joined to workflow runs/traces. No clustering, slicing dashboard, statistical significance, or root-cause workflow exists. |
| Dataset weights and slices | **[Partly remediated]** The loader/result retain weight and tags; weighted scorer means, overall, and pass rate are computed; the UI renders weights/tags and explicitly distinguishes weighted metrics from raw counts. A non-empty all-zero effective-weight set now fails with 400 before prediction instead of silently becoming equal-weighted. Grouped tag/slice analysis remains absent. |
| Dataset-version correctness | **[Remediated for browsing semantics]** Creation rejects future versions. `version=N` remains “added in N”; the separate `as_of_version=N` endpoint/view returns the active snapshot evaluation/restore use, including rows retired later as read-only members. Query combinations and ranges are validated, ordering is deterministic, Restore is confirmed explicitly, and source/generated architecture docs now describe both contracts. Cryptographic snapshot identity remains an evidence gap rather than a browsing bug. |
| Evaluation reproducibility | `CaliberEvalRun.results` does persist the evaluated rows inline, including example ID, input, expected output, prediction, scores/error, weight, and tags. It does not record a cryptographic content/run digest, full pre-truncation inventory or sampling decision, or a resolved bundle of skill content/version, prompt content/alias, draft workflow manifest, judge definition/model, and provider configuration. The dataset's `mlflow_digest` describes the latest external sync, not the evaluation snapshot; merge-only sync omits weights/tags and cannot remove locally retired rows or old inputs after input-changing revisions. Workflow refinement resolves the current active dataset by name (`orchestrator/workflow_stages.py:183-214`). An unversioned prompt ref resolves version `1` (`routes/evaluations.py:127-140`) while the request schema documents it as “latest” (`schemas.py:2760-2762`). |
| Baseline comparisons | Candidates now require the same dataset/version, scorer suite, and successful status. The UI discloses target, subject, and model, but does not reject those mismatches or compare pass threshold/sampling policy, so displayed deltas are safer ad hoc evidence—not controlled regression proof. |
| Judge/review correctness | Judges and queue question schemas remain mutable/unversioned; historical evals retain a token rather than an immutable definition snapshot. Judge test/alignment use bare lookups, and alignment is ephemeral. Review submit now enforces visible/active queue, pending state, answer types/options, and a conditional concurrency claim with retry after failed writeback. Reviewer assignment is still descriptive, and cross-system exactly-once recovery is incomplete. |
| Test-result trust | Prompt/tool/skill durable-run create routes accept browser-supplied scores, verdicts, outputs, and reasoning, then recompute only aggregates. These are UI histories, not trustworthy server-executed evidence records. |

### Automation-suite assessment

The repository now contains 266 backend test files, 110 frontend unit/spec files, and
8 Playwright specs. CI runs lint/type checks, backend tests, integration tests,
Vitest, typecheck, and a frontend build. That breadth is a real engineering
strength. Current frontend, browser, marked-integration, static, build, package,
and Compose checks are green. The broad backend xdist run was interrupted and is
not claimed green, so the following remains a coverage/design assessment rather
than a release-candidate claim. However:

- default admin fixtures hide non-admin authorization failures — this is how the
  eval list crash survived (now covered by non-admin tests, but the fixture
  default itself is unchanged and will hide the next one);
- no cross-user/project workflow-run authorization tests were found;
- no tests vary HITL required role, quorum, timeout, or actor identity while those
  fields remain exposed;
- deploy-gate tests now require missing/empty/archived datasets to fail, deterministic
  bounded ordering, and Preview; they correctly retain the completion-only contract
  rather than pretending it is a quality gate;
- dedicated preview preflight tests cover the blocked capability families, the
  content-pinned managed-file exception, digest/project failures, and nested
  subworkflow refusal; normal-run effect/egress and transitive child binding remain
  to be covered by a broker/capability contract;
- new focused tests cover import/clone preflight, Agent routes/UI, typed Aria input
  merge/skip/references, MCP command/host policy, first-party DB server modes, queued
  managed inputs, synchronous approval rejection, and sandbox limits;
- unequal/all-zero weights, incomplete-scorer aggregate policy, tags/weights/errors,
  active-as-of browsing, baseline filtering, review concurrency, MCP readback,
  service tokens, and sustained health polling now have regression coverage;
- Playwright files and scripts exist but are not run by the checked CI workflow;
  and
- the marked PostgreSQL MCP integration test is not provisioned by CI.

The latest remote CI run exposed two release-signal defects: artifact quota failures
turned otherwise passing test/build jobs red and prevented the wheel job, while the
security job failed on an installed `setuptools` advisory before gitleaks ran. The
reviewed workflow now invokes the isolated supported-Python audit helper with a safe
bootstrap floor and runs gitleaks whenever the job is not cancelled. Fresh local
package and audit checks pass, but only a new remote run can prove artifact-account,
action integration, and secret-scan behavior together.

Coverage-oriented success must not be used as product-claim validation.

## 5. Deployment experience

### Shipped capability

- mutable workflow drafts plus immutable published versions, diff, restore into a
  new draft, and publish;
- alias deployment records, optimistic concurrency, rollback, and live-deployment
  deletion protection;
- cron/event/manual triggers;
- a dormant workflow promotion/approval state machine;
- MCP dependency/tool-policy preflight on promotion, approval, queued and
  synchronous execution, plus three first-party database sidecars;
- publication as an HTTP API with generated OpenAPI, asynchronous run status, and
  backend bearer-token storage; and
- a Releases board aggregating some live workflow/KB state and audit events.

### Production gaps

1. `SINGLE_ENVIRONMENT = true`
   (`caliber/caliber-ui/src/lib/environment.ts:1-25`) hides Deployments and
   Promotions from the tab navigation and deploys immediately to the single live
   `prod` alias. Both panels remain reachable with `?tab=deployments` or
   `?tab=promotions`, so this is a cosmetic frontend restriction rather than an
   authorization boundary. `GATED_ALIASES` is empty
   (`workflows/promoter.py:91-108`), and the deep-linked deployment panel still
   contains stale copy implying that production requires approval.
   Single-environment operation is acceptable for the scoped target; the gap is the
   misleading hidden/deep-linkable UX and immediate rotation around an unsafe gate,
   not the absence of multi-environment promotion itself.
2. The deploy gate now fails closed for missing/empty/archived data, samples
   deterministically, and uses Preview, which refuses known unisolated dedicated
   nodes. It still uses a fake executor, checks completion only, ignores two exposed
   threshold fields, and is not mandatory for any alias by default. This is distinct
   from MCP external-boundary preflight, which defaults to `prod`.
3. The Releases page and API explicitly describe themselves as read-only. Formal
   enterprise signoff/waivers are excluded, but there is still no simple release
   checklist tying immutable evidence, gate outcome, operator confirmation,
   deployed version, and rollback lineage together.
4. There is no managed deployment-scoped configuration and secret inventory,
   binding, clear/rotate/revoke lifecycle. Workflow tool bindings can carry
   `secret_refs`, but those metadata references are not a secrets administration
   product. A basic per-deployment concurrency/resource policy is also absent. A
   separate multi-environment model, autoscaling, and canary/traffic management are
   optional for this scoped target.
5. New workflow API publishing is bearer-authenticated by default and the UI manages
   one-time tokens. Legacy/explicit public services, missing rate/quota/CORS policy,
   and the spoofable control-plane identity remain production gaps.
6. The Inspector exposes per-trigger cron, timezone, next-run preview, target
   deployment, and enablement. There is no central operations calendar/inventory,
   execution history, backfill, overlap policy, or missed-run handling UI.
7. Skills, tools, KBs, prompts, test sets, judges, and agents do not share one
   draft/version/test/publish/rollback contract.
8. `CaliberWorkflowDeployment.environment` exists, but promote requests cannot set
   it and alias rotation does not populate it. This is a dormant/inconsistent field
   and schema/API UX debt, not evidence that a multi-environment product is needed.
9. Published services validate the input object against the advertised schema, then
   JSON-serialize the whole object and seed the same string into every Start port;
   per-port runtime semantics therefore do not honor the schema. They also do not
   validate runtime output against the advertised output schema and provide polling
   only, with no request quotas or bounded execution policy. Callbacks and traffic
   splitting are optional.
10. Crash recovery is at-least-once for side effects. An expired running lease is
    reset to queued, and a run without a wait/approval checkpoint restarts from the
    beginning. There is no platform effect ledger or per-node idempotency key, so a
    mutation completed just before process failure can execute again.
11. The default deployment still exposes an admin identity fallback. MCP no longer
    permits arbitrary host executables by default, but production isolation relies
    on operator-configured sidecar/remote-host attestation, and dependency preflight
    does not recursively prove every subworkflow target.

Deployment is therefore possible for a self-hosting engineer, but it is not a
safe no-code release experience.

## 6. Operations and monitoring

### Implemented

- Server-sent live events and workflow run state.
- MLflow trace discovery/detail plus a two-trace comparison view.
- Span/tool-call detail, duration, token use, model, and cost where instrumentation
  supplies them.
- Aggregate trace volume/error, latency percentile, token, and cost charts.
- Workflow run events, current node, checkpoints, retry lineage, artifacts, and
  memory.
- Prometheus metrics, a database liveness endpoint, a `/readiness`
  configuration-status response, system-service status, runtime configuration inventory, audit
  log, and Allure report integration. `/readiness` always returns 200 and reports
  provider selector/feature flags rather than dependency connectivity
  (`routes/health.py:55-89`).
- Event-bus abstractions and signed outbound lifecycle webhooks.

### Missing for production operation

- alert-rule creation, routing, escalation, silence, acknowledgement, and history;
- SLO/SLI definitions, error budgets, and burn-rate views;
- continuous quality/evaluation monitoring and drift;
- per-agent and per-workflow health/ownership dashboards beyond the Overview's
  coarse enabled-agent coverage and assistant success ratios;
- searchable infrastructure/application log aggregation and repository-managed
  retention policy;
- alert-to-trace diagnosis, remediation, and incident history;
- per-workflow/deployment spend budgets and anomaly alerts;
- queue-depth/worker operations, single-instance failure recovery, and published
  load/resource limits; multi-replica HA is excluded;
- durable event replay and webhook retry/dead-letter handling; and
- readiness probes for MLflow, object storage, provider credentials/connectivity,
  event bus, worker liveness, and queue lag rather than database-only health.

**[Remediated for truthful liveness display]** AppShell now owns one
`useHealthStatus` observer and passes its state to both shell indicators, so there is
one sustained poll rather than two observer timers. Visible copy, tooltip, and ARIA
text say “API + database reachable/unreachable,” and fake-timer coverage proves the
cadence. `/health` still checks only API/package and database `SELECT 1`, not
workers, scheduler, queue lag, MLflow, object store, event bus, or provider
connectivity; those require separate readiness signals.

## 7. Platform UX

### Strengths

- Navigation groups Compose, Library, Knowledge, Evaluate, Observe, and Platform in
  a vocabulary that matches agent engineering.
- Visual language, cards, status badges, tables, empty states, modals, error
  boundaries, and React Query behavior are generally coherent.
- Workflow Studio supports novice templates and power-user graph operations.
- Artifact workspaces put authoring, tests, calibration, versions, and bindings
  close together.
- Live updates, deep links for core workflow routes, inline help, and inspector
  setup checks improve day-to-day usability.

### UX debt

1. Agents are now routed and Projects are reachable through the TopBar workspace
   selector/create dialog. Secrets, alerts, and benchmarks remain absent as routed
   workspaces even where lower-level fields, resolvers, or APIs exist. Organization,
   team, and membership administration is intentionally excluded.
2. Project selection is real, but its security boundary is still a client-supplied
   header and route adoption is inconsistent; managed files correctly require a
   selected project, which can surprise users operating in the “All workspaces” view.
3. Skills and Tools mix in-place workspaces with routed detail pages, producing
   different behavior for list selection versus deep links.
4. Workflow Detail tabs and its selected run are URL-addressable, but many
   artifact-workspace sub-tabs and local selection states elsewhere are not,
   weakening sharing and browser history.
5. Raw IDs remain common in evaluation subjects, trace/queue movement, and artifact
   relationships instead of searchable typed pickers. Run Evaluation specifically
   requires a manually typed prompt `name@version`, skill ID, or workflow-version ID
   (`Evaluations.tsx:328-350`).
6. Agent output schemas, loop-stop expressions, and field transformations beyond
   direct port-to-port mapping remain code-like. A type-aware visual mapping popover
   exists. Router conditions also have a visual builder, but not nested Boolean
   groups or typed field/value selection.
7. No global search, command palette, bulk lifecycle actions, dependency graph, or
   readiness checklist exists as projects grow.
8. Review Queue uses a rigid multi-column workflow and does not show the actual
   request/response context being labeled.
9. Navigation-hidden but deep-linkable deployment panels, API-only benchmark
   features, stale production-approval copy, and hard-coded mode flags make
   discoverability differ from implementation reality. Workflow import/clone and
   service-token lifecycle are now discoverable exceptions.
10. The very large page modules make consistent behavior harder to maintain.
11. Navigation and Settings are not permission-aware enough. The shell exposes
    settings/audit and other administrative destinations broadly, while Settings
    always fetches operator data and renders admin-only mutation controls that the
    backend may reject. Agent pages are now the positive counterexample: they fetch
    `/me`, hide admin-only lifecycle controls, and explain read-only access. That
    improves discoverability but cannot become a security boundary until C1's
    spoofable demo identity is replaced.

The UI is learnable for an AI engineer, but it is not yet an efficient growing
single-organization workspace or a safe guided path for a less technical operator.

## 8. Production access and safety (enterprise capabilities excluded)

Organization/team administration, built-in SSO/SCIM, multi-tenancy, compliance
certification, WORM/SIEM controls, enterprise collaboration, and formal segregation
of duties are **not evaluated**. The remaining requirements apply to any
network-reachable self-hosted product:

| Requirement | State | Finding |
| --- | --- | --- |
| Authentication | **Critical gap** | Client-side default credentials, a trusted client header, and no-header local-admin fallback remain unsafe. A server-validated session/token or strictly configured trusted proxy is sufficient; built-in enterprise SSO is not required. |
| Resource authorization | **Serious gap** | Four global scopes exist, but advertised project/visibility boundaries are applied inconsistently. No enterprise RBAC console is required; list/detail/mutation operations still must target the correct authorized resource. |
| Secrets | **Serious gap, readback contained** | Provider keys and MCP literal leaves are no longer returned through known browser/audit surfaces. MCP still accepts and stores literal credential JSON for runtime use. A durable encrypted/reference-backed resolver with rotation/revocation is required; an enterprise vault marketplace is not. |
| Published API authentication | **Partial** | New UI-published services are token-authenticated by default and expose token lifecycle. Explicit/legacy public services, no rate/quota/CORS policy, and C1's spoofable platform identity remain. |
| Effect isolation and egress | **Critical gap, managed-file path contained** | Preview/evaluation safely resolve content-pinned project files and refuse the other known unisolated dedicated effects. Normal runs/services still permit legacy filesystem/storage capabilities and unrestricted webhook/API egress; explicitly preview-enabled tools and knowledge queries retain policy. There is no universal broker or SSRF defense. |
| Extension/MCP execution | **Serious residual gap; arbitrary stdio remediated** | Registered/external-app Python still runs in-process. MCP commands/hosts/tools are fail-closed and DB integrations use hardened sidecars, but those sidecars default to the control-plane DB/credential; production needs a separate least-privilege target. Classification is operator-attested and child inspection is not transitive. |
| HITL/review correctness | **Misleading, review records improved** | A single authorized reviewer is sufficient. Queue state/type/concurrency checks now prevent ordinary overwrite/duplicate submission, but workflow role/quorum/timeout semantics remain false and cross-system recovery is incomplete. |
| Audit correctness | **Partial** | Transaction-coupled rows, filtering, MCP legacy redaction, actual service-token actor attribution, and export are useful. WORM/SIEM/compliance evidence is excluded; comprehensive secret/actor correctness still needs contract tests. |
| Release evidence and recovery | **Critical gap** | Formal enterprise signoff is excluded. Missing/empty/archived gate inputs and dedicated Preview effects fail closed, but fake/completion-only evidence, no mandatory `prod` quality gate, incomplete pinning, and duplicate normal effects still make one-operator release unsafe. |

Enterprise exclusions remove product-suite breadth requirements; they do not make an
untrusted identity boundary, a regression to arbitrary host command execution,
secret disclosure, SSRF, or unsafe release evidence acceptable.

## 9. API, runtime, and architecture assessment

### Architectural strengths

1. **Typed workflow core.** Pydantic manifests, compiler validation, an explicit IR,
   and a deterministic graph interpreter provide a stronger base than a
   frontend-only canvas.
2. **Durable execution state.** SQL-backed runs, events, checkpoints, approvals,
   leases, retry lineage, memory, artifacts, and audit rows are appropriate
   control-plane primitives.
3. **Modular integration seams.** Tool resolver, sandbox, storage backends, event
   buses, tracing, provider interfaces, knowledge runners, and service contracts are
   separable and testable.
4. **Good workflow resilience primitives.** Error boundaries, waits, event resume,
   bounded loops, checkpoints, cancellation, and worker heartbeats are substantive.
5. **Broad API.** The 40 registered route modules cover nearly every advertised
   artifact and lifecycle concept.
6. **Engineering test investment.** Backend, frontend, integration, and E2E assets
   are extensive even where the product contract needs correction.
7. **Concrete capability contracts.** Managed-file snapshots and MCP execution
   readiness are centralized, typed, fail-closed contracts rather than UI-only
   warnings. Their remaining transitive gaps are tractable architecture work.

### Architectural concerns

#### Documentation and deployment disagree on the product boundary

The root README says CALIBER is an MLflow server plugin that needs no new service
(`README.md:28-33`) and describes one deployment unit. A plugin entry point does
exist (`caliber/pyproject.toml:159-162`). The shipped Compose path, however, labels
CALIBER a **standalone ASGI service** on port 5001 talking to vanilla MLflow over
HTTP (`deploy/compose.yaml:31-50`; `deploy/caliber/Dockerfile:1-18`). Both modes may
be supported, but current documentation treats mutually different security,
storage, scaling, and failure boundaries as one architecture.

#### Workers and scheduler are colocated with the web process

`server.py:144-209,319-345` starts refinement, workflow-run, Aria, knowledge,
scheduler, janitor, and webhook loops in the ASGI lifespan. Atomic claims/leases
and cron idempotency provide useful multi-process safety, but the shipped Compose
file colocates every role in one CALIBER container. Independent effect isolation,
queue operations, single-instance recovery, and load validation are not shipped;
autoscaling and HA are excluded from the target.

#### Runtime semantics and scale require hardening

- Workflow and KB workers each claim and execute one job synchronously per polling
  iteration; there is no deployed worker pool or workload isolation.
- Direct parallel branches size a `ThreadPoolExecutor` to the branch count without
  a configured cap, while manifests permit large graphs. `for_each` is bounded, but
  per-item failures are collected while the node itself reports `ok`.
- Loops, `for_each`, and error boundaries wrap one inline target node rather than an
  arbitrary subgraph; arbitrary graph cycles are rejected. The visual language
  should describe this boundary clearly.
- Cancellation is observed between nodes, not as hard interruption of a long tool
  or model call.
- Expired run leases are reset from `running` to `queued`; unless execution had
  reached a wait/approval checkpoint, the worker restarts from the beginning
  (`orchestrator/workflow_run_worker.py:532-584,1479-1515`). Registered tools,
  MCP, webhooks, API requests, and external apps have no effect ledger or platform
  idempotency key. The heartbeat reduces overlap but cannot prevent a post-effect,
  pre-commit crash from duplicating an external mutation.
- Managed project-file nodes no longer need a host path on the explicitly bound
  preview/evaluation/gate/queued/synchronous surfaces; published services inherit
  the queued-worker binding. Calibration/refinement, standalone export, transient
  assistant drafts, nested child manifests, and dataset-manifest refs do not.
  Request `input_files` are materialized rather than dynamically mapped, and there
  is no managed folder/stream protocol. Legacy local file/folder nodes still accept
  process-accessible paths, while bucket nodes reuse process-wide credentials;
  normal live runs have no per-workflow folder/bucket capability object.
- Cron scheduling scans active deployments and uses an idempotency key to prevent
  same-minute duplicates across processes. It has no catch-up/backfill or
  per-workflow overlap/concurrency policy and silently falls back to UTC for an
  invalid timezone.
- In-process and plain NATS event paths do not provide durable replay; bounded
  subscriber queues can drop events. Outbound webhooks have no retry queue or DLQ.
- pgvector retrieval is disabled by default, so larger KB queries can fall back to
  loading and scoring chunks in Python rather than using ANN search.

These limits are acceptable for a local technical preview only if documented and
guarded. They are not a demonstrated production scale model.

#### Production defaults are development-oriented

Compose defaults to the fake LLM provider, local admin identity, MinIO default
credentials unless overridden, and host-mounted Allure assets. CSRF and rate
limiting default off. MCP command/host defaults are now fail-closed and DB MCP
sidecars are hardened, but there is still no explicit whole-platform production
profile/readiness gate.

#### Scoping is a cross-cutting API concern but remains route-by-route

The repeated bare `session.get` defects show that optional use of a generic query
helper is not a reliable authorization architecture. Parent-child scoping and
actor permissions need to be part of repository/service interfaces, not handler
discipline.

#### Artifact lifecycle semantics are fragmented

Workflows, prompts, skills, tools, KBs, datasets, and judges have different concepts
of draft, version, active/live, baseline, approval, promote, rollback, archive, and
visibility. `docs/version-management-ux-spec.md` correctly recognizes this as a
backend/UI contract problem; it is not only a styling issue.

#### Strategy and the requested vision conflict

`docs/roadmap.md:192-195` explicitly says CALIBER will not build a rival visual
builder or general automation/BPM engine. That is a defensible control-plane
strategy, but it conflicts with positioning CALIBER as a full low-code/no-code agent
engineering platform. Product leadership must choose one primary promise:

- **governed lifecycle control plane** that imports or wraps systems built elsewhere;
  or
- **end-to-end low-code agent builder** that owns implementation, integration,
  deployment, and operations.

The current repository is strongest as the first and incomplete as the second.

## 10. Incomplete, placeholder, dormant, and dead surfaces

- **[Remediated]** `App.tsx` described `Placeholder` as an unbuilt-page
  mechanism, but it was only the wildcard 404, so a mistyped URL rendered “This
  page lands in a follow-up milestone” and read as a missing CALIBER feature. It
  is now a client-rendered `NotFound` view with accurate copy and a link back to
  the dashboard; this does not establish the server's HTTP status.
- Deployments and Promotions have substantial code but are hidden from navigation
  by a hard-coded frontend constant while remaining query-string deep-linkable;
  backend gated aliases are empty and the panel's approval copy is stale.
- Workflow benchmark worksheet CRUD, client helpers, and tests have no routed page
  or executable server-side benchmark runner.
- An unlinked, manually deep-linkable workflow-version page offers YAML/Python
  export; normal version navigation goes to the editor. **[Remediated]** Workflow
  inventory now exposes YAML/JSON import and clone-as-new with validation/dependency
  preflight. Portable dependency bundles and mapping remain absent.
- **[Remediated]** Workflow service token CRUD is now exposed in the publishing UI
  and new services are auth-on by default; production identity, vault, quota, and
  explicit-public governance remain separate gaps.
- **[Remediated]** `WorkspaceSelector` is mounted in the TopBar and supports project
  creation/selection; the client-asserted header remains a security limitation.
- **[Remediated for the registered capability set]** Aria plan mutations now pause
  for schema-driven inputs, merge/validate answers, connect declared prior-step
  outputs, expose an explicit backend-backed **Skip step** action, and resume
  execution. Generated cookbook 12–15 prose still describes the obsolete
  approval-only/manual-artifact workflow and is stale.
- Demo workflow tools intentionally contain no-op/stub external actions; templates
  using them demonstrate orchestration, not production connectivity.
- Optimizer docstrings/README enumerate future algorithms that selection/provider/UI
  do not expose.
- Releases is explicitly observational, despite its placement implying a release
  operations room.
- **[Remediated]** Provider settings text and backend docstrings claimed
  secret-presence behavior while returning full keys. Behavior now matches the
  documented contract.
- **[Remediated]** Evaluation/schema documentation no longer claims workflow targets
  redirect, no longer advertises the removed reference target, and defines the
  unversioned prompt behavior accurately. Test Set architecture now distinguishes
  added-in from active-as-of membership.
- **[Remediated for the narrow signal]** One AppShell observer owns the sustained
  health poll and both visible indicators accurately label API/database reachability.
  Deep dependency/worker readiness is still absent.
- Fail-closed Preview intentionally makes several older cookbook verification paths
  non-executable: workflows containing `python_code` or another blocked dedicated
  capability node must use an isolated live-test mechanism that does not yet exist.
  Content-pinned managed `file_input` is now the safe exception, closing cookbook
  04's root document path. Cookbooks 03, 08, and 09 still need product/recipe
  redesign rather than being counted as unchanged successes.
- Workflow editor unmount autosave hides errors.
- Generated cookbook footers still claim every recipe is implementable through the
  UI, while `docs-site/cookbooks/FEASIBILITY.md` retains false HITL role/quorum/
  timeout and prompt-playground claims.
  The cookbook root README likewise says only 04, 05, and 11 need out-of-band work,
  contradicting the verified 07/10 limitations and the now-stale workaround prose
  in 12–15.
  `docs-site/cookbooks/training/content.py` still says evaluations cannot score
  workflows, the cookbook README's ladder omits cookbook 16, and cookbook 10's
  verification/assets still describe alignment as a manual by-hand step.
- Cookbook 05's README, scenario, verification, and generated training step say MCP
  invocation emits no MLflow spans, but the gateway wraps every allowed invocation
  in a `TOOL` span. The recipe still selects GitHub, which requires external `npx`
  provisioning and an environment-backed token; the shipped turnkey demonstration
  is now one of the PostgreSQL/pgvector/AGE sidecars. Documentation has not been
  rewritten around that deployable path.
- Cookbook 02 source/training says package import is API-only or absent, while
  `SkillDetail.tsx:599-624` exposes an **Import package** folder picker. The real gap
  is the ZIP-export/folder-import round trip, not absence of an import UI.
- Cookbook 10 labels two generic evaluation runs “baseline” and “candidate” but
  never selects **What to score** or a subject, so both default to generic `llm`
  rather than distinct artifacts; the candidate step also does not reselect its
  judge (`Evaluations.tsx:241,256-260,311-335`). Training claims per-example
  baseline deltas, while Evaluation Detail renders deltas only on aggregate cards.
  It then says trace IDs are available in candidate per-example detail, but that
  scorecard renders input, expected, prediction, scores, and verdict only
  (`EvaluationDetail.tsx:182-287`). Moving failures into Review Queues therefore
  requires manual recovery of source trace IDs through the original dataset or
  Observability. Cookbook 07's evaluation step has the same missing target/subject.
- Cookbook 16 violates the root cookbook folder contract: it has a README, generated
  training steps, and two assets, but no `scenario.yaml`, `build.yaml`,
  `test-data.yaml`, or `verification.yaml`. Its training step 65 also says the queue
  item shows trace input/output, while `ReviewQueues.tsx:575-603` shows only the
  trace ID and configured questions; reviewers must inspect Observability separately.
  Its evaluation step selects only the dataset and scorer, so the UI defaults to a
  generic model completion rather than the repaired workflow version
  (`Evaluations.tsx:241,256-260,311-335`). The text acknowledges that captured error
  output becomes the expected value, but never makes correction plus workflow-target
  selection a required precondition for its claimed “re-run after fix” proof.
  `docs-site/cookbooks/training/content.py:593` additionally claims Observability can
  create the set and set expected output, while the UI only chooses an existing set
  and submits a trace ID (`Observability.tsx:756-810`).
- The asset READMEs for cookbooks 04 and 09 link `../FEASIBILITY.md`, which resolves
  inside each cookbook and does not exist; the shared file is two levels above.
  Generated pages and source assets therefore disagree even where the merged
  remediation improved cookbook 03/08/10 prose.

## 11. Missing end-to-end workflows

These are not individual buttons; each is a product path that currently breaks.

1. **Complete standalone agent lifecycle:** Agent configuration/create/edit/preflight
   now works; model/prompt/tool/memory authoring → immutable version → runtime test
   → evaluate → deploy → health → rollback does not.
2. **No-code custom integration:** choose connector/API → bind write-only secret →
   test in deployment-equivalent sandbox → version → approve → reuse.
3. **Universal managed-data workflow:** Object Store → File Directory → pinned
   `file_input` works on preview/eval/gate/queued/sync and service-triggered queued
   runs. Calibration/refinement, standalone export, assistant drafts, nested child
   manifests, dataset refs, dynamic graph mapping, folders/streams, and
   deployment-scoped storage credentials remain outside that contract.
4. **Trustworthy release:** select immutable candidate + target deployment → run full
   quality/cost/latency/regression suite with the real executor → review evidence →
   explicitly confirm → publish → record outcome → rollback.
5. **Continuous evaluation:** sample production traces → apply versioned judge suite
   → detect drift/regression → alert owner → create remediation work item → verify
   candidate → close alert.
6. **Incident operations:** alert/SLO breach → affected agent/workflow health → trace
   cluster → compare last good/current release → retry/rollback → postmortem/audit.
7. **Portable solution bundle:** clone/import with graph and dependency preflight now
   works; dependency content, managed files, secrets, prompt aliases, conflicts, and
   environment mappings are not bundled/remapped.
8. **Broad Aria autonomous build:** the registered judge/dataset/review/calibration
   set now supports typed dependency-aware execution. Natural-language planning,
   discovery, dry-run, deep links, and arbitrary prompt/skill/tool/agent/workflow
   authoring remain absent.
9. **Safe credential lifecycle:** create write-only reference → bind to a deployment
   → rotate → see consumers → revoke → verify no values entered logs/browser/audit.

## 12. Prioritized roadmap

### Critical — block production claims and external rollout

#### P0. Establish production authentication and resource scoping

- Replace local auth in production with a server-validated session/token or a
  strictly configured trusted proxy that strips and injects identity headers.
  Built-in OIDC/SAML administration is not required.
- Refuse startup in production mode when local dev auth/default credentials are
  active.
- Centralize `get_authorized_*_or_404` services for every artifact and nested
  resource; inventory every direct `session.get` in routes.
- Add negative integration tests for anonymous versus operator access and for
  mismatched parent/project IDs across list/detail/mutation, run, trace, file,
  queue, judge, evaluation, deployment, and service routes.

**Exit criterion:** an unauthenticated client cannot become an administrator, and
an operator cannot read or mutate a resource through an unrelated parent/project ID.

#### P1. Remove secret exfiltration and secure published services

- **Working-tree containment shipped:** MCP API/audit/history responses are
  write-only with PATCH preservation, and new workflow services are auth-on with
  token lifecycle UI. The bullets below are the remaining production contract.
- Preserve the merged provider-key fix: reads return only
  presence/fingerprint and browser inputs remain write-only. Replace its
  process-local update mechanism with durable secret references and explicit
  clear/rotate/revoke semantics.
- Require secret references for MCP/auth headers; reject literal secret-shaped
  values or store them through an encrypted secret service.
- Preserve recursive MCP redaction before audit persistence and on legacy audit
  reads; generalize the same policy to every future secret-bearing structure.
- Provide a pluggable durable secret resolver with deployment scoping, rotation,
  revocation, and consumer visibility. Specific enterprise vault integrations are
  optional adapters.
- Preserve authenticated-by-default workflow services and token create/list/revoke;
  add operator-selected scopes/expiry/rotation plus CORS, quotas, and rate limits.

**Exit criterion:** no API response, browser state, trace, log, or audit row contains
resolved provider/MCP/service secrets; a UI-published service rejects anonymous
invocation by default.

#### P2. Make Preview and workflow-target evaluation side-effect-safe and control outbound egress

- **Working-tree containment shipped:** each previewed IR fails before its own
  execution when it contains ten known unisolated dedicated node types. This
  prevents those live effects but cannot simulate the workflow; the broker below
  remains required.
- Define one runtime-wide effect contract for Preview, workflow-target evaluation,
  deploy gates, test, retry, and replay. Default every integration to deterministic
  mock/recorded behavior unless the operator explicitly chooses an isolated live test.
- Route local file/folder I/O, object storage, MCP, webhook, API, external-app,
  registered-tool, and knowledge effects through a common broker with capability
  policy, audit, timeout, budget, and idempotency keys.
- Replace raw host paths and arbitrary bucket names with immutable references plus
  explicit per-workflow/per-deployment allowed roots, buckets, operations, and
  credential bindings. Published-service input must never become an unchecked host
  path or storage namespace.
- Preserve the new fail-closed stdio executable/module allowlists. Move every
  locally allowed MCP server into an isolated least-privilege worker and add signed/
  pinned package provenance; never relax the policy back to arbitrary host commands.
- Enforce approved schemes/domains, resolve and validate destination IPs, block
  loopback/private/link-local/metadata networks, defend against DNS rebinding, and
  add network-level egress isolation.
- Add an effect ledger so lease recovery and retries can resume or deduplicate
  mutations instead of blindly restarting them.

**Exit criterion:** ordinary Preview, workflow-target evaluation, and release-gate
runs cannot read process files outside explicit application-approved roots, mutate
unapproved process/object storage, reach the network, or change external systems;
authorized live tests remain constrained by OS/container and storage-IAM policy,
cannot reach unapproved roots/buckets or internal metadata/private services, and
remain idempotent across worker failure. API clients cannot select or launch
arbitrary host executables.

#### P3. Rebuild workflow release gates as real evidence gates

- **Working-tree containment shipped:** missing/empty/archived datasets fail closed,
  bounded selection is deterministic, and execution uses Preview. Completion-only
  fake-executor semantics and the absence of a mandatory `prod` quality gate remain
  blockers.
- Pass the live deployment configuration/executor into promotion.
- Preserve fail-closed missing/empty/archived handling and pin dataset, example IDs/content digest,
  judge, prompt, tool, skill, KB, model, provider configuration, and workflow
  versions. Reuse/extend the generic evaluator's future-version rejection and
  server-side weight/tag propagation in the release gate, and snapshot the actual
  resolved inputs.
- Evaluate expected outputs/custom judges and baseline regression—not completion.
- Add minimum sample size, failure budget, cost/token/latency thresholds,
  partial-error policy, and stored per-example evidence.
- Make the gate asynchronous and persist an immutable verdict linked to the
  candidate and target deployment.

**Exit criterion:** intentionally wrong output from a successfully completed real
agent fails promotion; empty or truncated evidence cannot pass silently.

#### P4. Make HITL and review-state behavior truthful

- Persist the HITL policy snapshot from the node.
- Support one authorized reviewer consistently across synchronous/queued paths,
  enforce deadline/timeout behavior, and queue approval-required agent tool calls.
- Remove role, quorum, assignment, SoD, and escalation controls from UI/docs unless
  the server actually implements them; they are not required by the scoped target.
- Preserve the new active/pending/completed and answer-schema checks plus conditional
  submission claim; add stuck-claim recovery and an idempotency key spanning MLflow
  writeback and local completion. Advanced reviewer assignment/SLAs are optional.
- Provide a simple release checklist that links evidence, operator confirmation,
  deployed version, and rollback lineage; formal signoff/waivers are excluded.

**Exit criterion:** every displayed approval control has the same server-enforced
meaning on every execution path; one valid decision is durable and cannot be
silently duplicated or overwritten.

#### P5. Ship a fail-closed production deployment profile

- Reconcile plugin and standalone architectures in documentation and threat models.
- Support separate web, workflow worker, eval worker, scheduler, and janitor roles
  where needed for effect isolation and recovery. Horizontal scaling is optional.
- Move registered tools, external-app entrypoints, and local stdio MCP processes to
  authenticated, resource-limited sandbox workers with per-run filesystem, network,
  CPU/memory/time, dependency, and secret policies. Never import extension code into
  the API/control-plane process.
- Remove fake provider/local identity/default credentials from production defaults;
  enable appropriate CSRF/rate-limit/gateway controls.
- Retain and monitor the repaired queued/synchronous trace-ID linkage.
- Add production readiness checks for DB migrations, object store, event bus, LLM,
  secret backend, MCP command availability, and queue workers.
- Publish single-instance load, failure-recovery, upgrade, and backup/restore limits.
  Multi-region/HA evidence is excluded.

**Exit criterion:** a production profile refuses unsafe configuration and survives a
web/worker restart without duplicate schedules, lost runs, or orphaned approvals;
malicious tool code cannot read control-plane memory/files/secrets or block the API.

### High — complete the product lifecycle and remaining no-code closure

#### H1. Finish the new composition contracts

- **Managed files ship on the named bound paths, including published services.**
  Add the same binder to calibration/refinement, standalone export, and transient
  assistant-draft execution; propagate it through nested child manifests and
  dataset-manifest refs; then add dynamic graph input mapping plus governed folder
  and streaming capabilities. Keep project/object/digest verification identical on
  every supported surface.
- **Three first-party database MCP sidecars ship.** Make catalog status explicitly
  “turnkey / external prerequisite / blocked,” recursively preflight child workflows,
  verify live sidecar readiness, require a separate target database and least-
  privilege role instead of the control-plane credential for production, and either
  provide a supported remote GitHub path or stop presenting it as quick-connect in
  the Python-only image.
- **Typed Aria inputs/references and explicit step skipping ship.** Add artifact/
  trace/workflow/agent pickers, richer planner intent mapping, editable draft
  inputs, dry-run/preflight, and created-artifact deep links; keep JSON escape
  hatches for power users.
- **Agent configuration and workflow import/clone ship.** Add immutable agent
  versions/runtime tests and portable workflow dependency/file/secret mapping.

**Exit criterion:** cookbook 04 works in direct and nested workflows; cookbook 05
uses a clearly deployable connector; 12–15 execute through Aria with real typed
inputs and documented evidence; 07 either has a supported GitHub boundary or an
explicit external prerequisite. No recipe relies on a hidden host path or skipped
mutation step.

#### Additional High priorities

1. **Complete Agent lifecycle and project context:** extend the routed configuration
   workspace with immutable versions, runtime tests, evaluate/deploy/rollback, and
   health; retain the mounted project selector. Membership administration is
   excluded.
2. **Asynchronous evaluation service:** durable queues, full dataset/sampling policy,
   progress/cancel/retry, slices, error policy, weights, cost/token/latency, and
   immutable judge snapshots.
3. **Continuous evaluation and CI:** production sampling, scheduled suites, drift,
   quality budgets, alerts, and a documented CLI/CI quality gate.
4. **Unified artifact lifecycle:** common draft/version/test/publish/rollback/archive
   contract and UI adapters for all versioned artifacts.
5. **Actionable operations:** alerts, SLOs, operator context, incident workflow, fleet
   health, spend budgets, queue/worker health, and retention/export.
6. **Reusable workflow assets:** build a portable dependency bundle, mapping wizard,
   reusable custom components, and discoverable export around the shipped safe
   import/clone preflight.
7. **Server-authoritative tests:** execute component cases server-side and store
   immutable inputs, versions, outputs, scorer code, environment, and provenance.
8. **Judge/review correctness:** version judges, persist alignment results, enforce
   reviewer/assignment policy if retained, recover stale submissions with a
   cross-system idempotency contract, and display complete trace context.
9. **Cookbook runner:** install/version sample bundles, check prerequisites, capture
    evidence, and keep documentation executable in CI.
10. **Retain the proven full-suite isolation:** per-process/worker tracking and
    artifact roots, disabled async export, and cleanup now complete a supported-
    Python local run. Keep an isolation regression and monitor test-root cleanup.
11. **Trustworthy remote CI release signal:** the isolated audit now upgrades the bootstrap
    floor and gitleaks runs after earlier failures. Clear/manage artifact quota, make
    required evidence independently retrievable, restore/prove the wheel job, and
    obtain a fully green remote run. A passing test command inside a red workflow is
    useful evidence but not a shippable release gate.

### Medium — improve scale, analysis, and usability

1. Visual agent-output JSON Schema, field-transformation/JSONPath-expression, and
   loop-stop builders beyond the existing type-aware direct-port mapping popover;
   extend the router builder with typed fields/values and nested AND/OR groups.
2. Dataset CSV/JSONL import/export, splits, dedupe, bulk edit, pagination/exhaustive
   snapshot indication, and slice management; preserve the new active-as-of view,
   tags, weight legend, zero-total rejection, and coverage display while adding
   durable denominators and grouped slice metrics.
3. Statistical comparisons, confidence intervals, failure clustering, regression
   attribution, and run-versus-run graph/state/output diffs.
4. Managed tool/plugin SDK with signed packages, dependencies, compatibility,
   sandbox policy, test/publish lifecycle, and deployment preflight.
5. Global search/command palette, bulk actions, dependency graph, readiness checks,
   saved views, and keyboard workflows.
6. Split frontend monoliths into domain hooks, panels, route state, and shared
   lifecycle components; add recoverable local drafts and visible save state.
7. Run Playwright against the production build in CI, including non-admin and
   mismatched parent/project association paths.
8. Optional deployment tiers: multiple environments, canary/traffic controls,
   autoscaling, and HA only when the product target expands beyond a single
   self-hosted deployment.

### Low — polish after trust and lifecycle closure

1. Preserve the one proven shell polling owner and precise API/database label; add
   separate worker/provider/storage/queue readiness before claiming system health.
2. Finish responsive behavior and URL-addressable tabs across every workspace.
3. Improve empty states, terminology, cross-artifact deep links, and inline docs.
4. Add marketplace/gallery polish, favorites, recently used assets, and richer
   template discovery.
5. Add presentation-quality release/evaluation exports.

## 13. Concrete implementation sequence

The roadmap should be executed in this order because later product work otherwise
builds on untrustworthy evidence and authorization:

1. **Security containment:** retain write-only provider/MCP reads, auth-on service
   publishing, and fail-closed Preview; replace literal MCP storage, disable local
   auth in production, build the missing live-effect/egress broker, and patch known
   project/run/eval/queue/judge authorization paths.
2. **Access/resource-scoping architecture:** centralize authenticated resource
   repositories; add a generated route-permission inventory and negative parent/
   project-association contract suite. Organization/membership models are excluded.
3. **Evidence correctness:** retain the fixed non-admin list/detail parity,
   future-version rejection, server-side weight/tag propagation, fail-closed row
   verdict, all-scorer error reporting, zero-total rejection, active-as-of browsing,
   evidence labels, and trace persistence. Add exhaustive pagination, durable
   coverage/slices, immutable resolved run snapshots, fully controlled baselines,
   server-authoritative test records, and real deploy gates.
4. **Truthful release:** make one-reviewer HITL path-independent, remove unsupported
   enterprise approval fields, enforce review state, and implement a simple evidence/
   confirmation/rollback record.
5. **Production topology:** isolate effectful worker roles, harden configuration,
   add an effect ledger/idempotency contract, and define backup/restore, upgrade,
   single-instance recovery, and operational readiness. HA is optional.
6. **No-code closure:** preserve the shipped bound managed-file paths, MCP policy/DB
   sidecars, typed Aria, Agent/project pages, and import/clone; extend them through
   alternate runners, child workflows, portable dependency mapping, full Agent
   lifecycle, and deployment-equivalent connector preflight.
7. **Continuous quality and operations:** async/continuous eval, SLOs/alerts, fleet
   health, incident workflow, and cost budgets.
8. **Consistency and scale UX:** unified lifecycle components, monolith reduction,
   global navigation/search, and bulk operations.

## Appendix A — Cookbook continuity

The earlier cookbook-specific audit was rechecked against the current reviewed
workspace. It remains useful evidence, but the fail-closed Preview change materially
changes cookbooks 03, 08, and 09: their `python_code` preview/evaluation steps are
now safely refused and therefore no longer complete product paths. Conversely,
managed files close cookbook 04's direct document bridge, DB sidecars provide a
shipped connector option for 05, and typed Aria inputs materially upgrade 12–15.

| Result | Cookbooks | Current meaning |
| --- | --- | --- |
| Core result UI-complete on the standard stack | 01, 04, 06, 12, 16 | Managed project files close 04's root document path, and typed Aria inputs close 12's creation path. Documentation and current integrated-suite verification caveats remain. Cookbook 16's package/regression claims are still incomplete. |
| Mostly UI-complete | 02, 03, 05, 07, 08, 09, 10, 13, 14, 15 | 05 has a deployable database-sidecar demonstration but its GitHub recipe is external and its default DB credential is dev-only. 13/14 can explicitly skip an unwanted missing-input step; 13/15 use real prerequisite IDs when performing trace/calibration work. 03/08/09 still expose Preview/evaluation limits. |
| Not UI-complete | 11 | Evidence aggregation, rubric evaluation, go/no-go record, and rollback lineage remain manual. Formal waivers and multi-party signoff are excluded. |

The scope-adjusted implementation totals are **5 core UI-complete, 10 mostly
complete, 0 partial, and 1 blocked**. The converged frontend/browser/integration
checks support these code paths, while the interrupted broad backend run prevents
treating the repository as an all-green release candidate.

That count deliberately distinguishes central capability from package cleanliness.
Cookbook 16 remains “core capability” because its trace → Test Set → queue/review
observability/triage path is reachable; its advertised regression-proof loop and
missing YAML package are separately failed qualifications. Cookbook 10 remains
“mostly” because controlled baseline-versus-candidate evaluation is its central
purpose and the recipe never identifies distinct subjects. Applying that criterion
explicitly avoids treating all documentation defects as equivalent product blockers.

The individual evidence boundary is:

| # | Cookbook | Verdict | Verified product path or blocker |
| ---: | --- | --- | --- |
| 01 | Trustworthy Intake Classifier | **Core UI-complete** | Prompt authoring/playground, Test Sets, evaluation/baseline, calibration, and observability exist; generic evaluation supports prompt targets. |
| 02 | Precision Skills | **Mostly UI-complete** | Authoring, render/trigger tests, calibration, binding, package preview/download, and import exist. Export produces a ZIP while import selects an unpacked folder/files, so the round trip leaves the app and needs conflict handling. |
| 03 | Policy-Safe Decision Tool | **Mostly UI-complete** | Tool wizard/tests/calibration plus live workflow `python_code` and HITL paths exist. The documented safe/mocked Preview now refuses the workflow because ordinary Preview cannot isolate `python_code`; no isolated live-test replacement exists. New reusable registry implementations still require importable Python. |
| 04 | Document-to-JSON Pipeline | **Core UI-complete for a root workflow** | Select a workspace, import an Object Store object into File Directory, select its content-pinned snapshot in `file_input`, and Preview/evaluate/run through the scoped extractor without a host path. Digest/project/object-version verification is real. The cookbook prose must be updated, and a managed file declared only inside a child subworkflow is not yet bound. |
| 05 | Governed Tool Connectivity | **Mostly UI-complete via DB sidecars; GitHub recipe remains external** | Policy/readiness/discovery/playground/calibration/runtime/deployment preflight ship, and PostgreSQL/pgvector/AGE presets target working sidecars. GitHub/Ollama/Playwright/MinIO/Hugging Face still need operator provisioning. The DB presets default to CALIBER's own database/credential, so they are a dev demonstration until configured with a separate least-privilege target. Recipe observability/token prose is stale. |
| 06 | Grounded Knowledge Assistant | **Core UI-complete** | KB create/build/explore/query/graph/calibration and workflow/review paths exist, subject to normal provider, storage, and AGE readiness. |
| 07 | Support Triage Copilot | **Mostly UI-complete** | Prompt/skill/tool/KB, router/HITL, run, evaluation, and review primitives compose, but the documented required `escalate_bug → human_approval → GitHub create_issue` branch reuses cookbook 05's `npx` integration and cannot run in the shipped image. Its evaluation step also leaves the target at generic LLM instead of the workflow. The non-GitHub build/run branches remain composable. |
| 08 | Incident Response Copilot | **Mostly UI-complete; workflow evaluation refused** | Prompt/skills, Python fixture nodes, router/HITL, live runs, and review queue exist. Generic workflow-target evaluation invokes Preview and now correctly refuses the two `python_code` nodes, so the recipe cannot produce its advertised workflow scorecard until CALIBER has an isolated evaluation mode. |
| 09 | Self-Healing Workflows | **Mostly UI-complete as operator recovery** | Run monitor, checkpoints, debugger, retry/resume, approval, manifest editing, and publish exist. The required Preview validation of the patched `python_code` workflow now fails closed; the operator can still perform a real run, but there is no safe pre-rerun substitute and the patch remains human-authored. |
| 10 | Trustworthy Evaluation | **Mostly UI-complete** | Test Sets, judges, evaluations, review queues, and manual human-alignment metrics ship. The advertised baseline/candidate runs never select distinct artifacts; the candidate step does not reselect a judge/target; training claims per-example baseline deltas while the UI shows aggregate-card deltas only; and evaluation rows expose no trace IDs for the claimed direct enqueue step. Completed queue labels are not automatically ingested into alignment, so the evaluation-to-review/alignment loop requires manual bridges. |
| 11 | Release Signoff Factory | **Blocked for the scoped release path** | Evidence sources exist, but the deterministic rubric, evidence aggregation, go/no-go decision record, and rollback lineage remain manual/outside CALIBER; Releases is observational. Formal waivers, segregation of duties, and multi-party signoff are excluded. |
| 12 | Aria Evaluation Harness | **Core implementation path** | The heuristic selects `judge.create` and `eval_dataset.create`; each missing schema is rendered as a typed form, validated, then passes through the normal mutation gate and creates the real artifact. Complex values still use JSON and generated instructions describe the obsolete manual workaround. |
| 13 | Aria Review Governance Queue | **Mostly UI-complete with real trace IDs or an explicit skip** | Queue fields are collected, the created `queue_id` is wired into `add_items`, and supplied trace IDs can be enqueued. For the advertised queue-first/no-traces alternative, **Skip step** submits `approved:false`; the backend marks `add_items` skipped and lets the plan settle cleanly. Literal heuristic planning and JSON-heavy complex fields keep this below core. |
| 14 | Aria Governance Starter Kit | **Mostly UI-complete after explicit unwanted-step skipping** | Judge, dataset, and queue creation execute from typed forms. If the literal domain heuristic also schedules `review_queue.add_items` without traces, **Skip step** now cleanly skips it and resumes the plan. The heuristic remains brittle and generated instructions still describe the older workaround. |
| 15 | Aria Triage & Recalibrate Loop | **Mostly UI-complete with prerequisites** | Typed forms collect queue schema, real trace IDs, workflow ID, and agent ID; the queue result feeds add-items and calibration can enqueue, park, poll, and resume. Existing traces/workflow/agent remain explicit prerequisites, and the generated documentation still needs to describe that prerequisite-led path more precisely. |
| 16 | Production Observability & Triage | **Core UI capability; advertised regression loop/source package incomplete** | Trace filtering/detail, Test Set capture, queue enqueue/review, and workflow-target evaluation exist; needing prior runs is inherent. The recipe's eval step leaves the target at generic LLM rather than the repaired workflow and does not require corrected gold, so its “re-run after fix” proof is not executable as written. The folder also omits all four promised YAML contracts, and queue-review copy overstates displayed trace context. |

These counts assess whether each cookbook's **central product path** is reachable,
not whether every generated/source instruction or asset contract is correct. Those
recipe/package defects are explicit qualifications, especially for cookbook 16; a
“core” result does not mean its documentation bundle is clean. Nor is a cookbook
that runs under the local-admin single-environment stack proof of authentication,
effect isolation, safe release evidence, failure recovery, or operational
completeness.

## Appendix B — Verification history and superseded passes

Retained to preserve the evidence trail. These results predate the current
converged checks above and are therefore confidence signals, not current-pass
counts.

- Before the latest Agent/import/file/Aria/MCP integration tracks converged, a
  supported-Python local pass reported **5,065 backend passed / 7 skipped / 94.43%
  coverage**, **6 integration passed / 3 skipped**, **109 frontend files / 1,478
  tests passed**, and **23 browser passed / 1 skipped**, with TypeScript, ESLint,
  Ruff, mypy, package build, and the isolated dev dependency audit passing. Those
  numbers belong to the earlier working tree and are deliberately not promoted to
  current results.
- The safe workflow-import/clone and Agent track separately reported **30 focused
  backend** and **35 focused frontend** tests passing plus TypeScript, ESLint, Ruff,
  and diff checks before the other tracks were merged. These are track-local
  historical results, not a substitute for a converged suite.
- After managed-file import validation was added, **13 focused workflow-import
  behavior tests passed**. A simultaneous shared `.coverage` write made that
  command's coverage-gate artifact unusable, so it is recorded only as focused
  behavior evidence rather than a broad-suite result.

- Historical GitHub Actions run `30286528826` at `851b04597` had **5,023 backend
  tests pass, 12 skip, and 94.43% coverage**; UI had **109 files / 1,467 tests** and
  integration had **6 pass / 3 skip**. The workflow was nevertheless red because
  artifact quota failures prevented evidence uploads/wheel execution and the old
  security bootstrap found `setuptools==79.0.1` before gitleaks ran. Those numbers
  describe that commit, not this working tree.
- Earlier local runs used unsupported Python 3.14 and one xdist attempt wedged while
  flushing shared MLflow trace artifacts. That diagnosis motivated per-process
  SQLite/artifact roots, synchronous trace export, and teardown cleanup. A later
  pre-integration supported-Python full run completed. The current all-extras
  Python 3.12 xdist run again became pathologically slow and was stopped after
  5,024 passes, 8 skips, and 6 failures; all six passed serially. The exact cause
  still requires isolation, and neither run is represented as a completed current
  coverage gate.
- An initial supported-Python run installed only the core/dev extras and therefore
  reported optional-provider import failures, sandbox-denied loopback probes, and a
  packaged-UI drift check. Reinstalling the actual full test extras, allowing local
  test sockets, and synchronizing the built SPA resolved those setup failures.
- The first browser pass exposed stale navigation/docs assertions and an unstarted
  documented MinIO prerequisite; a concurrent rerun also produced one editor
  timeout under backend-suite load. Those diagnostics are not counted as final
  product failures; the corrected, dependency-complete, uncontended historical
  browser result is summarized above.

## Final assessment

CALIBER now demonstrates that a sophisticated agent workflow **can be composed,
cloned/imported, connected to a content-pinned document, executed with relatively
little code, and inspected/debugged deeply**. Routed Agent configuration, typed
Aria capability execution, MCP readiness policy, and first-party DB sidecars are
real improvements, not placeholder UI.

With enterprise capabilities excluded and the reviewed implementation applied, the
current risk-adjusted score is **3.1/5**. The answer remains two-part:

- **Yes** for predominantly low-code workflow composition, safe link-preserving
  reuse, managed-document input, registered Aria automation, live execution by
  trusted technical operators, and deep inspection/debugging. Preview safely reads
  pinned files and remains fail-closed for effects it cannot isolate.
- **No** for building, evaluating, deploying, and operating a production-grade
  agent system end to end without manual engineering.

Today the realistic positioning is:

> **A late-alpha / early-beta-candidate, self-hosted agent engineering studio and
> lifecycle control plane for trusted technical teams—not yet a production-grade
> agent platform.**

The implementation choices were mostly rational. The remaining work is to make
authentication, resource scoping, secrets, transitive file/MCP capability policy,
effect isolation/idempotency, evidence, truthful HITL, least-privilege database
connectivity, release, and operations as real as the workflow runtime. A complete,
stable supported-Python backend suite with its coverage gate plus a green remote CI
matrix are still required before this working tree can be treated as a release
candidate.
