# CALIBER repository-wide product and architecture review

**Review date:** 2026-07-27

**Reviewed baseline:** clean repository state at
`8ddee7038caee65f83605e4caa0aa2307c7cab93`. This review independently evaluated
the report at that commit and the complete implementation, then applied the bounded
changes listed below in the working tree. The final evidence therefore describes
the reviewed diff on top of `8ddee7038`; it does not mislabel an uncommitted tree as
a new HEAD commit.

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

**No for production-grade end-to-end operation—even with enterprise readiness
removed from scope. Yes for predominantly low-code composition, local execution,
and inspection/debugging of deliberately isolated test runs.** CALIBER is an
unusually capable **low-code workflow development and debugging environment**, but
it is not yet a secure, governed, production-operable no-code agent platform. It can
visually compose and run sophisticated workflows; it cannot yet carry a developer/
operator reliably from idea to production without Python packaging, deployment
configuration, external secret handling, and significant operator engineering.

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

The production claim is still blocked by correctness and security defects, not
merely by missing polish. This pass reduced several risks without pretending that a
local patch can supply a production identity, vault, sandbox, or operations model:

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
   judge quality, regression, cost, or latency. Moreover, production aliases are
   ungated by default. This is safer containment, not release evidence.
5. **Ordinary Preview now refuses each compiled IR before that IR executes when it
   contains a dedicated unisolated file, folder, bucket, Python, MCP-resource,
   external-app, webhook, or API-request node.** A nested child is checked on child
   entry, so earlier safe parent nodes may already have run. Workflow-target
   evaluation and deploy-gate replay inherit the refusal. The product still lacks an
   effect broker: explicitly preview-enabled registered tools and knowledge queries
   retain their policy, while normal runs/services/retries expose application-
   unscoped paths, shared storage credentials, unrestricted egress, and at-least-once
   effects.
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

This independent pass accepted only changes with a narrow, testable contract. It
adds MCP response/audit containment; secure-by-default service publishing and token
UX; fail-closed Preview preflight and deploy-gate inputs; deterministic Test Set
snapshot browsing; complete-row scorecard aggregates and rejection of all-zero
effective weights; compatible baseline filtering and richer evaluation evidence;
review-queue state/type/concurrency checks; precise shared health polling; parent
resource authorization; and isolated MLflow test state plus CI security-signal
repairs. It deliberately does not claim that these containments are the missing
production subsystems.

Evaluation-row tags and scorer/model/target identity are now visible, weighted and
raw values are explained, incomplete rows are excluded from scorer aggregates, and
all-zero effective weights return a controlled 400 before prediction. Users can
browse the set active as of a dataset version while separately inspecting additions,
and baseline choices require the same dataset version and scorer suite; target,
subject, and model identity are disclosed but not enforced as compatibility gates.
Remaining evaluation limits include synchronous/truncated execution, mutable
judge/provider inputs, no cryptographic evidence bundle, no durable coverage schema,
and no continuous quality/cost/latency gate.

The scope-adjusted assessment rises from **2.6/5 to 2.7/5**. That does not change
the production answer: spoofable identity, arbitrary stdio command execution,
in-process extensions, normal-run egress/effect risks, completion-only release
evidence, and incomplete operations remain universal blockers.

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
| Developer debugging and run inspection | **4.0/5** | Best part of the product: run graph, events, checkpoints, retries, tool calls, memory, outputs, artifacts, and trace views. Trace-ID persistence is repaired in the reviewed workspace; deterministic replay remains incomplete. |
| Testing and evaluation | **2.9/5** | Real datasets, judges, weighted scorecards, dataset/scorer-compatible baselines, calibration, active-as-of browsing, and some regression gates. Incomplete rows no longer contaminate per-scorer aggregates and zero-total weights fail explicitly. There is still no durable large async eval, immutable run bundle, continuous eval, CI product-quality gate, or cost/latency gate. |
| Deployment and release management | **2.0/5** | Workflow versions, aliases, rollback, authenticated-by-default API publishing, token UX, and safer gate containment exist. The gate remains fake/completion-only and production aliases are ungated; deployment-scoped configuration/secrets and trustworthy evidence remain absent. |
| Operations and monitoring | **2.7/5** | Useful traces/metrics, token/cost/latency summaries, SSE, audit, system services, and one precise API/database health poll. Actionable alerts, deep readiness, queue/worker operations, drift, and failure recovery remain incomplete. |
| Platform UX | **3.3/5** | Cohesive visual language and broad discoverability; evaluation, Test Set, MCP, service-token, and health states are materially more truthful. Fragmented artifact idioms, missing agent/secret/alert pages, raw IDs, and giant workspaces remain material UX debt. |
| Production safety and access control | **1.5/5** | MCP API/audit readback, new-service auth defaults, selected parent scoping, and Preview containment improved. The default identity remains spoofable, stdio MCP composes into host command execution, literals remain stored, normal-run effects/egress are unbrokered, and extensions execute in-process. |
| Architecture and operability | **2.7/5** | Typed domain/runtime, durable SQL state, storage/event abstractions, extensive tests, deterministic gates, and isolated test state are strengths. Unbrokered effects/egress, arbitrary command execution, at-least-once effects, in-process extensions, hard-coded mode switches, and remaining scoping defects are material. |
| End-to-end low-code/no-code lifecycle | **2.5/5** | A user can build, test, debug, inspect evidence, and publish a token-protected API predominantly in the UI. Production security, packaging, release evidence, and operations still require manual engineering. |

