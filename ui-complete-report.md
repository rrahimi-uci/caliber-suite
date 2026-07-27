# CALIBER repository-wide product and architecture review

**Review date:** 2026-07-26

**Reviewed state:** commit `b9d8e786e46bc11a81e7a08956926768484afd64`
plus the current uncommitted remediation worktree (22 modified tracked files and
6 untracked files when this re-review began). The commit hash alone does **not**
reproduce the reviewed state.

**Product target used for scoring:** a self-hosted, single-organization platform
for trusted developers and technical operators. Enterprise-suite requirements are
explicitly out of scope: organization/team administration, SSO/SCIM, multi-tenant
isolation, compliance certification/evidence, segregation of duties, enterprise
collaboration, and multi-region/high-availability guarantees. Their absence is
documented only as a scope boundary and does **not** reduce the maturity score.

This exclusion does not remove baseline production requirements. Any network-
reachable deployment still needs real authentication, safe secrets, protection
from command execution and SSRF, effect-safe preview/evaluation, reliable retries,
trustworthy release evidence, authenticated published APIs, rollback, and
actionable monitoring.

**Scope:** React frontend, Starlette APIs, database model, workflow compiler/runtime,
background workers, evaluation and refinement systems, storage, observability,
deployment assets, tests, product documentation, all 16 numbered cookbooks, and
their generated training material.

## Executive summary

> **Can CALIBER realistically enable developers to build, test, evaluate, deploy,
> and operate production-grade AI agent systems with a predominantly
> low-code/no-code experience?**

**No for production-grade end-to-end operation—even with enterprise readiness
removed from scope. Yes for predominantly low-code composition, local execution,
and inspection/debugging of deliberately isolated test runs.** CALIBER is an
unusually capable **low-code
workflow development and debugging environment**, but it is not yet a secure,
governed, production-operable no-code agent platform. It can visually compose and
run sophisticated workflows; it cannot yet carry a developer/operator reliably from
idea to production without Python packaging, deployment configuration, external
secret handling, and significant operator engineering.

The strongest shipped capabilities are real, not mock UI:

- a typed model with 29 registered node kinds plus manifest/configuration support
  for GraphRAG retrieval, structured ports/output schemas, and manual, event, and
  cron triggers;
- a polished graph editor with 13 starting templates;
- durable workflow versions, validation, preview, queued execution, checkpoints,
  retry/resume, run events, tool-call details, memory inspection, artifacts, and
  trace-oriented debugging;
- substantial prompt, skill, knowledge-base, dataset, judge, review-queue,
  evaluation, gateway, audit, and observability surfaces; and
- a broad automated code-test suite.

The production claim is blocked by correctness and security defects, not merely by
missing polish:

1. **The shipped login is a client-side `admin/admin` demo and the backend trusts a
   browser-supplied identity header.** There is no shipped server-validated
   session/token or enforced trusted-proxy boundary. Built-in enterprise SSO is
   not required for the scoped target.
2. **Integration secrets can leave the server.** MCP credentials can be
   persisted, serialized, and audited as ordinary JSON. The current worktree
   does fix plaintext provider-key readback, but it does not provide a durable,
   restart-safe, write-only resolver or a clear/rotate/revoke, reference-based MCP
   secret lifecycle.
3. **Resource scoping is inconsistent.** Multiple detail and mutation routes use
   unscoped primary-key lookups. Under the single-organization/trusted-operator
   target this is an API correctness and accidental wrong-resource mutation risk,
   not a claimed cross-tenant security boundary.
4. **The workflow deploy gate is both weak evidence and side-effect-unsafe.** The
   production route uses a fake agent executor, treats an empty dataset as a pass,
   measures only completion, and replays the workflow without preview mode or
   runtime approvals. Gate evaluation can therefore repeat real external mutations
   while still saying nothing about output quality, cost, or latency.
5. **Preview and evaluation are not safe dry runs.** Dedicated MCP, webhook, API,
   and external-app nodes can call live integrations even when execution is marked
   `preview=True`. Webhook/API URLs are unrestricted server-side egress, with no
   private-network/metadata protection or central outbound policy.
6. **Human-approval behavior is internally inconsistent.** The scoped product does
   not require enterprise role/quorum/segregation-of-duties workflows, but the UI
   exposes those controls without enforcing them. Timeout behavior and queued versus
   synchronous execution also disagree. Unsupported controls should be removed or
   made truthful.
7. **The one-click service publish path creates a backend-authentication-free
   endpoint by default wherever CALIBER is reachable.** Token support exists in
   the backend but is not exposed in the UI.
8. **Registered extension code is not isolated from the control plane.** Registered
   tools, their test path, and external-app entrypoints import and invoke installed
   Python callables in-process. `python_code` nodes and Aria tool drafts do use a
   local subprocess sandbox, but it is explicitly not production-grade isolation.
9. **The default authentication boundary composes with stdio MCP into host command
    execution.** MCP registration accepts an arbitrary executable and arguments;
    the admin test path launches them without an allowlist or sandbox. In the
    shipped default stack, a client can self-assert or inherit the local-admin
    identity, turning this into a reachable control-plane RCE path.
10. **Operations stop at observability.** There are useful traces and metrics, but no
   alert policies, configurable SLOs, continuous evaluation, drift monitoring,
   incident workflow, detailed agent/workflow health, trustworthy queue/worker
   readiness, or demonstrated single-instance failure recovery. Lease recovery
   also restarts an interrupted workflow from the beginning without an effect
   ledger or platform idempotency key, so a crash can duplicate external side
   effects.

The latest worktree makes concrete progress: it removes plaintext provider-key
readback, repairs workflow trace-ID persistence, fixes the non-admin evaluation-list
crash, rejects future dataset versions, propagates weights/tags into server-side
scorecards, makes error-bearing rows fail, replaces the misleading wildcard page,
and connects both shell indicators to liveness. Several are only narrow fixes:
evaluation list/detail semantics still disagree, partial scorer failures still
influence aggregates, historical dataset browsing is incorrect, weight semantics
are absent from the UI, and the health label exceeds what its database probe proves.

Removing enterprise-only requirements raises the scope-adjusted assessment from
**2.5/5 to 2.6/5**, but does not change the production answer: the highest-risk
blockers are universal runtime, security, evidence, deployment, and operations
requirements.

The correct product label is **advanced alpha / self-hosted technical preview**.
The codebase shows a credible platform direction, but current defaults and several
control-plane paths must not be presented as production-safe behavior.

## Overall maturity assessment

Scale used here: **0 absent, 1 prototype, 2 partial, 3 usable with material gaps,
4 strong, 5 production-complete**. Scores are reviewer judgments, not test
coverage, and the overall score is risk-adjusted rather than an arithmetic mean.

| Dimension | Score | Assessment |
| --- | ---: | --- |
| Visual workflow composition | **4.0/5** | Broad typed primitives, templates, validation, and a strong graph editor. Some advanced fields still require JSON or code-like expressions. |
| Prompt, skill, tool, and knowledge engineering | **3.2/5** | Prompts, skills, and KBs are deep; reusable custom tools still require an importable Python implementation; standalone agents lack a lifecycle workspace. |
| Developer debugging and run inspection | **4.0/5** | Best part of the product: run graph, events, checkpoints, retries, tool calls, memory, outputs, artifacts, and trace views. Trace-ID persistence is repaired in the worktree; deterministic replay remains incomplete. |
| Testing and evaluation | **2.7/5** | Real datasets, judges, weighted scorecards, baselines, calibration, and some regression gates; no durable large async eval, immutable run bundle, continuous eval, CI quality gate, or cost/latency gate. Several records accept client-supplied results, and preview execution is not reliably side-effect-free. |
| Deployment and release management | **1.8/5** | Workflow versions, aliases, rollback, and API publishing exist. A single environment is acceptable for this target, but fake/side-effecting deploy gates, open services, weak release evidence, and absent deployment-scoped configuration/secret controls still prevent production trust. |
| Operations and monitoring | **2.6/5** | Useful trace/metric exploration, token/cost/latency summaries, SSE, health, audit, system services, and coarse fleet/success ratios. Multi-region HA is excluded, but actionable alerts, trustworthy health, queue/worker operations, drift, and failure recovery remain incomplete. |
| Platform UX | **3.2/5** | Cohesive visual language and broad discoverability. Enterprise administration is excluded; fragmented artifact idioms, missing agent/secret/alert pages, raw IDs, giant workspaces, and misleading surfaces remain material UX debt. |
| Production safety and access control | **1.0/5** | Enterprise IAM/compliance is excluded, but the default identity is spoofable, MCP secrets are serialized, published APIs default open, preview permits SSRF/live effects, and stdio MCP composes into host command execution. |
| Architecture and operability | **2.6/5** | Typed domain/runtime, durable SQL state, storage/event abstractions, and extensive tests are strengths. Unrestricted egress, arbitrary command execution, at-least-once effects, in-process extensions, hard-coded mode switches, and scoping defects remain material without any enterprise assumptions. |
| End-to-end low-code/no-code lifecycle | **2.3/5** | A user can build, test, and debug predominantly in the UI. Production deployment and operation still require manual security, packaging, evidence, and operational engineering. |

