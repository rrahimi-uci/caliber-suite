# CALIBER EGI delivery plan: three people, book-complete MVP

> **Controlling delivery plan as of 2026-10-24.** It supersedes the
> [five-person D1–D9 plan](CALIBER_FIVE_PERSON_DELIVERY_PLAN.md) for staffing, scope and calendar, and the monthly
> M2–M6 train in the [roadmap](roadmap.md) as the definition of the MVP. Package definitions and acceptance tests
> remain in the [reference architecture gap analysis](CALIBER_REFERENCE_ARCHITECTURE_GAP_ANALYSIS.md). It is a plan,
> not a capability claim: nothing below is implemented by this document.

## Decisions this plan records

The repository owner decided on 2026-10-24:

1. **The EGI program is the MVP.** The reference-architecture program (packages B01–B16, M01–M16) replaces the
   roadmap's narrower `caliber-v1.0.0` single-tenant prompt-refinement release as the MVP definition. The roadmap's
   monthly M2–M6 milestones are retired; its history and the shipped `0.1.0a1` alpha remain valid.
2. **Three full-time people.** Five working days per week each; not five people and not 1.5 developers.
3. **CALIBER is the reference implementation of *Building Enterprise General Intelligence*.** The MVP therefore
   includes the breadth the gap analysis had deferred (A2A federation, governed memory, the Knowledge Hub with
   automated extraction, multi-region execution, multi-tenancy with organizational administration, and a
   connector/framework ecosystem), plus the book contracts no package covered yet. These become packages M17–M26.
4. **Autonomy is a per-use-case ladder of agent-held roles** (added 2026-10-25). Roles stay fixed; which of them
   an agent may hold for a use case is a governed level, L0–L5, with the owner role always human. This is package
   M27 and [ADR 0004](architecture/adr/0004-autonomy-levels.md).

## Capacity

The availability model is unchanged from the five-person plan; only the head count changes.

| Weekly allocation | Person-weeks | Treatment |
| --- | ---: | --- |
| Gross staffing | 3.00 | Three people |
| Leave, organizational overhead and unrelated support | 0.75 | 25% unavailable; replace with measured availability |
| Effective program capacity | **2.25** | Planned delivery plus uncertainty |
| Maximum planned base work | **1.80** | Design, implementation, tests, review, docs and integration |
| Discovery/rework allowance at maximum base load | **0.45** | 25% of base work; not additional features |

A milestone lasts `ceil(base PW ÷ 1.8)` weeks (minimum four), and its **last week is the dedicated QA, fixes and
acceptance week**. The five-week cadence of the five-person plan no longer applies: at 1.8 base PW per week a
five-week milestone holds 9 PW, and keeping the existing D1–D9 package slices intact is cheaper and safer than
re-slicing 57 tracked issues.

| Stage | Packages | Base range, PW | Base midpoint, PW | Midpoint plus 25%, PW | Planned calendar |
| --- | --- | ---: | ---: | ---: | ---: |
| Beta | B01–B16 | 37–61 | 49 | 61.25 | **29 weeks** |
| Core platform (formerly the MVP) | M01–M15 | 40–67 | 53.5 | 66.9 | **31 weeks** |
| Book-complete increment | M17–M27, M16 | 78–139 | 108.5 | 135.6 | **62 weeks** |
| **Book-complete MVP** | all | **155–267** | **211** | **263.75** | **122 weeks** |

Calendar arithmetic: at the midpoint the work needs `263.75 ÷ 2.25 = 117.2` effective weeks; rounding each milestone
up to whole weeks gives 122, so about 5 weeks of headroom sit inside the calendar. At the **upper** estimate the
program needs `267 × 1.25 ÷ 2.25 = 148` weeks, roughly six months later. At the lower estimate it needs about 86
weeks. Ranges for M22–M26 are this plan's own estimates; the gap analysis gave M17–M21 order-of-magnitude
allowances marked low-confidence. Treat 122 weeks as the midpoint baseline and reforecast at every gate.

**Estimate changes from the 2026-10-24 execution review** (evidence in the issues): M17 narrowed to 15–20 PW
(midpoint 17.5) because only 3 of 110 database models carry `tenant_id` today; M25 to 6–8 (7) for 23 event-publishing
call sites; M26 to 4–8 (6) because the book's companion code implements checkpoints C01–C03 only; B16's D1 slice
raised from 0.5 to 1 PW (scenario harness, fault barriers and a required CI job), taken from its D5 slice.