**Risk-adjusted overall: 2.7/5, advanced alpha / self-hosted technical preview.**
Enterprise readiness is not scored; baseline production safety is. The ten
dimension scores have an arithmetic mean of 2.88, but the 1.5/5 safety dimension
still caps the overall judgment: spoofable identity, command execution, unbrokered
effects, and false release evidence remain blockers even though the workflow IDE is
strong.

## Current implementation state after this independent pass

### Accepted remediation and actual closure

This table records what is now true **and** the remaining contract boundary. It is
not a list of fully closed product areas.

| Area | Verified current behavior | Residual limit | Evidence / regression coverage |
| --- | --- | --- | --- |
| Evaluation visibility | Non-admin list no longer crashes: `owner_column()` supports `created_by`. Detail now resolves **through** `get_visible()`, so list and detail share the same default visibility predicate — a project header alone no longer unlocks another owner's run, and the creator's project-scoped rows outside the active project are no longer readable | List can additionally apply its explicit `only=<tier>` view filter. Scoping still depends on the client-supplied `X-CALIBER-Project` header and the demo identity of C1; that is an identity problem, not a default-predicate problem | `tests/test_scoping.py`, `tests/test_routes_evaluations_visibility.py` (incl. real create → list → detail round trips) |
| Dataset versions | Evaluation creation rejects future versions. `version=N` remains “added in N”; `as_of_version=N` uses the same active-membership predicate as evaluation/restore, with mutual-exclusion and range validation. The UI presents the paginated snapshot view, keeps later-retired members read-only, and makes restore explicit | The browser defaults to a 500-row page, evaluation has lower caps, and no cryptographic run/content digest or pre-truncation inventory proves exhaustive identity | `tests/test_routes_evaluations_reproducibility.py`, `tests/test_routes_eval_datasets.py`, `src/pages/__tests__/eval-dataset-detail.test.tsx` |
| Weights/tags/evidence | Loading and persisted rows retain both. Evaluation Detail renders tags, judge labels, prediction and error/incomplete state, target/subject/model, coverage, and an explicit weighted-versus-raw legend | Tags still lack grouped slice metrics; coverage is UI-derived from immutable rows rather than a durable aggregate schema | `tests/test_eval_scorecard_weighting.py`, `tests/test_routes_evaluations_reproducibility.py`, `src/pages/__tests__/evaluations.test.tsx` |
| Partial scorer failure and zero weights | Every failing scorer is reported; healthy raw scores remain for diagnosis, but an incomplete row has score 0, cannot pass, and is excluded from all per-scorer aggregates. A non-empty all-zero effective-weight set returns 400 before prediction | Aggregate schemas still do not persist valid-row/weight denominators for future asynchronous consumers | `tests/test_eval_scorecard_weighting.py`, `tests/test_routes_evaluations_reproducibility.py`, `tests/test_assistant_agent_tools.py` |
| Baseline comparison | The UI restricts baselines to successful runs with the same dataset/version and scorer suite, and discloses target, subject, and model identity | It does not reject a target/subject/model mismatch or compare threshold/sampling policy, and remains an ad hoc UI comparison rather than a controlled release gate | `src/pages/__tests__/evaluations.test.tsx` |
| Workflow trace linkage | Queued and synchronous runs persist `result.mlflow_trace_id`; the run trace panel and trace-to-run lookup can resolve it | Replay is not pinned to all resolved artifact/provider/configuration versions | `tests/test_workflow_run_trace_linkage.py` |
| MCP and provider secret readback | Provider reads return presence/fingerprint. MCP literal leaves are a write-only sentinel in list/detail/history/audit export; PATCH preserves unchanged leaves and safe env references remain visible. The UI omits unchanged sensitive maps and removes an obsolete token-env mapping when token auth is disabled | MCP literals still exist in ordinary DB JSON for runtime use; URI/command arguments are outside this three-field contract; no encrypted/reference-backed secret lifecycle, rotation, revocation, or consumer graph exists | `tests/test_mcp_servers.py`, `tests/test_mcp_servers_routes.py`, `src/pages/__tests__/mcp-servers.test.tsx` |
| Workflow service publishing | New services default to `auth_required=true`; the publish UI sends that choice, manages one-time bearer tokens, warns on explicit/legacy public state, scopes management through the parent workflow, and records the actual token/anonymous actor. OpenAPI download uses a parent-scoped authenticated Studio route without weakening the external bearer-gated route | Explicit public endpoints and legacy rows remain possible; no rate limits, quotas, CORS policy, durable vault, or trustworthy platform identity | `tests/test_routes_services.py`, `tests/test_cov90_routes_services.py`, `src/pages/__tests__/workflow-service-tab.test.tsx` |
| Review queue submission | Add/submit resolve the visible parent, archived queues reject writes, answer types/options are enforced, and an atomic pending→submitting claim prevents duplicate concurrent writeback; a failed external write restores pending | A crash can strand `submitting`; external success followed by local DB failure has no cross-system idempotency key; `reviewers`/`assigned_to` remain descriptive rather than enforced | `tests/test_routes_review_queues.py` |
| Preview and deploy-gate containment | Each `execute()` preflights its current IR and refuses ten dedicated unisolated capability-node types before that IR is traced/interpreted. A nested child inherits Preview and is blocked on child entry; earlier safe parent nodes may already have run. Deploy gates use Preview, deterministic ordering, and fail closed for missing/empty/archived datasets | Registered tools explicitly allowed in Preview and knowledge queries keep existing policy. The gate still uses a fake executor and completion-only metric, and normal runs lack an effect broker | `tests/test_workflow_preview_preflight.py`, `tests/test_workflow_promoter.py` |
| Shell health | AppShell owns one health-query observer and passes the result to Sidebar and TopBar; visible labels and ARIA text say only “API + database reachable,” and sustained polling is regression-tested | `/health` remains API/database liveness only, not worker, scheduler, queue, storage, MLflow, or provider readiness | `src/components/__tests__/sidebar-health-footer.test.tsx` |
| Test/CI isolation | Each test process/worker receives unique temporary MLflow SQLite and artifact roots; async trace export is disabled and roots are cleaned. Security audit bootstraps `setuptools>=83`, gitleaks is not skipped after an earlier failure, and the helper is invoked in an isolated CI environment | These changes repair the harness and workflow definition; they do not by themselves prove a new green remote CI run | `tests/conftest.py`, `tests/test_test_harness_isolation.py`, `.github/workflows/ci.yml`, `scripts/run-supported-python-security-audit.sh` |
| Unknown route UX | The wildcard renders a real Not Found view with a dashboard link | It is a client-rendered route boundary, not evidence of server HTTP-404 behavior | `src/pages/__tests__/app-shell-e2e.test.tsx` |
| Cookbook prose | Generated 03/08/10 material better reflects shipped side-effect, workflow-target evaluation, and alignment paths | Source/generated documentation still conflicts in the places catalogued in §10 | Generated artifact diff plus existing documentation checks |