**Risk-adjusted overall: 2.6/5, advanced alpha / self-hosted technical preview.**
Enterprise readiness is not scored; baseline production safety is. The ten
dimension scores have an arithmetic mean of 2.74, but the 1.0/5 production-safety
dimension caps the overall judgment: command execution, unsafe effects/egress,
open services, and false deploy evidence are production blockers even though the
workflow IDE itself scores highly.

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

- the 26 product route patterns backed by 26 lazy components in
  `caliber/caliber-ui/src/App.tsx` (25 page workspaces plus one workflow-run
  redirect), as well as login/wildcard handling, the workflow canvas/inspector/
  debugger, assistant shell, API client, and workspace state;
- the centralized backend registry of 40 route modules and 313 literal
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

### Current re-verification (2026-07-26)

The report and every changed path in scope were re-read against the current
worktree, not inferred from the earlier audit. Inventory was recounted directly:
40 registered route modules, 313 literal `Route(...)` declarations, 60 top-level
models, 26 authenticated route patterns / 26 lazy components, 29 registered
workflow component kinds, 13 workflow templates, 13 router operators, 262 backend
test files, 109 frontend unit/spec files, and 8 Playwright specs.

Fresh verification performed:

- `npm run typecheck` — **passed**;
- full Vitest — **109 files, 1,466 tests passed**;
- changed backend paths — **40 passed**;
- authentication/service/deployment/promoter/sandbox paths — **90 passed**;
- scheduler/runtime-approval paths — **24 passed**; and
- changed frontend paths — **21 passed**.

The full Vitest run emitted non-fatal existing warnings for unmatched MSW requests,
zero-size Recharts containers, a React `act(...)` boundary, and jsdom navigation.
Backend collection found **5,025 tests**; the PostgreSQL MCP module was skipped
because the configured database was unreachable. The full run was interrupted after
82m20s with **1,312 passed, 2 failed, 1 skipped, and 1 error**. Both failures were
sandbox `PermissionError`s while binding `127.0.0.1`; the error was an unclosed
SQLite connection reported as a `PytestUnraisableExceptionWarning`. Teardown also
reported unclosed sockets, and the run used unsupported Python 3.14. These results
are not a full-backend pass, but the environment-dependent failures are also not
treated here as confirmed CALIBER product regressions.
Focused subsets overlap and must not be summed. Playwright, real-provider/live-
integration scenarios, load testing, browser accessibility, and a penetration test were
not run. The repository and CI inventory below must not be mistaken for freshly
verified production behavior.

## Screens, pages, and components reviewed

| Product area | Reviewed surfaces | Result |
| --- | --- | --- |
| Shell and access | Login, App shell, sidebar, provider banner, local auth, route boundaries | Visually complete demo shell; not a production identity boundary. |
| Overview | Dashboard summary, live event badges, fleet coverage, and assistant execution/publish ratios | Useful high-level reliability entry point; not a configurable SLO/error-budget or per-agent/workflow health dashboard. |
| Prompts | Inventory/workspace, builder, playground, tests, calibration, versions, rollback, bindings | Deep and usable; version/governance semantics differ from other artifacts. |
| Skills | Inventory/workspace, wizard, render/trigger tests, packages, calibration, versions, bindings | Strong authoring path; routed and in-place experiences diverge. |
| Tools | Registry, wizard, detail, schema builder, sandbox, calibration, versions | Good registry UX around an implementation that must already exist in Python. |
| MCP | Catalog, connection setup, discovery, playground, policies, tests, calibration | Rich surface; catalog is not deploy-image aware and secret treatment is unsafe. |
| Files/storage | Object Store, project files API, workflow file/folder/bucket nodes | Multiple storage concepts do not yet compose into one safe file-reference contract. |
| Knowledge/RAG | KB inventory/editor, sources, builds, chunks, query playground, GraphRAG/AGE, calibration, versions | One of the strongest artifact workspaces; provider/storage readiness remains operator-managed. |
| Workflow Studio | Inventory/templates, React Flow editor, inspector, code view, versions, detail graph | Strong low-code composition environment. |
| Workflow runtime | Preview, queued runs, events, approvals, checkpoints, retry/resume, memory, artifacts, trace/debug panels | Deep runtime UX and repaired trace linkage; preview safety, approval policy, deterministic replay, and duplicate-side-effect risks remain serious. |
| Workflow deployment | Versions, navigation-hidden/deep-linkable deployments and promotions, service publishing, patches | Core backend primitives exist; current UI/defaults deliberately bypass production governance. |
| Test Sets | Dataset inventory/detail, examples, versions, trace import, MLflow sync | Useful curation; bulk/splits, historical-version display, immutable snapshots, and visible weight/slice semantics are incomplete. |
| Evaluation | Evaluations, detail scorecards, judges, alignment, review queues | Valuable ad hoc evaluation; not a continuous or release-grade evaluation system. |
| Aria | Assistant panel, plans, interactions, drafts, approval/publish flows | Broad shell; typed planning/execution is not reliable enough for autonomous no-code creation. |
| Observability | Trace search/detail/compare, metrics charts, Allure link, system services | Good inspection; lacks alert-to-action operations. |
| Gateway | Endpoints, guardrails, pricing, usage | Useful control-plane visibility; it does not by itself make CALIBER deployment secure or scalable. |
| Release/review controls | Audit log/export, review queues, Releases | Useful foundations; release evidence, review-state correctness, and advertised approval behavior are incomplete. Formal enterprise signoff is excluded. |
| Settings | Assistant, provider, services/runtime inventory, versioning, Allure | Mostly an environment-backed inventory. Provider keys are now write-only in the browser, but updates are process-local and there is no secret lifecycle. |

No production page was found for **Agents, Secrets, Alerts/SLO administration,
Benchmarks, or detailed agent/workflow health**. A Project API and
`WorkspaceSelector.tsx` exist, but the selector has no production caller. Missing
organization/team/role-administration pages are excluded from this assessment.

## 1. Overall product completeness

### Lifecycle closure

| Lifecycle stage | What works | What prevents a predominantly no-code production path |
| --- | --- | --- |
| Idea and design | Aria chat/plans, prompt builder, workflow templates, component guidance | Aria heuristic plans can omit typed inputs; no guided solution/cookbook installer; no standalone agent workspace. |
| Build | Visual workflows, prompts, skills, KBs, schemas, MCP/API/webhook nodes | Reusable tool implementation needs Python packaging; file/object references are fragmented; several advanced fields are raw JSON/expressions. |
| Test | Preview, sandboxes, component test runs, datasets, judges, workflow eval | Preview can execute live MCP/network/external-app effects; there is no workflow unit/assertion suite, server-authoritative component result record, full-dataset async runner, or reusable test-suite policy. |
| Evaluate | Weighted row scorecards, custom judges, baselines, alignment, review queues, prompt and workflow refinement gates | Synchronous caps, incomplete scorer semantics, no immutable run bundle or slice UI, no cost/latency, no scheduled/continuous eval, no CI product-quality gate, and mutable judges. |
| Deploy | Publish versions, alias deployments, rollback, workflow HTTP service | A single environment is acceptable, but the fake/side-effecting deploy gate, empty-set pass, weak evidence, and authentication-free default service publish are not. Formal multi-party approval is excluded. |
| Operate | SSE updates, trace detail/compare, tokens/cost/latency, logs/events, audit, health, coarse fleet/success ratios | Trace linkage is repaired; actionable alerts, trustworthy health, queue/worker visibility, effect idempotency, recovery evidence, and incident diagnosis remain incomplete. Multi-region HA is excluded. |
| Control | Four scopes, audit rows, review queues, dormant workflow promotion state machine | Spoofable identity, MCP secret disclosure, inconsistent resource scoping, misleading HITL controls, and observational release evidence remain. Organization/membership governance is excluded. |

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

#### C2 — MCP integration secrets can leave the server