**Added 2026-10-25:** package M27, autonomy levels (4–6 PW, midpoint 5), in D10. It defines which roles an agent
principal may hold per use case and enforces it; see [ADR 0004](architecture/adr/0004-autonomy-levels.md).

## Calendar

**Assumed start: Monday 2 November 2026.** Due dates are the Friday of each milestone's QA week and move with the
start date. Holidays are inside the 25% unavailability.

| ID | Weeks | QA week | Due | Base PW | Outcome |
| --- | --- | ---: | --- | ---: | --- |
| D1 | 1–4 | 4 | 2026-11-27 | 6.5 | Fixed reference scope, early exposure controls, disposable action/recovery prototype |
| D2 | 5–12 | 12 | 2027-01-22 | 13 | Pinned admission, execution authority, owner fencing and authorized context manifest |
| D3 | 13–20 | 20 | 2027-03-19 | 14 | One governed model-proposed external action, exact approval and restart-safe reconciliation |
| D4 | 21–25 | 25 | 2027-04-23 | 8.5 | Verified outcome, side-effect-free replay and actual workspace evaluation |
| D5 | 26–29 | 29 | **2027-05-21** | 7 | **Beta**: installable, documented, with public failure scenarios |
| D6 | 30–38 | 38 | 2027-07-23 | 15 | Credential leases, signed promotion, protected audit and operator controls |
| D7 | 39–46 | 46 | 2027-09-17 | 14 | Knowledge lifecycle, execution isolation and trajectory-based release checks |
| D8 | 47–54 | 54 | 2027-11-12 | 14 | Second connector recovery, persistent budgets and bounded delegation |
| D9 | 55–60 | 60 | **2027-12-24** | 10.5 | **Core platform proof**: independent install/restore/pilot and measured limits (no release decision) |
| D10 | 61–73 | 73 | 2028-03-24 | 22.5 | Governance inventory, autonomy levels, routing and ownership transfer, remaining platform contracts |
| D11 | 74–83 | 83 | 2028-06-02 | 17.5 | Multi-tenant isolation and organizational administration |
| D12 | 84–93 | 93 | 2028-08-11 | 18 | Regional cells, residency and region-loss recovery |
| D13 | 94–103 | 103 | 2028-10-20 | 18 | Knowledge Hub: governed extraction, ontology and Hub views |
| D14 | 104–112 | 112 | 2028-12-22 | 15 | Governed memory and A2A federation |
| D15 | 113–122 | 122 | **2029-03-02** | 17.5 | **Book-complete MVP**: ecosystem, companion case, security review and release decision |

D1–D4 remain engineering previews, D6–D9 core-platform previews and D10–D14 MVP previews. No milestone inherits a
Beta or MVP label because its week arrives.

## Team

The five responsibility roles of the five-person plan (A–E) stay as hats so the tracked issues keep their `Role`
field. Three people wear them:

| Person | Hats | Primary responsibility |
| --- | --- | --- |
| P1 — technical lead / backend | A, B | Contracts, admission, policy, dispatcher, effect ledger, recovery, routing, federation |
| P2 — platform / security | C | Execution ownership, identity, secrets, isolation, deployment, tenancy, regions |
| P3 — full stack / test and reliability | D, E | Approvals and operator UI, SDK/CLI, docs and cookbooks, fault harness, CI and release evidence |

Run at most two substantial implementation streams; the third person integrates, reviews and tests. **P2 is the
bottleneck**: the five-person plan already found platform/security work exceeding role C in D6–D9, and M17 and M18
add more. Before D6, move the backend halves of M02, M05 and M08 to P1 and the load/outage drills of M12 to P3, as
the five-person plan proposed for B and E. Before D11, confirm the same split for M17/M18 or reforecast.