Large product decisions remain open: identity, durable reference secrets,
systematic resource authorization, arbitrary stdio command execution, a real effect
broker for normal/live execution, evidence-grade release gates, canonical HITL,
isolated extensions, idempotent side effects, production topology, and operations.

### Recommendation decision ledger

Every recommendation in the prior report was treated as a hypothesis. “Useful” is
not equivalent to “complete,” and “important” is not automatically small enough for
this pass.

| Change or decision | Verdict | Reviewer assessment |
| --- | --- | --- |
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
5. **Current remote CI proof.** The repaired harness is now proven by a complete
   supported-Python local run plus integration, browser, static, build, and package
   checks. There is still no new GitHub Actions run proving artifact upload,
   dependency audit, gitleaks, packaging, and the matrix green together.
6. **Release, HITL, isolation, and topology.** Deploy gates remain completion-only,
   manifest approval semantics remain inconsistent, extensions execute in-process,
   and production roles/recovery are incomplete.

## Verification

### Final reviewed-workspace verification (2026-07-27)

All results below were produced from the reviewed working tree on top of
`8ddee7038`, not copied from an earlier report or described as a new commit.
Inventory was recounted directly: **40** registered route modules, **314** literal
`Route(...)` declarations, **60** top-level models, **26** lazy routed components
(25 workspaces plus one workflow-run redirect), **29** workflow component kinds,
**13** workflow templates, **13** router operators, **264** backend test files,
**109** frontend unit/spec files, and **8** Playwright spec files.

| Check | Final result | What it establishes |
| --- | --- | --- |
| Full backend, Python **3.12.10**, actual full test extras, `pytest -n auto --dist loadscope -q` | **5,065 passed, 7 skipped, 0 failed in 542.94s; 94.43% coverage** against the 80% gate | The complete encoded backend suite, packaged-UI parity check, and repaired per-worker MLflow isolation complete cleanly under supported Python. Six skips are the opt-in integration module and one is an unavailable PostgreSQL MCP target. |
| Marked backend integration suite | **6 passed, 3 skipped, 5,065 deselected in 8.23s** | Local MLflow integration passes. The three PostgreSQL MCP cases remain skipped because no `POSTGRES_URL`/PostgreSQL service was provisioned. |
| Focused optional-provider regression set | **297 passed in 954.79s** | The modules that failed in the first core/dev-only diagnostic all pass once the repository's actual optional test extras are installed. This overlaps the full run and is not added to its count. |
| Frontend Vitest | **109 files / 1,478 tests passed in 142.00s** | All unit/component/page contracts pass, including evaluation evidence, active-as-of datasets, MCP write-only edits, service tokens/OpenAPI download, and sustained shared health polling. Existing test-console warnings remain debt but did not hide failures. |
| Browser E2E, Chromium + repository MinIO dependency | **23 passed, 1 skipped in 49.0s** | The full eight-spec product journey set passes, including object storage, KB build/query, workflow/HITL/event/wait runs, navigation, docs, and auth shell. Only the explicitly tagged Apache AGE path is skipped because PostgreSQL/AGE was not provisioned. |
| Frontend static/build | ESLint passed; TypeScript passed; two production builds passed (**2,511 modules**, final build 1.46s) | The final source typechecks/lints and produces a deterministic SPA. The built output was synchronized into `src/caliber/ui`. |
| Backend static | Ruff check passed under both the CI-pinned version and fresh 0.16 rule set; CI-pinned Ruff format reports **479 files formatted**; mypy reports **272 source files / no issues** | Changed and untouched backend code still satisfies the repository's official style/type contracts. `PLR0917` was added to the existing intentional too-many-arguments exemption after Ruff 0.16 promoted the rule. |
| Python package | Built `caliber-0.1.0.dev0` wheel and sdist successfully; both were inspected and contain `caliber/ui/index.html` plus hashed SPA assets | The frontend is not merely buildable in its own directory; the distributable Python artifacts carry it. |
| Dependency/security workflow | The isolated supported-Python **dev** audit reports **no known vulnerabilities**; the CI helper now uses `setuptools>=83`, and gitleaks is conditioned only on cancellation | This repairs the previous bootstrap/advisory and skipped-secret-scan definition. It is not a new remote Actions result. The optional DSPy stack still emits the repository's known `diskcache 5.6.3` advisory and is runtime-blocked unless explicitly overridden. |