- **[Remediated]** `_llm_setup_status()` resolved and returned full OpenAI and
  Anthropic keys at `routes/settings.py:1318-1333`; `GET /settings/llm` exposed
  them to any operator at `:1341-1347`, despite saying “Never returns secret
  values.” `Settings.tsx:630-705` read those values into browser password
  fields, and `tests/test_settings_routes.py:30-49` intentionally asserted the
  plaintext response. The route now returns presence plus a masked
  `••••<last 4>` fingerprint, the Settings fields are write-only, and the tests
  assert the secret is absent from the response body.
- Provider updates still mutate the running process environment. They are not a
  durable, restart-safe secret resolver and expose no clear, rotate, revoke, or
  deployment-binding workflow.
- MCP `env`, `headers`, and `auth_config` are ordinary JSON fields
  (`db/models.py:2024-2057`) and are serialized by list/detail routes for any
  authenticated user (`routes/mcp_servers.py:108-139`). The API permits literal
  tokens/passwords; update/delete audit details can include full configuration.
- `secrets.py:1-39,52-148` supports environment and file sources only. Manifest
  `secret_refs` are metadata, not a complete per-run injection/rotation system.

This blocks safe production use until secrets are write-only, reference-based,
scoped, and never serialized back to browsers or audit logs.

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
  control plane. Service publish/unpublish and metadata reads are likewise keyed by
  an unscoped workflow ID, and token administration is global rather than
  project-bound (`routes/services.py:144-163,224-398`);
- tool list/version paths apply visibility, while some direct detail/source/update
  paths start from an unrestricted primary-key lookup;
- evaluation creation resolves dataset, skill, workflow version, and judge IDs with
  unscoped lookups, so a known ID can cross project boundaries
  (`routes/evaluations.py:209-286,351-391`);
- review submission checks neither `reviewers`/`assigned_to` nor pending state, so
  any signed-in user with the IDs can submit or overwrite completed work
  (`routes/review_queues.py:387-430`); and
- judge test/alignment and several nested dataset routes bypass the scoped parent
  lookup (`routes/judges.py:229-338`).

Multi-tenancy is excluded from the product target, so these routes are not scored
as tenant-isolation failures. They remain serious resource-integrity and access-
control defects for a single organization with multiple developers, and can mutate
or expose the wrong workflow/project when an ID is known or associated incorrectly.

#### C4 — non-admin evaluation visibility is inconsistent — **[Partly remediated]**

`apply_visibility_filter()` required `visibility`, `owner`, and `project_id`
(`db/scoping.py:63-67,92-103`). `CaliberEvalRun` has `created_by`, not `owner`
(`db/models.py:758-814`). `list_evaluations()` passed that model to the helper
(`routes/evaluations.py:285-305`), so a non-admin list request deterministically
raised `AttributeError` before SQL execution — a hard 500 rather than a filtered
list. The detail path avoided the generic helper but granted access only to
admins, public rows, or rows in the active project
(`routes/evaluations.py:310-329`). It omitted a `created_by == identity.user_id`
branch, so the creator could not retrieve their own user-scoped run when it had
no project. Existing list tests use the default admin fixture and did not
exercise the crashing branch.

The crash and creator-readback defects were reproduced and fixed: the scoping
helper now resolves the ownership column (`owner`, else `created_by`, else a loud
`TypeError`), and detail adds a creator branch. The fix is narrower than its
comments claim, however. A non-admin project list requires both the active project
and `created_by == identity.user_id` (`db/scoping.py:123-129`), while detail permits
*any* row whose `project_id` equals the client-supplied active project, and also
permits the creator regardless of the row's visibility or project
(`routes/evaluations.py:315-336`). The client-supplied
`X-CALIBER-Project` is not a reliable resource-scoping boundary. The new tests cover
the original crash and project-less creator
case, not same-project/different-owner or creator-in-another-project semantics.
There is also a create-to-list hole: eval creation accepts no visibility field,
while the model defaults to `visibility="project"`. Creating without an active
project persists `project_id=None`; the creator detail branch can read that row, but
the list filter cannot return it. Tests seed a synthetic `visibility="user"` row
rather than exercising this actual create → list → detail path.

#### C5 — workflow deployment gates can give a false safety signal

- `routes/workflow_deployments.py:93-140` calls `promote()` without the live
  configuration or an executor.
- `workflows/promoter.py:1544-1573` therefore calls `build_executor(None)`, which
  selects `FakeWorkflowExecutor` (`:176-190`). The candidate is not evaluated with
  the production agent provider.
- `evaluate_deploy_gates()` treats a missing/empty dataset as `passed=True` with a
  1.0 pass rate (`:1304-1315`).
- It silently selects at most the first 50 active examples without an explicit
  deterministic ordering or sampling record (`workflows/promoter.py:112,1291-1319`).
- For non-empty data it counts only `run.status == "completed"` (`:1328-1345`). It
  does not compare output to expected values, call a judge, measure regression,
  cost, or latency.
- The Inspector exposes `min_overall_delta` and `max_tone_regression`, but this gate
  reads only `min_pass_rate` (`Inspector.tsx:3404-3443`;
  `workflows/promoter.py:1333-1335`), making two configured controls decorative.
- The gate calls `execute(plan, ...)` without `preview=True` or runtime approvals
  (`workflows/promoter.py:1322-1333`). Execution defaults both controls off
  (`workflows/runtime.py:2730-2743`), so approval-requiring tools do not pause and
  live MCP, webhook, API, or external-app mutations can run once per gate example.

This cannot safely be treated even as a dry-run smoke check. It must be rebuilt as
a side-effect-contained evidence gate; in its current form it must not authorize
production release.

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

#### C7 — one-click workflow service publishing defaults to no backend auth

- New services default `auth_required` to false (`routes/services.py:185-207`;
  `db/models.py:2085-2089`).
- Invocation, status, and OpenAPI enforce a token only when that opt-in bit is true
  (`routes/services.py:414-480,549-611`).
- `WorkflowDetail.tsx` publishes with an empty payload and shows “Open · no auth”;
  it exposes no auth toggle or token lifecycle even though backend token machinery
  exists.

An explicit, high-friction “public endpoint” choice can be supported later. It
must not be the low-code default.

Open invocations are also audited as `service_token:{workflow_id}` even when no
token was supplied (`routes/services.py:509-530`), so their actor lineage is
misleading.

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
  to `LocalSubprocessToolSandbox` (`workflows/runtime.py:5639-5656`;
  `assistant/agent_tools.py:959-983`). It uses a temporary directory, `python -I`,
  an empty environment, and a hard timeout.
- That service explicitly states that production needs container/VM/kernel
  isolation (`tool_sandbox/service.py:34-42,95-105`), and Compose deploys no
  separate sandbox service.

This requires fully trusted workflow authors as well as administrator-controlled
installed packages; an explicit allowlisted entrypoint registry is absent. The local
subprocess is useful containment for the two integrated paths, but the mixed
execution model is incompatible with untrusted workflow authors/extension code and is
not a production-grade sandbox boundary.

#### C9 — preview/evaluation can perform live unrestricted network effects

`preview=True` does not establish a universal effect boundary:

- dedicated MCP nodes call the live gateway (`workflows/runtime.py:5199-5214`);
- webhook and API-request nodes call the real sender (`:5253-5274,5303-5335`);
- external-app entrypoints execute in-process and merely receive preview metadata
  (`:2666-2714`); and
- only selected registered-tool side effects are mocked, while knowledge builds
  have their own skip behavior.

Generic workflow evaluation uses preview execution, so an evaluation dataset can
repeat those live calls. The user-facing Preview action has the same exposure.
Worse, webhook/API manifests accept arbitrary URLs and headers
(`workflows/manifest.py:744-792`), and the default sender passes them to
`httpx.Client.request` with no scheme/host/IP allowlist, private/loopback/link-local
or cloud-metadata block, DNS-rebinding defense, or centralized egress policy
(`workflows/runtime.py:2445-2472`). This is both a correctness problem and an SSRF/
internal-network access primitive for anyone allowed to author or preview a
workflow. Preview must default to deterministic mocks, and all real outbound calls
need a central policy and network-level containment.

#### C10 — arbitrary stdio MCP registration composes with demo auth into command execution

- MCP create accepts an arbitrary `command` and `args`
  (`routes/mcp_servers.py:195-206`; `schemas.py:3086-3106`).
- The test-connection path is admin-only (`routes/mcp_servers.py:326-364`), but C1
  shows that the default stack trusts a client assertion and otherwise falls back
  to `@local-admin`.
