# R1: Roadmap, plans, specifications and decisions — critical synthesis

Scope: what the Caliber program says it is building, what it has built, where its own plans already address (or do not address) progressive autonomy, and where they conflict with the "Work -> Roles -> Human/Agent Assignment -> Capabilities -> Autonomy -> Evaluation -> Governance" vision. Posture: critical, not advocating. Facts are cited to a file or issue; inferences are labelled as inference.

Snapshot: `main` at `82f41d1b85d` (2026-10-31); issue state from `gh` the same day (one caveat on lag, see section 2.3). Book spec read from the sibling checkout `../building-enterprise-general-intelligence` at `dacedba`, the commit the plans pin.

---

## 0. Headline findings

1. **The program is no longer the product the PRD describes.** On 2026-10-24, one day after the 0.1.0a1 prerelease, the owner replaced the narrow "governed prompt-refinement, single-tenant v1.0.0" MVP with the EGI program: be "the reference implementation of *Building Enterprise General Intelligence*" (a book by the same author). The scope jumped from one journey to 16 Beta packages (B01-B16) plus 27 MVP packages (M01-M27), 35 validation scenarios, 15 milestones (D1-D15) and a 122-week midpoint estimate (`docs/CALIBER_EGI_DELIVERY_PLAN.md`). The same week, `docs/competitive-analysis.md` recommended the opposite: "own the wedge, don't chase breadth".
2. **An autonomy model already exists and is accepted: ADR 0004 (L0-L5, per use case, owner always human).** It is a design, not code. No `principal_type`, `autonomy.py`, `governance.py` or `principals.py` exists on main. Nothing in the repo records whether a decision-maker was a person or an agent. Enforcement is scheduled for D10 (M27, epic #632, 9 child tasks, all open), and D10 cannot start until the D9 control #617 closes.
3. **ADR 0004 defines "autonomy" differently from the book it claims to follow.** The book defines a run's autonomy as the *grant scope* (tools, data, time, tokens, fan-out, delegation, mutation scope; `01-part-i.md` principle 2). ADR 0004 defines it as *which decision-chain roles an agent may hold* (editor, reviewer, approver). These are orthogonal axes (authority envelope versus oversight structure). The plan says "actual authority is the intersection" (`map-gap.md` section 7) but no task parameterises grants, budgets or delegation by level.
4. **Evidence-driven promotion is specified, but its evidence source is circular.** Promotion to L3 requires a measured period "at the current level" and a trajectory gate over "that period's decisions" (ADR 0004 invariant 5; #868). At L2 the agent holds no reviewer or approver chair, so no agent review decisions exist to measure. The plan has no shadow or advisory-agent mode (`grep -ci shadow` on the manifest returns 0). Defaults of 30 days and 200 decisions (#868) are fixed constants; zero failures in 200 decisions only bounds the failure rate near 1.5% at 95% confidence (rule of three, inference).
5. **Beta is 64% closed by task count (80 of 125) after six days, against a plan that allots 29 calendar weeks.** The estimates are person-weeks; execution is agent-driven (D1 findings say "agent wall-clock, not person-hour"). The real constraints are therefore human review capacity, external reviewers and real-provider evidence (#608, still open and unforecast), not the staffing model the calendar assumes. D2's acceptance report states plainly that no human or external review occurred and that PRs were merged by the implementing agent after review by "a fresh independent agent".

---

## 1. What the program is

### 1.1 Stated thesis (as written)

| Source | Thesis |
| --- | --- |
| `docs/CALIBER_REFERENCE_ARCHITECTURE_GAP_ANALYSIS.md` section 1 | Evolve CALIBER into a "single-tenant, single-region reference platform for governed agent workflows"; "the most important investment is a consistent execution contract" across calibration, workflow, registry, approval and release. Book principle: "The model proposes. The system decides. The evidence proves." |
| `map-gap.md` section 2 | "Control plane" means "authoritative decisions and lifecycle management", not a dashboard. Eight principles including *Control plane first*, *Deny unsupported behavior*, *Model proposes; system decides*, *Evidence before autonomy* ("A global 'autonomous' toggle or a second prompt is [not] independent review"). Final recommendation: "a governance and lifecycle control plane, not another all-in-one agent framework". |
| `docs/CALIBER_EGI_DELIVERY_PLAN.md` decisions 1-4 | (1) EGI program is the MVP; (2) three full-time people; (3) CALIBER is the reference implementation of the whole book, so A2A federation, governed memory, Knowledge Hub, multi-region and connector ecosystem become packages M17-M26; (4) "Autonomy is a per-use-case ladder of agent-held roles ... owner role always human" (M27, ADR 0004). |
| `README`/release notes | "MLflow-integrated control plane for trusted agentic workflows" with "a person, not the model, deciding when a change goes live" (`docs/reports/release-0.1.0a1-notes.md`). |

### 1.2 Intended end state

"Book-complete MVP" at D15: all of B01-B16, M01-M27 and V01-V35 pass within the single-tenant scope (V28-identity required; V28-shared-tenancy explicitly excluded), a chapter-to-contract-to-test traceability table has no unexplained row, an independent security review is complete, and a release decision is made (`CALIBER_EGI_DELIVERY_PLAN.md`, D15 and "Book traceability"). Midpoint estimate 122 weeks (211 base person-weeks plus 25%); upper bound about 148 weeks. Calendar dates are unset pending #608. The plan itself says the natural stopping points are D5 (Beta) and D9 (core proof) and that neither may be advertised as the MVP.

### 1.3 Explicit non-goals and non-claims

| Source | Non-goal / non-claim |
| --- | --- |
| `BETA_MVP_EXECUTION_PLAN.md` gate table | Beta is "not a claim of full autonomy, arbitrary executable trust, general connector safety, shared hosting or full enterprise MVP". Core proof (D6-D9) is not a release. |
| Owner decision 2026-10-26 | Shared-customer hosting excluded; no universal tenant-column retrofit; no cross-tenant quotas; V28-shared-tenancy "explicitly excluded, not a passing or skipped test". One organization per installation, many projects and users. |
| `map-gap.md` sections 2, 16 | Do not rebuild inference servers, workflow engines, identity servers or telemetry databases. Not an all-in-one agent framework. No broad rewrites. Do not claim enterprise tenancy, hostile-code isolation, platform-wide governed autonomy, recovery across every effect path or superior scale. |
| ADR 0004 "What this is not" | Not a global switch; not a change to what any role may do; not a replacement for Aria's `AssistantApprovalMode` (it maps onto it). |
| PRD (superseded) | Non-goals were multi-tenancy, SSO/SCIM, HA, multi-region, managed hosting, untrusted tool execution. **Most of these are now in scope** (OIDC/SCIM in M17, regions in M18); only shared tenancy and managed hosting remain excluded. The non-goal list was inverted within a week. |
| Alpha scope | Alpha is a prompt journey; defaults are simulated (`fake` LLM and evaluator); no second approver on Apply; preview areas behind feature flags. |

**Observation.** The retained non-goals are thin relative to the in-scope list. There is no stated non-goal about which kinds of enterprise work the platform will not govern, or what autonomy levels it will not support. That absence matters for the vision (section 4).

---

## 2. What Beta/MVP delivers versus what is deferred

### 2.1 Staging

Source: `docs/planning/beta-mvp-execution.json` (291 tasks, 58 epics, 15 milestone controls, 15 gap rows G01-G22) and live issue state.

| Stage | Milestones | Packages | Tasks | Closed | Gate | Stated outcome |
| --- | --- | --- | ---: | ---: | --- | --- |
| Beta | D2-D5 (D1 closed earlier) | B02-B16 | 125 | 80 | #613 | One governed vertical slice: pinned admission, run-bound grants, authorized context, exact-intent approval, fenced effect, unknown-state recovery, acceptance and replay, evidence export, installable SDK/UI/CLI, upgrade from the alpha database, independent boundary security review. "Decide Beta release AND whether to fund MVP." |
| Core proof | D6-D9 | M01-M15 | 93 | 0 | #617 | Credential leases, signed bundles/canary, protected audit, approval timers and scoped stop, trajectory gates, isolation, knowledge lifecycle, feedback loop, second connector, budgets, child tasks, second use case, restore and load drills. "A separate MVP release" is explicitly not claimed. |
| Full retained MVP | D10-D15 | M16-M27 | 73 | 0 | #623 | Governance, autonomy, routing, contracts (D10); enterprise identity (D11); regions (D12); Knowledge Hub (D13); memory and A2A federation (D14); catalog, companion case and final security review (D15). |

Per-milestone counts (tasks, closed): D2 35/35, D3 50/35, D4 17/6, D5 23/4, D6 25/0, D7 29/0, D8 20/0, D9 19/0, D10 25/0, D11 9/0, D12 6/0, D13 8/0, D14 10/0, D15 15/0. All 80 closures happened 2026-10-26 to 2026-10-31 (2, 27, 17, 17, 13, 4 per day).

### 2.2 Milestone themes and epics

| Milestone | Epics (issue) | Theme |
| --- | --- | --- |
| D2 | B02 #517, B03 #518, B07 #524, B11 #530, B12 #533, B13 #536 | Immutable run manifests and pins; subject/workload/service-principal identity, run-bound grants, typed policy decisions; authorized context manifest and rule bundle; trace ownership; reference-profile refusals; single execution-owner lease with fencing, outbox/inbox. **Closed; D2 acceptance PASS with exceptions** (`docs/reports/egi/D2-acceptance.md`). |
| D3 | B04 #520, B05 #522, B06 #523/#589, B07 #525, B08 #526, B11 #531, B12 #534, B13 #537 | Protected-effect dispatcher, logical effect ledger and receipts, exact-intent approvals, bounded typed agent step. 35 of 50 closed; open work includes routing OpenAPI (#750), MCP (#759) and model-selected calls (#768) through the dispatcher, approval consumption inside the dispatcher transaction (#735) and the approval UI (#743). |
| D4 | B09 #527, B10 #528, B12 #535, B14 #538 | Deterministic acceptance, historical replay, counterfactual fork, workspace release evaluation (done), evidence package. |
| D5 | B14 #539, B15 #540, B16 #542, #594, #595 | Evidence export and verifier, synthetic downstream plus cookbook, public fault matrix, upgrade path, promote preview areas to supported, independent Beta security gate (#851). |
| D6 | M01 #543, M02 #544, M05 #547, M06 #548, M09 #551 | Credential leases and workload identity; signed bundles, canary, rollback; protected audit archive; approval timers, claim/reassign, overrides, scoped emergency stop; trajectory gates and waivers. |
| D7 | M04 #546, M07 #549, M09 #552, M10 #553/#592, M11 #555, M14 #560 | Knowledge lifecycle and tombstones; isolated worker profile; trajectory suites and drift; feedback to dataset to candidate release; hardened deployment; conformance suite. |
| D8 | M03 #545, M08 #550, M11 #556, M13 #559, M15 #562 | Second connector, partial effects and compensation; durable budgets; bounded child tasks; second use case. |
| D9 | M10 #554, M11 #557, M12 #558, M14 #561, M15 #563 | Conformance, restore, workload envelope, SLOs, pilot. Gate #617. |
| D10 | M23 #604, M24 #605, M25 #606, M27 #632 | Routing decisions and ownership transfer; GovernanceRecord and risk tiers; remaining contracts and a second channel; autonomy levels. |
| D11 | M17 #598 | OIDC, SCIM, deprovisioning fan-out within 60 s. |
| D12 | M18 #599 | Regional cells and residency. |
| D13 | M21 #602 | Knowledge Hub: governed extraction, ontology, views. |
| D14 | M19 #600, M20 #601 | A2A gateway and federated task adoption (pinned A2A v0.3.0); governed cross-run memory. |
| D15 | M22 #603, M26 #607, M16 #564 | Connector/framework catalog with certification; companion case and book traceability; independent security review and release decision. |

### 2.3 What Beta explicitly defers (with issue numbers)

- Anything autonomy-related: M27 (#632; children #663, #798, #849, #856, #862, #868, #872, #876, #880), M24 GovernanceRecord (#605; #790, #797, #804, #811). ADR 0004 "Consequences": "until M27 (D10) the platform behaves as L1 everywhere, with Aria's own mode as the only exception, and that is what the Beta documents."
- Credential leasing (M01 #543), signed promotion (M02 #544), protected audit (M05 #547), isolation (M07 #549): Beta runs on a "trusted-author" profile with refusals, not isolation.
- Persistent budgets (M08 #550); Beta has per-step reservations only (B08.4 #761).
- Child tasks and multi-agent delegation (M13 #559), second connector (M03 #545), second use case (M15 #562).
- Agent identity as a principal type (#663, D10). D2 delivered service principals (cron and service-token ingress) deliberately "not a promotion ... to an autonomous owner, reviewer or approver" (ADR 0004, Principals).
- The exact intent of "Beta" per #613: bounded real-provider evidence, independent Beta security review, restore smoke. None of these have occurred (#851, #886, #890 open).

*Caveat on lag.* #997 (B05.5d) shows open although its PR (#1052) is on main (HEAD~1); a few counts above may be off by one or two for merged-but-not-closed issues.

### 2.4 Scope growth since planning

The manifest has 291 tasks; the corrections document (2026-10-26) says 280. Eleven tasks numbered #947-#997 (for example #958 service principals, #967/#968 lineage for non-user admission, #973 knowledge reads outside admitted runs, #990/#996/#997 effect follow-ups) were discovered during D2/D3 execution. Four D2/D3 bug-class follow-ups (#641, #973, #979, #1048) are filed as "follow-up" after the parent closed. That is normal for real work but means the 122-week estimate was not stable even over five days.

---

## 3. Where the plans already address the vision's concerns, and how well

Legend for "built": **Code** = exists on main and tested; **Spec** = specified in ADR/plan only; **Absent** = no mention.

| Vision concern | Where addressed | Built? | Assessment |
| --- | --- | --- | --- |
| Progressive autonomy ladder | ADR 0004 (L0-L5); `map-gap.md` section 7 (evidence/observability/safety/memory requirements per level); EGI plan M27; manifest #798-#880 | Spec only. Aria's `AssistantApprovalMode` (`manual, auto_safe, auto_all, agent_review, full_autonomy`, `assistant/models.py:42`) is the only live autonomy dial, per-session, not per-use-case. | The most complete autonomy artifact in the repo. Strong on invariants (distinct principals, effect-class ceiling, automatic asymmetric demotion). Weak on granularity and evidence sourcing (section 4). |
| Roles | ADR 0004 four chairs (`editor`, `reviewer`, `approver`, `owner`) layered over platform scopes (`caliber.viewer/operator/approver/admin`, `auth.py:94-97`) and project roles (`owner/editor/reviewer/viewer`, `resource_access.py:20-23`) | Code for scopes and project roles. Chairs are not an object in code. | Three role vocabularies, none domain-specific. `approver` is a platform scope, not a project role (`ACTION_APPROVE` = owner and reviewer, `resource_access.py:193`), so ADR 0004's table mixes layers. The book distinguishes at least eight accountable owners (domain, risk, data, resource, model-governance, evaluation, release, risk authority; `08-part-viii.md` ch. 26 table) plus three human controls (approval, intervention, operator control; ch. 14); ADR 0004 compresses these into `owner` and `approver`. |
| Human/agent assignment | ADR 0004 "Principals": `principal_type` human or agent, behaviour pins; #663 | Absent in code. Approval records carry `decided_by` as a free string (`approval_decision.v1.json`). Distinct-actor check compares account strings (`assert_distinct_actor`, `workflows/approval_policy.py:266`). | Beta approvals are human-only by construction. `policy/principal.py` types subject (user, auth method in `session/pat/trusted_header`) and workload (digest-derived id); `ServicePrincipal` exists for cron/service-token ingress. No agent principal. |
| Evidence-based promotion | ADR 0004 inv. 5; #868 (window AND count, human owner approval, risk ceiling, signed promotion artifact) | Spec. | Specified to "like any release (M02)" so it inherits signing and rollback. Circular evidence source and fixed thresholds (section 4.3). |
| Automatic demotion | ADR 0004 inv. 6; #872 (durable incident IDs, one demotion per newly qualifying event set, block new higher-autonomy dispatch, preserve in-flight reconciliation) | Spec. | Well thought through for idempotence (the 300 s janitor repeat). Metrics are lagging (acceptance failures, rollbacks, incidents, trajectory failures). No leading indicators (reviewer disagreement, drift, human-override rate, queue age) although the book lists them (`07-part-vii.md` "Human load"). |
| Supervision | ADR 0004 (owner "audits evidence"); M06 #548 (timers, claim/reassign, override with rationale, scoped stop); UI #880 | Spec. Only one live supervision loop: Aria drafts via `agent_review` reviewer identity (`assistant/service.py` ~5490-5545). | Supervision is framed as per-effect approval plus an emergency stop scoped to project, workflow or connector (#776). No supervisor role for an *agent*, no per-principal stop, no sampling-based post-hoc review. |
| Escalation | M06.1 (#757): "extend existing B06 expiry service with escalation/intervention routing"; ADR 0004 L3 "human, within the approval SLA (M06 timers and escalation)"; #856 "creates one human InterventionTask" | Spec, and thin. The InterventionTask contract appears in the traceability table as owned by M06 but **no task in the manifest defines it**; only #856 and #908 consume it. Gap analysis M06 text says only `block` timeout is accepted today. | Escalation target, SLA, coverage and capacity are undefined. The book treats this as a core design problem (ch. 14: "human capacity is finite"; queue age, abandonment, reassignment, after-hours coverage). |
| Accountability | ADR 0004 "owner role is human at every level"; GovernanceRecord with `human owners/approvers` (#790); evidence package naming principal, type and pins (#876, B14) | Spec; evidence package v1 (#818, #826) still open. | Accountability is a field on a record, not a delegation chain. The book's `Risk tier/autonomy -> Risk owner` and `Exception -> Named risk authority` are folded into one `owner`. |
| Governance | M24 #605: GovernanceRecord per use case, T1-T4 tiers, risk-tier gate, waivers, reports; M09.1b #827 (critical gates non-waivable) | Spec. | Reasonable and book-aligned. T0 is dropped; the ceiling map T1->L5, T2->L4, T3->L3, T4->L2 (#798) is a hard-coded table. |
| Evaluation | B09 acceptance/replay (#527), B10 release evaluation (done), M09 trajectory gates (#551/#552), M10 feedback loop (#553, #592), M12 SLOs | Partial code: workspace release evaluation consumer and gate evidence exist (migrations 0128/0129). | Evaluates *releases of behaviour bundles* against frozen trajectory cases (#820 counterfactual fork). It is not designed to evaluate *production decisions of an agent holding a role*; #868 nevertheless consumes "a passing trajectory gate over that period's decisions" with no task defining that dataset. |
| Capabilities | B03 CapabilityEnvelope/grants (run-bound, subject intersect workload intersect project role); B04 ToolManifest; M02/M25 skill/pack manifests; M22 catalog and certification (D15); M14 conformance | Code for grants and tool admission (`policy/grants.py`, `effects/admission.py`); M22 absent. | Capability = tool/skill manifest + a per-run grant. No role-to-capability binding: a grant is derived from the initiating subject's project role, not from the chair an agent occupies. |
| Work decomposition | Workflow manifests and IR; approval nodes with `required_role`; use case = GovernanceRecord | Code for workflows. | The unit of governance is the whole use case/workflow. There is no "work item" or "responsibility" object between a use case and a workflow node. |
| Multi-agent coordination | M13 (#559) bounded child task: scope and budget are intersections of the parent; M23 OwnershipTransfer with acknowledgment (#832); M19 A2A | Spec, D8/D10/D14. | Child authority intersect is a good rule. Ownership transfer is "distinct from the execution-owner lease" but is not wired to human-or-agent assignment. |
| Observation / continuous monitoring | Joined evidence package (B14), M05 audit archive, M12 SLOs | Partial. | Observability of runs is strong; observability of a *role-holder's quality over time* is not a modeled object. |

### 3.1 The one existing autonomy implementation (Aria)

`AssistantApprovalMode` in `assistant/models.py:42` is the only code that lets a non-human principal hold a decision chair today. ADR 0004 maps it (`manual/auto_safe/auto_all` to L1, `agent_review` to L3, `full_autonomy` to L4/L5) and #862 will clamp it. Two facts matter:

- `config.py:1283` requires the reviewer identity to be "distinct from the author and listed in `CALIBER_APPROVER_USERS` or `CALIBER_ADMIN_USERS`". The agent is a *human-shaped account holding a scope*, with no type, no pins, no effect ceiling.
- `docs/architecture/protected-effect-paths.md` "Findings": "In the `auto_all` and `full_autonomy` Aria modes an agent can invoke write and external-action OpenAPI tools, run workflows, run draft source in the sandbox and publish drafts". So the live autonomous path is *more permissive* than the Beta's own refusal profile, and the repo documents that honestly. The risk is that Beta messaging ("L1 everywhere") is true of the governed path and false of Aria.

---

## 4. Tensions between the plan's assumptions and the vision

### 4.1 Summary

| # | Plan assumption (cite) | Vision | Severity (inference) |
| --- | --- | --- | --- |
| T1 | Autonomy is **one scalar per use case**; roles move in a fixed order editor, reviewer, approver (ADR 0004 table) | Roles at independent levels; individual responsibilities move without rebuilding the workflow | High |
| T2 | "Autonomy" = roles an agent may hold (oversight axis); the grant envelope is a separate system (B03, M08, M13) | Autonomy is one concept spanning authority, supervision and evidence | High |
| T3 | Promotion evidence comes from a period "at the current level"; no shadow or advisory mode | Observe, evaluate, validate, then approve | High |
| T4 | Autonomy above L2 requires a deterministic acceptance predicate (ADR 0004 consequences; #849) | Autonomy for qualitative work (drafting, analysis, triage) also progresses | High |
| T5 | Static tier ceilings (T1->L5 ... T4->L2, #798) and fixed thresholds (30 days, 200 decisions, #868) | Evidence-driven, per-slice, risk-adjusted trust | Medium |
| T6 | "owner" is a single human role that sets policy, grants waivers, stops, audits and approves promotion (ADR 0004) | Separation of accountable roles (outcome owner, risk owner, supervisor, evaluator) | Medium |
| T7 | Approval is a per-proposal act consumed atomically in the dispatcher transaction (B06, #735) | Exception-supervised operation: sampling, post-hoc review, batch approval | Medium |
| T8 | Human-only approvals are baked into Beta documents and contracts (PRD line 151; `approval-ux-contract.md`; `approval_decision.v1`) | Human to agent transition without redesign | Medium (cheap to fix now) |
| T9 | Role = project-role string on an account; the work-to-worker assignment is absent | Work -> Roles -> Human/Agent Assignment | High (gap, not conflict) |
| T10 | Program success = book traceability (M26), V01-V35 internal scenarios | Platform proven by enterprises moving responsibilities along the ladder | Medium |
| T11 | Competitive-analysis positioning "own the wedge, don't chase breadth" versus EGI breadth (M17-M26) | A common platform needs breadth in capabilities, but not necessarily the book's breadth | Medium |

### 4.2 Detail

**T1 granularity.** The ADR table lists role by level, but the level is a single GovernanceRecord field (`autonomy_level`), so the roles are not independently settable. Concretely inexpressible: "human editor, agent reviewer" (an AI reviewer over human work is a common first step), "agent operator/applier, human approver", "AI-assisted human validator or approver". L1 gives AI assistance only to the editor chair. The vision's own example (Developer autonomous agent; Reviewer agent plus human oversight; Validator human plus AI assist; Approver human; Operator autonomous agent) cannot be represented. The `Operator` and `Verifier` roles in the decision chain text (propose, review, approve, apply, verify) are not in the table; `apply` is the dispatcher and `verify` is deterministic acceptance code (B09), so those two chairs can never be held by a human or an agent as a principal.

**T2 two axes.** The book defines autonomy as a grant scope (principle 2) and says "evidence before autonomy" widens "those grants" (principle 8). ADR 0004 cites that principle for the oversight ladder. In the plan the grant envelope (B03/M01), budgets (M08), delegation (M13) and routing (M23) are all built without reference to the level; `map-gap.md` line "Actual authority is the intersection of use-case level, risk tier, capability grant, project/resource scope, connector capability and current policy" lists five inputs and no task computes the intersection except the dispatcher's effect-class ceiling (#856). This is an unresolved design choice, not an error: the oversight axis alone is a defensible simplification, but then "autonomy" in the vision (which includes authority) is only half modeled.

**T3 circular evidence.** ADR 0004 inv. 5 and #868 require: measured period, no tripped demotion metric, passing trajectory gate "over that period's decisions", and human owner decision. Walk the L2 to L3 case: at L2 the editor is an agent and reviewer/approver are humans. The only agent-produced artifacts are proposals; the human review decisions are the reference. For L3 the agent must show it can review. Without a shadow reviewer (agent reviews recorded but non-binding, compared with the human decision), there is no evidence about agent review quality before promotion. The same applies L3 to L4. M09 evaluates a candidate against frozen cases; it does not compare an agent role-holder against human decisions. The book itself treats shadow as a release stage (`08-part-viii.md` ch. 27, "Shadow: what would the candidate do on representative traffic without effects"); ADR 0004 does not borrow it for role promotion. Also relevant: #868's "defaults 30 days/200 decisions" mean low-volume use cases may never qualify, and 200 clean decisions only supports a failure rate below about 1.5% (rule of three, inference), weak for L4 reversible writes at scale.

**T4 acceptance-predicate cap.** ADR 0004: "a use case that cannot express an acceptance predicate cannot go above L2" and invariant 2 demands "at least one deterministic check that no model output can satisfy by text alone". The book is less strict: its Acceptance Engine "evaluates executable invariants first and calibrated judges only for criteria that cannot be computed" (`04-part-iv.md` ch. 13), and judges must be calibrated against human labels. So the plan is *more* conservative than its source. Consequence (inference): drafting, summarisation, triage, research and many support tasks have no deterministic oracle and are capped at L2 by design, which narrows the "autonomous enterprise" claim to domains with machine-checkable outcomes. That may be the right safety call, but it is a product boundary nowhere stated as a non-goal.

**T5 static policy.** The tier-to-level ceiling is a constant table; promotion thresholds are constants "configurable only by authorized policy". The plan has no mechanism for per-slice or per-effect-class trust (for example L4 for document-request creation but L2 for amendments in the same use case) beyond the L4 effect-class split; no confidence-bound or risk-weighted rule; "evidence" is the absence of tripped metrics.

**T6 owner conflation.** In code the project role `owner` is the project administrator (`ROLE_OWNER`, multi-admin, transfer-ownership; requires operator+approver scopes, `resource_access.py:220`). In ADR 0004 `owner` is the accountable human who sets policy, grants waivers, stops and audits. In the book, "risk tier/autonomy" belongs to the **risk owner** and exceptions to a **named risk authority**, distinct from the domain owner. #868 has the *owner* approving promotion, so the party operationally rewarded by more autonomy approves it; the book splits this. Terminology collision plus conflation of four duties.

**T7 supervision by exception.** The vision's progression Human-supervised to Exception-supervised has only a partial analogue: L4 routes irreversible and export effects to humans, L3 requires every approval human. There is no human-sampled post-hoc review, no confidence-gated escalation of an agent approval to a human (only effect-class gating), and no "agent approves, human audits sample" mode. M06 does build escalation timers but for expiry, not for uncertainty.

**T8 baked-in human-only assumption.** `docs/prd.md` line 151: "The release decision is human-owned. Automation, an agent, or a passing test suite cannot approve or merge a production decision." (superseded, but not retracted in that text). `approval_decision.v1.json` and `approval_request.v1.json` have only `decided_by` and `requested_by` strings; both schemas are closed (`additionalProperties: false`, ADR 0003). `approval-ux-contract.md` already specifies a view field `requested_by_type` "from the account's `principal_type` (ADR 0004)", but `principal_type` does not exist anywhere in `caliber/src` and arrives in #663 at D10, while the panel (#743) is a D3 task. That is a dependency inversion inside the plan.

**T9 assignment.** Work reaches a role-holder in three ways today: the initiating human (subject), the schedule/service token (service principal), or Aria's session. M23 "routing" means *model* route selection (`RunManifest.pins.model_routes`) and handoff ownership transfer, not deciding whether a task goes to a person or an agent. SCIM groups (M17.4b #893) arrive at D11 and their mapping to roles is not specified in the epic. Nothing models a responsibility ("document clerk", "case reviewer") as an object that a human or an agent can fill, with per-assignee history.

**T10 program goal.** M26 (#607) exists to make every book chapter point to a passing test. Success criteria are internal (V01-V35 scenarios on a synthetic mortgage case) plus a pilot gated by #608 (independent operator, domain owner, real provider budget), none of which are booked. Nothing in the plan observes an organization actually moving a responsibility up the ladder.

**T11 strategy fork.** `docs/competitive-analysis.md` (reviewed 2026-10-23): "Own the wedge, don't chase breadth", "Not a general automation tool", and lists MLflow absorbing the loop as the largest threat. The EGI plan (2026-10-24) adds A2A, memory, Hub, regions and a catalog. Both documents are live in `docs/` with no reconciliation. (The map-gap document partly reconciles by dropping shared tenancy.)

### 4.3 Where the plan is stronger than the vision requires

- Demotion is automatic and asymmetric (humans promote, system demotes), with idempotent incident watermarks (#872).
- Agent independence is not equated with model-name differences ("Different model pins alone do not prove independence", `BETA_MVP_EXECUTION_PLAN.md`), and the chain needs a deterministic check.
- Distinct principals per chair, never the same principal twice (inv. 1), and approval fallback goes to a human rather than failing (inv. 3).
- No role's permission set changes with the level ("The permission model does not change with the level; the kind of principal in each chair does"), which keeps role design stable. This is a real strength: it makes human to agent substitution a principal swap, not a workflow redesign. The gap is that nothing yet makes the chairs *addressable* (no chair assignment object).

---

## 5. Sequencing

### 5.1 What in D6-D15 is foundational to the vision

| Package | Milestone | Why foundational | Concern |
| --- | --- | --- | --- |
| M06 approval timers, claim/reassign, overrides, scoped stop (#548) | D6 | The only supervision, escalation and kill-switch machinery. | Stop scopes are project, workflow, connector (#776), not principal or role; no per-agent revoke short of account disable. InterventionTask has no defining task. |
| M09 trajectory gates (#551, #552) and M10 feedback (#553, #592) | D6-D7 | Core of evaluation and of "continuous improvement". | Built for pre-release candidate qualification, not for live decision scoring (T3). |
| M02 signed bundles, canary, rollback (#544) | D6 | Promotion as a signed, rollbackable release; ADR 0004 inv. 5 reuses it. | Level changes are releases of what object? A GovernanceRecord change is not a bundle; #868 says "record signed promotion artifact" without defining its type. |
| M01 workload identity and credential leases (#543) | D6 | An agent-held role needs its own bounded credentials. | Parallel to M27.1's "agent principal with immutable pins" (#663): two notions (workload identity, agent principal) to reconcile. |
| M13 child task (#559) and M23 ownership transfer (#604) | D8, D10 | Delegation and handoff between role-holders. | Ownership transfer is scoped to routing between execution owners, not human to agent. |
| M24 GovernanceRecord (#605) | D10 | System of record for use case, owner, tier, level. | Gated behind D9 (#790 blocked by #617). |
| M27 autonomy (#632) | D10 | The ladder. | Section 4. |
| M17 identity (#598) | D11 | Real organizational identities and deprovisioning of agent accounts. | Reasonable. |
| M22 certification (#603) | D15 | Certified capabilities (skills/adapters) are the natural unit a role may hold. | Latest possible. |

Not foundational for the autonomy vision (inference): M18 regions (#599), M19 A2A (#600), M21 Knowledge Hub (#602), M26 book traceability (#607). They are justified by "book-complete", not by the vision, and sit on the critical path of M16's release decision.

### 5.2 Candidates to pull earlier (hypotheses, with costs)

1. **Principal capacity on decision records before Beta evidence freezes.** Add a principal-type/role field to approval request/decision and the evidence package (#818) in D3-D4, ahead of M27.1. Rationale: contracts are closed and readers-first (ADR 0003); adding a required field later forces `v2` of approval and evidence schemas and "old Beta exports remain verifiable by their version" (#876). Cost: small; risk: designing a field before the model is settled.
2. **Minimal GovernanceRecord (owner, risk tier, level default L1) ahead of D10.** `map-gap.md` row G20 says "basic ownership cannot wait until D10", and section 13 says "D3 foundations; D10 enforcement", yet #790 and #663 are blocked by D9 control #617, so nothing in the manifest delivers it before D9. M02 release signing (D6) and M09 gates (D6-D7) are built first and then retrofitted by #797 (`deploy_gate.py`, `workspace_release_governance.py`).
3. **Shadow/advisory agent decisions** recorded beside human approvals from Beta onward, so D10 promotion has real evidence. Cheap on the existing approval boundary; not currently anywhere in the plan.
4. **InterventionTask contract** (owner M06, D6): it is a prerequisite for #856 and #908 and has no producing task.
5. **Per-principal revoke and stop scope** in M06.4 (#776), not only project/workflow/connector.

### 5.3 Likely mis-scoped

- **M27 at 4-6 PW for 9 tasks** includes enforcement, promotion with a signed artifact, incident ingestion, evidence schema v2 and a Governance UI. Largest unknown is evidence production, not enforcement; the estimate does not appear to include it (inference).
- **M24 at 3-6 PW** carries the governance inventory that the vision treats as a first-class layer, while M18/M19/M21 get 12-24 PW each.
- **Estimation model.** All PW figures assume human staffing at 2.25 effective person-weeks per week. Actual closures run at about 13 plan tasks/day, with D2 (planned weeks 5-12) closed in days. The plan's critical constraints (human review, external reviewers, real-provider approval, #608) are not in the estimate model.
- **M26 and V-scenario coverage as a gate** may drive effort toward traceability rows rather than toward the behaviors that would let an operator trust agents (T10).

### 5.4 Beta decisions that are hard to reverse

| Decision | Reference | Why hard to reverse |
| --- | --- | --- |
| Closed strict control-record schemas with readers-before-writers versioning | ADR 0003 | Every missing field (for example principal capacity) costs a `v2` and a dual-reader period once a tagged release carries the schema. |
| Run identity as subject plus workload (digest-derived), grant bound to run | #657, #664, #684, ADR 0002 | Agent principals must fit this triple; a second identity concept (M27.1, M01.1) would duplicate it. |
| Four effect classes (`read`, `reversible_write`, `irreversible_write`, `export`) as the vocabulary of ADR 0001, the L4 ceiling and approval requirements | ADR 0001, ADR 0004 | The book has five risk classes R0-R4 (`06-part-vi.md` "Risk classes"); the mapping is not written; `risk_class` in `approval_request` is a free string. |
| One execution-owner process holding a fenced lease; all others API-only | ADR 0002, #668, #673 | Throughput and multi-agent concurrency are bound to one process until a superseding ADR and re-fencing of all 13 loops. |
| Exact-intent approval consumed atomically in the dispatcher transaction | #735, #718 | Fits per-effect approval; sampling, batching and post-hoc review (T7) would be a new path beside it. |
| Immutable run manifest with DB triggers | #656, #680 | Excellent for evidence; implies assignment (who/what holds a chair) must be pinned or recorded elsewhere, or runs are not replayable by role. |
| Single tenant `local` required on every new record | ADR 0001/0003, owner decision | Shared tenancy would be a retrofit (explicit exclusion). Consistent with a per-customer platform. |
| SQL-owned durable execution (no Temporal) | `map-gap.md` section 13 | Switching needs an ADR and re-estimate; "running two competing workflow histories makes recovery ambiguous". |
| A2A pinned to v0.3.0 | `BETA_MVP_EXECUTION_PLAN.md` | The book at the same commit says "pin A2A 1.0.1" (`05-part-v.md` correction table); the plan states the pin is "not claimed to be the latest". Divergence from the spec it implements. |

---

## 6. Maturity assessment and claims versus code

### 6.1 Honest status

- **Alpha 0.1.0a1** is a published prerelease (2026-10-23, `gh release list`), not on PyPI, core journey = prompt refinement, simulated providers by default. The audit that preceded it (`docs/caliber-alpha-gaps.md`) found seven P0 blockers at commit `dfe48481471`, days before release: cross-project mutation of live prompts (P0-1), Apply reporting success with a fake promoter (P0-3), canned 0.94 vs 0.88 evaluation scores persisted as evidence (P0-4), no working install path (P0-5). These were fixed, but they show the baseline was not trustworthy a week ago.
- **Repository.** First commit 2026-06-26; 890 commits; 253 since 2026-10-24; 99 on 2026-10-28 alone. Merged PRs per day 2026-10-26 to 2026-10-31: 22, 27, 28, 20, 13, 9.
- **Beta.** D2 closed with "PASS for engineering acceptance, with scope exceptions" (D2 report). Candidly stated limits: no human or external review; merged by the implementing agent after a "fresh independent agent" review; no reforecast; one push-CI run on main failed once and needed fixes for a slow-runner test (#979), a pytest-asyncio incompatibility (#981) and a log-handler test (#982). Coverage reported: 99.43% of added lines, 95.73% line and 88.56% branch overall; backend 10,727 passed. Only 12 of the Beta V-scenarios exist as test files (V00, V02-V10, V13, V14); V01, V11, V12 remain (B15.2 #857, B09). D3 has 15 open tasks including every model-selected and MCP/OpenAPI routing task (#750, #759, #768), so "the model proposes, the system decides" is not yet true of agent-chosen tool calls.
- **External dependencies untouched.** #608 (named staffing, independent security reviewer, domain owner, pilot operator, independent adapter author, provider budget, calendar) is open. D1 and D5/D15 gates require people the plan itself calls "external dependencies" whose "unavailability blocks the relevant gate, it does not pass it".
- **Governance of the program itself.** The plan says "Only the owner merges PRs" (`BETA_MVP_EXECUTION_PLAN.md`, `CALIBER_EGI_DELIVERY_PLAN.md` workflow step 7). The repo `CLAUDE.md` authorizes agent self-merge after validation, and D2 was merged that way. The program therefore operates, informally, at something like "agent editor, agent reviewer, agent merger, human owner after the fact" for reversible code changes, with CI as the deterministic check, without the measured promotion ADR 0004 requires of its customers. Treat as an anecdote about agent-review reliability (inference): follow-ups after closure (#641, #973 knowledge-read bypass found after B07 closed, #978/#979) suggest the independent-agent review missed integration-level defects that surfaced later.
- **Documentation sprawl.** Superseded or partly stale documents remain in the main docs tree: `prd.md`, `roadmap.md`, `CALIBER_FIVE_PERSON_DELIVERY_PLAN.md`, `two-week-alpha-plan.md`, `mvp-v1-architecture-and-backlog-review.md`, the gap analysis (G01-G12 versus map-gap G01-G22, two numbering schemes) and `architecture/index.md` (reviewed 2026-08-10, no ADR links). The plan flags precedence rules, which helps, but a newcomer has at least three overlapping plans.

### 6.2 Spot-checks of documentation claims against code

| # | Claim | Where | Check | Result |
| --- | --- | --- | --- | --- |
| 1 | "The dispatcher raises `unsupported_in_reference_profile` (#520)" for `refuse` inventory rows | ADR 0001 | `effects/dispatcher.py:494` raises `UnsupportedEffectPath("unsupported_effect_path")`; the string `unsupported_in_reference_profile` appears only in docs and one test file. `docs/operate/reference-profile.md` says the refusal arrives with B04.7 (#774, open). | **Overstated in the ADR** (present tense for pending work; different error code). Operate doc is accurate. |
| 2 | "Nothing records at decision time whether a role-holder was a person or an agent" | ADR 0004 Context | `grep principal_type caliber/src` returns nothing; `approval_decision.v1.json` has `decided_by` only. | **Confirmed.** |
| 3 | "The vocabulary applies from D2 so earlier packages use it" | ADR 0004 Status | No code or schema in D2/D3 uses level or principal type; `approval-ux-contract.md` assumes `principal_type` (D10). | **Not backed**; inverted dependency (T8). |
| 4 | Single-owner lease, fencing, 13 loops incl. `EffectReconcilerTask` | ADR 0002 | `ownership.py`, `execution_role.py`, `orchestrator/effect_reconciler.py` exist; worker discovery is by AST (`worker_inventory.py`). | **Backed.** |
| 5 | "No code enforces one active CALIBER process" | PRD implementation table | `ownership.py` (D2, #668) now exists. | **Stale** (PRD is marked superseded). |
| 6 | "The prepared Alpha tag/assets remain unpublished" | Gap analysis section 1 | `gh release list` shows `caliber-v0.1.0a1` published 2026-10-23. | **Stale.** |
| 7 | Beta V-scenarios exist for D2 (V02, V03, V06, V09, V10, V14) | D2 acceptance | Files present under `caliber/tests/egi_scenarios/` (197 test functions; the report says 237 collected, plausibly with parametrization). | **Backed** (not re-run). |
| 8 | Aria independent reviewer "must be distinct and approver-scoped" | ADR 0004 Context | `config.py:1283-1289` description and `assistant/service.py` reviewer checks. | **Backed.** |
| 9 | "Planned: `autonomy.py`, `governance.py`, `principals.py`" | #663, #790, #798 | None exist. | **Consistent** with a D10 plan; ADR states this. |
| 10 | 280 tasks | `EXECUTION_CONTRACT_CORRECTIONS.md` | Manifest has 291. | Scope grew by 11 in days. |
| 11 | "Only the owner merges PRs" | Plan | D2 report and repo `CLAUDE.md` describe agent self-merge. | **Contradiction inside the repo's own governance docs.** |

I did not re-run tests, coverage or CI, so numbers from the D2 report and gap analysis are reported, not verified.

### 6.3 Net maturity judgment

A strong, unusually well documented *design discipline* (ADRs, strict schemas, negative-test oracles, inventory-driven refusal, honest limitations) over a *young execution layer* (Beta slice 64% by task count, many essential paths still ungoverned: model-selected tool calls, MCP, OpenAPI). The autonomy model is a reasoned ADR with a task chain, zero code, and no evidence yet about how any piece behaves. The program's own claims are generally careful about "not implemented", but present-tense phrasing in ADRs leaks (check 1) and stale superseded documents remain discoverable (checks 5, 6).

---

## 7. What this implies for the vision (brief, inference)

- ADR 0004 is a valid first cut at *oversight autonomy per use case*. It is not yet a model of *Work -> Roles -> Assignment -> Capabilities*, because chairs are not objects, assignment is not modeled, capabilities are not bound to chairs, and the level is a scalar.
- The cheapest high-leverage moves are **contract-level** and fall in the Beta window: capacity/principal fields on approval and evidence records, a chair/assignment concept in the GovernanceRecord design before M24.1, and a shadow-decision record so evidence exists when M27 needs it. Everything else in the autonomy chain can legitimately wait for D10.
- The program's hardest assumption for the vision is the **acceptance-predicate requirement** (T4) and the **per-effect human approval** model (T7). They bound where the platform can credibly claim autonomy.
- A reframing worth testing against alternatives (not asserted): treat the ladder's unit as *(responsibility x effect class x risk tier)* with the use case providing defaults, and treat the level as derived evidence state plus policy cap rather than a configured scalar.

---

## 8. What I could not verify

- Whether #997's PR (#1052) state and other recently merged tasks are correctly reflected in issue state (lag).
- Test counts, coverage and CI results (not re-run).
- The content of GitHub issue bodies beyond the epics and a handful of children (M27, M06, M24 read; the other 280 children read only via the manifest summary).
- Whether M17 SCIM group-to-role mapping is specified in a child issue (I did not read #893).
- The book's chapters 11 (routing, ownership transfer) and 18 (coordination) in full, so the claim that M23 ownership transfer is not human-or-agent assignment rests on the plan text only.
- Whether any external reviewer or pilot operator has actually been approached (#608 has no recorded bookings).
- Competitive analysis claims about third-party products (not in scope to verify).