Diagnostic failures were investigated rather than erased. An unsupported local
Python 3.14 environment was rejected as evidence. The first Python 3.12 run had only
core/dev extras, restricted loopback sockets, and an unsynchronized built SPA, so it
reported optional-import, socket, and package-drift failures; the supported full-
extras run above resolves them. Initial browser passes exposed an unstarted MinIO
prerequisite, two stale navigation/docs assertions, and one editor timeout while the
full backend suite saturated the same machine. After correcting the test contracts,
starting the documented dependency, and rerunning without contention, the complete
browser suite passed. Temporary services were returned to their prior state.
Coverage fragments, Playwright/Allure reports, temporary build distributions,
browser test state, Python caches, and the temporary Python 3.12 environment created
by this review were removed afterward. The synchronized `src/caliber/ui` payload is
retained intentionally because it is the package input validated above; pre-existing
ignored developer data such as `mlruns`, dependency directories, and caches outside
this review were not destructively purged.

Not tested here: real paid LLM/provider calls, arbitrary external MCP applications,
PostgreSQL/Apache AGE integration, sustained load/soak or multi-process failover,
formal browser accessibility, or penetration testing. No new remote GitHub Actions
run proves artifact upload and action integration. Passing tests prove encoded
behavior—not the production safety or lifecycle completeness assessed in this
report.

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

- the 26 lazy product routes/components in `caliber/caliber-ui/src/App.tsx`
  (25 page workspaces plus one workflow-run redirect), plus login redirects and
  wildcard handling, the workflow canvas/inspector/
  debugger, assistant shell, API client, and workspace state;
- the centralized backend registry of 40 route modules and 314 literal
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
| MCP | Catalog, connection setup, discovery, playground, policies, tests, calibration | Rich surface with write-only API/audit/UI containment; catalog is not deploy-image aware and literal storage lacks a durable secret lifecycle. |
| Files/storage | Object Store, project files API, workflow file/folder/bucket nodes | Multiple storage concepts do not compose into one safe file-reference contract. Preview/evaluation refuse dedicated file/storage nodes, while live runs still lack application-level allowed-root/bucket capabilities and rely on OS/container/storage-IAM boundaries. |
| Knowledge/RAG | KB inventory/editor, sources, builds, chunks, query playground, GraphRAG/AGE, calibration, versions | One of the strongest artifact workspaces; provider/storage readiness remains operator-managed. |
| Workflow Studio | Inventory/templates, React Flow editor, inspector, code view, versions, detail graph | Strong low-code composition environment. |
| Workflow runtime | Preview, queued runs, events, approvals, checkpoints, retry/resume, memory, artifacts, trace/debug panels | Deep runtime UX and repaired trace linkage. Preview now fails closed for known unisolated dedicated nodes, but cannot simulate them; approval policy, deterministic replay, live-run isolation, and duplicate-side-effect risks remain serious. |
| Workflow deployment | Versions, navigation-hidden/deep-linkable deployments and promotions, service publishing, tokens, patches | Core primitives and secure new-service default exist; release gates/default aliases and evidence still bypass production governance. |
| Test Sets | Dataset inventory/detail, additions history, active-as-of snapshot, restore, trace import, MLflow sync | Useful curation; bulk/splits, immutable content digests, exhaustive snapshot proof, and grouped slice semantics remain incomplete. |
| Evaluation | Evaluations, detail scorecards, judges, alignment, review queues | Valuable ad hoc evaluation; not a continuous or release-grade evaluation system. |
| Aria | Assistant panel, plans, interactions, drafts, approval/publish flows | Broad shell; typed planning/execution is not reliable enough for autonomous no-code creation. |
| Observability | Trace search/detail/compare, metrics charts, Allure link, system services | Good inspection; lacks alert-to-action operations. |
| Gateway | Endpoints, guardrails, pricing, usage | Useful control-plane visibility; it does not by itself make CALIBER deployment secure or scalable. |
| Release/review controls | Audit log/export, review queues, Releases | Review state/type/concurrency and MCP audit safety improved; release evidence, recovery/idempotency, and advertised workflow-approval behavior remain incomplete. Formal enterprise signoff is excluded. |
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
| Build | Visual workflows, prompts, skills, KBs, schemas, MCP/API/webhook nodes | Reusable tool implementation needs Python packaging; file/object references are fragmented and not capability-scoped; several advanced fields are raw JSON/expressions. |
| Test | Preview, sandboxes, component test runs, datasets, judges, workflow eval | Preview now refuses known unisolated dedicated nodes instead of executing them, but there is no effect-broker simulation, workflow unit/assertion suite, server-authoritative component record, full-dataset async runner, or reusable suite policy. |
| Evaluate | Weighted row scorecards, custom judges, safer baselines, active-as-of snapshots, alignment, review queues, prompt/workflow refinement gates | Synchronous caps, no immutable resolved run bundle or grouped slice UI, no cost/latency, no scheduled/continuous eval, no CI product-quality gate, and mutable judges. |
| Deploy | Publish versions, alias deployments, rollback, authenticated-by-default workflow HTTP service and token UI | A single environment is acceptable, but the fake/completion-only and default-disabled release gate plus weak immutable evidence are not production release management. Formal multi-party approval is excluded. |
| Operate | SSE updates, trace detail/compare, tokens/cost/latency, logs/events, audit, health, coarse fleet/success ratios | Trace linkage is repaired; actionable alerts, trustworthy health, queue/worker visibility, effect idempotency, recovery evidence, and incident diagnosis remain incomplete. Multi-region HA is excluded. |
| Control | Four scopes, audit rows, review queues, dormant workflow promotion state machine | Spoofable identity, stored MCP literals, inconsistent resource scoping, misleading HITL controls, and observational release evidence remain. Organization/membership governance is excluded. |

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
cost/latency evidence is captured, and `GATED_ALIASES` is empty by default. The gate
must not authorize a production release.

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