- The gateway passes the configured executable and arguments directly to
  `StdioServerParameters`/`stdio_client`, with no command/package allowlist,
  signature check, separate worker, or OS/container sandbox
  (`mcp_gateway.py:279-295`). The process starts even if MCP initialization then
  fails.

Therefore any client that can reach the default API can obtain the shipped admin
identity, register a process command, and make CALIBER execute it on the host. This
is a critical remote-code-execution chain in the default deployment model. Arbitrary
stdio registration must be disabled in production; approved servers need signed/
allowlisted packages and an isolated, least-privilege execution worker.

## 2. Workflow Builder

### Capability assessment

| Capability | State | Evidence-based assessment |
| --- | --- | --- |
| Agent nodes | **Shipped** | Agent nodes support inline/registered prompts, tools, skills, handoffs, memory/session, model settings, and output schemas. Deployed nodes sync into the backend Agent Fleet. |
| Standalone agent creation/management | **API-only** | Agent CRUD/fleet records exist, but there is no routed Agents registry, authoring, deployment, version, or health workspace in the UI. |
| Prompt creation/engineering | **Strong** | Builder, templates, variables, playground, test history, baseline, calibration, bindings, aliases, and rollback are substantial. |
| Skill creation/engineering | **Strong** | Wizard, content, trigger/render tests, scenarios, packages, calibration, bindings, and skill versions are present. |
| Reusable tool creation | **Partial/code-required** | The wizard requires a Python dotted module and callable already importable by the runtime (`ToolWizard.tsx:254-296`). Schemas and tests are low-code; implementation and packaging are not. |
| Tool sandboxing | **Partial/unsafe for extensions** | `python_code` nodes and Aria source-tool drafts use a constrained local subprocess. Registered tools, their tests, and external-app entrypoints still execute imported Python in-process; the local sandbox is not container/VM/kernel isolation. |
| MCP integration | **Partial** | Registration, discovery, invocation, policy, generated tests, and calibration exist. The GitHub catalog entry launches `npx`, while the final `python:3.12-slim` image contains no Node runtime (`McpServers.tsx:833-841`; `deploy/caliber/Dockerfile:9-18`). |
| Knowledge/RAG | **Strong** | Ingestion, chunking, embeddings, dense/hybrid retrieval, GraphRAG, Apache AGE, query playground, builds, calibration, versions, and workflow nodes ship. |
| Structured outputs | **Shipped, developer-oriented** | Agent/workflow output schemas and JSON validation exist, but agent output-schema authoring remains a raw JSON textarea. Tool input/output schemas have a visual builder. Published services do not validate runtime output against their advertised schema. |
| Multi-agent orchestration | **Shipped** | Handoffs, parallel fan-out/join, session-scoped per-agent-node memory, handoff context, subworkflows, and multi-agent templates are real. This is not an arbitrary shared multi-agent state store. |
| Conditional logic | **Shipped** | Ordered router branches and fallback behavior execute in the runtime. The visual IF field/operator/value builder has 13 operators; it lacks typed field/value pickers and nested AND/OR groups. |
| Parallel execution | **Shipped** | Parallel, join-all/join-any, and bounded `for_each` concurrency are implemented. |
| Loops | **Shipped** | `for_each` and bounded loop nodes execute with caps and stop conditions. Stop expressions are still code-like. |
| Human-in-the-loop | **Partial/misleading** | Explicit approval/tool nodes can pause/resume, which is enough for a one-reviewer target. Advertised role/quorum/assignment/SoD controls do not work and should be removed or implemented; timeout/path behavior is inconsistent. An approval-required tool selected inside an Agent is skipped rather than queued. |
| Wait/event resume | **Shipped** | Durable `wait_until` and named external-event resume are integrated with checkpoints and run actions. |
| Scheduling | **Partial** | Per-workflow manual/event/cron triggers, cron/timezone fields, next-run preview, target deployment, an Enabled toggle, and the scheduler ship. There is no central schedule inventory/calendar, execution history, backfill, overlap/missed-run policy, or independently deployed scheduler role. |
| Versioning | **Inconsistent** | Workflows have mutable drafts plus immutable published versions, diff, restore, aliases, and rollback. Prompt/skill/tool/KB/test-set idioms differ; some artifacts lack governed promotion or true rollback. |
| Optimization | **Partial** | Provider code supports MetaPrompt, SkillMetaPrompt, GEPA, DSPy BootstrapFewShot, and DSPyMIPRO; selection reaches four names and prompt UI options expose MetaPrompt/GEPA. README claims of nine optimizers are not current behavior. |
| Workflow reuse | **Partial** | Subworkflows, node copy/duplicate, restore-as-draft, and YAML/Python export are useful, although the export page is unlinked and only manually deep-linkable. There is no duplicate-as-new-workflow action, import UI, portable dependency bundle, or reusable custom-node library. |

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
2. MCP catalog entries are not preflighted against the actual runtime image.
3. Object Store, project files, run files, and local-path ingestion do not share one
   first-class file-reference protocol. Cookbook 04 still needs a host path.
4. The async run request advertises `input_files`, but the queued create/worker path
   does not materialize them; the synchronous preview path has separate file binding.
5. The default Aria planner emits matching mutation steps with empty inputs, and
   interaction answers are not merged into those inputs. Approval invokes the
   handler and fails validation; denial skips the step. The autonomous cookbooks
   therefore require users to recreate the artifacts manually.
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
| Clone workflows | **Partial** | Nodes/selections can be duplicated, and “Restore as draft” copies a selected version into a new draft in the same workflow. No duplicate-as-new-workflow action with dependencies exists. |
| Reuse components | **Partial** | Prompts, skills, tools, KBs, and subworkflows are reusable. No governed component bundle/custom node marketplace exists. |
| Import/export | **Partial** | An unadvertised, manually deep-linkable workflow-version page exports YAML and Python. Editor Code View exposes manifest JSON/generated Python but no download control. Manifest import is API-client-only; no portable dependency-bundle/mapping round trip exists. Skill export/import is not a clean ZIP round trip. |
| Publish an API | **Partial and unsafe by default** | Service/OpenAPI/status endpoints exist; auth configuration and tokens are absent from the UI, so the default has no backend authentication wherever CALIBER is reachable. |
| Preview safely | **Unsafe** | The preview flag mocks only selected paths. MCP, webhook, API-request, and external-app nodes can perform live effects, so Preview and workflow evaluation are not reliable dry runs. |

### Trace-link correctness defect — **[Remediated]**

`WorkflowRunResult` carries both `mlflow_run_id` and `mlflow_trace_id`, and runtime
execution populates both (`workflows/runtime.py:2101-2121,2816-2819`). The async
worker and synchronous route persisted only the MLflow run ID
(`orchestrator/workflow_run_worker.py:1700-1704`;
`routes/workflow_versions.py:945-957`). No code path in `caliber/src/caliber/`
ever assigned `CaliberWorkflowRun.trace_id` for a workflow run, yet two shipped
endpoints read it: the in-app span viewer
(`routes/workflow_runs.py:951-972`) and trace→run localization
(`routes/workflow_versions.py`, `RUN_BY_TRACE_PATH`). The integrated trace panel
was therefore always empty and by-trace lookup could never resolve a workflow
run, despite a real trace having been produced.

Both persistence sites now assign `result.mlflow_trace_id`. Four new tests cover
the queued and synchronous paths and both dependent endpoints; all four fail
against the pre-fix code.

### Frontend maintainability and data-loss risk

Several product workspaces are monoliths:

- `KnowledgeBases.tsx`: about 9,006 lines;
- `Prompts.tsx`: about 6,620 lines;
- `components/workflows/Inspector.tsx`: about 6,225 lines;
- `WorkflowEditor.tsx`: about 5,237 lines; and
- `WorkflowDetail.tsx`: about 4,582 lines.

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
| Dataset evaluation | Real, but synchronous; defaults to 50 examples and caps workflow targets at 20 (`routes/evaluations.py:72-79,400-406`). The UI displays the current dataset version but cannot select a historical version and does not expose truncation, `max_examples`, or pass threshold. Workflow-target rows can perform live integration effects. |
| Regression testing | Prompt and workflow refinement/calibration enforce candidate gates, but the workflow path defaults to structural/fake evidence unless a real provider is configured and is not dataset-version-pinned. Prompt promotion verdicts remain advisory/operator-supplied; the separate direct deployment “gate” is only a fake-executor completion check. |
| Benchmark management | Benchmark worksheet CRUD APIs and a frontend helper library exist, but no routed UI uses them and no server-side benchmark runner executes them. They should not be counted as shipped benchmark management. |
| Prompt evaluation | Deepest evaluation surface. The public claims still overstate optimizer breadth and promotion uniformity. |
| Offline evaluation | Possible with deterministic/fake/local components, but generic evaluation explicitly requires a real configured provider. No reproducible offline bundle is presented. |
| Continuous evaluation | **Absent.** No schedule, production sampling policy, drift detector, alert, or automated feedback-to-eval monitor was found. |
| Quality metrics | Scorers and judges are useful. A run is completed if at least one row has no recorded error; an error on every row makes it failed (`routes/evaluations.py:460-466`). **[Partly remediated]** A row with a scorer error can no longer pass, but surviving scores still feed per-scorer aggregates and `overall`, and only the first scorer error is retained (`eval/scorecard.py:328-370`). There is no scorer-coverage/completeness metric. |
| Cost/token/latency metrics in eval | **Absent from generic eval records.** These exist in observability but are not joined into scorecards or release gates. |
| Failure analysis | Per-row failure evidence is useful, but generic eval rows are not joined to workflow runs/traces. No clustering, slicing dashboard, statistical significance, or root-cause workflow exists. |
| Dataset weights and slices | **[Partly remediated]** The loader now carries weight/tags and the scorecard computes weighted scorer means, `overall`, and `pass_rate`. Frontend result types/table omit both fields (`caliber-ui/src/api/types.ts:1374-1384`; `EvaluationDetail.tsx:225-263`), and no grouped slice analysis exists. Raw pass/fail counts are displayed next to weighted rates without distinction. All-zero weights are accepted, producing `overall=0`/`pass_rate=0` and empty aggregates even when raw rows passed. |
| Dataset-version correctness | **[Partly remediated]** Creation now rejects a requested version greater than the current dataset version. Historical browsing is still wrong: `list_examples?version=N` selects rows *created at N* and, by default, still excludes superseded rows (`routes/eval_datasets.py:248-295`), while evaluation and restore reconstruct the active set *as of N*. The UI can therefore preview a different set from the one it restores or evaluates. |
| Evaluation reproducibility | `CaliberEvalRun.results` does persist the evaluated rows inline, including example ID, input, expected output, prediction, scores/error, weight, and tags. It does not record a cryptographic content/run digest, full pre-truncation inventory or sampling decision, or a resolved bundle of skill content/version, prompt content/alias, draft workflow manifest, judge definition/model, and provider configuration. The dataset's `mlflow_digest` describes the latest external sync, not the evaluation snapshot; merge-only sync omits weights/tags and cannot remove locally retired rows or old inputs after input-changing revisions. Workflow refinement resolves the current active dataset by name (`orchestrator/workflow_stages.py:183-214`). An unversioned prompt ref resolves version `1` (`routes/evaluations.py:127-140`) while the request schema documents it as “latest” (`schemas.py:2760-2762`). |
| Baseline comparisons | The UI offers a run as baseline based only on matching `dataset_id` (`EvaluationDetail.tsx:67-78,150-169`). It does not require the same dataset version, target, target version/content, subject, model/provider, or scorer/judge suite, so displayed deltas are not controlled regression evidence. |
| Judge/review correctness | Judges and queue question schemas are mutable/unversioned; historical evals retain a token rather than an immutable definition snapshot. Judge test/alignment use bare lookups, and alignment is ephemeral. Review submission does not enforce queue-active/pending or answer type/options rules; repeats can overwrite local state and duplicate MLflow assessments, with external write occurring before local completion. Reviewer assignment is optional for this target. |
| Test-result trust | Prompt/tool/skill durable-run create routes accept browser-supplied scores, verdicts, outputs, and reasoning, then recompute only aggregates. These are UI histories, not trustworthy server-executed evidence records. |

### Automation-suite assessment

The repository contains 262 backend test files, 109 frontend unit/spec files, and
8 Playwright specs. CI runs lint/type checks, backend tests, integration tests,
Vitest, typecheck, and a frontend build. That breadth is a real engineering
strength. However:

- default admin fixtures hide non-admin authorization failures — this is how the
  eval list crash survived (now covered by non-admin tests, but the fixture
  default itself is unchanged and will hide the next one);
- no cross-user/project workflow-run authorization tests were found;
- no tests vary HITL required role, quorum, timeout, or actor identity while those
  fields remain exposed;
- deploy-gate tests encode empty-dataset/completion-only behavior rather than
  rejecting it;
- unequal dataset weights are now covered, but zero-weight and partial-scorer
  completeness contracts remain under-specified;
- Playwright files and scripts exist but are not run by the checked CI workflow;
  and
- the marked PostgreSQL MCP integration test is not provisioned by CI.

Coverage-oriented success must not be used as product-claim validation.

## 5. Deployment experience

### Shipped capability

- mutable workflow drafts plus immutable published versions, diff, restore into a
  new draft, and publish;
- alias deployment records, optimistic concurrency, rollback, and live-deployment
  deletion protection;
- cron/event/manual triggers;
- a dormant workflow promotion/approval state machine;
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
   contains stale copy implying that production requires approval. Single-
   environment operation is acceptable for the scoped target; the gap is the
   misleading hidden/deep-linkable UX and immediate rotation around an unsafe gate,
   not the absence of multi-environment promotion itself.
2. The deploy gate has the fake-executor/empty-pass/completion-only defects described
   above and can execute live external effects while replaying its evidence set.
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
5. Workflow API publishing defaults to backend-authentication-free access wherever
   CALIBER is reachable, and the UI omits token management.
6. The Inspector exposes per-trigger cron, timezone, next-run preview, target
   deployment, and enablement. There is no central operations calendar/inventory,
   execution history, backfill, overlap policy, or missed-run handling UI.
7. Skills, tools, KBs, prompts, test sets, judges, and agents do not share one
   draft/version/test/publish/rollback contract.
8. `CaliberWorkflowDeployment.environment` exists, but promote requests cannot set
   it and alias rotation does not populate it. This is a dormant/inconsistent field
   and schema/API UX debt, not evidence that a multi-environment product is needed.
9. Published services validate input schema but not the runtime output against the
   advertised output schema; they provide polling only and no request quotas or
   bounded execution policy. Callbacks and traffic splitting are optional.
10. Crash recovery is at-least-once for side effects. An expired running lease is
    reset to queued, and a run without a wait/approval checkpoint restarts from the
    beginning. There is no platform effect ledger or per-node idempotency key, so a
    mutation completed just before process failure can execute again.
11. The default deployment exposes an admin identity fallback while admin-created
    stdio MCP configurations can select and launch arbitrary host executables in the
    CALIBER process boundary.

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
- Prometheus metrics, a database liveness endpoint, a `/readiness` configuration-
  status response, system-service status, runtime configuration inventory, audit
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

**[Partly remediated]** The sidebar footer's animated “System Online” indicator was
hard-coded; it and TopBar now use the same `useHealthStatus` implementation and the
footer reports “System Unreachable” when `/health` fails. They do **not** share one
poll: each mounted component creates its own immediate/30-second request cycle.
More importantly, `/health` proves only API/package and database `SELECT 1`
reachability. It does not check workers, scheduler, queue lag, MLflow, object store,
event bus, or provider connectivity, so the label still overstates platform health.

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

1. Agents and Projects exist in backend concepts but are missing from navigation;
   secrets, alerts, and benchmarks are absent as routed product
   workspaces even where lower-level fields, resolvers, or APIs exist. Organization,
   team, and membership administration is intentionally excluded.
2. The unused `WorkspaceSelector` means the project header/local-storage mechanism
   is not a reachable product workflow.
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
9. Navigation-hidden but deep-linkable deployment panels, API-only benchmark/
   import/token features, stale production-approval copy, and hard-coded mode flags
   make discoverability differ from implementation reality.
10. The very large page modules make consistent behavior harder to maintain.
11. Navigation and Settings are not permission-aware enough. The shell exposes
    settings/audit and other administrative destinations broadly, while Settings
    always fetches operator data and renders admin-only mutation controls that the
    backend may reject. Users discover authorization through failures rather than
    an explicit role/capability model.

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
| Secrets | **Critical gap** | Provider keys are no longer readable in the browser, but MCP accepts literal credential JSON and serializes it. A durable write-only resolver with rotation/revocation is required; an enterprise vault marketplace is not. |
| Published API authentication | **Critical gap** | UI-published workflow services default to no backend authentication, and the UI does not expose the existing token lifecycle. |
| Effect isolation and egress | **Critical gap** | Preview/evaluation can execute live MCP/webhook/API/external-app effects; webhook/API nodes permit SSRF-capable unrestricted egress. |
| Extension/MCP execution | **Critical gap** | Registered/external-app Python runs in-process, while arbitrary stdio MCP configuration composes with default admin auth into host command execution. |
| HITL/review correctness | **Misleading** | A single authorized reviewer is sufficient for this target. The product must remove unsupported role/quorum/SoD controls or implement them, enforce timeout/path consistency, and prevent invalid or repeated review-state overwrites. |
| Audit correctness | **Partial** | Transaction-coupled rows, filtering, redaction, and export are useful. WORM/SIEM/compliance evidence is excluded, but actor attribution and secret redaction must still be correct. |
| Release evidence and recovery | **Critical gap** | Formal enterprise signoff is excluded. Fake/empty/completion-only gates, live side effects, incomplete pinning, and duplicate effects after recovery still make a simple one-operator release unsafe. |

