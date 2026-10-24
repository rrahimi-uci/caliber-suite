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
| Beta | B01–B16 | 37–61 | 49 | 61.25 | **30 weeks** |
| Core platform (formerly the MVP) | M01–M15 | 40–67 | 53.5 | 66.9 | **31 weeks** |
| Book-complete increment | M17–M26, M16 | 65–129 | 97 | 121.25 | **57 weeks** |
| **Book-complete MVP** | all | **142–257** | **199.5** | **249.4** | **118 weeks** |

Calendar arithmetic: at the midpoint the work needs `249.4 ÷ 2.25 = 110.8` effective weeks; rounding each milestone
up to whole weeks gives 118, so about 7 weeks of headroom sit inside the calendar. At the **upper** estimate the
program needs `257 × 1.25 ÷ 2.25 ≈ 143` weeks, roughly six months later. At the lower estimate it needs about 79
weeks. Ranges for M22–M26 are this plan's own estimates; the gap analysis gave M17–M21 order-of-magnitude
allowances marked low-confidence, and their "+" upper bounds are open-ended. Treat 118 weeks as the midpoint
baseline and reforecast at every gate.

## Calendar

**Assumed start: Monday 2 November 2026.** Due dates are the Friday of each milestone's QA week and move with the
start date. Holidays are inside the 25% unavailability.

| ID | Weeks | QA week | Due | Base PW | Outcome |
| --- | --- | ---: | --- | ---: | --- |
| D1 | 1–4 | 4 | 2026-11-27 | 6 | Fixed reference scope, early exposure controls, disposable action/recovery prototype |
| D2 | 5–12 | 12 | 2027-01-22 | 13 | Pinned admission, execution authority, owner fencing and authorized context manifest |
| D3 | 13–20 | 20 | 2027-03-19 | 14 | One governed model-proposed external action, exact approval and restart-safe reconciliation |
| D4 | 21–25 | 25 | 2027-04-23 | 8.5 | Verified outcome, side-effect-free replay and actual workspace evaluation |
| D5 | 26–30 | 30 | **2027-05-28** | 7.5 | **Beta**: installable, documented, with public failure scenarios |
| D6 | 31–39 | 39 | 2027-07-30 | 15 | Credential leases, signed promotion, protected audit and operator controls |
| D7 | 40–47 | 47 | 2027-09-24 | 14 | Knowledge lifecycle, execution isolation and trajectory-based release checks |
| D8 | 48–55 | 55 | 2027-11-19 | 14 | Second connector recovery, persistent budgets and bounded delegation |
| D9 | 56–61 | 61 | **2027-12-31** | 10.5 | **Core platform proof**: independent install/restore/pilot and measured limits (no release decision) |
| D10 | 62–71 | 71 | 2028-03-10 | 16.5 | Governance inventory, routing and ownership transfer, remaining platform contracts |
| D11 | 72–80 | 80 | 2028-05-12 | 15 | Multi-tenant isolation and organizational administration |
| D12 | 81–90 | 90 | 2028-07-21 | 18 | Regional cells, residency and region-loss recovery |
| D13 | 91–100 | 100 | 2028-09-29 | 18 | Knowledge Hub: governed extraction, ontology and Hub views |
| D14 | 101–109 | 109 | 2028-12-01 | 15 | Governed memory and A2A federation |
| D15 | 110–118 | 118 | **2029-02-02** | 14.5 | **Book-complete MVP**: ecosystem, companion case, security review and release decision |

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

A domain owner, an independent pilot operator and a security reviewer remain **external dependencies**. Book them
during D1 and again before D9 and D15; their unavailability blocks the relevant gate, it does not pass it.

## Milestone contracts