#### C9 — Preview refuses known unisolated capability nodes — **[Partly remediated]**

`execute(..., preview=True)` now scans the complete compiled IR before creating a
trace or entering the interpreter. If it finds `file_input`, `folder_input`,
`input_bucket`, `output_bucket`, `output_folder`, `mcp_resource`, `python_code`,
`external_app`, `webhook`, or `api_request`, it returns a structured error naming
every blocked node and executes none of them. Scanning dormant as well as currently
reachable nodes is intentionally fail-closed. Nested subworkflow execution inherits
Preview and applies the same rule to the child. Focused tests prove blocked handlers
are not invoked.

This materially changes the earlier finding: ordinary Preview, generic
workflow-target evaluation, and direct deploy-gate replay no longer perform those
dedicated effects. They instead cannot evaluate such a workflow at all. The
restriction is honest containment, not a universal effect architecture:

- registered tools retain the explicit `allow_in_preview` contract and knowledge
  queries can still execute; knowledge builds keep their separate preview-skip;
- normal queued/synchronous/service runs still let process-accessible local paths,
  author-selected storage namespaces under process credentials, and installed
  external code execute without a central capability broker;
- webhook/API manifests still accept arbitrary destinations and the default sender
  lacks scheme/domain/IP policy, private/link-local/metadata blocking, DNS-rebinding
  defense, and network-level containment; and
- retries/lease recovery still lack an effect ledger, so normal mutations remain
  at-least-once.

The architectural requirement remains a common effect broker with immutable file/
object references, per-deployment capabilities, centralized egress policy, audit,
budgets, and idempotency. The platform must not relabel “Preview refused” as “the
workflow was safely evaluated.”

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
| MCP integration | **Partial** | Registration, discovery, invocation, policy, generated tests, calibration, and write-only API/audit/UI containment exist, but none of the eight quick-connect templates has a viable default preflight in the shipped container. Credentials still lack a durable reference lifecycle. |
| File/folder/object-storage nodes | **Shipped for live runs; refused in Preview** | Local and bucket nodes provide useful composition but are not application-scoped to approved roots/buckets. Preview/evaluation now fail the whole workflow before these nodes execute; live runs still rely on process filesystem access and process-wide storage credentials/IAM. |
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
2. MCP catalog entries are not preflighted against the actual runtime image. The
   eight definitions are at `McpServers.tsx:831-1001`; the final image is Python-only
   (`deploy/caliber/Dockerfile:9-18`), shipped Compose omits `POSTGRES_URL`, and the
   gateway resolves a missing `${VAR}` to an empty string
   (`mcp_gateway.py:438-454`). The dialog retains template env maps but exposes no
   general env-value editor (`McpServers.tsx:255-383,498-522`).
3. Object Store, project files, run files, and local-path ingestion do not share one
   first-class file-reference protocol. Cookbook 04 still needs a host path, and
   workflow file/folder/bucket nodes have no per-workflow root/bucket capability.
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
| Publish an API | **Partial, auth-on by default** | Service/OpenAPI/status endpoints and one-time bearer-token create/list/revoke UI exist; explicit/legacy public state is warned. Rate limits, CORS, quotas, durable secret storage, and a trustworthy platform identity remain absent. |
| Preview safely | **Fail-closed containment** | Preview refuses the entire IR before execution when it contains ten known unisolated dedicated capability nodes. That prevents mixed live/dry behavior, but registered-tool/knowledge-query policy and the lack of a real effect broker mean this is refusal—not universal isolated simulation. |

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