Enterprise exclusions remove product-suite breadth requirements; they do not make an
untrusted identity boundary, host command execution, secret disclosure, SSRF, or
unsafe release evidence acceptable.

## 9. API, runtime, and architecture assessment

### Architectural strengths

1. **Typed workflow core.** Pydantic manifests, compiler validation, an explicit IR,
   and a deterministic graph interpreter provide a stronger base than a frontend-
   only canvas.
2. **Durable execution state.** SQL-backed runs, events, checkpoints, approvals,
   leases, retry lineage, memory, artifacts, and audit rows are appropriate control-
   plane primitives.
3. **Modular integration seams.** Tool resolver, sandbox, storage backends, event
   buses, tracing, provider interfaces, knowledge runners, and service contracts are
   separable and testable.
4. **Good workflow resilience primitives.** Error boundaries, waits, event resume,
   bounded loops, checkpoints, cancellation, and worker heartbeats are substantive.
5. **Broad API.** The 40 registered route modules cover nearly every advertised
   artifact and lifecycle concept.
6. **Engineering test investment.** Backend, frontend, integration, and E2E assets
   are extensive even where the product contract needs correction.

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
limiting default off. A single replica is acceptable; the remaining defaults make
first boot easy but need an explicit, fail-closed production profile and readiness
gate.

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
  export; normal version navigation goes to the editor. Manifest import has no UI;
  duplicate-as-new-workflow and portable dependency bundles are absent.
- Workflow service token CRUD exists but the publishing UI cannot enable auth or
  manage tokens.
- `WorkspaceSelector` exists but is not mounted.
- Aria plan mutations in cookbooks 12–15 have empty inputs: approval fails handler
  validation, while denial skips the step. Typed inputs are not collected or merged.
- Demo workflow tools intentionally contain no-op/stub external actions; templates
  using them demonstrate orchestration, not production connectivity.
- Optimizer docstrings/README enumerate future algorithms that selection/provider/UI
  do not expose.
- Releases is explicitly observational, despite its placement implying a release
  operations room.
- **[Remediated]** Provider settings text and backend docstrings claimed
  secret-presence behavior while returning full keys. Behavior now matches the
  documented contract.
- Evaluation schema documentation still says workflow targets return a redirecting
  400 even though the route executes workflow targets, and the eval-run model
  docstring mentions a stale `reference` target absent from the current schema.
  It also documents an unversioned prompt subject as “latest” while runtime loads
  version 1.
- **[Partly remediated]** The sidebar and TopBar indicators now share hook code,
  but instantiate separate polls and label a database-only liveness probe as
  whole-system health.
- Workflow editor unmount autosave hides errors.
- Generated cookbook footers still claim every recipe is implementable through the
  UI, while `docs-site/cookbooks/FEASIBILITY.md` retains false HITL role/quorum/
  timeout and prompt-playground claims. `cookbooks/training/content.py` still says
  evaluations cannot score workflows, the cookbook README's ladder omits cookbook
  16, and cookbook 10's verification/assets still describe alignment as a manual
  by-hand step. Generated pages and source assets therefore disagree even where the
  current worktree improved cookbook 03/08/10 prose.

## 11. Missing end-to-end workflows

These are not individual buttons; each is a product path that currently breaks.

1. **Standalone agent lifecycle:** create agent → configure model/prompt/skills/tools/
   memory → version → test → evaluate → deploy → monitor → rollback.
2. **No-code custom integration:** choose connector/API → bind write-only secret →
   test in deployment-equivalent sandbox → version → approve → reuse.
3. **Uploaded document to workflow:** upload binary → select first-class file ref in
   run form → parse in queued worker → preserve checksum/media/lineage → inspect
   output without a host path.
4. **Trustworthy release:** select immutable candidate + target deployment → run full
   quality/cost/latency/regression suite with the real executor → review evidence →
   explicitly confirm → publish → record outcome → rollback.
5. **Continuous evaluation:** sample production traces → apply versioned judge suite
   → detect drift/regression → alert owner → create remediation work item → verify
   candidate → close alert.
6. **Incident operations:** alert/SLO breach → affected agent/workflow health → trace
   cluster → compare last good/current release → retry/rollback → postmortem/audit.
7. **Reusable solution bundle:** clone/import workflow with dependent prompt/skill/
   tool/KB/dataset contracts → map secrets/files → preflight → publish.
8. **Aria autonomous build:** natural-language goal → typed dependency-aware plan →
   collect missing fields → validate/dry-run → approve by risk → execute every step →
   deep-link created artifacts.
9. **Safe credential lifecycle:** create write-only reference → bind to a deployment
   → rotate → see consumers → revoke → verify no values entered logs/browser/audit.

## 12. Prioritized roadmap

### Critical — block production claims and external rollout

#### C0. Establish production authentication and resource scoping

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

#### C1. Remove secret exfiltration and secure published services

- Preserve the current worktree's provider-key fix: reads return only
  presence/fingerprint and browser inputs remain write-only. Replace its
  process-local update mechanism with durable secret references and explicit
  clear/rotate/revoke semantics.
- Require secret references for MCP/auth headers; reject literal secret-shaped
  values or store them through an encrypted secret service.
- Redact structured secret keys recursively before audit persistence.
- Provide a pluggable durable secret resolver with deployment scoping, rotation,
  revocation, and consumer visibility. Specific enterprise vault integrations are
  optional adapters.
- Default workflow services to authenticated; expose auth, token scopes, expiry,
  rotation, revoke, CORS, quotas, and rate limits before Publish.

**Exit criterion:** no API response, browser state, trace, log, or audit row contains
resolved provider/MCP/service secrets; a UI-published service rejects anonymous
invocation by default.

#### C2. Make preview/evaluation side-effect-safe and control outbound egress

- Define one runtime-wide effect contract for preview, test, evaluation, deploy
  gates, retry, and replay. Default every integration to deterministic mock/recorded
  behavior unless the operator explicitly chooses an isolated live test.
- Route MCP, webhook, API, external-app, registered-tool, and knowledge effects
  through a common broker with capability policy, audit, timeout, budget, and
  idempotency keys.
- Disable arbitrary stdio MCP command registration in production. Permit only
  signed/allowlisted server packages and launch them in an isolated least-privilege
  worker, never the API/control-plane process.
- Enforce approved schemes/domains, resolve and validate destination IPs, block
  loopback/private/link-local/metadata networks, defend against DNS rebinding, and
  add network-level egress isolation.
- Add an effect ledger so lease recovery and retries can resume or deduplicate
  mutations instead of blindly restarting them.

**Exit criterion:** ordinary Preview, evaluation, and release-gate runs cannot reach
the network or mutate external systems; authorized live tests cannot reach internal
metadata/private services and remain idempotent across worker failure. API clients
cannot select or launch arbitrary host executables.

#### C3. Rebuild workflow release gates as real evidence gates

- Pass the live deployment configuration/executor into promotion.
- Fail closed for missing/empty datasets and pin dataset, example IDs/content digest,
  judge, prompt, tool, skill, KB, model, provider configuration, and workflow
  versions. Reuse/extend the generic evaluator's future-version rejection and
  server-side weight/tag propagation in the release gate, and snapshot the actual
  resolved inputs.
- Evaluate expected outputs/custom judges and baseline regression—not completion.
- Add minimum sample size, failure budget, cost/token/latency thresholds, partial-
  error policy, and stored per-example evidence.
- Make the gate asynchronous and persist an immutable verdict linked to the
  candidate and target deployment.

**Exit criterion:** intentionally wrong output from a successfully completed real
agent fails promotion; empty or truncated evidence cannot pass silently.

#### C4. Make HITL and review-state behavior truthful

- Persist the HITL policy snapshot from the node.
- Support one authorized reviewer consistently across synchronous/queued paths,
  enforce deadline/timeout behavior, and queue approval-required agent tool calls.