A domain owner, an independent pilot operator, a security reviewer and an **adapter author outside the team** (M22
needs two independently conformant adapters) are **external dependencies**. Book them in D1 task 0 (#608) and
again before D9 and D15; their unavailability blocks the relevant gate, it does not pass it.

## Milestone contracts

**D1–D8** keep the contracts, ordering, exit evidence and demos of the
[five-person plan](CALIBER_FIVE_PERSON_DELIVERY_PLAN.md#detailed-milestone-contracts); only their length and the
person wearing each lead hat change. Two additions apply from D1:

- **B01's ADR fixes the identifiers later packages need**: tenant, region and residency fields on run, event,
  artifact and binding records, and the CloudEvent envelope (`tenantid`, `correlationid`, `causationid`,
  `traceparent`). Single-tenant, single-region values are fine until D11/D12; adding the columns then would rewrite
  every admitted run. This stays inside B01's 1–2 PW range.
- **The Beta scenario is the book's companion case.** B15's synthetic document-request fixture uses the
  Harborlight Mortgage anchor case `MORT-2026-000184` and its C01–C03 data from the book's companion code
  (Appendix H), so the Beta already exercises the book's own example.
- **D1 is task-sized now.** Task 0 readiness (#608) first; then the downstream service and harness (#568) by week 2,
  the two disposable prototypes (#625, #626), the connector experiment (#567) and the re-estimate (#572); the
  protected-effect inventory (#627) before the ADRs (#565) and schemas (#566); the trace deny fix (#569), unsafe-profile
  refusal (#570) and approval UX contract (#571); and the scenario harness and CI job (#541). The control issue #609
  holds the order.

**Allocation changes from the 2026-10-24 review.** M09's waiver record moves into D6 (#551) because M02 and M24
build on it; trajectory slices, drift and the pilot protocol finish in D7 (#552). M14 task 1 splits: Beta contracts in
D7 (#560), second connector and child task in D9 (#561). The trace-to-refinement loop (#592, about 0.6 PW) sits under
M10 in D7 and draws on that milestone's reserve.

**D9 — Core platform proof (weeks 55–60).** As in the five-person plan minus M16: finish M14 conformance, M10
feedback/candidate release, M11 independent install/upgrade/restore, M12 workload/SLO/recovery drills and the M15
second-use-case pilot. **Exit evidence:** M01–M15 and V01–V24 pass with retained artifacts. **Decision:** none on
release; the independent security review and the MVP decision move to D15, because D11–D12 change the trust and
deployment boundary the review would have certified.

**D10 — Governance, autonomy, routing and contract completeness (weeks 61–73).** M24 governance inventory and
exception service; M27 autonomy levels, after M24's GovernanceRecord exists; M23 routing decisions, ownership
transfer and model economics; M25 remaining platform contracts and a second experience channel. **Exit evidence:**
V25–V27 and V35. **Demo:** a risk-tier change blocks a release until a scoped, expiring waiver is approved; a use
case at L3 refuses an agent approval and routes it to a human, is promoted to L4 on evidence, and is demoted
automatically when an agent-approved apply fails acceptance; a route falls back with a recorded RoutingDecision; a
handoff transfers ownership only after acknowledgment.

**D11 — Multi-tenancy and organizational administration (weeks 74–83).** M17. **Exit evidence:** V28; zero
cross-tenant leakage across runs, traces, knowledge, evaluations, audit and exports. **Decision:** D12 does not start
while any cross-tenant finding is open.

**D12 — Regions and residency (weeks 84–93).** M18. **Exit evidence:** V29; a region-loss drill meets the agreed
RPO/RTO without moving resident data. **Demo:** promote the same signed release to two regional cells and lose one.

**D13 — Knowledge Hub (weeks 94–103).** M21. **Exit evidence:** V30; an extracted rule cannot enforce anything until
its per-field verdicts pass and a human approves the KnowledgeRelease.

**D14 — Memory and federation (weeks 104–112).** M20 then M19. **Exit evidence:** V31–V32; untrusted provenance
never becomes durable memory, and a remote peer can neither widen authority nor have an artifact adopted without
local checks.

**D15 — Ecosystem, companion case and book-complete MVP release (weeks 113–122).** M22 catalog and certification;
M26 companion case and book traceability; M16 complete safety/migration matrix, independent security review over the
final trust boundary, rebuilt artifacts and the release decision. **Exit evidence:** **all B01–B16, M01–M27 and
V01–V35 pass**, the [traceability tables](#book-traceability) below have no uncovered row, and the gap analysis's MVP
definition of done holds. **Decision:** release within the measured envelope, or remain a release candidate with
explicit blockers.

## New packages M17–M27

Base PW ranges for M18–M21 come from the gap analysis's deferred backlog (M17 narrowed on code evidence); M22–M27 are this plan's estimates. The issue for each package (#598–#607, #632) holds the executable task table. Each
package splits into 1–2 day child issues when its milestone starts.

| Package | Book source | Concrete slices | Depends on | Acceptance | PW (mid) | Milestone |
| --- | --- | --- | --- | --- | ---: | --- |
| M17 Multi-tenant isolation and organizational administration | Ch. 4, 21, 26; App. G | 1. Tenant as first-class scope in admission and the tenant router. 2. Per-tenant data and key separation, enforced in queries. 3. OIDC SSO and SCIM provisioning (the book requires identities from the enterprise IdP; SAML is out of scope). 4. Per-tenant quotas and budgets. 5. Cross-tenant leakage suite. | B03, B11, M01, M05, M08 | V28: no cross-tenant read, write, enumeration or export; a deprovisioned user loses access within the stated bound | 15–20 (17.5) | D11 |
| M18 Multi-region execution and residency cells | Ch. 4, 15, 27 | 1. Global definition plane vs regional cells. 2. Home region/shard on WorkflowExecution. 3. Residency enforcement for data, knowledge and artifacts. 4. Per-region Environment/KnowledgeBinding and promotion. 5. Region-loss drill with RPO/RTO. | B13, M02, M03, M04, M11, M12, M17 | V29: resident data never leaves its region; region loss recovers within target; promotion is per cell | 12–24 (18) | D12 |
| M19 A2A gateway and federated task adoption | Ch. 19; App. G phase 4 | 1. A2A gateway/Task Bridge. 2. FederatedTask state mapping, deadlines and budgets. 3. Signed task envelope and peer trust. 4. Authority intersection with the remote peer. 5. Local artifact adoption checks and federated trace links. 6. Cancellation. | M01, M13, B14 | V32: a peer cannot widen scope, duplicate an effect or have an artifact adopted without passing local checks | 6–12 (9) | D14 |
| M20 Governed cross-run memory | Ch. 6, 17 | 1. MemoryRecord with tenant, scope, consent, purpose allowlist, TTL and provenance. 2. Writes as gated proposals. 3. Corrections and supersession. 4. Deletion propagation into derived context. | B07, M04, M05, M17 | V31: untrusted provenance cannot become durable memory; deleted memory cannot resurface | 4–8 (6) | D14 |
| M21 Knowledge Hub: governed extraction, ontology and Hub views | Ch. 5 | 1. KnowledgeArtifact with extractor and verifier versions and per-field verdicts. 2. Corroboration and review routes. 3. Versioned ontology and tenant extensions. 4. Hub views. 5. Platform-declared blocking fields. Task 1 is an ADR deciding whether to reuse the author's `policy-logic-forge` extractor (the book's Chapter 5 reference) or rebuild; reuse moves the estimate toward 12 PW. | M04, M06, M17 | V30: an extracted rule cannot enforce until verified and released by a human; a superseded rule stops enforcing | 12–24 (18) | D13 |
| M22 Connector and framework catalog with certification | Ch. 3, 7, 9; App. D | 1. Catalog of connectors and framework adapters. 2. Certification by the M14 conformance suite and published CompatibilityReports. 3. Trust tiers and revocation. 4. Two independently conformant adapters. | M02, M14 | V33: an uncertified or revoked adapter cannot be admitted; two certified adapters pass identical safety scenarios | 6–12 (9) | D15 |
| M23 Routing, ownership transfer and model economics | Ch. 11, 18, 28 | 1. RoutingDecision from a route service and the LLM gateway. 2. OwnershipTransfer with acknowledgment states. 3. Release-pinned model eligibility and fallback. 4. Cost-aware routing and model/tool substitution behind release gates. | B02, B08, M02, M08, M09, M13 | V26: every route records candidates, exclusions and fallback; ownership never transfers without acceptance; substitution needs a gate | 4–8 (6) | D10 |
| M24 Governance inventory, risk tiers and exceptions | Ch. 26 | 1. GovernanceRecord per use case (owners, risk tier, autonomy, data classes, effects, controls, review date). 2. ExceptionWaiver service with scope, compensating controls and expiry. 3. Release gates read the risk tier. 4. Governance reporting. | M02, M09 | V25: an expired waiver or overdue review blocks release; reports reconcile with the inventory | 3–6 (4.5) | D10 |
| M25 Remaining platform contracts and a second channel | Ch. 4, 8, 9, 12, 14 | 1. Artifact contract. 2. CloudEvent envelope with tenant/correlation/causation and `traceparent`. 3. Plan IR eligibility/effect checks with `capability_envelope_id` and `risk_class`. 4. SkillPackManifest and CompatibilityReport. 5. NotificationDelivery and one more channel through the same admission contract. | B02, B13, M02 | V27: every Appendix C contract has one owner and a contract test; the second channel cannot bypass admission | 6–8 (7) | D10 |
| M26 Companion case and book traceability | App. H; Ch. 16–17 | 1. Run the companion code's implemented checkpoints C01–C03 of `MORT-2026-000184` on CALIBER and map C04–C28 to V-scenarios. 2. Publish the chapter/contract traceability with a test per row. 3. Regression suite mirroring every Chapter 17 antipattern correction (AP-01–AP-16). 4. Cross-link the book repository to CALIBER. | M15, M22, M25; every row's package | V34: the companion case passes through the reference contracts; every traceability row points at a passing test | 4–8 (6) | D15 |
| M27 Autonomy levels | Ch. 1, 13, 14, 26; [ADR 0004](architecture/adr/0004-autonomy-levels.md) | 1. `principal_type` (human/agent) on accounts with the agent's behaviour pins. 2. Level vocabulary L0–L5 and `autonomy_level`/`max_autonomy_level`/demotion thresholds on the GovernanceRecord. 3. Enforcement at the approval boundary: distinct principals, agent independence, effect-class ceiling, tier bound. 4. Promotion as a governed release; automatic demotion on tripped metrics. 5. Principal and pins on every decision in evidence and UI; Aria's approval mode reads the use case's level. | B03, B06, B08, B09, M02, M06, M09, M24 | V35: an agent approval above the level's ceiling is refused and routed to a human; promotion without the measured period is refused; a same-principal chain is refused; a failed agent-approved apply demotes the use case with evidence | 4–6 (5) | D10 |

Validation scenarios V25–V35 for these packages are in the gap analysis's
[scenario matrix](CALIBER_REFERENCE_ARCHITECTURE_GAP_ANALYSIS.md#scenario-matrix).

## Book traceability

These tables are the completeness test for "reference implementation of the book". D15 does not pass while a row
lacks a delivered package and a passing test.

### Appendix C contracts

| Contract | Chapter | Packages |
| --- | --- | --- |
| RunManifest | 4 | B02 |
| Artifact | 4 | M25, B14 |
| Event (CloudEvent envelope) | 4 | B13, M25 |
| AgentStepRequest / AgentStepResult | 3 | B08 |
| KnowledgeArtifact | 5 | M21 |
| KnowledgeRelease | 5 | B07, M04 |
| KnowledgeBinding | 5, 27 | M04, M18 |
| ContextManifest | 6 | B07 |
| MemoryRecord | 6 | M20 |
| Skill manifest | 8 | M02, M25 |
| SkillPackManifest | 9 | M25, M22 |
| RoutingDecision | 11, 28 | M23 |
| OwnershipTransfer | 11, 18 | M23 |
| Plan IR (CandidatePlan, PlanNode) | 12 | M25 |
| AcceptanceDecision | 13 | B09 |
| ApprovalRequest / ApprovalDecision | 14 | B06 |
| InterventionTask | 14 | M06 |
| WorkflowExecution | 15 | B13, M18 |
| TaskEnvelope | 18 | M13 |
| FederatedTask | 19 | M19 |
| CapabilityEnvelope | 21 | B03 |
| PolicyDecision | 21 | B03 |
| CredentialLease | 21 | M01 |
| ToolManifest | 22 | B04, M14 |
| EffectRecord / EffectReceipt | 22 | B05, M03 |
| OutboxRecord / InboxReceipt | 22 | B13 |
| EvaluationResult / ValidationReport / ReleaseGateDecision | 25 | B10, M09 |
| GovernanceRecord / ExceptionWaiver | 26 | M24, M27 |
| ChangeBundle / ReleaseManifest | 27 | M02 |
| EnvironmentBinding / PromotionDecision | 27 | M02, M18 |
| BudgetReservation | 28 | B08, M08 |

### Chapters

| Chapter | Packages |
| --- | --- |
| 1 What turns an LLM call into a production system | B01, B04–B06 |
| 2 From model output to safe action | B04, B05, B08 |
| 3 Keep the agent framework in its lane | B08, M14 |
| 4 Design the architecture around the model | B02, B13, M25 |
| 5 Give knowledge an owner | B07, M04, M21 |
| 6 Build context without losing the plot | B07, M20 |
| 7 Build a paved road for agent teams | B15, M14, M22 |
| 8 Package capabilities as versioned Skills | M02, M25 |
| 9 Turn Skills into a coherent domain platform | M22, M25 |
| 10 Use ReAct without creating a runaway loop | B08, M08 |
| 11 Route the work without losing ownership | M23 |
| 12 Turn proposed plans into safe, executable work | M25 |
| 13 Use critique to improve, never to self-approve | B09, M09, M27 |
| 14 Put people where their judgment matters | B06, M06, M25, M27 |
| 15 Keep long-running work alive through failure | B09, B13, M18 |
| 16 Choose patterns that fit real enterprise work | M26 |
| 17 Architecture patterns that fail in production | B16, M26 |
| 18 Coordinate specialists without losing control | M13, M23 |
| 19 MCP, A2A and local control | B04, M19 |
| 20 Security starts outside the prompt | B12, M07 |
| 21 Carry identity, narrow authority | B03, M01, M17 |
| 22 Make external effects safe to retry | B04, B05, B13, M03 |
| 23 Contain the damage when a model is manipulated | M06, M07 |
| 24 Follow a run from request to verified outcome | B11, B14, M05 |
| 25 Improve the system without handing it control | B10, M09, M10 |
| 26 Make governance part of how the platform runs | M24, M27 |
| 27 Release new behavior without breaking work in flight | M02, M11, M18 |
| 28 Treat performance as a business decision | M08, M12, M23 |
| Appendix H Companion implementation (Harborlight Mortgage) | B15, M26 |

The book's chapter list is from `book/manuscript.md` in the
[book repository](https://github.com/rrahimi-uci/building-enterprise-general-intelligence) as of its commit
`dacedba`. Recheck both tables when the book changes.

## Execution structure

Every level has one home, so a developer can walk from the strategy to the test that proves a task.

| Level | Where it lives | What it must state |
| --- | --- | --- |
| Strategy | [Decisions this plan records](#decisions-this-plan-records) | Why the program exists and its boundary |
| Roadmap | [Calendar](#calendar) | Order, dates and stage gates |
| Milestone | GitHub milestone D1–D15 plus its **milestone control issue** (#609–#623) | Work order, kickoff checklist, QA-week checklist, exit evidence, demo, decision |
| Epic | **Package issue** (one per package part, e.g. #519 and #520 for B04) | Goal, why, tasks of this part, blocked-by/provides-to issues, code seams, out of scope, acceptance, validation commands, deliverables |
| Task | A row of the package issue's task table | The exact change, its output artifact, done-when and test |
| Subtask | 1–2 day **child issue** (sub-issue of the package issue), created at the milestone kickoff | Files, test, done-when; D1's are already created (#565–#572, #608, #625–#627) |
| Deliverable | The "Deliverables at close" list of each issue | Modules, migrations, tests, docs, ADRs, reports that must exist |
| Acceptance | The issue's acceptance checklist | Observable criteria with a denial case where applicable, and the V-scenario IDs |
| Validation | The issue's validation commands; V01–V34 in [gap analysis §13](CALIBER_REFERENCE_ARCHITECTURE_GAP_ANALYSIS.md#13-end-to-end-validation-strategy); `docs/reports/egi/Dk-acceptance.md` | Exact commands, the CI job that runs them, and the retained evidence |

**Definition of ready (a child issue may start)**: its parent's blocked-by issues are closed or the needed slice is
merged; it names the files to change, the test to add and the done-when; it is at most two days.

**Definition of done (a package issue may close)**: every task row's done-when is met; its acceptance checklist is
ticked with links to the passing CI run; changed behavioral code has ≥95% line coverage under the repository's
pytest-cov run; its deliverables exist on `main`; docs it lists are updated and regenerated.

**Priority.** Inside a milestone, follow the control issue's work order. Across milestones, a later milestone's
work starts only after its blocked-by slices are merged; idle capacity goes to the current milestone's tests,
reviews and the next milestone's kickoff refinement, never to new features.

### Shared conventions

All issues use these names, so packages that build on each other agree.

- ADRs: `docs/architecture/adr/NNNN-<slug>.md`. B01 creates 0001 (reference profile), 0002 (execution ownership)
  and 0003 (control-record schemas: proposal, receipt, decision, CloudEvent envelope, tenant/region/residency).
- Versioned schemas: `caliber/src/caliber/contracts/` with JSON schemas under `schemas/`; fixtures under
  `caliber/tests/fixtures/contracts/`.
- Scenario tests V01–V34: `caliber/tests/egi_scenarios/test_vNN_<slug>.py`, run by the CI job
  **EGI scenarios (PostgreSQL)** that #541 adds, with the same pgvector/pg16 service and `CALIBER_TEST_POSTGRES_URL`
  as the existing Migration parity job.
- Disposable downstream system of record: `caliber/tests/egi_scenarios/downstream/`, a deterministic HTTP service
  with stable-key idempotency, query by key, expected-version preconditions and an independent mutation counter.
  #568 builds it in D1; every effect test reuses it.
- Reference profile: `CALIBER_PROFILE=reference`, read in `caliber/src/caliber/config.py`.
- Milestone evidence: `docs/reports/egi/Dk-acceptance.md`, written in the QA week.

### Overlap resolutions

Where two packages touch the same mechanism, one builds it and the other extends it:

| Mechanism | Builds it | Extends it | Rule |
| --- | --- | --- | --- |
| Waiver record (scope, compensating control, approver, expiry) | M09, for release/evaluation gates | M24, for every other control, plus the governance inventory | One waiver store |
| BudgetReservation (tokens, spend, concurrency) and retry ownership | M08 | M17 adds the tenant dimension; M23 reads it for cost-aware routing | One reservation ledger |
| Eligible model routes | B02 pins them in the RunManifest | M23 records RoutingDecision and falls back within the pinned list | No second pinning mechanism |
| CloudEvent envelope | B01 defines it (ADR 0003); B13 emits it for new outbox events | M25 converts every remaining producer and adds the contract test | One envelope |
| Tenant and region identifiers | B01 defines them; B02 stores constant defaults on every run | M17 and M18 enforce them | No backfill migration later |
| Companion case | B15 builds the Beta fixture from the book's anchor case `MORT-2026-000184` (C01–C03 data) | M26 runs the full Appendix H case and the traceability tests | One fixture lineage |
| Distinct-approver check | B06, for runtime effect approvals | #589 reuses it for prompt/job Apply | One eligibility function |
| Trace authorization | B11, project scope | M17 adds tenant scope | One check |
| Upgrade testing | #594 (0.1.0a1 → Beta database) | M11 (general upgrade preflight and backup automation) | One migration test harness |
| Autonomy (who may hold a role) | M24 stores `autonomy_level` on the GovernanceRecord; B06 owns the distinct-approver check | M27 enforces the level table and promotion/demotion; Aria's `AssistantApprovalMode` reads the level | One level per use case, no second role model |

## Tracking

- [GitHub Project #3](https://github.com/users/rrahimi-uci/projects/3) tracks every package issue, milestone control
  issue and child issue, with repository milestones D1–D15 carrying the due dates above. Use the Package, Stage and
  Role fields to group and filter.
- D1 is task-sized now: readiness #608, then #565–#572 and #625–#627. Later milestones are split into child issues at their
  kickoff, by the control issue's checklist.
- The roadmap's monthly milestones and Project #4 are closed. Beta tickets #588–#596, created from the alpha
  release notes' "Toward Beta" list, were folded into the EGI packages they duplicate or attached to them as child
  issues.
- Demos and slip handling follow the
  [five-person plan](CALIBER_FIVE_PERSON_DELIVERY_PLAN.md#five-week-delivery-cadence-and-ticket-standard), read with
  variable-length milestones: weeks before the last are implementation and integration, the last is QA.

## Reforecasting

- Reforecast after D1 feasibility, D3's protected-action proof, D5 Beta acceptance, D9 core proof and each of
  D10–D14. Use remaining base work plus remaining reserve divided by measured effective capacity; do not add 25%
  again to work that already includes it.
- If funding or staffing shrinks, the natural stopping points are D5 (Beta) and D9 (core platform). Neither is the
  book-complete MVP, and neither may be advertised as one.
- More people shorten D10–D15 most, because those packages are least dependent on each other once M17 lands; the
  Beta and core milestones are dependency-bound and gain less.