The repository contains 264 backend test files, 109 frontend unit/spec files, and
8 Playwright specs. CI runs lint/type checks, backend tests, integration tests,
Vitest, typecheck, and a frontend build. That breadth is a real engineering
strength. However:

- default admin fixtures hide non-admin authorization failures — this is how the
  eval list crash survived (now covered by non-admin tests, but the fixture
  default itself is unchanged and will hide the next one);
- no cross-user/project workflow-run authorization tests were found;
- no tests vary HITL required role, quorum, timeout, or actor identity while those
  fields remain exposed;
- deploy-gate tests now require missing/empty/archived datasets to fail, deterministic
  bounded ordering, and Preview; they correctly retain the completion-only contract
  rather than pretending it is a quality gate;
- dedicated preview preflight tests require ten unisolated node types and nested
  subworkflows to fail before handlers execute; normal-run effect/egress and
  registered-tool/knowledge-query policies remain to be covered by a broker contract;
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
   threshold fields, and is disabled for every alias by default.
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
| Secrets | **Serious gap, readback contained** | Provider keys and MCP literal leaves are no longer returned through known browser/audit surfaces. MCP still accepts and stores literal credential JSON for runtime use. A durable encrypted/reference-backed resolver with rotation/revocation is required; an enterprise vault marketplace is not. |
| Published API authentication | **Partial** | New UI-published services are token-authenticated by default and expose token lifecycle. Explicit/legacy public services, no rate/quota/CORS policy, and C1's spoofable platform identity remain. |
| Effect isolation and egress | **Critical gap, Preview contained** | Preview/evaluation now refuse ten known unisolated dedicated node types. Normal runs/services still permit application-unscoped filesystem/storage capabilities and unrestricted webhook/API egress; explicitly preview-enabled tools and knowledge queries retain their policy. There is no broker or SSRF defense. |
| Extension/MCP execution | **Critical gap** | Registered/external-app Python runs in-process, while arbitrary stdio MCP configuration composes with default admin auth into host command execution. |
| HITL/review correctness | **Misleading, review records improved** | A single authorized reviewer is sufficient. Queue state/type/concurrency checks now prevent ordinary overwrite/duplicate submission, but workflow role/quorum/timeout semantics remain false and cross-system recovery is incomplete. |
| Audit correctness | **Partial** | Transaction-coupled rows, filtering, MCP legacy redaction, actual service-token actor attribution, and export are useful. WORM/SIEM/compliance evidence is excluded; comprehensive secret/actor correctness still needs contract tests. |
| Release evidence and recovery | **Critical gap** | Formal enterprise signoff is excluded. Missing/empty/archived gate inputs and dedicated Preview effects now fail closed, but fake/completion-only evidence, default-disabled gates, incomplete pinning, and duplicate normal effects still make one-operator release unsafe. |

Enterprise exclusions remove product-suite breadth requirements; they do not make an
untrusted identity boundary, host command execution, secret disclosure, SSRF, or
unsafe release evidence acceptable.

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
- Local file/folder nodes accept process-accessible paths without an application
  allowed-root policy, and S3/MinIO bucket nodes select namespaces while reusing
  process-wide credentials within their IAM permissions. Preview/evaluation now
  refuse these dedicated nodes, but normal live runs still have no per-workflow
  capability object or worker-level filesystem/object-store boundary. OS/container
  permissions and the local storage backend's configured root remain outer
  containment boundaries.
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
- **[Remediated]** Workflow service token CRUD is now exposed in the publishing UI
  and new services are auth-on by default; production identity, vault, quota, and
  explicit-public governance remain separate gaps.
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
  Cookbook 03's mocked-write Preview, cookbook 08's workflow-target evaluation, and
  cookbook 09's pre-rerun Preview therefore need product/recipe redesign rather than
  being counted as unchanged successes.
- Workflow editor unmount autosave hides errors.
- Generated cookbook footers still claim every recipe is implementable through the
  UI, while `docs-site/cookbooks/FEASIBILITY.md` retains false HITL role/quorum/
  timeout and prompt-playground claims.
  The cookbook root README likewise says only 04, 05, and 11 need out-of-band work,
  contradicting the verified 07/10 limitations and failed advertised Aria paths in
  12–15.
  `docs-site/cookbooks/training/content.py` still says evaluations cannot score
  workflows, the cookbook README's ladder omits cookbook 16, and cookbook 10's
  verification/assets still describe alignment as a manual by-hand step.
- Cookbook 05's README, scenario, verification, and generated training step say MCP
  invocation emits no MLflow spans, but `mcp_gateway.py:134-186` wraps every allowed
  gateway invocation in a `TOOL` span. Its observability instructions are therefore
  the inverse of the current implementation for calls that reach the gateway;
  policy-blocked calls return before tracing. The training also says to supply a
  GitHub token in the dialog, while the UI accepts only an environment-variable name
  and never a value.
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

1. **Standalone agent lifecycle:** create agent → configure model/prompt/skills/tools/
   memory → version → test → evaluate → deploy → monitor → rollback.
2. **No-code custom integration:** choose connector/API → bind write-only secret →
   test in deployment-equivalent sandbox → version → approve → reuse.