**D1–D8** keep the contracts, ordering, exit evidence and demos of the
[five-person plan](CALIBER_FIVE_PERSON_DELIVERY_PLAN.md#detailed-milestone-contracts); only their length and the
person wearing each lead hat change. Two additions apply from D1:

- **B01's ADR fixes the identifiers later packages need**: tenant, region and residency fields on run, event,
  artifact and binding records, and the CloudEvent envelope (`tenantid`, `correlationid`, `causationid`,
  `traceparent`). Single-tenant, single-region values are fine until D11/D12; adding the columns then would rewrite
  every admitted run. This stays inside B01's 1–2 PW range.
- **The Beta scenario is the book's companion case.** B15's synthetic document-request fixture uses the
  Harborlight Mortgage evidence-review case of Appendix H, so the Beta already exercises the book's own example.

**D9 — Core platform proof (weeks 56–61).** As in the five-person plan minus M16: finish M14 conformance, M10
feedback/candidate release, M11 independent install/upgrade/restore, M12 workload/SLO/recovery drills and the M15
second-use-case pilot. **Exit evidence:** M01–M15 and V01–V24 pass with retained artifacts. **Decision:** none on
release; the independent security review and the MVP decision move to D15, because D11–D12 change the trust and
deployment boundary the review would have certified.

**D10 — Governance, routing and contract completeness (weeks 62–71).** M24 governance inventory and exception
service; M23 routing decisions, ownership transfer and model economics; M25 remaining platform contracts and a
second experience channel. **Exit evidence:** V25–V27. **Demo:** a risk-tier change blocks a release until a
scoped, expiring waiver is approved; a route falls back with a recorded RoutingDecision; a handoff transfers
ownership only after acknowledgment.

**D11 — Multi-tenancy and organizational administration (weeks 72–80).** M17. **Exit evidence:** V28; zero
cross-tenant leakage across runs, traces, knowledge, evaluations, audit and exports. **Decision:** D12 does not start
while any cross-tenant finding is open.

**D12 — Regions and residency (weeks 81–90).** M18. **Exit evidence:** V29; a region-loss drill meets the agreed
RPO/RTO without moving resident data. **Demo:** promote the same signed release to two regional cells and lose one.

**D13 — Knowledge Hub (weeks 91–100).** M21. **Exit evidence:** V30; an extracted rule cannot enforce anything until
its per-field verdicts pass and a human approves the KnowledgeRelease.

**D14 — Memory and federation (weeks 101–109).** M20 then M19. **Exit evidence:** V31–V32; untrusted provenance
never becomes durable memory, and a remote peer can neither widen authority nor have an artifact adopted without
local checks.

**D15 — Ecosystem, companion case and book-complete MVP release (weeks 110–118).** M22 catalog and certification;
M26 companion case and book traceability; M16 complete safety/migration matrix, independent security review over the
final trust boundary, rebuilt artifacts and the release decision. **Exit evidence:** **all B01–B16, M01–M26 and
V01–V34 pass**, the [traceability tables](#book-traceability) below have no uncovered row, and the gap analysis's MVP
definition of done holds. **Decision:** release within the measured envelope, or remain a release candidate with
explicit blockers.

## New packages M17–M26

Base PW ranges for M17–M21 come from the gap analysis's deferred backlog; M22–M26 are this plan's estimates. Each
package splits into 1–2 day child issues when its milestone starts.

| Package | Book source | Concrete slices | Depends on | Acceptance | PW (mid) | Milestone |
| --- | --- | --- | --- | --- | ---: | --- |
| M17 Multi-tenant isolation and organizational administration | Ch. 4, 21, 26; App. G | 1. Tenant as first-class scope in admission and the tenant router. 2. Per-tenant data and key separation, enforced in queries. 3. OIDC/SAML SSO and SCIM provisioning. 4. Per-tenant quotas and budgets. 5. Cross-tenant leakage suite. | B03, M01, M05, M08 | V28: no cross-tenant read, write, enumeration or export; a deprovisioned user loses access within the stated bound | 10–20 (15) | D11 |
| M18 Multi-region execution and residency cells | Ch. 4, 15, 27 | 1. Global definition plane vs regional cells. 2. Home region/shard on WorkflowExecution. 3. Residency enforcement for data, knowledge and artifacts. 4. Per-region Environment/KnowledgeBinding and promotion. 5. Region-loss drill with RPO/RTO. | M11, M12, M17 | V29: resident data never leaves its region; region loss recovers within target; promotion is per cell | 12–24 (18) | D12 |
| M19 A2A gateway and federated task adoption | Ch. 19; App. G phase 4 | 1. A2A gateway/Task Bridge. 2. FederatedTask state mapping, deadlines and budgets. 3. Signed task envelope and peer trust. 4. Authority intersection with the remote peer. 5. Local artifact adoption checks and federated trace links. 6. Cancellation. | M01, M13, B14 | V32: a peer cannot widen scope, duplicate an effect or have an artifact adopted without passing local checks | 6–12 (9) | D14 |
| M20 Governed cross-run memory | Ch. 6, 17 | 1. MemoryRecord with tenant, scope, consent, purpose allowlist, TTL and provenance. 2. Writes as gated proposals. 3. Corrections and supersession. 4. Deletion propagation into derived context. | B07, M04, M05 | V31: untrusted provenance cannot become durable memory; deleted memory cannot resurface | 4–8 (6) | D14 |
| M21 Knowledge Hub: governed extraction, ontology and Hub views | Ch. 5 | 1. KnowledgeArtifact with extractor and verifier versions and per-field verdicts. 2. Corroboration and review routes. 3. Versioned ontology and tenant extensions. 4. Hub views. 5. Platform-declared blocking fields. | M04, M17 | V30: an extracted rule cannot enforce until verified and released by a human; a superseded rule stops enforcing | 12–24 (18) | D13 |
| M22 Connector and framework catalog with certification | Ch. 3, 7, 9; App. D | 1. Catalog of connectors and framework adapters. 2. Certification by the M14 conformance suite and published CompatibilityReports. 3. Trust tiers and revocation. 4. Two independently conformant adapters. | M02, M14 | V33: an uncertified or revoked adapter cannot be admitted; two certified adapters pass identical safety scenarios | 6–12 (9) | D15 |
| M23 Routing, ownership transfer and model economics | Ch. 11, 18, 28 | 1. RoutingDecision from a route service and the LLM gateway. 2. OwnershipTransfer with acknowledgment states. 3. Release-pinned model eligibility and fallback. 4. Cost-aware routing and model/tool substitution behind release gates. | M08, M09, M13 | V26: every route records candidates, exclusions and fallback; ownership never transfers without acceptance; substitution needs a gate | 4–8 (6) | D10 |
| M24 Governance inventory, risk tiers and exceptions | Ch. 26 | 1. GovernanceRecord per use case (owners, risk tier, autonomy, data classes, effects, controls, review date). 2. ExceptionWaiver service with scope, compensating controls and expiry. 3. Release gates read the risk tier. 4. Governance reporting. | M02, M09 | V25: an expired waiver or overdue review blocks release; reports reconcile with the inventory | 3–6 (4.5) | D10 |
| M25 Remaining platform contracts and a second channel | Ch. 4, 8, 9, 12, 14 | 1. Artifact contract. 2. CloudEvent envelope with tenant/correlation/causation and `traceparent`. 3. Plan IR eligibility/effect checks with `capability_envelope_id` and `risk_class`. 4. SkillPackManifest and CompatibilityReport. 5. NotificationDelivery and one more channel through the same admission contract. | B02, B13, M02 | V27: every Appendix C contract has one owner and a contract test; the second channel cannot bypass admission | 4–8 (6) | D10 |
| M26 Companion case and book traceability | App. H; Ch. 16–17 | 1. Run the Harborlight Mortgage case end to end on CALIBER. 2. Publish the chapter/contract traceability with a test per row. 3. Regression suite mirroring every Chapter 17 antipattern correction. 4. Cross-link the book repository to CALIBER. | M15, M25 | V34: the companion case passes through the reference contracts; every traceability row points at a passing test | 2–4 (3) | D15 |

New validation scenarios V25–V34 are the acceptance column above; add them to the gap analysis's scenario matrix
when each package's milestone starts.

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
| GovernanceRecord / ExceptionWaiver | 26 | M24 |
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
| 13 Use critique to improve, never to self-approve | B09, M09 |
| 14 Put people where their judgment matters | B06, M06, M25 |
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
| 26 Make governance part of how the platform runs | M24 |
| 27 Release new behavior without breaking work in flight | M02, M11, M18 |
| 28 Treat performance as a business decision | M08, M12, M23 |
| Appendix H Companion implementation (Harborlight Mortgage) | B15, M26 |

The book's chapter list is from `book/manuscript.md` in the
[book repository](https://github.com/rrahimi-uci/building-enterprise-general-intelligence) as of its commit
`dacedba`. Recheck both tables when the book changes.

## Tracking

- [GitHub Project #3](https://github.com/users/rrahimi-uci/projects/3) tracks every package slice, with repository
  milestones D1–D15 carrying the due dates above.
- The roadmap's monthly milestones and Project #4 are closed. Beta tickets #588–#596, created from the alpha
  release notes' "Toward Beta" list, were folded into the EGI packages they duplicate or attached to them as child
  issues.
- Child-issue standard, demos and slip handling follow the
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