- Remove role, quorum, assignment, SoD, and escalation controls from UI/docs unless
  the server actually implements them; they are not required by the scoped target.
- Enforce review queue active/pending/completed state, answer schema, and replay-
  safe submission. Advanced reviewer assignment/SLAs are optional.
- Provide a simple release checklist that links evidence, operator confirmation,
  deployed version, and rollback lineage; formal signoff/waivers are excluded.

**Exit criterion:** every displayed approval control has the same server-enforced
meaning on every execution path; one valid decision is durable and cannot be
silently duplicated or overwritten.

#### C5. Ship a fail-closed production deployment profile

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

#### C6. Close the advertised no-code composition blockers

- Introduce a first-class immutable file reference across Object Store, project
  files, run forms, queued workers, tools, KBs, and attachments.
- Make the MCP catalog deployment-aware and provide a runnable GitHub integration
  in the exact shipped image/sidecar model.
- Give Aria typed capability forms, editable step inputs, output references,
  preflight, and dependency-aware execution.

**Exit criterion:** cookbooks 04, 05, and 12–15 complete on the shipped stack without
host paths, shell/package installation, failed or skipped mutation steps, or manual
artifact recreation.

### High — complete the product lifecycle

1. **Agent workspace and project navigation:** standalone Agent lifecycle, mounted
   project selector, dependency context, and health. Membership administration is
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
6. **Reusable workflow assets:** duplicate-as-new-workflow, manifest import UI,
   portable dependency bundle, mapping wizard, reusable custom components, and
   validation/preflight that turns the existing unlinked YAML/Python export into a
   discoverable round trip.
7. **Server-authoritative tests:** execute component cases server-side and store
   immutable inputs, versions, outputs, scorer code, environment, and provenance.
8. **Judge/review correctness:** version judges, persist alignment results, validate
   queue state/answers, and display the complete trace context.
9. **Cookbook runner:** install/version sample bundles, check prerequisites, capture
    evidence, and keep documentation executable in CI.

### Medium — improve scale, analysis, and usability

1. Visual agent-output JSON Schema, field-transformation/JSONPath-expression, and
   loop-stop builders beyond the existing type-aware direct-port mapping popover;
   extend the router builder with typed fields/values and nested AND/OR groups.
2. Dataset CSV/JSONL import/export, splits, dedupe, bulk edit, slice management,
   correct active-as-of historical views, visible weight semantics, zero-weight
   validation, and grouped slice metrics.
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

1. Consolidate shell health into one query and label it accurately as API/database
   reachability; reserve “system healthy” for worker/provider/storage/queue-aware
   readiness.
2. Finish responsive behavior and URL-addressable tabs across every workspace.
3. Improve empty states, terminology, cross-artifact deep links, and inline docs.
4. Add marketplace/gallery polish, favorites, recently used assets, and richer
   template discovery.
5. Add presentation-quality release/evaluation exports.

## 13. Concrete implementation sequence

The roadmap should be executed in this order because later product work otherwise
builds on untrustworthy evidence and authorization:

1. **Security containment:** retain write-only provider-key reads, switch service
   publish to auth-on, remove literal MCP credential storage, disable local auth in
   production, contain preview/evaluation egress, and patch known project/run/eval/
   queue/judge authorization paths.
2. **Access/resource-scoping architecture:** centralize authenticated resource
   repositories; add a generated route-permission inventory and negative parent/
   project-association contract suite. Organization/membership models are excluded.
3. **Evidence correctness:** retain the fixed non-admin list crash, future-version
   rejection, server-side weight/tag propagation, fail-closed row verdict, and
   trace persistence. Close the remaining list/detail scoping mismatch, historical
   dataset-view bug, partial-scorer aggregate leakage, zero-weight semantics,
   immutable run snapshots, controlled baselines, tag slices, server-authoritative
   test records, and real deploy gates.
4. **Truthful release:** make one-reviewer HITL path-independent, remove unsupported
   enterprise approval fields, enforce review state, and implement a simple evidence/
   confirmation/rollback record.
5. **Production topology:** isolate effectful worker roles, harden configuration,
   add an effect ledger/idempotency contract, and define backup/restore, upgrade,
   single-instance recovery, and operational readiness. HA is optional.
6. **No-code closure:** file refs, deploy-aware MCP, typed Aria, agent/project pages,
   secure service publishing, and bundle import/clone.
7. **Continuous quality and operations:** async/continuous eval, SLOs/alerts, fleet
   health, incident workflow, and cost budgets.
8. **Consistency and scale UX:** unified lifecycle components, monolith reduction,
   global navigation/search, and bulk operations.

## 14. Cookbook continuity appendix

The earlier cookbook-specific audit was rechecked against the current worktree and
remains useful evidence of end-to-end composition. The prose corrections for
cookbooks 03, 08, and 10 improve the generated pages but do not change which product
paths actually execute.

| Result | Cookbooks | Current meaning |
| --- | --- | --- |
| Core result UI-complete on the standard stack | 01, 03, 06, 07, 08, 09, 10, 16 | The central author/run/inspect result can be reached, subject to the platform-wide security and production caveats in this report. |
| Mostly UI-complete | 02 | Skill engineering works, but package export/import is not a clean ZIP round trip. |
| Intended Aria workflow only partial | 12, 13, 14, 15 | Final artifacts can be built manually elsewhere in the UI, but Aria's empty-input mutations fail when approved and are skipped only when denied. |
| Not UI-complete | 04, 05, 11 | Host-local document input blocks 04; the missing Node/`npx` runtime for the GitHub MCP tile blocks 05; and 11 still requires manual evidence aggregation, rubric evaluation, go/no-go recording, and rollback lineage. Formal waivers and multi-party signoff are excluded. |

The scope-adjusted totals are **8 core UI-complete, 1 mostly complete, 4 partial,
and 3 blocked**.

The individual evidence boundary is:

| # | Cookbook | Verdict | Verified product path or blocker |
| ---: | --- | --- | --- |
| 01 | Trustworthy Intake Classifier | **Core UI-complete** | Prompt authoring/playground, Test Sets, evaluation/baseline, calibration, and observability exist; generic evaluation supports prompt targets. |
| 02 | Precision Skills | **Mostly UI-complete** | Authoring, render/trigger tests, calibration, binding, package preview/download, and import exist. Export produces a ZIP while import selects an unpacked folder/files, so the round trip leaves the app and needs conflict handling. |
| 03 | Policy-Safe Decision Tool | **Core UI-complete on shipped Compose** | Tool wizard/tests/calibration plus workflow `python_code` and HITL paths exist. New reusable registry implementations still require importable Python, and registered-tool execution is in-process. |
| 04 | Document-to-JSON Pipeline | **Blocked** | Object Store upload and document extraction exist separately, but `extract_document` requires a CALIBER-host filesystem path while `input_bucket` decodes object bytes as text. The UI cannot pass an uploaded binary as the required file reference. |
| 05 | Governed Tool Connectivity | **Blocked in shipped image** | MCP catalog/setup/discovery/playground/policy/calibration exist, but the GitHub tile launches `npx`; Node/npm/npx is present only in the UI build stage, not the final Python runtime image. |
| 06 | Grounded Knowledge Assistant | **Core UI-complete** | KB create/build/explore/query/graph/calibration and workflow/review paths exist, subject to normal provider, storage, and AGE readiness. |
| 07 | Support Triage Copilot | **Core UI-complete on shipped Compose** | Prompt/skill/tool/KB, router/HITL, run, evaluation, and review primitives compose. It remains a large manual build and must use a connector actually runnable in the deployed image. |
| 08 | Incident Response Copilot | **Core UI-complete** | Prompt/skills, Python fixture nodes, router/HITL, workflow-target evaluation, and review queue exist. The generated page now reflects shipped workflow evaluation. |
| 09 | Self-Healing Workflows | **Core UI-complete as operator recovery** | Run monitor, checkpoints, debugger, retry/resume, approval, manifest editing, preview, and publish exist. The patch is human-authored; the product does not yet make this autonomous self-healing. |
| 10 | Trustworthy Evaluation | **Core UI-complete** | Test Sets, judges, evaluations, review queues, and manual human-alignment metrics ship. Completed queue labels are not automatically ingested into alignment, and source verification/assets still describe parts of alignment as manual. |
| 11 | Release Signoff Factory | **Blocked for the scoped release path** | Evidence sources exist, but the deterministic rubric, evidence aggregation, go/no-go decision record, and rollback lineage remain manual/outside CALIBER; Releases is observational. Formal waivers, segregation of duties, and multi-party signoff are excluded. |
| 12 | Aria Evaluation Harness | **Partial; advertised Aria path fails** | Judges and Test Sets can be built manually. Aria emits empty-input `judge.create`/dataset steps; approval fails validation and denial merely skips them. |
| 13 | Aria Review Governance Queue | **Partial; advertised Aria path fails** | Review Queues can be operated manually, but Aria cannot provide the queue schema/trace IDs through typed step inputs. |
| 14 | Aria Governance Starter Kit | **Partial; advertised Aria path fails** | Judges, Test Sets, and Review Queues exist as manual pages; the “one sentence to whole kit” mutations do not execute successfully. |
| 15 | Aria Triage & Recalibrate Loop | **Partial; advertised Aria path fails** | Queue and workflow calibration can be driven manually. Aria does not successfully create/enqueue/start them, so the claimed job wait/poll/resume chain is not demonstrated. |
| 16 | Production Observability & Triage | **Core UI-complete for an existing runtime** | Trace filtering/detail, Test Set capture, queue enqueue/review, and evaluation rerun exist; needing prior runs is inherent to the operations scenario. |