3. **Uploaded document to workflow:** upload binary → select first-class file ref in
   run form → bind only approved root/bucket capabilities → parse in queued worker →
   preserve checksum/media/lineage → inspect output without a raw host path or
   process-wide storage credential.
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
- Disable arbitrary stdio MCP command registration in production. Permit only
  signed/allowlisted server packages and launch them in an isolated least-privilege
  worker, never the API/control-plane process.
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
  fake-executor semantics and default-disabled aliases remain blockers.
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

#### P6. Close the advertised no-code composition blockers

- Introduce a first-class immutable file reference across Object Store, project
  files, run forms, queued workers, tools, KBs, and attachments.
- Make the MCP catalog deployment-aware and provide a runnable GitHub integration
  in the exact shipped image/sidecar model. Preflight the built container for the
  configured executable, required environment variables, credential references,
  discovery, and a safe real invocation; today GitHub/Ollama/Playwright require
  absent `npx`, MinIO requires absent Docker, Hugging Face invokes the nonexistent
  `pip run` subcommand, and the PostgreSQL-family templates receive an empty
  `POSTGRES_URL` in shipped Compose.
- Give Aria typed capability forms, editable step inputs, output references,
  preflight, and dependency-aware execution.

**Exit criterion:** cookbooks 04, 05, 07, and 12–15 complete on the shipped stack
without host paths, shell/package installation, failed or skipped mutation steps,
or manual artifact recreation.

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
6. **No-code closure:** file refs, deploy-aware MCP, typed Aria, agent/project pages,
   complete service policy, and bundle import/clone.
7. **Continuous quality and operations:** async/continuous eval, SLOs/alerts, fleet
   health, incident workflow, and cost budgets.
8. **Consistency and scale UX:** unified lifecycle components, monolith reduction,
   global navigation/search, and bulk operations.

## Appendix A — Cookbook continuity

The earlier cookbook-specific audit was rechecked against the current reviewed
workspace. It remains useful evidence, but the fail-closed Preview change materially
changes cookbooks 03, 08, and 09: their `python_code` preview/evaluation steps are
now safely refused and therefore no longer complete product paths.

| Result | Cookbooks | Current meaning |
| --- | --- | --- |
| Core result UI-complete on the standard stack | 01, 06, 16 | The central author/run/inspect result can be reached, subject to the platform-wide security and production caveats in this report. Cookbook 16's source package and advertised regression-proof steps remain incomplete. |
| Mostly UI-complete | 02, 03, 07, 08, 09, 10 | Existing author/run paths are useful, but each has a broken bridge. In 03/08/09, the new rational Preview containment exposes a previously hidden product gap: workflows containing `python_code` are refused rather than safely simulated/evaluated. Other qualifications remain below. |
| Intended Aria workflow only partial | 12, 13, 14, 15 | Final artifacts can be built manually elsewhere in the UI, but Aria's empty-input mutations fail when approved and are skipped only when denied. |
| Not UI-complete | 04, 05, 11 | Host-local document input blocks 04; catalog executables/environment are not viable in the shipped container for 05; and 11 still requires manual evidence aggregation, rubric evaluation, go/no-go recording, and rollback lineage. Formal waivers and multi-party signoff are excluded. |

The scope-adjusted totals are **3 core UI-complete, 6 mostly complete, 4 partial,
and 3 blocked**.

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
| 04 | Document-to-JSON Pipeline | **Blocked** | Object Store upload and document extraction exist separately, but `extract_document` requires a CALIBER-host filesystem path while `input_bucket` decodes object bytes as text. The UI cannot pass an uploaded binary as the required file reference. |
| 05 | Governed Tool Connectivity | **Blocked in shipped image** | MCP catalog/setup/discovery/playground/policy/calibration exist, but none of the eight quick-connect defaults preflights successfully as shipped: GitHub/Ollama/Playwright launch absent `npx`, MinIO launches absent Docker, Hugging Face uses invalid `pip run`, and PostgreSQL/pgvector/AGE resolve `${POSTGRES_URL}` to empty because Compose does not inject it. The dialog has no general environment-value/secret editor. Cookbook claims that MCP emits no MLflow spans also contradict the current gateway tracer. |
| 06 | Grounded Knowledge Assistant | **Core UI-complete** | KB create/build/explore/query/graph/calibration and workflow/review paths exist, subject to normal provider, storage, and AGE readiness. |
| 07 | Support Triage Copilot | **Mostly UI-complete** | Prompt/skill/tool/KB, router/HITL, run, evaluation, and review primitives compose, but the documented required `escalate_bug → human_approval → GitHub create_issue` branch reuses cookbook 05's `npx` integration and cannot run in the shipped image. Its evaluation step also leaves the target at generic LLM instead of the workflow. The non-GitHub build/run branches remain composable. |
| 08 | Incident Response Copilot | **Mostly UI-complete; workflow evaluation refused** | Prompt/skills, Python fixture nodes, router/HITL, live runs, and review queue exist. Generic workflow-target evaluation invokes Preview and now correctly refuses the two `python_code` nodes, so the recipe cannot produce its advertised workflow scorecard until CALIBER has an isolated evaluation mode. |
| 09 | Self-Healing Workflows | **Mostly UI-complete as operator recovery** | Run monitor, checkpoints, debugger, retry/resume, approval, manifest editing, and publish exist. The required Preview validation of the patched `python_code` workflow now fails closed; the operator can still perform a real run, but there is no safe pre-rerun substitute and the patch remains human-authored. |
| 10 | Trustworthy Evaluation | **Mostly UI-complete** | Test Sets, judges, evaluations, review queues, and manual human-alignment metrics ship. The advertised baseline/candidate runs never select distinct artifacts; the candidate step does not reselect a judge/target; training claims per-example baseline deltas while the UI shows aggregate-card deltas only; and evaluation rows expose no trace IDs for the claimed direct enqueue step. Completed queue labels are not automatically ingested into alignment, so the evaluation-to-review/alignment loop requires manual bridges. |
| 11 | Release Signoff Factory | **Blocked for the scoped release path** | Evidence sources exist, but the deterministic rubric, evidence aggregation, go/no-go decision record, and rollback lineage remain manual/outside CALIBER; Releases is observational. Formal waivers, segregation of duties, and multi-party signoff are excluded. |
| 12 | Aria Evaluation Harness | **Partial; advertised Aria path fails** | Judges and Test Sets can be built manually. Aria emits empty-input `judge.create`/dataset steps; approval fails validation and denial merely skips them. |
| 13 | Aria Review Governance Queue | **Partial; advertised Aria path fails** | Review Queues can be operated manually, but Aria cannot provide the queue schema/trace IDs through typed step inputs. |
| 14 | Aria Governance Starter Kit | **Partial; advertised Aria path fails** | Judges, Test Sets, and Review Queues exist as manual pages; the “one sentence to whole kit” mutations do not execute successfully. |
| 15 | Aria Triage & Recalibrate Loop | **Partial; advertised Aria path fails** | Queue and workflow calibration can be driven manually. Aria does not successfully create/enqueue/start them, so the claimed job wait/poll/resume chain is not demonstrated. |
| 16 | Production Observability & Triage | **Core UI capability; advertised regression loop/source package incomplete** | Trace filtering/detail, Test Set capture, queue enqueue/review, and workflow-target evaluation exist; needing prior runs is inherent. The recipe's eval step leaves the target at generic LLM rather than the repaired workflow and does not require corrected gold, so its “re-run after fix” proof is not executable as written. The folder also omits all four promised YAML contracts, and queue-review copy overstates displayed trace context. |

