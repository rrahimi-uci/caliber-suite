# CALIBER repository-wide product and architecture review

**Review date:** 2026-07-28

**Reviewed baseline:** clean `main` at
`b2b838cbeb85e36c47905a9d61b2dc436a67c59d` on 2026-07-28. `HEAD`, `origin/main`,
and `origin/HEAD` resolved to the same commit, and no source or test file was
modified in the working tree, before this report edit. The implementation reviewed
here is therefore committed and pushed code, not an uncommitted candidate layered
over an older baseline. The working tree that earlier editions of this report
reviewed reached `main` as three commits on top of the former `e4f9cb901` baseline:
`5d9ae6cc0`, the substantive MCP-execution/tool-sandbox/preview-effect containment
change (128 files, 127 excluding this report); `ce64cd075`, which adds
`retention-days: 7` to the four previously unbounded artifact uploads in
`.github/workflows/ci.yml`; and `b2b838cbe`, which stops uploading the unconsumed
gitleaks SARIF (`.github/workflows/ci.yml:340`). The two CI commits touch no product
code. Appendix B records earlier pre-commit measurements of that work; they are
historical, not current, and nothing described in this report remains uncommitted.

**This refresh is an independent read-only implementation audit.** It updates this
report only; it does not silently patch the newly identified defects. “Accepted” in
the decision ledger means the prior implementation change was rational and is
retained, not that every residual gap was fixed in this review.

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
| The findings themselves | [§1 Critical correctness and security findings](#critical-correctness-and-security-findings) (C1–C11) and §2–§11 |
| What to do about them, in order | [§12 Prioritized roadmap](#12-prioritized-roadmap), [§13 Implementation sequence](#13-concrete-implementation-sequence) |
| Cookbook-by-cookbook status | [Appendix A](#appendix-a--cookbook-continuity) |
| Superseded verification history | [Appendix B](#appendix-b--verification-history-and-superseded-passes) |

Findings carry an explicit state: **[Remediated in the reviewed baseline]**,
**[Partly remediated]**, or no marker for open. Scores are risk-adjusted reviewer
judgements, not test coverage.

## Executive summary

> **Can CALIBER realistically enable developers to build, test, evaluate, deploy,
> and operate production-grade AI agent systems with a predominantly
> low-code/no-code experience?**

**Not yet for production-grade end-to-end operation—even with enterprise readiness
removed from scope. Yes for a predominantly low-code build, test, inspect, and
controlled self-hosted execution path.** CALIBER is now more than a canvas or an
API collection: the current implementation closes several concrete no-code bridges
that the previous report correctly identified. It is a credible **low-code agent
engineering studio and lifecycle control plane**, but it still cannot carry a user
from idea to production without security, release, and operations engineering.

The strongest shipped capabilities are real, not mock UI:

- a typed model with 29 registered node kinds plus manifest/configuration support
  for GraphRAG retrieval, structured ports/output schemas, and manual, event, and
  cron triggers;
- a polished graph editor with 13 starting templates and guarded import/clone
  inventory/preflight,
  dependency validation, and a mounted project/workspace selector;
- durable workflow versions, validation, preview, queued execution, checkpoints,
  retry/resume, run events, tool-call details, memory inspection, artifacts, and
  trace-oriented debugging;
- standalone Agent configuration list/detail/declaration-check/history pages plus
  `/me`-gated admin create/edit/status/delete controls and non-admin read-only UX,
  while honestly stopping short of immutable agent versions, runtime agent testing,
  or verification that a supplied MLflow experiment ID exists;
- content-pinned managed project files selectable in Workflow Studio and resolved
  across direct preview, evaluation, deploy-gate, queued, synchronous, and
  published-service queued execution;
- typed Aria input interactions, schema validation, explicit leaf-step skipping, and
  result-to-input references for the shipped capability registry;
- fail-closed MCP command/host policy on the principal forward-deployment paths,
  readiness UX,
  and three deployable first-party database MCP sidecar definitions (usable for the
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
2. **MCP secret readback is contained on the MCP surface, but secret management is
   not complete.** Literal leaves in MCP `env`, `headers`, and `auth_config` are now
   write-only in the `/mcp-servers` responses, UI edits, MCP history, and generic
   audit list/export; safe environment references remain visible and PATCH can
   preserve hidden values. The assistant's parallel `create_mcp_server` path is
   outside that contract: literal credentials arriving as plan slots are echoed back
   by the stored latest plan, are copied verbatim into the draft `artifact` that the
   draft read endpoints return (the draft row and its literals survive publish), and
   publish writes them onto the server row without the create-path sentinel check.
   Those assistant reads are owner-scoped and the published row itself still reads
   back sanitized through `/mcp-servers`, so this is an uncontained credential copy
   rather than a cross-tenant disclosure. Literals also remain ordinary JSON at rest
   for runtime use, with no durable encrypted/reference-backed resolver, deployment
   binding, rotation, or revocation lifecycle.
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
   wall-time handling. Output is clipped only after unbounded capture and several
   configured limits are not propagated to normal workflow/Aria constructors. That
   is useful containment, not a container/VM/kernel boundary.
9. **The previous arbitrary-stdio MCP RCE chain is remediated in the current
   implementation, but the production boundary remains operator-dependent and is
   not applied to every alias transition.** Every MCP
   test/invocation now applies an exact executable/module or host allowlist and a
   sanitized environment; prod deployment and runtime paths require an external
   boundary. Three database presets use non-root, read-only-root-filesystem,
   capability-dropped sidecars. Normal promotion/approval checks MCP readiness, but
   rollback and refinement-candidate alias rotation do not; server deletion checks the current
   active target but not rollback checkpoints. The sidecar host list is operator
   attestation, remote HTTPS does not itself constrain the remote service, and
   root-only dependency inspection does not recursively prove every deployed
   subworkflow dependency.
10. **A database MCP tool classified as read/no-approval is not actually
    read-only.** `run_query` accepts `EXPLAIN` and arbitrary `SELECT` calls, while
    its connection is privileged and autocommit. This review reproduced acceptance
    of both `EXPLAIN ANALYZE DELETE FROM victim` and
    `SELECT drop_graph('g', true)`. Because all three UI presets mark `run_query`
    as read/allowed/no-approval and Compose defaults the sidecars to CALIBER's own
    database credential, an agent can mutate control-plane data through a path
    presented as read-only. Parser checks are not a security boundary; this needs
    a database-enforced read-only transaction/session and a separate least-privilege
    target role before the DB presets are production-eligible.
11. **Operations stop at observability.** There are useful traces and metrics, but no
   alert policies, configurable SLOs, continuous evaluation, drift monitoring,
   incident workflow, detailed agent/workflow health, trustworthy queue/worker
   readiness, or demonstrated single-instance failure recovery. Lease recovery
   also restarts an interrupted workflow from the beginning without an effect
   ledger or platform idempotency key, so a crash can duplicate external side
   effects.

The latest work also adds guarded new-workflow import/clone semantics, first-class
managed files, typed Aria execution, routed Agent configuration, MCP readiness and
sidecars, a hardened local tool subprocess, queued file binding, synchronous HITL
rejection instead of silent pass-through, and runtime MCP preflight. These are real
closures. They do **not** create a secret vault, general extension sandbox, complete
agent lifecycle, evidence-grade deployment gate, continuous evaluation service, or
production operations system. The report also corrects earlier overclaims: the
import dialog inventories dependencies rather than mapping them, local DB MCP tests
exercise stdio connectors against PostgreSQL rather than the shipped HTTP-sidecar
topology, sandbox output is clipped only after capture, and Agent experiment
"preflight" checks only that an ID string is non-empty.

Evaluation-row tags and scorer/model/target identity are now visible, weighted and
raw values are explained, incomplete rows are excluded from scorer aggregates, and
all-zero effective weights return a controlled 400 before prediction. Users can
browse the set active as of a dataset version while separately inspecting additions,
and baseline choices require the same dataset version and scorer suite; target,
subject, and model identity are disclosed but not enforced as compatibility gates.
Remaining evaluation limits include synchronous/truncated execution, mutable
judge/provider inputs, no cryptographic evidence bundle, no durable coverage schema,
and no continuous quality/cost/latency gate.

The scope-adjusted assessment is **3.0/5**. Working product paths and a completed
supported-Python/backend coverage run justify maturity above prototype status.
However, the database MCP read-classification bypass, preflight gaps on alias
rollback/refinement, spoofable identity, inconsistent authorization, in-process
extensions, normal-run egress/effect risks, completion-only release evidence, and
incomplete operations prevent retaining the previous 3.1 risk-adjusted score.

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
| Prompt, skill, tool, agent, and knowledge engineering | **3.6/5** | Prompts, skills, and KBs are deep; routed Agent configuration and declaration checks now exist. Reusable custom tools still require importable Python, Agent history is audit-backed rather than immutable/versioned, and experiment reachability is not verified. |
| Developer debugging and run inspection | **4.0/5** | Best part of the product: run graph, events, checkpoints, retries, tool calls, memory, outputs, artifacts, and trace views. Trace-ID persistence is repaired in the reviewed baseline; deterministic replay remains incomplete. |
| Testing and evaluation | **2.9/5** | Real datasets, judges, weighted scorecards, compatible baselines, calibration, active-as-of browsing, and some regression gates. There is still no durable large async eval, immutable run bundle, continuous eval, CI product-quality gate, or cost/latency gate. |
| Deployment and release management | **2.3/5** | Versions, aliases, rollback, authenticated API publishing, forward MCP deployment preflight, and first-party sidecars exist. Rollback/refinement alias rotation bypasses MCP preflight; `prod` does not require the fake/completion-only quality gate or human promotion by default; deployment-scoped secrets and trustworthy evidence remain absent. |
| Operations and monitoring | **2.6/5** | Useful traces/metrics, token/cost/latency summaries, SSE, audit, system services, and one precise API/database health poll. Release aggregation is globally unscoped, and actionable alerts, deep readiness, queue/worker operations, drift, and failure recovery remain incomplete. |
| Platform UX | **3.6/5** | Agents, import/clone, project selection, managed files, typed Aria forms, MCP readiness, evaluation, and service-token states are discoverable. The import "mapping" is read-only inventory, Agent setup remains JSON/ID-heavy, and fragmented lifecycle idioms, raw IDs, and giant workspaces remain material debt. |
| Production safety and access control | **1.9/5** | Arbitrary stdio launch is fail-closed, managed files are pinned/scoped, service auth defaults are safer, and Preview containment improved. A no-approval DB "read" tool can mutate the default control-plane database; identity remains spoofable, releases/Aria have scoping defects, literals remain stored, normal-run effects/egress are unbrokered, and registered extensions execute in-process. |
| Architecture and operability | **3.0/5** | Typed domain/runtime, durable SQL state, managed-file protocol, MCP policy/sidecars, storage/event abstractions, and extensive tests are strengths. DB-enforced read-only policy, universal alias preflight, transitive dependency policy, at-least-once effects, in-process extensions, hard-coded modes, and scoping defects remain. |
| End-to-end low-code/no-code lifecycle | **3.1/5** | A user can build, clone/import, bind managed documents, collect typed Aria inputs, test, debug, inspect evidence, and publish an authenticated API predominantly in the UI. Production security, release evidence, and operations still require manual engineering. |

**Risk-adjusted overall: 3.0/5, late alpha / early-beta candidate for trusted
self-hosted teams.** Enterprise readiness is not scored; baseline production
safety is. The arithmetic mean is higher than the reported overall, but the 1.9/5
safety dimension and 2.3/5 release dimension cap the judgment: a mutation-capable
no-approval database path, spoofable identity, unbrokered effects, in-process
extensions, and false release evidence remain blockers even though the workflow IDE
is strong.

## Current implementation state after this independent pass

### Accepted remediation and actual closure

This table records what is now true **and** the remaining contract boundary. It is
not a list of fully closed product areas.

| Area | Verified current behavior | Residual limit | Evidence / regression coverage |
| --- | --- | --- | --- |
| Agent configuration workspace | `/agents` and `/agents/:agentId` provide searchable inventory, admin-scoped registration/mutation, detail/edit, enable/disable, skill-name checks, a non-empty experiment-ID check, audit-backed history, and permanent deletion. Both pages fetch `/me` and hide mutation controls from non-admins | This is refinement-fleet configuration, not a complete agent lifecycle. Experiment existence/connectivity is not verified, skill resolution is globally unscoped, explicit `null` PATCH values can reach non-null columns and return 500, and the detail page still calls the admin-only audit endpoint for viewers and shows its 403. There is no immutable version, rollback, archive, runtime test, deployment, or health view; `/me` also rests on C1's spoofable identity | `src/pages/Agents.tsx`, `src/pages/AgentDetail.tsx`, `routes/agents.py`, agent route/UI tests |
| Workflow import and clone | Inventory actions import YAML/JSON or clone a selected saved version. Preview validates the graph and inventories tool/skill/KB/dataset/MCP/subworkflow/managed-file references, then creates a fresh ID, actor-derived owner/project scope, and v1 draft when its checks pass | The dialog's “Dependency mapping” is read-only inventory: it cannot remap anything. Secret detection is key-name heuristic only, so a bearer value under `Authorization` or embedded in a command can pass. MCP import readiness trusts stored discovery and can accept a disabled/policy-blocked server; prompt aliases are unverified. Dependencies/files stay linked rather than copied. This is useful guarded import, not a portable or universally safe bundle | `components/workflows/WorkflowImportDialog.tsx`, `routes/workflows.py`, `workflows/validation.py`, import route/UI tests |
| Managed project files | Object Store can copy an object into the active File Directory; Workflow Studio selects the resulting pinned snapshot. Named preview, evaluation, gate, queued, synchronous, and queued-service paths validate metadata and read/verify content; queued `input_files` materialize in the worker | Evaluation resolves a known workflow version without visibility first and then binds that workflow's project, allowing cross-project managed-file content access. A physically missing object escapes several route/gate error contracts; synchronous execution has already committed `running` before binding and can leave that row stuck. Approval does not recheck a file that disappears after gate evaluation. Binding is also absent from calibration/refinement, export, assistant drafts, nested child manifests, and dataset refs; folders/streams remain unsupported | `workflows/file_tools.py`, `routes/workflow_versions.py`, `routes/evaluations.py`, `orchestrator/workflow_run_worker.py`, `workflows/promoter.py`, associated file/runtime tests |
| Aria typed execution | Missing capability inputs pause the plan with a schema-driven form; answers merge into the step, validate the registry schema, then re-enter the risk gate. A rejected interaction marks that step skipped. Deterministic `$from_step` references connect declared outputs, and async calibration can park/poll/resume | Skip settles a leaf/no-dependent plan, but readiness accepts only dependencies in `done`; skipping a producer leaves dependents waiting and can strand the plan paused. Judge/queue lists are global, queue mutation accepts an unscoped ID, and calibration performs unscoped workflow/agent lookups. The planner is literal-keyword based, exposes JSON for complex fields, and cannot author arbitrary workflows/prompts/tools | `assistant/capabilities.py`, `assistant/plans.py`, `assistant/executor.py`, `components/aria/planView.tsx`, Aria backend/UI tests |
| MCP execution policy and first-party DB integrations | Test, discovery, calibration, direct invocation, queued/synchronous runtime, normal promotion and promotion approval apply command/host/discovered-tool policy. Stdio launch is fail-closed to configured executable/Python-target allowlists; runtime exposes readiness/blockers. PostgreSQL, pgvector, and AGE presets target separate hardened Compose sidecars | `run_query` is classified read/no-approval yet accepts mutation-capable SQL on a privileged autocommit connection; Compose points it to the control-plane database/credential by default. Rollback and refinement candidate rotation bypass deployment preflight, deletion ignores rollback checkpoints, policy changes can newly require approval after a run's immutable binding was created, and child inspection is not transitive. Five external presets need provisioning; sidecar trust is operator attestation and rate limits are process-local | `mcp_servers/db/identifiers.py`, `mcp_servers/db/connection.py`, `mcp_policy.py`, `mcp_gateway.py`, deployment/server routes, Compose, MCP tests |
| Local source-code sandbox | Python source-tool execution uses private workdirs, empty environment, `-I`, AST private/dunder rejection, POSIX limits, a hard timeout, and process-group termination. Returned output is byte-clipped | It remains same-host containment, not a production sandbox. `communicate()` and runner `StringIO` capture output unbounded before clipping. The dominant workflow path wires timeout and optional output only, not configured memory/file/descriptor overrides; Aria uses class defaults, including a lower output cap. Registered tools, their tests, and external-app entrypoints still run in-process | `tool_sandbox/service.py`, `tool_sandbox/_runner.py`, `workflows/runtime.py`, sandbox tests |
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
| Test/CI isolation | Each test process/worker receives unique temporary MLflow SQLite and artifact roots; async trace export is disabled and roots are cleaned. The current remote run completed backend, UI, integration, lint/type, dependency-audit, and gitleaks steps successfully | The workflow is still red because artifact quota blocked coverage/Allure/UI-dist uploads and skipped the dependent wheel job. Seven-day retention and removal of an unused SARIF upload are rational quota controls, but the quota had not recovered. Push gitleaks scanned only the latest one-commit range, not the large preceding implementation commit, and the dependency audit used dev rather than every CI extra | `tests/test_test_harness_isolation.py`, `.github/workflows/ci.yml`, `scripts/run-supported-python-security-audit.sh`, Actions run `30373909000` |
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
| Add routed Agent list/detail/configuration controls | **Accepted with a narrower product label** | The pages close the missing inventory/configuration workflow, but the experiment check proves only a non-empty string, skill resolution is unscoped, null PATCH values can violate non-null columns, and viewers still hit an admin-only audit request. They must not be relabeled as a full standalone agent build/test/deploy/rollback lifecycle. |
| Call the non-empty experiment-ID check “experiment-binding preflight” | **Rejected as an overclaim** | No MLflow lookup or connectivity check occurs. The truthful label is a stored-configuration/declaration check until the backend resolves the experiment. |
| Import or clone by trusting source identity, files, or dependencies | **Rejected and replaced** | The implementation generates a fresh ID, derives owner/project from context, creates only a new workflow plus v1 draft, rejects inline secrets/unresolved dependencies, verifies managed snapshots in the selected project, and leaves prompt aliases visibly unverified. Valid refs stay linked rather than being silently copied. |
| Copy all workflow dependencies during clone/import | **Deferred** | Silent copying would break provenance and secret/alias semantics. Current link-preserving import plus dependency inventory/preflight is directionally correct; the dialog does not actually map dependencies, secret detection is heuristic, and MCP readiness must be strengthened before calling the path safe. A future portable bundle needs typed mapping and conflict policy. |
| Call the import dependency status list “Dependency mapping” | **Rejected as inaccurate UX/report terminology** | It has no source-to-target selectors, conflict choices, copy operation, or remapping action. It is dependency inventory/preflight only. |
| First-class content-pinned managed files | **Accepted on the explicitly bound runtime paths** | Logical ref plus row ID, object version, size and digest verification is a substantial improvement over host paths. Direct preview/evaluation/gate, queued/synchronous execution, and queued published-service invocation are covered; the alternate-runner, nested-manifest, dataset-ref, dynamic-mapping, folder, and streaming boundaries in C9 prevent declaring universal runtime parity. |
| Typed Aria inputs, step skipping, and step-output references | **Accepted with a dependency-semantics defect** | Schema-driven collection, validation, and sibling references close real gaps. Skip is correct only for a leaf/no-dependent step: a skipped producer never satisfies readiness, so its dependents wait indefinitely. Capability list/mutation handlers also need the same visibility contract as REST. |
| Treat a stdio executable allowlist as a production sandbox | **Rejected** | All invocation paths should retain the allowlist and sanitized environment, but local stdio remains containment only. The allowlist gates the executable: only a command resolving to the CALIBER interpreter has its `-m` module or script path checked, so any other launcher an operator adds — the `npx`/`docker`/`pip` catalog presets — passes arbitrary arguments. Requiring an external boundary is also keyed to alias strings in `CALIBER_MCP_REQUIRE_EXTERNAL_ISOLATION_FOR_ALIASES`, which defaults to the single literal `prod` and is matched case-sensitively against an unvalidated alias path segment, so `production`, `prod-eu`, or `PROD` promotes with local containment and no blocker. The requirement must be keyed to a deployment's environment class, not an alias string. |
| Treat an allowlisted sidecar hostname as independently proven isolation | **Rejected as an overclaim** | The shipped DB sidecars themselves have rational Compose controls, but `managed_sidecar_hosts` is operator attestation. The UI/report must state that fact and retain deployment/runtime preflight. |
| First-party PostgreSQL/pgvector/AGE MCP sidecars | **Accepted for development connectivity; rejected as a production-safe connector** | They make three catalog paths deployable in shipped Compose and correct the earlier `localhost` collision. But `run_query` can mutate while marked read/no-approval, uses autocommit, and defaults to the control-plane credential. A DB-enforced read-only session plus separate least-privilege target is mandatory. The other five entries remain operator work. |
| Treat local DB MCP integration as live HTTP-sidecar E2E | **Rejected as an evidence overclaim** | The marked tests launch local stdio Python MCP processes against PostgreSQL. Static Compose validation checks sidecar definitions, but no test exercises CALIBER → streamable HTTP sidecar → database as shipped. |
| Add resource limits to the local source sandbox | **Accepted as partial hardening** | POSIX limits, AST restrictions and process-group kill reduce accidental and common abuse. Output is clipped after unbounded pipe/StringIO capture, and memory/file/descriptor configuration is not propagated through the dominant workflow/Aria constructors. This does not justify untrusted execution or cover registered in-process extensions. |
| Describe sandbox response clipping as bounded output memory | **Rejected** | Clipping occurs after `communicate()` and `StringIO` accumulation. Enforce streaming/pipe budgets during execution before claiming a memory bound. |
| Seven-day CI artifact retention and removal of unused gitleaks SARIF upload | **Accepted as rational quota controls** | Both reduce unnecessary storage without disabling the scan gate. They did not restore the quota in run `30373909000`; short retention also narrows evidence availability, and the wheel remains unproven remotely. |
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
   boundary it enforces is not yet trustworthy. Release aggregation and Aria
   capability handlers add newly verified global/unscoped paths; evaluation can
   use an unscoped workflow ID to read its project's managed-file content.
2. **Database MCP enforcement (C11).** A read/no-approval tool accepts mutating
   SQL on an autocommit connection. Use a database-enforced read-only transaction
   and dedicated least-privilege role; then test mutation functions and
   `EXPLAIN ANALYZE` against the real database engine.
3. **Evaluation evidence completeness.** Add immutable resolved run bundles,
   durable denominators/slices, asynchronous full-dataset execution, and
   cost/latency/continuous quality policies.
4. **Deep readiness probes (§6).** `/health` remains API + database only even
   though the shell now labels and polls that narrow signal correctly.
5. **Evidence immutability.** No content digest or pre-truncation inventory on an
   evaluation run, so a pinned run is reproducible by convention, not by proof.
6. **Complete remote release pipeline proof.** The supported backend coverage run,
   UI suite/build, integration subset, static checks, dependency audit, and gitleaks
   command all passed remotely. The workflow remains red because artifact quota
   blocked every evidence upload and skipped the wheel job. The push gitleaks range
   covered only the latest CI-only commit rather than the large implementation
   commit, and Pages publishing is separately misconfigured.
7. **Transitive and transition-safe execution policy.** Alternate runners and child
   subworkflows remain outside complete managed-file/MCP propagation. MCP preflight
   is absent on rollback and refinement alias rotation; deletion ignores rollback
   checkpoints; managed-file disappearance is not rechecked at approval and can
   leave synchronous runs stuck.
8. **Release, HITL, isolation, and topology.** Deploy gates remain completion-only,
   manifest approval semantics remain inconsistent, extensions execute in-process,
   and production roles/recovery are incomplete.

## Verification

### Current-baseline verification status (2026-07-28)

The current committed baseline has substantially stronger verification than the
prior report recorded. GitHub Actions run
[`30373909000`](https://github.com/rrahimi-uci/caliber-suite/actions/runs/30373909000)
at `b2b838c` completed the
supported backend suite and coverage gate, UI suite/build, integration subset,
static checks, supported dependency audit, and gitleaks command successfully. The
workflow conclusion is nevertheless **failure**, because repository artifact
quota rejected the coverage, Allure, and UI-distribution uploads and the dependent
wheel job was skipped. Successful commands inside a red workflow are evidence for
those commands, not a green release signal.

Current source inventory was recounted directly: **40** registered route modules,
**316** literal `Route(...)` declarations, **60** top-level models, **28** lazy
routed components, **29** workflow component kinds, **13** workflow templates,
**13** router operators, **266** backend test files, **110** frontend unit/spec
files, and **8** Playwright spec files. Inventory is not a pass result.

| Check | Current-pass status | Evidence boundary |
| --- | --- | --- |
| Full supported-Python backend suite and coverage gate | **Passed remotely** | Python 3.11: **5,123 passed / 12 skipped / 93.92% coverage** in the successful test step. The earlier interrupted 5,024-pass local xdist attempt is superseded as current backend evidence and retained only in Appendix B. |
| Integration behavior | **Remote subset and local DB connector checks passed** | Remote CI: **6 passed / 3 skipped** because `POSTGRES_URL` was absent. Local marked run: **9 passed / 1 optional DSPy collection skip / 5,094 deselected** against a real PostgreSQL/pgvector/AGE database via locally launched **stdio Python MCP processes**. This does not exercise the shipped streamable-HTTP sidecar network/topology end to end; Compose validation is static. |
| Frontend Vitest, TypeScript, ESLint, and production build | **Passed remotely** | **110 files / 1,490 tests passed**; TypeScript and the production Vite build passed. A local default-concurrency attempt had two timeouts/worker errors, while the affected/unstarted suites passed individually; the remote CI result with its configured timeouts/retries is the authoritative converged count. |
| Browser E2E with MinIO/PostgreSQL/AGE/MCP dependencies | **Passed locally with AGE opt-in skipped** | **23 passed / 1 skipped** using isolated SQLite and the documented MinIO dependency. The AGE-specific spec remains gated by `CALIBER_EXPECT_AGE`. The browser MCP case verifies a failed external invocation path, not successful HTTP-sidecar execution. |
| Lint, format, typing, package, Compose, and security | **Checks passed; release pipeline incomplete** | Remote lint/format and strict mypy passed; the isolated dev dependency audit reported no known vulnerabilities and gitleaks reported no leaks. Local wheel build and three Compose validations passed. Remote wheel was skipped after artifact upload failures. The audit did not cover every CI extra, and push gitleaks scanned only the single HEAD CI commit, so the 128-file implementation commit lacks equivalent remote range evidence. |
| Adversarial DB read-classification probe | **Failed the safety contract** | Calling current `assert_read_only()` accepted `EXPLAIN ANALYZE DELETE FROM victim` and `SELECT drop_graph('g', true)`. No destructive SQL was sent to a database; this was a parser-policy probe proving the advertised read classifier fails open for mutation-capable PostgreSQL syntax. |
| Artifact and documentation publication | **Failed** | All six executed upload steps in run [`30373909000`](https://github.com/rrahimi-uci/caliber-suite/actions/runs/30373909000) returned `Failed to CreateArtifact: Artifact storage quota has been hit. Unable to upload any new artifacts. Usage is recalculated every 6-12 hours.`, so this baseline has no uploaded coverage, Allure, or UI-distribution evidence and the dependent wheel job was skipped. The block is not attributable to this repository's retention settings: every upload in `.github/workflows/ci.yml` is bounded (`retention-days: 1` for the three same-run Allure result sets at lines 126, 157, and 196; `7` at lines 118, 188, and 299; `14` for the rendered Allure report at line 253), and this repository retains only **22.1 MB across six unexpired artifacts**. Actions artifact storage is metered per account, however: non-expired artifacts across all repositories owned by the same account total **about 474.6 MB against the 500 MB Free allowance**, 419.2 MB of it in one unrelated repository. The rejection is therefore consistent with a genuinely exhausted account-level quota, and bounding retention in this repository cannot clear it. No test failed for this reason, but the evidence is genuinely absent and another run — after account-wide artifact reclamation — is required to produce it. That reclamation has since been performed outside this review: 185 unexpired artifacts dated 2026-06-29 to 2026-07-03 were deleted from this repository and 542 Allure artifacts from the unrelated repository, taking the account from about 474.6 MB to **59.2 MB against the 500 MB allowance**. Run [`30382308475`](https://github.com/rrahimi-uci/caliber-suite/actions/runs/30382308475) at `d1f43a66e` nevertheless still returned the same rejection about ten minutes later, which is consistent with the documented 6-12 hour usage recalculation rather than with remaining exhaustion; the evidence therefore remains absent at this baseline and a later run is still required to produce it. Pages run [`30372019082`](https://github.com/rrahimi-uci/caliber-suite/actions/runs/30372019082) regenerated documentation but `actions/configure-pages` could neither find nor create the Pages site with the available repository setting/token. |

Historical and focused results are retained in Appendix B. The current evidence
proves the named commands only; it does not convert a red artifact-dependent
workflow, unscanned implementation range, or unexercised HTTP-sidecar topology into
an all-green release candidate.

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
| Agents | Inventory, create form, detail/edit, skill/config checks, audit history, enable/disable/delete | A useful refinement-fleet configuration workspace. `/me` hides mutations, but viewer detail still calls the admin-only audit API, the experiment check is non-empty text only, and setup is ID/JSON-heavy. It is not immutable versioning, runtime testing, deployment, rollback, or health. |
| MCP | Catalog, setup, discovery, playground, policies, tests, calibration, readiness/blockers | Rich surface with write-only containment and deploy-aware policy. Three database presets deploy as dev/self-hosted sidecars, but read/no-approval mutation and transition-preflight defects make them unsafe for production. Five external presets require provisioning, and literal storage lacks a durable secret lifecycle. |
| Files/storage | Object Store, active File Directory, project selector, immutable import, managed file-input picker, queued inputs, folder/bucket nodes | A content-pinned project-file path composes across named preview/evaluation/gate/queued/synchronous paths. Cross-project evaluation access, missing-object state handling, approval TOCTOU, alternate runners, nested/dataset refs, dynamic mapping, folders, and streams remain incomplete. |
| Knowledge/RAG | KB inventory/editor, sources, builds, chunks, query playground, GraphRAG/AGE, calibration, versions | One of the strongest artifact workspaces; provider/storage readiness remains operator-managed. |
| Workflow Studio | Inventory/templates, import/clone dialog, dependency inventory, React Flow editor, managed-file picker, inspector, code view, versions, detail graph | Strong low-code composition and reuse environment; the dialog cannot map dependencies and its secret/MCP preflight is incomplete. Linked dependencies are not a portable bundle. |
| Workflow runtime | Preview, queued/synchronous runs, managed files, events, approvals, checkpoints, retry/resume, memory, artifacts, trace/debug panels | Deep runtime UX. Managed file input is safely previewable; other blocked effects are not simulated. Synchronous approval now refuses rather than bypasses, but canonical HITL, deterministic replay, transitive child binding, live-run isolation, and duplicate-side-effect risks remain. |
| Workflow deployment | Versions, navigation-hidden/deep-linkable deployments and promotions, service publishing, tokens, patches | Core primitives and secure new-service default exist; rollback/refinement bypass MCP preflight, managed-file approval has TOCTOU, and release gates/default aliases/evidence still bypass production governance. |
| Test Sets | Dataset inventory/detail, additions history, active-as-of snapshot, restore, trace import, MLflow sync | Useful curation; bulk/splits, immutable content digests, exhaustive snapshot proof, and grouped slice semantics remain incomplete. |
| Evaluation | Evaluations, detail scorecards, judges, alignment, review queues | Valuable ad hoc evaluation; not a continuous or release-grade evaluation system. |
| Aria | Assistant panel, plans, typed input interactions, step references, async job polling, drafts, approval/publish flows | Registered plans collect/execute missing inputs, but producer skip deadlocks dependents and capabilities bypass resource scoping. Literal planning, small breadth, JSON-heavy fields, and lack of discovery keep it guided rather than autonomous. |
| Observability | Trace search/detail/compare, metrics charts, Allure link, system services | Good inspection; lacks alert-to-action operations. |
| Gateway | Endpoints, guardrails, pricing, usage | Useful control-plane visibility; it does not by itself make CALIBER deployment secure or scalable. |
| Release/review controls | Audit log/export, review queues, Releases | Review state/type/concurrency and MCP audit safety improved; Releases aggregates globally without visibility/project filtering and lacks query-error UX. Evidence, recovery/idempotency, and workflow-approval behavior remain incomplete. Formal enterprise signoff is excluded. |
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
| Build | Visual workflows, guarded import/clone, prompts, skills, KBs, managed files, schemas, MCP/API/webhook nodes | Reusable tool implementation needs Python packaging; clone/import inventories rather than maps dependencies and has heuristic secret/MCP checks; complex schemas/conditions/Aria fields still use JSON or expressions. |
| Test | Preview, managed-file preflight, local source sandbox, component test runs, datasets, judges, workflow eval | Preview reads correctly scoped/existing pinned files and refuses other known unisolated nodes, but unscoped evaluation and missing-object lifecycle defects remain. There is no effect-broker simulation, workflow assertion suite, server-authoritative component record, full-dataset async runner, or reusable suite policy. |
| Evaluate | Weighted row scorecards, custom judges, safer baselines, active-as-of snapshots, alignment, review queues, prompt/workflow refinement gates | Synchronous caps, no immutable resolved run bundle or grouped slice UI, no cost/latency, no scheduled/continuous eval, no CI product-quality gate, and mutable judges. |
| Deploy | Publish versions, alias deployments, rollback, authenticated workflow HTTP service, token UI, principal-path MCP preflight, and three first-party database sidecars | A single environment is acceptable, but rollback/refinement bypass MCP preflight, the quality gate is completion-only/not mandatory for `prod`, and DB `run_query` is mutation-capable against the default control-plane credential. Formal multi-party approval is excluded. |
| Operate | SSE updates, trace detail/compare, tokens/cost/latency, logs/events, audit, health, coarse fleet/success ratios | Trace linkage is repaired; actionable alerts, trustworthy health, queue/worker visibility, effect idempotency, recovery evidence, and incident diagnosis remain incomplete. Multi-region HA is excluded. |
| Control | Four scopes, audit rows, review queues, MCP command/host/tool policy, principal-path runtime/deployment preflight, and a dormant promotion state machine | Spoofable identity, mutation-capable DB read tooling, stored MCP literals, inconsistent resource scoping, transition/transitive policy gaps, misleading HITL controls, and observational release evidence remain. Organization/membership governance is excluded. |

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
- `deploy/caliber/compose.yaml:93-94` publishes port 5001 directly and `:103-106`
  defaults the dev user, admin, approver, and operator to `@local-admin`.

CALIBER can be placed behind a trusted identity proxy, but the shipped default does
not include or enforce one. Because Compose defaults the dev user and every
privileged list to `@local-admin`, a direct API request with **no identity header**
is also an admin in the default stack. A production deployment must neither trust
clients to self-assert the header nor enable this fallback.

#### C2 — MCP readback containment without a durable secret system — **[Partly remediated]**

- **[Remediated from baseline `b9d8e786e`]** The baseline implementation resolved
  and returned full OpenAI and Anthropic keys and populated browser password fields.
  The reviewed baseline instead returns only presence and a masked fingerprint
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
- The contract is scoped to the `/mcp-servers` routes and the audit trail; the
  assistant's MCP path is outside it. Literal `env`/`headers`/`auth_config` values
  supplied as `slot_overrides` or request context become plan slots with no type or
  redaction constraint (`assistant/models.py:352-353,438-445`,
  `assistant/service.py:2126-2134,1818-1848`), and the whole plan is persisted into
  the session workbench and served back by the latest-plan route
  (`assistant/service.py:899-901,908-931`). The draft row stores the same artifact
  verbatim (`assistant/service.py:3096-3099,3166`) and the draft API returns it
  because `DraftResponse.artifact` is unfiltered (`assistant/models.py:404`,
  `assistant/service.py:5160-5193`). Publishing writes those literals onto the server
  row without the create-side sentinel check
  (`assistant/publisher.py:352-355` versus `routes/mcp_servers.py:123-130`); reads of
  that row through `/mcp-servers` are still sanitized, so the uncontained readback is
  the plan and draft surface, not the registry. Nothing under `assistant/` imports
  `mcp_secrets`, and the assistant is enabled by default (`config.py:785-788`). Those
  surfaces are session-owner-scoped with no admin override
  (`assistant/service.py:557-568,5189-5192`) and the assistant's draft and publish
  audit details exclude the credential fields
  (`assistant/service.py:3174-3190,5594-5615`), so this is readback for the creating
  operator rather than cross-user disclosure — but it is outside the write-only
  contract and has no regression test: the end-to-end assistant MCP test never
  supplies a credential field (`tests/test_assistant_service.py:2012-2088`).
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
  unscoped lookups, so a known ID can cross project boundaries. For a workflow
  target it then constructs the managed-file resolver from the target workflow's
  project and persists the run under the caller's project. A known foreign workflow
  version can therefore expose that project's pinned file content, not merely its
  metadata (`routes/evaluations.py:233-268,378-418,467-475`);
- **[Partly remediated]** Test Set example browsing and review add/submit now resolve
  the visible parent. Review submission enforces active/pending state and claims the
  row atomically. Other nested dataset mutation/restore/sync and review
  administration paths still need the systematic route inventory; and
- judge test/alignment and several nested dataset routes still bypass the scoped
  parent lookup (`routes/judges.py:229-338`);
- Aria's judge and review-queue list capabilities query every active row, queue
  add-items accepts any known ID through an unscoped helper, and calibration uses
  bare workflow/agent lookups (`assistant/capabilities.py:138-238`;
  `routes/review_queues.py:298-302`; `routes/workflow_calibration.py:203-220`); and
- Releases requires an authenticated user but its timeline and live-state queries
  return global release audit rows, active workflow deployments, and active KBs
  without owner/project/visibility predicates (`routes/releases.py:81-198`).

Multi-tenancy is excluded from the product target, so these routes are not scored
as tenant-isolation failures. They remain serious resource-integrity and
access-control defects for a single organization with multiple developers, and can mutate
or expose the wrong workflow/project when an ID is known or associated incorrectly.
The evaluation-managed-file path makes the impact concrete content disclosure.

#### C4 — non-admin evaluation list/detail visibility — **[Remediated in the reviewed baseline]**

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

- `routes/workflow_deployments.py:156-163` now passes the live configuration into
  `promote()`, but still no executor.
- `workflows/promoter.py:1657` discards that configuration when selecting the
  executor — `executor = executor or build_executor(None)` — so `build_executor`
  resolves the provider to `"fake"` and returns `FakeWorkflowExecutor` (`:180`,
  `:191-194`). Inside `evaluate_deploy_gates` the configuration is used for one
  thing only: binding the managed project-file runtime (`:1306`, `:1322-1325`,
  `storage_config=resolved_config.workflow_storage`); the knowledge runtime
  runners are still built without it (`:1311-1314`). The candidate is therefore
  not evaluated with the production agent provider.
- `evaluate_deploy_gates()` now fails closed with 0% for missing, empty, or archived
  datasets. It orders active examples by creation time plus example ID before
  applying the bounded sample, so repeated evaluation selects the same rows.
- For non-empty data it counts only `run.status == "completed"` (`:1419-1421`). It
  does not compare output to expected values, call a judge, measure regression,
  cost, or latency.
- The Inspector exposes `min_overall_delta` and `max_tone_regression`
  (`Inspector.tsx:3480-3484,3501-3505`), but this gate reads only `min_pass_rate`
  (`Inspector.tsx:3460-3464`; `workflows/promoter.py:1422`). The two keys are not
  inert everywhere — the refinement eval stage harvests every deploy-gate threshold
  and forwards it to the candidate gate (`orchestrator/workflow_stages.py:569-573`;
  `workflows/refinement.py:459-460,469-477`), and the calibration gate reads
  `min_overall_delta` (`workflows/calibration.py:1015-1018,1034-1046`) — but nothing
  in the promotion path they are edited under consults them, so as deploy-gate
  controls they are decorative.
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

#### C6 — human-in-the-loop policy is represented but not enforced — **[Partly remediated]**

The manifest and inspector expose `required_role`, `approval_count`, and
`timeout_behavior` (`workflows/manifest.py:765-771`;
`caliber/caliber-ui/src/components/workflows/Inspector.tsx:5712-5779`). At runtime:

- the interpreter only checks whether a node ID is in an approved set and labels
  the MVP path as pass-through (`workflows/runtime.py:5921-5936`);
- the worker creates exactly one request with a hard-coded
  `{"timeout_behavior":"block"}` snapshot
  (`orchestrator/workflow_run_worker.py:1923-1941`); and
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

**[Remediated for silent synchronous bypass]** Synchronous real execution now
preflights the compiled plan and rejects any approval-requiring node before creating
the run, directing callers to queued execution
(`routes/workflow_versions.py:795-817`). The same manifest therefore no longer
silently passes an approval node, but queued execution remains the only approval
engine and still ignores the configured role/quorum/timeout fields. Approval
semantics should ultimately be canonical rather than path-specific refusal.

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
  returning the callable for direct execution (`workflows/runtime.py:2350-2372`;
  `workflows/tools.py:193-227`).
- The Tool test-run path describes itself as sandbox-isolated, but imports the module
  and invokes `wrapped(**tool_input)` in the web process
  (`routes/tools.py:401-480`).
- External-app nodes let a workflow operator type any
  `package.module:callable` in the Inspector (`Inspector.tsx:2148-2158`), then
  import and invoke that installed entrypoint in-process without an allowlist
  (`workflows/runtime.py:2620-2656,2725-2785`, dispatched from
  `workflows/runtime.py:6057-6071`). Nothing narrows the target: the manifest field
  enforces only a non-empty string (`workflows/manifest.py:779`), the compiler
  passes it through (`workflows/compiler.py:564`), and configuration ships
  allowlists for MCP stdio commands, Python modules/scripts, and remote hosts but
  none for external-app entrypoints (`config.py:855-883`). Ordinary Preview refuses
  `external_app` before the interpreter starts (C9), so this exposure is normal and
  queued execution; registered tools are deliberately outside that preflight
  (`workflows/runtime.py:137-139`) and their in-process path stays reachable in
  Preview.
- In contrast, `python_code` workflow nodes and Aria-authored source tools are wired
  to `LocalSubprocessToolSandbox`. The reviewed baseline uses a temporary
  directory, `python -I`, an empty environment, private/dunder AST rejection,
  POSIX CPU/address-space/file-size/open-file limits, response clipping, a hard
  timeout, and process-group termination. However, `Popen.communicate()` captures
  the full child pipes and the runner accumulates `StringIO` before `_clip()` is
  applied (`tool_sandbox/service.py:123-132,194-201`;
  `tool_sandbox/_runner.py:65-80`), so output memory is not actually bounded.
- `LocalSubprocessToolSandbox.from_config()` (`tool_sandbox/service.py:62-69`) is the
  only reader of `tool_sandbox_max_memory_bytes`, `tool_sandbox_max_file_bytes`, and
  `tool_sandbox_max_open_files` (`config.py:847-849`; a repo-wide search finds those
  names only in `config.py`, `service.py`, and `tests/test_config.py`). Its only caller
  is the standalone sandbox app (`tool_sandbox/server.py:63`), which runs only as its
  own process under `python -m caliber.tool_sandbox` (`tool_sandbox/__main__.py:15-29`,
  optional `[sandbox]` extra) and has no in-tree client: no remote-sandbox client or
  sandbox-URL setting exists in `src`. Every in-process construction therefore bypasses
  `from_config` — the workflow `python_code` node passes node timeout plus the optional
  plan output size (`workflows/runtime.py:5760-5763`), and Aria draft test/run/eval
  construct the class with no arguments at all (`assistant/service.py:2564`;
  `assistant/agent_tools.py:981,1099`).
- Memory, file-size, and descriptor caps consequently always take the class defaults.
  Those happen to equal the configuration defaults (256 MiB / 1 MiB / 32,
  `service.py:51-53` vs `config.py:847-849`), so what is silently discarded is any
  operator override via `CALIBER_TOOL_SANDBOX_MAX_MEMORY_BYTES`, `_MAX_FILE_BYTES`, or
  `_MAX_OPEN_FILES` (`config.py:1456-1458`); those three limits are also absent from the
  Settings surface, which exposes timeout and output only (`routes/settings.py:1213-1235`).
  The output cap is the one default that actually diverges: real workflow runs receive
  `config.tool_sandbox_max_output_bytes` through `plan.max_output_bytes`
  (`workflows/promoter.py:957`; `workflows/runtime.py:2120-2126`), but Aria's paths and
  any config-less preview/eval replay retain the class's 64 KiB instead of
  configuration's 1 MiB (`tool_sandbox/service.py:50`; `config.py:846`). No test
  exercises `from_config`; the sandbox HTTP tests inject a sandbox instead
  (`tests/test_tool_sandbox_service.py:122,142,168`).
- The backend itself states that production needs container/VM/kernel isolation
  (`tool_sandbox/service.py:37-44`), adds a Windows caveat for process-group
  termination (`tool_sandbox/service.py:175-182`), and repeats the disclaimer for the
  POSIX limits (`tool_sandbox/_runner.py:101-106`). A standalone sandbox app does exist
  (`tool_sandbox/server.py`, `tool_sandbox/__main__.py`), but no Compose service runs it
  and no setting names a remote sandbox endpoint — the `CALIBER_TOOL_SANDBOX_*` keys in
  `deploy/caliber/compose.yaml:155-158` only tune the local subprocess limits, and every
  call site constructs `LocalSubprocessToolSandbox` directly
  (`assistant/service.py:2564`, `assistant/agent_tools.py:981,1099`,
  `workflows/runtime.py:5763`). Sandboxed code therefore runs as a child of the same
  process that serves the request: Compose defines a single `caliber` app service
  (`deploy/caliber/compose.yaml:69`) and the workflow scheduler is an in-process asyncio
  task (`server.py:329,365`).

This requires fully trusted workflow authors as well as administrator-controlled
installed packages; an explicit allowlisted entrypoint registry is absent. The local
subprocess is useful containment for the two integrated paths, but incomplete
configuration propagation and post-capture clipping weaken even that local
resource contract. The mixed
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

That contract has two newly verified correctness holes. Evaluation resolves a
workflow version without first checking caller visibility and deliberately builds
the resolver for the target workflow's project; a known foreign version ID can
therefore disclose the referenced file content (C3). Separately, physical object
read failures are not caught by the preview, synchronous, or gate lifecycle code.
Synchronous execution commits a `running` row before binding, so a deleted backing
object can leave an orphaned running row. Promotion approval rechecks MCP but not a
managed file that disappeared after gate evaluation. The worker path has the more
complete failure transition, but the advertised parity is not yet reliable.

This closes cookbook 04's root-workflow host-path bridge and preserves honest
containment for other effects. It is not a universal effect architecture:

- registered tools retain the explicit `allow_in_preview` contract and knowledge
  queries can still execute; knowledge builds keep their separate preview-skip;
- calibration/refinement, standalone export runtime, and transient assistant-draft
  execution do not construct this binder. Nested child manifests do not receive a
  resolver/tool override, and dataset-manifest refs are outside the protocol; child
  alias and MCP dependency inspection also remain runtime/transitive concerns;
- import validates pinned metadata but its broad “inline secret” rejection is
  key-name heuristic only; bearer credentials in an `Authorization` value or
  embedded command text can pass import inspection;
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

#### C10 — arbitrary stdio MCP host-command execution — **[Remediated for launch; separate transition gaps remain]**

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
- normal promotion, promotion approval, queued submission, worker start, and
  synchronous execution preflight the exact manifest snapshot and discovered tool
  policy;
- aliases listed in `CALIBER_MCP_REQUIRE_EXTERNAL_ISOLATION_FOR_ALIASES` (default:
  the single literal `prod`) require a boundary classified as remote HTTPS or an
  operator-attested managed sidecar (`config.py:917-923`;
  `mcp_policy.py:312-321,444,454-457`); and
- Compose ships PostgreSQL, pgvector, and AGE MCP servers as separate non-root,
  read-only-root-filesystem, capability-dropped, resource-limited sidecars on an internal network,
  with an explicit in-network database URL.

This rationally closes “choose `/bin/sh` through the API”. It does not reduce the
default-allowed surface to nothing: stock defaults admit the CALIBER interpreter
plus the module `caliber.mcp_servers.db` (`config.py:856-877`;
`deploy/caliber/compose.yaml:162-164`), so a stdio record running
`${PYTHON} -m caliber.mcp_servers.db --mode relational|vector|graph` passes
readiness with no operator configuration change (verified at HEAD: `ready=True`,
`boundary="local_containment"`, no blockers, `production_isolated=False`). That
server takes its target solely from `POSTGRES_URL`
(`mcp_servers/db/connection.py:30-33`), which is not a protected key
(`mcp_policy.py:104-123`) and is applied to the child verbatim from the record's
`env` mapping, including `${VAR}` inheritance from the API process
(`mcp_gateway.py:522-526,533-539`). The DSN is never checked against a policy, so
such a record can name any PostgreSQL the API process can reach — in Compose that
includes the control plane's own `postgres:5432/caliber` — and the child runs as the
API process user in a temporary directory, without the sidecars' non-root,
read-only-root-filesystem, capability-dropped, resource-limited controls
(`deploy/caliber/compose.yaml:20-26`). Registration requires the admin scope
(`routes/mcp_servers.py:236`), the record must still classify the tool explicitly
before invocation (`mcp_policy.py:391-415`), and `prod` aliases still reject a
`local_containment` boundary (`config.py:917-918`; `mcp_policy.py:444-454`) — but
connection tests, tool tests, ad-hoc and queued runs, and non-`prod` aliases do not,
and relational mode carries `execute_sql` alongside the C11 `run_query` behavior
(`mcp_servers/db/server.py:22-32`). This is the local-stdio twin of the Compose
default in C11 and needs the same remedy: a separate least-privilege role and target
database, not an allowlist entry. Policy is also not yet an invariant of every
deployment transition:

- rollback directly swaps the stored version ID without calling
  `deployment_blockers()` (`routes/workflow_deployments.py:331-357`;
  `workflows/promoter.py:1805-1834`);
- approval of a workflow-refinement candidate publishes and calls `_rotate_alias()`
  directly, with only a docstring expectation that the alias is ungated
  (`apply.py:368-396`; `workflows/promoter.py:1763-1802`);
- that external-boundary requirement is keyed to the alias *string*, not to a
  declared environment class, and nothing constrains the alias. Promote and
  rollback take it from the URL path (`routes/workflow_deployments.py:110,127,333`),
  queued and synchronous runs take it from an unconstrained body field that
  defaults to `manual` (`schemas.py:1419,1434`; `routes/workflow_runs.py:113-116,557,575`),
  and no layer normalizes or validates it. A probe against a local-stdio dependency
  at HEAD returned the boundary blocker for alias `prod` and an empty blocker list
  for `production`, `live`, `staging`, and `PROD` — the last because configured
  aliases are lower-cased while the incoming alias is compared verbatim
  (`mcp_policy.py:444,562-563`). The shipped console avoids this only because it
  collapses to one alias (`caliber-ui/src/lib/environment.ts:12,19,23-24`), but the
  API is the operative boundary, so the requirement should be keyed to a declared
  environment class rather than an alias name; and
- MCP deletion checks dependencies of current active deployment targets only, not
  versions retained in rollback checkpoints, so a later rollback can target an
  orphaned server (`routes/mcp_servers.py:354-375`); and
- the gateway revalidates live tool policy immediately before invocation but does
  not carry the returned `requires_approval` decision back into the runtime's
  immutable binding approval gate. If policy changes from no-approval to
  approval-required during a run, the invocation can proceed without a new
  decision (`mcp_policy.py:391-429`; `mcp_gateway.py:174-192`;
  `workflows/runtime.py:2030-2036,5232-5239`).

Five catalog presets remain blocked unless an operator provisions and allowlists
them, and the allowlist constrains only `argv[0]`. Arguments are policy-checked in
exactly one case: when the resolved executable is the CALIBER interpreter, which
must then use an allowlisted `-m` module or an allowlisted absolute script
(`mcp_policy.py:214-253`). For any other allowlisted executable, `stdio_launch()`
forwards the stored `args` verbatim (`mcp_policy.py:346-366`), and registration
never rejects them: `create`/`update` accept any `command`/`args` (length-bounded
only, `schemas.py:3130-3163`) and surface the violation solely as an
`execution.blockers` entry, with enforcement deferred to the moment the transport
opens (`routes/mcp_servers.py:113-119,234-311,314-351`; `mcp_gateway.py:331-339`).
That is fail-closed, but the stored record is the thing an operator later
"unblocks" with one environment variable.

This matters because all five remaining presets are launchers rather than servers:
`npx -y @modelcontextprotocol/server-github`
(`caliber-ui/src/pages/McpServers.tsx:984-985`), `docker run -i --rm …
quay.io/minio/aistor/mcp-server-aistor:latest --allow-write --allow-delete`
(`:1146-1161`), `pip run huggingface-mcp-server` (`:1183-1184`), `npx -y
ollama-mcp-server` (`:1207-1208`), and `npx @playwright/mcp@latest`
(`:1226-1227`). Enabling any of them means adding `npx`, `pip`, or `docker` to
`CALIBER_MCP_STDIO_COMMAND_ALLOWLIST`, after which an admin-editable `args` list
chooses an arbitrary npm or PyPI package to run under that executable. A
current-code probe confirmed both halves: under the shipped default allowlist
(`${PYTHON}` only) `/bin/sh -c id` is refused with "stdio command '/bin/sh' is not
in CALIBER_MCP_STDIO_COMMAND_ALLOWLIST", and once a second executable is
allowlisted `execution_readiness()` returns ready with no blockers and
`stdio_launch()` returns `('/bin/echo', ['arbitrary', 'argv'])`. The console
directs operators toward exactly that change (`McpServers.tsx:543,1336-1337`).
"No shell" is accurate but does not constrain a launcher that interprets its own
arguments; the missing control is argument-level policy — pinned argv or a
per-preset launch template — before any launcher is allowlisted. Two facts bound
the current severity: the child's `PATH` is replaced by
`CALIBER_MCP_STDIO_SAFE_PATH` (default `/bin:/usr/bin`) and server records cannot
override it (`mcp_gateway.py:517`; `mcp_policy.py:114`), so a launcher generally
needs further operator PATH provisioning; and no shipped Compose file mounts the
Docker socket, so the MinIO path — the one whose arguments could bind the host
root or request privileges and remove containment entirely — additionally requires
an operator to grant Docker daemon access.

Sidecar membership is hostname attestation, external HTTPS is not proof of the
remote workload, rate limits are process-local, and dependency inspection is not
transitive through child workflows. C1 also remains critical independently:
spoofed admin identity can mutate MCP configuration inside the allowlisted
boundary.

#### C11 — database MCP `run_query` is mutation-capable while classified read/no-approval

This is a current production-blocking defect, distinct from explicit write tools:

- `_READ_KEYWORDS` includes `explain`, and `assert_read_only()` checks only the
  leading keyword, stacked statements, and data-modifying CTEs. Its own comment
  acknowledges that a database read-only transaction is the durable fix
  (`mcp_servers/db/identifiers.py:63-64,232-264`);
- `run_query()` then calls the ordinary query helper, whose connection is privileged
  and `autocommit=True` (`mcp_servers/db/tools_relational.py:105-109`;
  `mcp_servers/db/connection.py:37-52`);
- all three database UI presets classify `run_query` as `allowed=true`,
  `side_effect_level=read`, and `requires_approval=false`
  (`caliber-ui/src/pages/McpServers.tsx:958-963,1043-1047,1091-1096,1131-1137`); and
- Compose defaults `CALIBER_MCP_POSTGRES_URL` to the same `caliber` database and
  credential used by the control plane (`deploy/caliber/compose.yaml:15-19,99-100`).

A direct current-code probe accepted both
`EXPLAIN ANALYZE DELETE FROM victim` and `SELECT drop_graph('g', true)`. The first
executes a DML plan; the second illustrates the broader PostgreSQL fact that a
`SELECT` can invoke a side-effecting function. Keyword/regex filtering cannot prove
read-only SQL. Run this tool inside a database-enforced read-only transaction or
session, use a separate role that lacks DDL/DML and dangerous function privileges,
and point production presets at a separate target database. Add engine-backed
regression tests for `EXPLAIN ANALYZE`, data-modifying functions/procedures, stacked
statements, and CTEs. Until those controls exist, the DB presets are development
connectors only and must not be described as safe read tooling.

## 2. Workflow Builder

### Capability assessment

| Capability | State | Evidence-based assessment |
| --- | --- | --- |
| Agent nodes | **Shipped** | Agent nodes support inline/registered prompts, tools, skills, handoffs, memory/session, model settings, and output schemas. Deployed nodes sync into the backend Agent Fleet. |
| Standalone agent creation/management | **Partial, routed** | Inventory/detail/declaration checks/history plus admin-scoped create/edit/enable/delete ship. `/me`-aware pages hide mutations for non-admins, but still issue an admin-only audit query that viewers see fail. Experiment “preflight” checks only a non-empty ID; skill checks are unscoped, null PATCH can 500, and setup remains raw-ID/JSON-heavy. This is not immutable versions, runtime testing, deployment, rollback, or health. |
| Prompt creation/engineering | **Strong** | Builder, templates, variables, playground, test history, baseline, calibration, bindings, aliases, and rollback are substantial. |
| Skill creation/engineering | **Strong** | Wizard, content, trigger/render tests, scenarios, packages, calibration, bindings, and skill versions are present. |
| Reusable tool creation | **Partial/code-required** | The wizard requires a Python dotted module and callable already importable by the runtime (`ToolWizard.tsx:254-296`). Schemas and tests are low-code; implementation and packaging are not. |
| Tool sandboxing | **Partial/unsafe for extensions** | `python_code` and Aria source-tool drafts use a resource-limited, AST-restricted local subprocess. Registered tools, their tests, and external-app entrypoints still execute imported Python in-process; the local backend is not container/VM/kernel isolation. |
| MCP integration | **Partial, materially improved but unsafe DB read contract** | Registration, discovery, invocation, per-tool policy/rate controls, tests, calibration, write-only containment, forward runtime/deployment preflight, and readiness UX exist. Three database presets deploy in first-party sidecars, but their read/no-approval `run_query` can mutate the default shared control-plane database. Rollback/refinement/deletion/approval-policy transition gaps remain; five external presets require provisioning. |
| File/folder/object-storage nodes | **Managed file shipped on named paths; lifecycle defects remain** | Object Store → File Directory → pinned managed `file_input` composes in preview/eval/deploy-gate/queued/sync paths, including service-triggered queued runs. However, unscoped evaluation can read a foreign workflow's file, a deleted backing object can escape route/gate failure handling and strand synchronous state, and approval has a file-disappearance TOCTOU gap. Alternate runners, nested manifests, folders/streams, and legacy effects remain incomplete. |
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
| Workflow reuse | **Useful partial** | Subworkflows, node copy/duplicate, restore-as-draft, clone-as-new, YAML/JSON import with dependency inventory, and YAML/Python export exist. Import preserves links rather than mapping/copying them; key-name-only secret detection and stale-discovery MCP checks prevent calling it universally safe. No portable bundle or reusable custom-node library exists. |

### Builder strengths

The builder is not a thin canvas. `workflows/component_catalog.py:51-80` registers 29 typed
node types and `Workflows.tsx:49-206` offers 13 templates, including single agent,
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
   but skip is only safe for a leaf step, capability visibility is inconsistent,
   the planner remains literal-keyword based, complex fields use JSON, and the set
   cannot author arbitrary prompts, tools, agents, or workflows.
5. Import/clone is link-preserving rather than portable, and its “Dependency
   mapping” display is inventory only. Users still need a real mapping workflow for
   secrets, managed files, prompt aliases, and unavailable artifacts; secret/MCP
   preflight also needs hardening.
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
| Import/export | **Partial but discoverable** | Workflow inventory imports YAML/JSON with graph/dependency/managed-file checks, fresh identity/ownership, and v1 draft creation. Secret rejection is key-name heuristic and MCP readiness uses stored discovery. An unlinked version page exports YAML/Python. Valid refs are linked rather than mapped/copied; no portable bundle exists, and skill ZIP export/folder import remains asymmetric. |
| Publish an API | **Partial, auth-on by default** | Service/OpenAPI/status endpoints and one-time bearer-token create/list/revoke UI exist; explicit/legacy public state is warned. Rate limits, CORS, quotas, durable secret storage, and a trustworthy platform identity remain absent. |
| Preview safely | **Fail-closed plus managed files** | Preview safely resolves content-pinned project files and refuses the IR for other known unisolated dedicated nodes. Registered-tool/knowledge-query policy, child managed binding, and lack of a real effect broker mean this is not universal isolated simulation. |

### Trace-link correctness defect — **[Remediated]**

At baseline `b9d8e786e`, `WorkflowRunResult` carried and populated both
`mlflow_run_id` and `mlflow_trace_id`, but the async worker and synchronous route
persisted only the run ID. The in-app span viewer and trace-to-run lookup read
`CaliberWorkflowRun.trace_id`, so the integrated trace panel stayed empty and
by-trace lookup could not resolve a workflow run despite a real trace.

The reviewed baseline assigns `result.mlflow_trace_id` in both the queued worker
(`orchestrator/workflow_run_worker.py:1825-1826`) and synchronous route
(`routes/workflow_versions.py:1011-1012`). Four new tests cover
the queued and synchronous paths and both dependent endpoints; all four fail
against the pre-fix code.

### Frontend maintainability and data-loss risk

Several product workspaces are monoliths:

- `KnowledgeBases.tsx`: about 9,006 lines;
- `Prompts.tsx`: about 6,620 lines;
- `components/workflows/Inspector.tsx`: about 6,382 lines;
- `WorkflowEditor.tsx`: about 5,239 lines; and
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
| Dataset evaluation | Real, but synchronous; defaults to 50 examples and caps workflow targets at 20 (`routes/evaluations.py:80,84,427-433`). Active-as-of snapshots are now browsable, but the evaluation form still does not expose all truncation/threshold policy. Workflow targets containing a known unisolated dedicated node now fail Preview before execution rather than performing the effect; that is safe refusal, not successful evaluation. |
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
and Compose checks are green, and the current remote backend suite completed with
5,123 passes and its coverage gate. The workflow remains red at publication, so the
following is both a strong test result and a coverage/design warning rather than a
release-candidate claim:

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
- those tests do **not** cover a skipped Aria producer with dependents, Agent PATCH
  nulls, release/Aria cross-project visibility, a physically deleted managed object,
  MCP rollback/refinement preflight, rollback-checkpoint deletion references,
  approval-policy changes during a run, or mutation through DB `run_query`;
- unequal/all-zero weights, incomplete-scorer aggregate policy, tags/weights/errors,
  active-as-of browsing, baseline filtering, review concurrency, MCP readback,
  service tokens, and sustained health polling now have regression coverage;
- Playwright files and scripts exist but are not run by the checked CI workflow;
  and
- the marked PostgreSQL MCP integration test is not provisioned by CI, and the
  local run exercises stdio processes rather than the shipped HTTP sidecars.

Current Actions run `30373909000` proves that the backend/UI/integration commands,
lint/type checks, supported dev dependency audit, and gitleaks command can pass at
the baseline. It simultaneously proves the release signal is still broken: quota
failures rejected every artifact upload and skipped the wheel. The gitleaks push
range contained only the last CI-only commit, not the preceding large implementation
commit, and Pages run `30372019082` failed repository-site configuration after docs
generation. Retention/SARIF reductions are correct, but another run is required to
prove restored artifacts, packaging, the intended secret-scan range, and Pages.

Coverage-oriented success must not be used as product-claim validation.

## 5. Deployment experience

### Shipped capability

- mutable workflow drafts plus immutable published versions, diff, restore into a
  new draft, and publish;
- alias deployment records, optimistic concurrency, rollback, and live-deployment
  deletion protection;
- cron/event/manual triggers;
- a dormant workflow promotion/approval state machine;
- MCP dependency/tool-policy preflight on forward promotion, promotion approval,
  queued and synchronous execution, plus three first-party database sidecars;
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
   deployed version, and rollback lineage together. Its API also returns global
   release audit/live workflow/KB rows without the visibility/project predicates
   used by the underlying resource workspaces.
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
    does not recursively prove every subworkflow target. Rollback and refinement
    candidate alias rotation bypass MCP preflight, deletion can orphan a rollback
    checkpoint, and a newly approval-required policy can race an existing run.
12. Managed-file gate success is not durable release evidence: promotion approval
    does not re-read a pinned object, so deletion between evaluation and approval can
    rotate an alias to an unusable version. The same storage exception is not
    normalized consistently across preview/gate/synchronous paths.
13. Remote CI has passing test/build/security commands but is still red because
    release artifacts cannot upload; the dependent wheel did not run. Documentation
    publication separately fails at repository Pages configuration.

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
- project/visibility-scoped release timeline and live-state aggregation with an
  explicit query error state in the Releases UI.

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
6. Agent registration still asks for a raw MLflow experiment ID, comma-separated
   skill names, and three JSON textareas; its detail check only verifies the ID is
   non-empty. Agent output schemas, loop-stop expressions, and field transformations
   beyond direct port mapping remain code-like. Router conditions have a visual
   builder, but not nested Boolean groups or typed field/value selection.
7. No global search, command palette, bulk lifecycle actions, dependency graph, or
   readiness checklist exists as projects grow.
8. Review Queue uses a rigid multi-column workflow and does not show the actual
   request/response context being labeled.
9. Navigation-hidden but deep-linkable deployment panels, API-only benchmark
   features, stale production-approval copy, and hard-coded mode flags make
   discoverability differ from implementation reality. Workflow import/clone and
   service-token lifecycle are discoverable exceptions, but the import dialog calls
   a read-only status inventory “Dependency mapping,” implying controls it lacks.
10. The very large page modules make consistent behavior harder to maintain.
11. Navigation and Settings are not permission-aware enough. The shell exposes
    settings/audit and other administrative destinations broadly, while Settings
    always fetches operator data and renders admin-only mutation controls that the
    backend may reject. Agent pages fetch `/me`, hide lifecycle mutations, and
    explain read-only access, but Agent Detail still always queries the admin-only
    audit API and exposes the resulting 403 text to viewers. Permission-aware
    rendering is therefore only partly closed and cannot become a security boundary
    until C1's spoofable identity is replaced.
12. Releases has no query-error state even though its global aggregation endpoint
    can fail independently, and it offers no project/visibility context for the
    globally returned rows.

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
| Resource authorization | **Serious gap** | Four global scopes exist, but advertised project/visibility boundaries are inconsistent. Releases and Aria expose global/unscoped data, and an unscoped workflow evaluation can bind and read a foreign project's managed file. No enterprise RBAC console is required; list/detail/mutation/execution must still target the authorized resource. |
| Secrets | **Serious gap, readback contained** | Provider keys and MCP literal leaves are no longer returned through known browser/audit surfaces. MCP still accepts and stores literal credential JSON for runtime use. A durable encrypted/reference-backed resolver with rotation/revocation is required; an enterprise vault marketplace is not. |
| Published API authentication | **Partial** | New UI-published services are token-authenticated by default and expose token lifecycle. Explicit/legacy public services, no rate/quota/CORS policy, and C1's spoofable platform identity remain. |
| Effect isolation and egress | **Critical gap, managed-file path contained** | Preview/evaluation safely resolve content-pinned project files and refuse the other known unisolated dedicated effects. Normal runs/services still permit legacy filesystem/storage capabilities and unrestricted webhook/API egress; explicitly preview-enabled tools and knowledge queries retain policy. There is no universal broker or SSRF defense. |
| Extension/MCP execution | **Critical gap despite arbitrary-stdio remediation** | Registered/external-app Python still runs in-process. MCP launch is allowlisted, but a DB tool labeled read/no-approval can mutate through a privileged autocommit connection against the default control-plane database. Rollback/refinement transitions bypass preflight, policy approval can race, classification is operator-attested, and child inspection is not transitive. |
| HITL/review correctness | **Misleading, review records improved** | A single authorized reviewer is sufficient. Queue state/type/concurrency checks now prevent ordinary overwrite/duplicate submission, but workflow role/quorum/timeout semantics remain false and cross-system recovery is incomplete. |
| Audit correctness | **Partial** | Transaction-coupled rows, filtering, MCP legacy redaction, actual service-token actor attribution, and export are useful. WORM/SIEM/compliance evidence is excluded; comprehensive secret/actor correctness still needs contract tests. |
| Release evidence and recovery | **Critical gap** | Formal enterprise signoff is excluded. Missing/empty/archived gate inputs and dedicated Preview effects fail closed, but fake/completion-only evidence, no mandatory `prod` quality gate, managed-file approval TOCTOU, incomplete pinning, duplicate normal effects, and a red artifact pipeline still make one-operator release unsafe. |

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
  normal live runs have no per-workflow folder/bucket capability object. Physical
  object deletion is also not normalized across preview/gate/synchronous lifecycle
  handling; synchronous binding can fail after the run is committed as `running`.
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
sidecar containers are hardened, but their `run_query` policy is not database-
enforced read-only and their default target is the privileged control-plane
database. There is still no explicit whole-platform production profile/readiness
gate.

#### Scoping is a cross-cutting API concern but remains route-by-route

The repeated bare `session.get` defects show that optional use of a generic query
helper is not a reliable authorization architecture. Aria capability helpers and
the Releases aggregator bypass the route-level pattern entirely; evaluation uses an
unscoped target to select another project's file resolver. Parent-child scoping and
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

The refreshed audit found no new material `TODO` or placeholder implementation in
the Agent/file/Aria/MCP paths changed since the prior baseline; observed bare `pass`
statements there are intentional exception/fallback branches. The following routed,
dormant, misleading, or documentation-only surfaces remain substantive product debt:

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
  inventory. Portable dependency bundles and mapping remain absent; secret scanning
  is key-name heuristic and MCP readiness can accept disabled/policy-blocked stored
  discovery.
- **[Remediated]** Workflow service token CRUD is now exposed in the publishing UI
  and new services are auth-on by default; production identity, vault, quota, and
  explicit-public governance remain separate gaps.
- **[Remediated]** `WorkspaceSelector` is mounted in the TopBar and supports project
  creation/selection; the client-asserted header remains a security limitation.
- **[Partly remediated for the registered capability set]** Aria plan mutations now pause
  for schema-driven inputs, merge/validate answers, connect declared prior-step
  outputs, and expose an explicit backend-backed **Skip step** action. A leaf skip
  settles, but a skipped producer leaves dependents waiting indefinitely; list and
  mutation capabilities also bypass normal visibility scoping. Generated cookbook
  12–15 prose still describes the obsolete approval-only/manual-artifact workflow
  and is stale.
- Demo workflow tools intentionally contain no-op/stub external actions; templates
  using them demonstrate orchestration, not production connectivity.
- Optimizer docstrings/README enumerate future algorithms that selection/provider/UI
  do not expose.
- `config.py` still describes a “verified namespace profile” as a possible
  production MCP boundary, while current `mcp_policy.py` explicitly treats the only
  recognized bubblewrap profile as local containment and refuses it for production.
  Align the configuration comment/docs with the enforced policy.
- Releases is explicitly observational, despite its placement implying a release
  operations room; its API aggregates globally without project/visibility scoping
  and its UI has no query-error state.
- Agent configuration is routed, but its “MLflow experiment binding” check is only
  non-empty text, several fields require raw JSON/names, viewer detail still calls
  an admin-only audit API, and explicit null PATCH values can produce a server 500.
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

1. **Complete standalone agent lifecycle:** Agent configuration/create/edit/declaration checks
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
   environment mappings are not bundled/remapped. The current dialog is inventory,
   not mapping, and its heuristic secret/MCP checks are not a complete trust gate.
8. **Broad Aria autonomous build:** the registered judge/dataset/review/calibration
   set now supports typed dependency-aware execution. Natural-language planning,
   discovery, dry-run, deep links, and arbitrary prompt/skill/tool/agent/workflow
   authoring remain absent.
9. **Safe credential lifecycle:** create write-only reference → bind to a deployment
   → rotate → see consumers → revoke → verify no values entered logs/browser/audit.
10. **Safe database connector release:** configure a separate least-privilege target
    → prove read-only behavior in the database → classify/approve mutation tools →
    preflight every forward/rollback/refinement alias transition → preserve rollback
    dependencies → monitor and revoke. The current DB presets do not close this path.

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
- Apply the same actor/project/visibility context to Aria capabilities and release
  aggregation. Authorize an evaluation's workflow version before constructing its
  project/file resolver.
- Add negative integration tests for anonymous versus operator access and for
  mismatched parent/project IDs across list/detail/mutation, run, trace, file,
  queue, judge, evaluation, deployment, and service routes.

**Exit criterion:** an unauthenticated client cannot become an administrator, and
an operator cannot read or mutate a resource through an unrelated parent/project ID.

#### P1. Remove secret exfiltration and secure published services

- **Shipped in the reviewed baseline:** the MCP write-only contract landed in
  `e4f9cb901` — API, audit, and history responses replace literal `env`,
  `headers`, and `auth_config` leaves with a sentinel on read as well as write,
  `${VAR}` references stay visible, and PATCH preserves an existing leaf when the
  sentinel is sent back (`mcp_secrets.py`; `routes/mcp_servers.py:113`;
  `routes/audit.py:101`). `5d9ae6cc0` added the execution-policy layer on top of
  that contract, not the response containment. New workflow services default to
  `auth_required` and expose mint/list/revoke bearer tokens in the UI
  (`routes/services.py:219`); scopes, expiry, and rotation are not yet
  operator-selectable. The bullets below are the remaining production contract.
- Preserve the merged provider-key fix: reads return only
  presence/fingerprint and browser inputs remain write-only. Replace its
  process-local update mechanism with durable secret references and explicit
  clear/rotate/revoke semantics.
- Require secret references for MCP/auth headers; reject literal secret-shaped
  values or store them through an encrypted secret service.
- Replace key-name-only workflow-import scanning with typed secret-bearing field
  validation, including authorization header values and command/argument content;
  never label stored discovery as current MCP readiness.
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

- **Shipped in the reviewed baseline:** each previewed IR is refused before the
  tracer or interpreter starts when it contains any of ten dedicated node types
  ordinary Preview cannot isolate (`workflows/runtime.py:140-153,161-172,2830-2854`).
  Nine are refused unconditionally; a content-pinned managed `file_input` is the one
  scoped exception, and it is still fail-closed — it executes only through a
  run-scoped resolver that re-verifies row metadata, object version, byte length, and
  the bytes' digest, and raises when no resolver is bound
  (`workflows/runtime.py:4547-4553`; `workflows/file_tools.py:141-154`). A legacy
  host-path `file_input` remains blocked. This prevents those live effects but cannot
  simulate the workflow; the broker below remains required.
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
- Make DB read policy an engine-enforced property: execute `run_query` inside a
  read-only transaction/session under a separate role/database that lacks mutation
  and dangerous function privileges. Test `EXPLAIN ANALYZE` and side-effecting
  functions against PostgreSQL; do not rely on keyword parsing.
- Call the same MCP dependency/readiness gate on forward promotion, approval,
  rollback, and refinement rotation; preserve/check rollback checkpoint references
  before server deletion. Bind approval policy to a reviewed snapshot/hash or pass
  current approval context through the gateway so a policy change cannot bypass it.
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

- **Shipped in the reviewed baseline:** missing/empty/archived datasets fail closed,
  bounded selection is deterministic, and execution uses Preview. Completion-only
  fake-executor semantics remain — `promote()` falls back to `build_executor(None)`
  when no executor is injected, which selects `FakeWorkflowExecutor` because the
  provider defaults to `fake` with no config, and the promote route supplies only
  `config` (used for managed-file binding), never an executor. So does the absence
  of a mandatory `prod` quality gate: `GATED_ALIASES` is empty, so no alias requires
  a gate to exist or a human promotion step before the alias rotates. Both remain
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
- Revalidate managed-file existence/content at promotion decision time and normalize
  storage failures into durable failed run/gate states rather than orphaned
  `running` rows or uncaught responses.

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
- Extend MCP stdio policy from `argv[0]` to full argv. Today only the executable is
  allowlisted, and the sole argument rule applies when the command resolves to the
  CALIBER interpreter — and even then inspects just `-m <module>` or an absolute
  script path, not trailing arguments; every other allowlisted executable receives
  its stored `args` verbatim, unvalidated at the API boundary
  (`mcp_policy.py:214-253,346-366`; `schemas.py:3140`). Ship pinned per-preset launch
  templates or an explicit argument allowlist so that enabling a launcher the shipped
  catalog already needs — `npx` for the GitHub/Ollama/Playwright presets — cannot
  reintroduce arbitrary package or container execution.
- Key the external-MCP-isolation requirement to a declared environment class rather
  than the literal alias strings in `CALIBER_MCP_REQUIRE_EXTERNAL_ISOLATION_FOR_ALIASES`
  (default `prod`), and validate/normalize the deployment alias at the route boundary.
  The requirement is a set-membership test on an unnormalized alias against a
  lowercased configured set, so even under the default configuration an alias spelled
  `Prod` skips it; the alias itself is an unchecked path parameter and no promotion
  path constrains it (`config.py:917-923`; `mcp_policy.py:444,454-457,562-563`;
  `routes/workflow_deployments.py:108-110`; `workflows/promoter.py:1632-1675`). This
  is currently an API-level exposure — the shipped UI offers only the single live
  `prod` alias (`caliber-ui/src/lib/environment.ts:12,19,23-25`) — but an
  API-created alias can carry a published service (`routes/services.py:175`;
  `schemas.py:3491`), and the same alias-keyed rule gates queued/synchronous runtime
  preflight as well as promotion (`routes/workflow_runs.py:575`;
  `routes/workflow_versions.py:793`; `orchestrator/workflow_run_worker.py:1497`), so
  a live-but-differently-named alias silently accepts local stdio execution.
- Stream/enforce stdout/stderr limits while the child runs instead of clipping only
  after `communicate()`, and construct every sandbox from the same configuration so
  memory/file/descriptor/output limits apply to workflow and Aria paths.
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
  verify the actual HTTP-sidecar topology, enforce database read-only sessions,
  preflight rollback/refinement transitions, require a separate target database and
  least-privilege role instead of the control-plane credential for production, and
  either provide a supported remote GitHub path or stop presenting it as
  quick-connect in the Python-only image.
- **Typed Aria inputs/references and explicit step skipping ship.** Add artifact/
  trace/workflow/agent pickers, richer planner intent mapping, editable draft
  inputs, skip propagation/cascade semantics, capability visibility scoping,
  dry-run/preflight, and created-artifact deep links; keep JSON escape hatches for
  power users.
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
11. **Trustworthy remote CI release signal:** the backend/UI/integration/static/
    security commands now pass remotely, seven-day retention is bounded, and unused
    SARIF upload is removed. Clear/manage artifact quota, make required evidence
    independently retrievable, restore/prove the wheel job, scan the implementation
    commit range rather than only a CI-only HEAD commit, repair Pages configuration,
    and obtain a fully green run. Passing commands inside a red workflow are useful
    evidence but not a shippable release gate.
12. **Close new lifecycle correctness defects:** reject/null-normalize Agent PATCH
    fields, scope skill checks, avoid viewer audit 403s, make import dependency
    terminology truthful, handle missing managed objects on every run path, and add
    negative regression tests for each finding in this review.

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
   auth in production, database-enforce read-only MCP queries under a separate role,
   build the missing live-effect/egress broker, and patch known project/run/eval/
   release/Aria/queue/judge authorization paths.
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
   make MCP preflight invariant across rollback/refinement transitions, enforce
   streamed sandbox output/resource policy, add an effect ledger/idempotency
   contract, and define backup/restore, upgrade, single-instance recovery, and
   operational readiness. HA is optional.
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
shipped development connector option for 05, and typed Aria inputs materially
upgrade 12–15. The DB connector is not production-safe while its read/no-approval
query path can mutate, and Aria skip is safe only when the skipped step has no
dependents.

| Result | Cookbooks | Current meaning |
| --- | --- | --- |
| Core result UI-complete on the standard stack | 01, 04, 06, 12, 16 | Managed project files close 04's root document path, and typed Aria inputs close 12's creation path. Documentation and current integrated-suite verification caveats remain. Cookbook 16's package/regression claims are still incomplete. |
| Mostly UI-complete | 02, 03, 05, 07, 08, 09, 10, 13, 14, 15 | 05 has a deployable database-sidecar demonstration, but its read policy is unsafe, GitHub recipe is external, and default DB credential is dev-only. 13/14 can skip their unwanted leaf add-items step; skipping any producer with dependents can strand an Aria plan. 13/15 use real prerequisite IDs when performing trace/calibration work. 03/08/09 still expose Preview/evaluation limits. |
| Not UI-complete | 11 | Evidence aggregation, rubric evaluation, go/no-go record, and rollback lineage remain manual. Formal waivers and multi-party signoff are excluded. |

The scope-adjusted implementation totals are **5 core UI-complete, 10 mostly
complete, 0 partial, and 1 blocked**. The converged backend coverage, frontend,
browser, and connector checks support these code paths. The red artifact pipeline,
unexercised HTTP-sidecar topology, and correctness/security findings prevent
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
| 05 | Governed Tool Connectivity | **Mostly UI-complete as a development demo; GitHub recipe remains external** | Policy/readiness/discovery/playground/calibration and principal runtime/deployment preflight ship, and PostgreSQL/pgvector/AGE presets target sidecars. `run_query` can mutate while classified read/no-approval, rollback/refinement bypass preflight, and local tests do not exercise the HTTP-sidecar topology. GitHub/Ollama/Playwright/MinIO/Hugging Face require provisioning. The DB presets default to CALIBER's own credential; production requires DB-enforced read-only behavior and a separate least-privilege target. |
| 06 | Grounded Knowledge Assistant | **Core UI-complete** | KB create/build/explore/query/graph/calibration and workflow/review paths exist, subject to normal provider, storage, and AGE readiness. |
| 07 | Support Triage Copilot | **Mostly UI-complete** | Prompt/skill/tool/KB, router/HITL, run, evaluation, and review primitives compose, but the documented required `escalate_bug → human_approval → GitHub create_issue` branch reuses cookbook 05's `npx` integration and cannot run in the shipped image. Its evaluation step also leaves the target at generic LLM instead of the workflow. The non-GitHub build/run branches remain composable. |
| 08 | Incident Response Copilot | **Mostly UI-complete; workflow evaluation refused** | Prompt/skills, Python fixture nodes, router/HITL, live runs, and review queue exist. Generic workflow-target evaluation invokes Preview and now correctly refuses the two `python_code` nodes, so the recipe cannot produce its advertised workflow scorecard until CALIBER has an isolated evaluation mode. |
| 09 | Self-Healing Workflows | **Mostly UI-complete as operator recovery** | Run monitor, checkpoints, debugger, retry/resume, approval, manifest editing, and publish exist. The required Preview validation of the patched `python_code` workflow now fails closed; the operator can still perform a real run, but there is no safe pre-rerun substitute and the patch remains human-authored. |
| 10 | Trustworthy Evaluation | **Mostly UI-complete** | Test Sets, judges, evaluations, review queues, and manual human-alignment metrics ship. The advertised baseline/candidate runs never select distinct artifacts; the candidate step does not reselect a judge/target; training claims per-example baseline deltas while the UI shows aggregate-card deltas only; and evaluation rows expose no trace IDs for the claimed direct enqueue step. Completed queue labels are not automatically ingested into alignment, so the evaluation-to-review/alignment loop requires manual bridges. |
| 11 | Release Signoff Factory | **Blocked for the scoped release path** | Evidence sources exist, but the deterministic rubric, evidence aggregation, go/no-go decision record, and rollback lineage remain manual/outside CALIBER; Releases is observational. Formal waivers, segregation of duties, and multi-party signoff are excluded. |
| 12 | Aria Evaluation Harness | **Core implementation path** | The heuristic selects `judge.create` and `eval_dataset.create`; each missing schema is rendered as a typed form, validated, then passes through the normal mutation gate and creates the real artifact. Complex values still use JSON and generated instructions describe the obsolete manual workaround. |
| 13 | Aria Review Governance Queue | **Mostly UI-complete with real trace IDs or a leaf-step skip** | Queue fields are collected, the created `queue_id` is wired into `add_items`, and supplied trace IDs can be enqueued. In the advertised queue-first/no-traces plan, `add_items` is a leaf, so **Skip step** lets that plan settle. This does not generalize: a skipped producer leaves dependents waiting. Capability scoping, literal planning, and JSON-heavy fields keep this below core. |
| 14 | Aria Governance Starter Kit | **Mostly UI-complete after an unwanted leaf-step skip** | Judge, dataset, and queue creation execute from typed forms. If the heuristic also schedules leaf `review_queue.add_items` without traces, **Skip step** settles that plan. Producer skip propagation is absent, capability visibility is inconsistent, and generated instructions remain stale. |
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
  numbers belong to an earlier pre-commit state of the work later committed as
  `5d9ae6cc0` and are deliberately not promoted to current results.
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
  describe that commit, not the `b2b838cbe` baseline reviewed here. The gate's cause
  of failure then changed: at `e4f9cb901` the isolated audit passed and gitleaks
  logged “no leaks found”, but the action treats its unread SARIF upload as fatal, so
  the job still failed. With that upload disabled, Security scan concluded
  successfully in run `30373909000`, while the artifact-quota failures in the other
  jobs persisted.
- Earlier local runs used unsupported Python 3.14 and one xdist attempt wedged while
  flushing shared MLflow trace artifacts. That diagnosis motivated per-process
  SQLite/artifact roots, synchronous trace export, and teardown cleanup. A later
  pre-integration supported-Python full run completed. A 2026-07-27 all-extras
  Python 3.12 xdist attempt then became pathologically slow and was stopped after
  5,024 passes, 8 skips, and 6 failures; all six passed serially. That attempt is
  retained as historical timing/concurrency evidence only: current Actions run
  `30373909000` supersedes it with a completed **5,123 passed / 12 skipped / 93.92%**
  supported-Python coverage result.
- **The MLflow test-harness isolation defect behind that wedge is closed at this
  baseline and is regression-covered.** The pre-`e4f9cb901` harness isolated only the
  tracking/registry URI per xdist worker
  (`sqlite:///$TMPDIR/caliber-test-mlflow-<worker>.db`) and left the artifact root at
  MLflow's `./mlruns` default, so trace artifacts still accumulated in
  `caliber/mlruns`. `caliber/tests/conftest.py:47-61` now creates one
  `caliber-test-mlflow-<worker>-<pid>` temp root per process, points
  `MLFLOW_TRACKING_URI`, `MLFLOW_REGISTRY_URI`, **and** the artifact root
  (`_MLFLOW_SERVER_ARTIFACT_ROOT`) inside it, and disables async trace logging; the
  root is removed at both `pytest_sessionfinish` and `atexit`
  (`conftest.py:64-82`, `conftest.py:195-202`).
  `caliber/tests/test_test_harness_isolation.py:24-44` is the regression: it asserts
  that the SQLite file and the artifact root share one `caliber-test-mlflow-` parent,
  that async trace logging is off, and that a freshly created experiment's
  `artifact_location` resolves under that root. A local re-run of that test plus
  `test_mlflow_tracing.py`, `test_assistant_tracing.py`, and
  `test_observability_trace.py` — 50 tests, all passing — left
  `caliber/mlruns/0/traces` unchanged at 8,150 entries, created no
  `caliber/mlflow.db`, and leaked no temp root. That re-run used the checked-out
  Python 3.14.4 virtualenv, outside this project's
  `requires-python = ">=3.10,<3.13"` (`caliber/pyproject.toml:10`), so it is
  mechanism evidence only; the supported-Python signal for the same test remains the
  Python 3.11 suite in Actions run `30373909000`. Three residual limits stand:
  `_MLFLOW_SERVER_ARTIFACT_ROOT` is an MLflow-private name
  (`mlflow/tracking/_tracking_service/utils.py:29` in mlflow 3.14.0), so the
  guarantee rests on an unsupported contract that only this regression would surface
  if it were renamed; teardown is best-effort — `shutil.rmtree(..., ignore_errors=True)`
  swallows failures and neither hook runs if a worker is killed; and the ~35 MB /
  8,150-directory pre-fix `caliber/mlruns` residue is only gitignored
  (`.gitignore:7`, `caliber/.gitignore:88`), not cleaned by the harness.
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
cloned/imported with guarded dependency inventory, connected to a content-pinned
document, executed with relatively little code, and inspected/debugged deeply**.
Routed Agent configuration, typed Aria capability execution, MCP launch policy, and
first-party DB sidecars are real improvements, not placeholder UI. Their boundaries
also matter: Agent checks are not runtime proof, import does not map dependencies,
Aria skip can strand dependents, and the DB read/no-approval contract is unsafe.

With enterprise capabilities excluded and the reviewed implementation applied, the
current risk-adjusted score is **3.0/5**. The answer remains two-part:

- **Yes** for predominantly low-code workflow composition, guarded link-preserving
  reuse, managed-document input, registered Aria automation, live execution by
  trusted technical operators, and deep inspection/debugging. Ordinary authorized
  Preview reads verified pinned files and remains fail-closed for effects it cannot
  isolate.
- **No** for building, evaluating, deploying, and operating a production-grade
  agent system end to end without manual engineering.

Today the realistic positioning is:

> **A late-alpha / early-beta-candidate, self-hosted agent engineering studio and
> lifecycle control plane for trusted technical teams—not yet a production-grade
> agent platform.**

The implementation choices were mostly rational. The remaining work is to make
authentication, resource scoping, secrets, transitive file/MCP capability policy,
database-enforced read-only behavior, effect isolation/idempotency, evidence,
truthful HITL, least-privilege database connectivity, release, and operations as
real as the workflow runtime. The stable supported-Python backend suite and coverage
gate are now proven remotely. A green artifact-dependent CI pipeline, successful
remote package job, implementation-range secret scan, and repaired Pages publication
are still required before this baseline can be treated as a release candidate.