These counts assess cookbook completion, not production readiness. A cookbook that
runs under the local-admin single-environment stack is not proof of authentication,
effect isolation, safe release evidence, failure recovery, or operational
completeness.

## Current worktree changes and their actual closure

The uncommitted worktree contains targeted remediation. This table records what is
now true **and** the remaining contract boundary; it should not be read as a list of
fully closed product areas.

| Area | Verified current behavior | Residual limit | Evidence / regression coverage |
| --- | --- | --- | --- |
| Evaluation visibility | Non-admin list no longer crashes: `owner_column()` supports `created_by`. A project-less run's creator can read detail | Detail does not mirror list ownership and treats the client-supplied active-project header as authoritative. A route-created project-less eval defaults to project visibility, so detail can read it but list cannot return it | `tests/test_scoping.py`, `tests/test_routes_evaluations_visibility.py` |
| Dataset version input | Evaluation creation rejects a version above the dataset's current version, and evaluated row content is stored inline in the run | Historical browse does not reconstruct active-as-of-N; there is no cryptographic run/content digest or full pre-truncation inventory/sampling record | `tests/test_routes_evaluations_reproducibility.py` |
| Weights/tags | Generic loading retains both; scorer aggregates, `overall`, and `pass_rate` use weights; tags reach rows | Frontend omits both, slices are absent, zero-weight semantics conflict with raw counts, and partial scorer failures still influence aggregates | `tests/test_eval_scorecard_weighting.py`, `tests/test_routes_evaluations_reproducibility.py` |
| Partial scorer failure | A row with a scorer error cannot pass | Surviving scores still affect aggregates/overall, only the first error is retained, and coverage is not reported | `tests/test_eval_scorecard_weighting.py` |
| Workflow trace linkage | Queued and synchronous runs persist `result.mlflow_trace_id`; the run trace panel and trace-to-run lookup can resolve it | Replay is not pinned to all resolved artifact/provider/configuration versions | `tests/test_workflow_run_trace_linkage.py` |
| Provider-key readback | `GET /settings/llm` returns presence plus a masked fingerprint; browser key inputs are write-only and cleared after save | Updates are process-local/non-durable; MCP credential serialization and the broader secret lifecycle remain open | `tests/test_settings_routes.py`, `src/pages/__tests__/settings.test.tsx` |
| Shell health | Sidebar and TopBar derive status from `useHealthStatus`; sidebar shows unreachable on request failure | Two independent polls run, and `/health` proves only backend/database liveness rather than platform health | `src/components/__tests__/sidebar-health-footer.test.tsx` |
| Unknown route UX | The wildcard renders a real Not Found view with a dashboard link | It is a client-rendered route boundary, not evidence of server HTTP-404 behavior | `src/pages/__tests__/app-shell-e2e.test.tsx` |
| Cookbook prose | Generated 03/08/10 material better reflects shipped side-effect, workflow-evaluation, and alignment paths | Source/generated documentation still conflicts in the places catalogued in §10 | Generated artifact diff plus existing documentation checks |

Large product decisions remain open: identity, MCP/reference secrets, systematic
resource authorization, arbitrary stdio command execution, safe preview/egress,
evidence-grade release gates, truthful HITL/review state, auth-on service
publishing, isolated extensions, idempotent side effects, production topology, and
operations.

### Verification of the current worktree

- `npm run typecheck` — **passed**.
- Full Vitest suite — **109 files, 1,466 tests passed**.
- Focused changed backend paths — **40 passed**.
- Focused authentication/service/deployment/promoter/sandbox paths — **90 passed**.
- Focused scheduler/runtime-approval paths — **24 passed**.
- Focused changed frontend paths — **21 passed**.
- A consolidated run over the 19 backend modules that the worktree changes touch or
  feed into (evaluations, scorecard, scoping, settings, readiness, workflow
  runs/versions, eval datasets/gate) — **320 passed, 0 failed** in 47s. This is the
  broadest green signal for the changed paths; it is not a full-suite pass.
- `ruff check`, `ruff format --check`, and `mypy` are clean on every changed module;
  `npm run eslint` is clean on the changed frontend files; `npm run build` succeeds.
- Backend collection found **5,025 tests** with the unreachable PostgreSQL MCP
  module skipped. The run was interrupted after 82m20s at **1,312 passed, 2 failed,
  1 skipped, and 1 error**. The failures were sandbox-denied localhost binds; the
  error was an unclosed SQLite connection warning. The unsupported Python 3.14 run
  also reported unclosed sockets during teardown, so no full-backend pass is
  claimed and these environment-dependent failures are not classified as confirmed
  product regressions.
- The existing external-app preview test also passed; it confirms that preview
  invokes the entrypoint, which is evidence for the unsafe effect-boundary finding,
  not evidence that preview is isolated.
- These focused subsets overlap and are not summed. They verify encoded behavior,
  including several unsafe defaults; they do not validate real providers, external
  integrations, browser E2E behavior, load, accessibility, or security. Multi-
  replica HA is outside the revised target.

#### Test-harness defect found while attempting the full backend run

A full-suite attempt reached 98% in ~3h20m and then wedged in teardown. Sampling the
xdist workers showed their main threads blocked in
`lock_PyThread_acquire_lock → __psynch_cvwait` alongside live
`MLflowTraceLoggingWorker` threads, i.e. stuck flushing MLflow's async trace-export
queue rather than executing tests.

The cause is an incomplete isolation in `caliber/tests/conftest.py:38-49`. It points
`MLFLOW_TRACKING_URI`/`MLFLOW_REGISTRY_URI` at a per-xdist-worker temp SQLite file
(verified: `caliber-test-mlflow-gw*.db` are created under the system temp dir), but
it does **not** isolate the MLflow *artifact* root. With a SQLite tracking store the
artifact root defaults to `./mlruns` relative to the working directory, so every run
appends trace artifacts to `caliber/mlruns/0/traces/`. That directory had accumulated
**8,575** trace directories, 425 of them from this session alone.

This matters twice over: it is silent repo/storage pollution, and the end-of-run
flush appears to degrade as the store grows, which is the most likely explanation for
the teardown wedge. Fixing it means isolating the artifact location as well as the
tracking URI. It is a harness change affecting all ~5,000 tests, so it is recorded
here rather than bundled into this remediation. CI is unaffected because it starts
from a clean checkout.

## Final assessment

CALIBER already demonstrates that a sophisticated agent workflow **can be composed
and locally executed with relatively little code, and that deliberately isolated
runs can be inspected and debugged deeply**. Its workflow runtime, developer
inspection tools, prompt/skill/knowledge workspaces, and audit/evaluation
foundations are substantial enough to justify continued product investment.

With enterprise capabilities excluded, the score rises modestly from **2.5/5 to
2.6/5**. The answer becomes two-part:

- **Yes** for predominantly low-code workflow composition, local execution, and
  inspection/debugging of deliberately isolated test runs.
- **No** for building, evaluating, deploying, and operating a production-grade
  agent system end to end without manual engineering.

Today the realistic positioning is:

> **A self-hosted, developer-oriented agent workflow studio and lifecycle control-
> plane preview—not yet a production-grade agent platform.**

The shortest credible path to the revised vision is not more canvas nodes or an
enterprise administration suite. It is to make authentication, resource scoping,
secrets, effect isolation/idempotency, evidence, truthful HITL, release, deployment,
and operations as real and composable as the workflow runtime already is.