These counts assess whether each cookbook's **central product path** is reachable,
not whether every generated/source instruction or asset contract is correct. Those
recipe/package defects are explicit qualifications, especially for cookbook 16; a
“core” result does not mean its documentation bundle is clean. Nor is a cookbook
that runs under the local-admin single-environment stack proof of authentication,
effect isolation, safe release evidence, failure recovery, or operational
completeness.

## Appendix B — Verification history and superseded passes

Retained only to preserve the evidence trail; it is superseded by the current
[Verification](#verification).

- Historical GitHub Actions run `30286528826` at `851b04597` had **5,023 backend
  tests pass, 12 skip, and 94.43% coverage**; UI had **109 files / 1,467 tests** and
  integration had **6 pass / 3 skip**. The workflow was nevertheless red because
  artifact quota failures prevented evidence uploads/wheel execution and the old
  security bootstrap found `setuptools==79.0.1` before gitleaks ran. Those numbers
  describe that commit, not this working tree.
- Earlier local runs used unsupported Python 3.14 and one xdist attempt wedged while
  flushing shared MLflow trace artifacts. That diagnosis motivated per-process
  SQLite/artifact roots, synchronous trace export, and teardown cleanup. The fresh
  supported-Python full run now completes, so the old wedge is historical rather
  than a current limitation.
- An initial supported-Python run installed only the core/dev extras and therefore
  reported optional-provider import failures, sandbox-denied loopback probes, and a
  packaged-UI drift check. Reinstalling the actual full test extras, allowing local
  test sockets, and synchronizing the built SPA resolved those setup failures.
- The first browser pass exposed stale navigation/docs assertions and an unstarted
  documented MinIO prerequisite; a concurrent rerun also produced one editor
  timeout under backend-suite load. Those diagnostics are not counted as final
  product failures; the corrected, dependency-complete, uncontended final browser
  result is recorded in Verification.

## Final assessment

CALIBER already demonstrates that a sophisticated agent workflow **can be composed
and locally executed with relatively little code, and that deliberately isolated
runs can be inspected and debugged deeply**. Its workflow runtime, developer
inspection tools, prompt/skill/knowledge workspaces, and audit/evaluation
foundations are substantial enough to justify continued product investment.

With enterprise capabilities excluded and the accepted containment/correctness work
applied, the current risk-adjusted score is **2.7/5**. The answer remains two-part:

- **Yes** for predominantly low-code workflow composition, live execution by
  trusted technical operators, and deep inspection/debugging. Ordinary Preview is
  now honestly fail-closed for known unisolated dedicated nodes rather than a
  universal simulation environment.
- **No** for building, evaluating, deploying, and operating a production-grade
  agent system end to end without manual engineering.

Today the realistic positioning is:

> **A self-hosted, developer-oriented agent workflow studio and lifecycle
> control-plane preview—not yet a production-grade agent platform.**

The shortest credible path to the revised vision is not more canvas nodes or an
enterprise administration suite. It is to make authentication, resource scoping,
secrets, effect isolation/idempotency, evidence, truthful HITL, release, deployment,
and operations as real and composable as the workflow runtime already is.
