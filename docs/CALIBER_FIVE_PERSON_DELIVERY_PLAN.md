# CALIBER delivery plan for a five-person team

> **Planning proposal, not a commitment and not part of the `0.1.0a1` alpha.** It describes a possible post-alpha program
> (a Beta and an MVP gate) built on the [reference architecture gap analysis](CALIBER_REFERENCE_ARCHITECTURE_GAP_ANALYSIS.md).
> The current product boundary is [Alpha scope and limitations](start/alpha-scope.md).
>
> **Superseded for staffing, scope and calendar on 2026-10-24** by the
> [three-person, book-complete EGI delivery plan](CALIBER_EGI_DELIVERY_PLAN.md): three people, not five; the MVP now
> covers the whole book (packages M17–M26 and milestones D10–D15); M16 moves to D15. The D1–D8 milestone contracts,
> package allocation and ticket standard below still apply; the week numbers and five-week cadence do not.

## Decision and planning basis

**Recommendation: fund delivery one milestone at a time, use five weeks per milestone, reach the Beta gate at week 25, and make an explicit investment decision before continuing to the MVP gate at week 45.** Each milestone contains four weeks of implementation/integration and one dedicated week of QA, fixes and acceptance. Demonstrate progress at the end of weeks 2 and 4 of each milestone, then review the QA evidence at the end of week 5. These are planning targets, not release promises.

This plan refines the [reference architecture gap analysis](CALIBER_REFERENCE_ARCHITECTURE_GAP_ANALYSIS.md), merged in [PR #513](https://github.com/rrahimi-uci/caliber-suite/pull/513). It preserves all B01–B16 and M01–M16 packages and their acceptance criteria. It introduces nine delivery milestones, **D1–D9**, to avoid confusing delivery checkpoints with the report's MVP package IDs or the older roadmap's monthly milestones. No platform capability is implemented by this document.

**Staffing assumption:** “5 person per week” means five full-time people, each available five working days per week. If it means five person-days total per week, this schedule does not apply. Week 1 starts when the named team, supported scope, synthetic downstream fixture and development environments are available; no calendar start date is assumed.

Evidence baseline: this planning refinement starts from CALIBER `b137bd1bd5faf096dde49c81893465cd2535b2f8`; the technical assessment and book snapshots remain those recorded in the gap analysis. It is a capacity and sequencing refinement, not a new implementation audit or a refreshed publication-status claim.

### What the team is building

- One tenant, one region, PostgreSQL, external MLflow and object storage; at least two workspaces for isolation tests.
- Beta: one synthetic evidence-review → missing-document-request use case; one supported connector; one configured real model adapter plus a deterministic scripted adapter; trusted connectors only.
- MVP: stronger workload identity, isolated execution, signed releases, protected audit, operational recovery, a second use case and one bounded internal child task.
- Reuse the existing application, workflow engine, registry, worker, approvals and ledgers. Keep unsupported effect paths refused until they satisfy the same control contract.
- Defer federation, multi-region execution, SaaS multi-tenancy, generalized automated compliance extraction, a connector marketplace and governed long-term memory. The gap analysis already excludes these from its estimates.

The essential invariant remains: **model proposal → current authority/policy → exact approval when required → controlled effect → verified outcome → joined evidence → evaluation/replay**. Reducing connector breadth is acceptable; removing an authority, recovery or evidence gate is not.

## Capacity, cost and uncertainty

| Weekly allocation | Person-weeks | Person-days | Treatment |
| --- | ---: | ---: | --- |
| Gross staffing | 5.00 | 25.00 | Five people, not five parallel feature teams |
| Leave, organizational overhead and unrelated support | 1.25 | 6.25 | 25% unavailable to this program; replace with measured availability |
| Effective program capacity | **3.75** | **18.75** | Available for planned delivery plus uncertainty |
| Maximum planned base work | **3.00** | **15.00** | Includes design, implementation, tests, review, docs and integration |
| Discovery/rework allowance at maximum base load | **0.75** | **3.75** | 25% of base work; not additional features |

The availability deduction and uncertainty reserve cover different things. Do not charge normal review/testing/documentation against the unavailable 25%: those are already in base effort. Do not add another generic contingency on top of these figures. The weekly figures are planning ceilings, not a claim that every milestone needs the same effort. Protect the final QA week; unused capacity supports earlier testing, risk reduction and backlog preparation without changing the five-week cadence or adding features.

| Stage | Base effort range, PW | Base midpoint, PW | Midpoint plus 25%, PW | Planning duration | Gross staffed cost, PW |
| --- | ---: | ---: | ---: | ---: | ---: |
| Beta, D1–D5 | 37–61 | 49 | 61.25 | 25 weeks | 125 |
| MVP increment, D6–D9 | 42–70 | 56 | 70.00 | 20 weeks | 100 |
| Through MVP | **79–131** | **105** | **131.25** | **45 weeks** | **225** |

Calendar arithmetic: `5 Beta milestones × 5 weeks = 25 weeks`; `4 MVP milestones × 5 weeks = 20 additional weeks`; `9 × 5 = 45 weeks` in total. At five people, gross staffed cost is `45 × 5 = 225 PW`. Effective capacity is `45 × 3.75 = 168.75 PW`, containing the unchanged 105 PW base midpoint, 26.25 PW uncertainty reserve and **37.5 PW of scheduling headroom**. This is additional calendar capacity, not extra feature scope or a new engineering estimate. Salary/vendor costs require actual rates and are not estimated here.

The nine dedicated QA weeks use `9 × 3.75 = 33.75 PW` of effective team capacity **within** those 168.75 PW; they are not added again. The remaining 36 weeks provide 135 effective PW for implementation, integration and continuous testing. Base package estimates already include tests, review, fixes and documentation; the QA week schedules that work and retains capacity for regression findings. A milestone needing the full 15 base PW must allocate at most 12 base PW to its first four weeks and at most 3 to QA/fixes in its final week. Refine the implementation-versus-QA split before starting each milestone; these aggregate budgets do not prove every specialist can finish on time.

The assessment's **27–44 week capacity-only calculation** remains historical sizing context, not the delivery forecast for this fixed cadence. The current baseline is **45 weeks**, with gates that can slip if evidence is incomplete. The midpoint reserve fits the aggregate calendar. The upper estimate does **not** fit stage by stage: Beta's upper reserved effort (76.25 PW) fits D1–D5's 93.75 effective PW, but the MVP increment's upper reserved effort (87.5 PW; 70 PW base, 3.5 PW/week) exceeds D6–D9's 75 effective PW and 60 PW planned-base ceiling. The 163.75 PW aggregate only fits 168.75 PW by spending Beta headroom on MVP work, which the Beta investment decision forbids. At the upper range the MVP increment therefore needs about 24 weeks (87.5 ÷ 3.75, consistent with the gap analysis's 14–24 week MVP capacity-only figure), i.e. a fifth MVP milestone and an MVP gate near week 50. Treat week 45 as the midpoint baseline and reforecast at D1, D3 and every release gate; unused Beta capacity cannot remove a later dependency or specialist constraint.

If only 60% of the team is available, effective capacity is 3 PW/week and the 45-week calendar provides 135 PW, just 3.75 PW above the 131.25 PW midpoint including reserve. Heavier milestones may then require more time despite the aggregate fit. Protect the team from competing roadmap commitments; do not shorten QA to compensate for unavailable staff.

## Team responsibilities

| Person / role | Primary responsibility | Secondary responsibility |
| --- | --- | --- |
| A — technical lead / backend | Contracts, admission, policy and release integration | Code review, dependency decisions and backlog refinement |
| B — backend / runtime | Dispatcher, effect ledger, recovery, bounded agents and child tasks | Connector conformance and integration tests |
| C — platform / security | Execution ownership, auth/secrets, isolation, deployment and restore | Threat-model review and operational evidence |
| D — full stack / SDK | Exact approvals, incident views, SDK/CLI, examples and user documentation | Backend contract work and pilot support |
| E — test / reliability engineer | Fault harness, public-path automation, evaluation, CI and release evidence | Testable design, observability and load/recovery drills |

These are five people, not five departments. A–D own tests for their changes; E supplies shared tooling and independent integration checks. D and E must be able to contribute code, and A/B must cover security-sensitive backend work with C. C is not assumed to implement identity, isolation and deployment simultaneously. Run at most two substantial implementation streams; use the other capacity for integration, review, tests and the next dependency-ready slice.

**C is the MVP bottleneck unless work is reassigned.** Counting the gap analysis's component/owner column and splitting shared tags evenly, security- or platform-tagged work is about 10 PW in D1–D5 (fits C's 18.75 effective PW) but about **24 PW in D6–D9** (M01, M02, M04, M05, M07, M08, M11, M12) against C's **15 effective PW** over 20 weeks, while backend-tagged MVP work is only about 16.5 PW against A+B's 30 PW. Before D6, assign at least 9 PW of that work away from C: B owns M05's archive export/integrity chain and M08's bulkheads/queues, A owns M02's bundle verification at admission, E owns M12's load/outage drills and co-owns M11's upgrade/restore automation, and C keeps M01, M07 and the deployment artifact. Confirm the split at the D5 reforecast; otherwise D6–D9 slip on one person's calendar even though the aggregate capacity fits.

A domain owner, an independent pilot operator and a security reviewer are **external dependencies**, not hidden sixth full-time engineers. Book their availability during D1 and again before D6. Their time/cost is excluded from the 225 staffed PW; if unavailable, move the affected gate or explicitly allocate one of the five to the work and reforecast. An implementer's self-review does not substitute for the independent MVP review.

## Milestone overview

Base effort below is a planning allocation of package midpoints, not a new estimate. Each milestone's base effort divided by its duration is at most 3 PW/week; unused headroom stays available for dependency delays. D6 sits exactly at that ceiling and cannot be relieved by earlier milestones, because no MVP work may start before the Beta decision at D5; D3 and D7 were brought under it by moving one B07 slice into D2 and one M11 preparation slice into D9. This is an average capacity check, not a claim that every specialist's weekly assignments have already been leveled. Allocation details follow the milestone definitions.

| ID | Planned weeks | Implementation / integration | QA / fixes week | Base PW | Demonstrable outcome |
| --- | --- | --- | ---: | ---: | --- |
| D1 | 1–5 | 1–4 | 5 | 6 | Fixed reference scope, early exposure controls, disposable action/recovery prototype |
| D2 | 6–10 | 6–9 | 10 | 13 | Pinned admission, execution authority, owner fencing and authorized context manifest |
| D3 | 11–15 | 11–14 | 15 | 14 | One governed model-proposed external action, exact approval and restart-safe reconciliation |
| D4 | 16–20 | 16–19 | 20 | 8.5 | Verified outcome, side-effect-free replay and actual workspace evaluation |
| D5 | 21–25 | 21–24 | 25 | 7.5 | Installable, documented Beta with public failure scenarios |
| D6 | 26–30 | 26–29 | 30 | 15 | Credential leases, signed promotion, protected audit and operator controls |
| D7 | 31–35 | 31–34 | 35 | 14 | Knowledge lifecycle, execution isolation and trajectory-based release checks |
| D8 | 36–40 | 36–39 | 40 | 14 | Second connector recovery, persistent budgets and bounded delegation |
| D9 | 41–45 | 41–44 | 45 | 13 | Independent install/restore/pilot and measured operating limits |

D1–D4 are engineering previews restricted to fixtures and the declared development profile. D6–D8 are MVP previews. None inherits a Beta/MVP label merely because its planned week arrives. Demonstrations can show partially integrated functionality but must label it as such.

## Detailed milestone contracts

### D1 — Scope, exposure reduction and feasibility (weeks 1–5)

**Implementation/integration:** weeks 1–4. **Dedicated QA and fixes:** week 5; execute the exit tests below, resolve blockers, rerun regressions and record the gate decision.

**Lead:** A; C owns exposure controls; B/E own the disposable prototype; D prepares approval UX and the example contract.

1. Freeze the supported profile, connector semantics, proposal/receipt schemas and ownership ADR (B01).
2. Begin B11 project-scoping fixes and B12 safe defaults/refusals using existing project identity. Complete run-level correlation and dispatcher-dependent enforcement later; these packages are not finished in D1.
3. Prototype B04/B05 against a disposable downstream: persist a proposal, dispatch a stable key, lose the receipt after commit and query the resulting state. Keep the prototype isolated from supported execution until production contracts and authority are integrated in D3.
4. Start B16's independent downstream mutation counter and crash barrier harness; record the first fixtures and commands.

**Exit evidence:** approved scope/ADRs; source/API map of every protected path; reproducible lost-receipt experiment yielding one downstream mutation; early trace-access tests; connector supports a stable key and query/precondition semantics or the plan explicitly blocks dependent work on Unknown. Document unresolved risks with owners. No claim that the prototype already enforces the full book principle.

**Demo:** request a synthetic document, drop the response, and inspect the actual downstream state. **Decision:** re-estimate B04/B05 before starting broader implementation; switch to a cooperative fixture connector if the proposed first connector cannot meet the contract.

### D2 — Admission and authority foundations (weeks 6–10)

**Implementation/integration:** weeks 6–9. **Dedicated QA and fixes:** week 10; execute the exit tests below, resolve blockers, rerun regressions and record the gate decision.

**Lead:** A; B handles admission/state; C handles owner fencing; D/E cover visibility, context fixtures and API tests.

1. Finish immutable manifests, pins, required lineage and admission intent conflicts (B02).
2. Implement separate subject/workload identity, scoped grants and current policy/revocation decisions (B03).
3. Build most of B13's owner lease, fencing and transactional outbox/inbox; finish dispatcher integration in D3.
4. Build B07's approved source inventory, source authorization before loading and ordered context manifest with trust tiers; continue B11 trace ownership/correlation and B12 deployment configuration.

**Order:** land B02 admission before B03 integration; prepare policy fixtures in parallel. Integrate context authorization only after B03 decisions exist. Outbox ownership depends on the persisted run identity.

**Exit evidence:** V02, V06 and V10 foundation assertions pass through admission/read APIs; a second owner is refused; foreign-workspace payloads are denied before load. This is not yet the full dispatch-path version of those scenarios. Same key with changed intent conflicts and admitted pins do not silently change.

**Demo:** admit a pinned run, change its source alias, and show unchanged pins; attempt admission/access with foreign identity and show denial. **Decision:** no protected production-like connector dispatch until D3 closes the authority/effect boundary.

### D3 — Governed action and safe restart (weeks 11–15)

**Implementation/integration:** weeks 11–14. **Dedicated QA and fixes:** week 15; execute the exit tests below, resolve blockers, rerun regressions and record the gate decision.

**Lead:** B; A handles policy/agent integration; C handles fencing and deployment enforcement; D handles approvals; E runs failure injection.

1. Finish B04/B05 production dispatcher and logical effect ledger, reusing only reviewed prototype code.
2. Complete exact proposal/evidence/policy/resource binding, expiry, supersession and concurrent approval consumption (B06).
3. Finish B07's allocation/omission/redaction records and the manual rule bundle; add bounded typed AgentStep and adapter conformance (B08).
4. Close B11 and B13 across actual execution; integrate most remaining B12 dispatcher refusal/security controls. Finish B12's full release-profile checks in D4.

**Order:** establish B04/B05 protected dispatch before B06 approval consumption and B08 tool proposals. Freeze B07 context before final AgentStep conformance. Each supported HTTP/registered/OpenAPI/MCP/model-tool route must either enter the common contract or fail visibly; one connector does not mean leaving other reachable write paths unguarded.

**Exit evidence:** integrated V01 control/effect path and V02–V10 denial, stale approval, intent conflict, restart, timeout and duplicate-event cases pass. V01 acceptance waits for D4; complete joined export/verifier evidence waits for D5. Independent downstream state shows one logical mutation after receipt loss; Unknown blocks dependent writes; no supported effect bypass exists.

**Demo:** a model proposes a document request, another user approves the exact intent, the worker crashes after commit, recovery reconciles it without a second mutation. Repeat with revoked permission and show no new effect. **Decision:** reforecast the remaining program from measured cycle time; do not broaden connectors while this gate fails.

### D4 — Outcome proof, replay and release evaluation (weeks 16–20)

**Implementation/integration:** weeks 16–19. **Dedicated QA and fixes:** week 20; execute the exit tests below, resolve blockers, rerun regressions and record the gate decision.

**Lead:** A; B implements replay/acceptance; E exercises the evaluation worker; C finishes safe-profile checks; D builds the evidence presentation.

1. Complete deterministic acceptance and recorded-observation replay with no live mutations (B09).
2. Complete workspace evaluation execution, lease recovery, pinned evidence and manual/automatic release eligibility (B10).
3. Finish B12; start the joined evidence schema/export/verifier (B14).

**Exit evidence:** V11–V13 pass; V14 profile/owner assertions pass; a successful transport with wrong business state fails acceptance; stale or failing evaluation blocks Apply; replay has zero external effects. B14 remains partial until D5 verifies every link and access path.

**Demo:** reject a false-success receipt, replay a valid run offline, then block a release using a failed evaluated trajectory. **Decision:** retain any unresolved evidence gaps as release blockers, not documentation caveats.

### D5 — Beta release and investment checkpoint (weeks 21–25)

**Implementation/integration:** weeks 21–24. **Dedicated QA and fixes:** week 25; execute the exit tests below, resolve blockers, rerun regressions and record the gate decision.

**Lead:** E; D owns the cookbook and SDK/CLI journey; A/B/C close integration, migration and deployment findings.

1. Finish joined evidence export, integrity verification, authorization/redaction/retention (B14).
2. Complete the synthetic downstream fixture distribution, UI/SDK/CLI cookbook, install instructions and operator intervention guide (B15).
3. Complete B16's public fault matrix, process-kill suite, migration/restore smoke, browser journey and exact-artifact acceptance report.

**Exit evidence:** **all B01–B16, V01–V14 and the gap analysis's complete Beta definition of done pass**. A fresh documented install completes the entire request → proposal → decision → effect → accepted outcome → evidence → replay path. State which configured-provider checks were run; deterministic tests do not establish real model quality.

**Demo:** a team member other than the feature implementer follows the cookbook from a clean install and resolves an injected Unknown. **Decision:** release Beta only after its gates pass; either stop at this usable bounded reference slice, fund MVP, or first resolve measured pilot problems. No automatic commitment to D6–D9.

### D6 — Stronger trust and operations controls (weeks 26–30)

**Implementation/integration:** weeks 26–29. **Dedicated QA and fixes:** week 30; execute the exit tests below, resolve blockers, rerun regressions and record the gate decision.

**Lead:** C for identity/audit with A/B implementing release and backend contracts; D owns operator controls; E starts trajectory design.

1. Complete workload identity/credential leases, cancellation/revocation and broker-outage behavior (M01).
2. Complete signed bundles, identical-digest promotion, canary assignment, rollback and in-flight quarantine (M02).
3. Complete protected audit archive/integrity/reader roles/retention (M05), and approval timers, reassignment, explicit overrides and scoped emergency stop (M06).
4. Define M09 trajectory slices, judge/version contracts and pilot protocol; execution and results finish in D7.

**Exit evidence:** V16, V19 and V23 pass; operator-stop/approval-race assertions from V24 pass; archive outage cannot silently discard required evidence. M02 initially uses the existing Beta evaluation gate; trajectory-based strengthening lands in D7.

**Demo:** revoke a lease, reject a tampered bundle, roll back new assignment without changing old receipts, and execute a scoped stop. **Decision:** verify security-review and pilot access are booked before committing later release dates.

### D7 — Knowledge lifecycle, isolation and behavior evaluation (weeks 31–35)

**Implementation/integration:** weeks 31–34. **Dedicated QA and fixes:** week 35; execute the exit tests below, resolve blockers, rerun regressions and record the gate decision.

**Lead:** C for isolation; A/B handle knowledge and release integration; D prepares extension/operator docs; E owns trajectory and containment evidence.

1. Finish approved knowledge activation/revocation, lineage and deletion/tombstones (M04).
2. Finish isolated worker profile, no ambient secrets, controlled egress/resource limits and pinned executable images (M07).
3. Finish M09's critical trajectory gates, scoped waivers and controlled real-provider pilot results, explicitly distinct from offline tests.
4. Prepare M10 feedback/dataset schemas, M11 deployment/backup artifacts and M14 conformance harness. These are partial packages; no M10 production promotion or M14 portability claim yet.

**Exit evidence:** V17, V18 and V22 pass, with retained positive/negative results; malicious content cannot gain authority and isolated code cannot access host secrets or unauthorized egress. Unsupported execution trust levels remain refused.

**Demo:** revoke a source and attempt reuse through derived context; block a candidate that improves average quality but fails a safety slice. **Decision:** unresolved containment or knowledge-revocation defects block MVP; do not relabel them optional to meet the week target.

### D8 — Recovery, budgets and bounded delegation (weeks 36–40)

**Implementation/integration:** weeks 36–39. **Dedicated QA and fixes:** week 40; execute the exit tests below, resolve blockers, rerun regressions and record the gate decision.

**Lead:** B; A handles budget/delegation integration; C advances deployment/restore; D prepares the second example; E tests process failures.

1. Complete second connector, Partial/InterventionRequired, separately authorized compensation and controlled redrive (M03).
2. Complete persistent hierarchical budgets, retry ownership, connector bulkheads and bounded/fair queues (M08).
3. After M08's reservation contract lands, complete one durable scoped internal child task, cancellation/deadline propagation and typed join (M13).
4. Continue M11 deployment/upgrade/restore automation and prepare M15's second domain fixture and acceptance predicates. M15 awaits final conformance/pilot in D9.

**Exit evidence:** V15 and V21 pass; V24 budget/overload assertions pass; canceled parents leave no authorized orphan mutation; failed compensation preserves explicit intervention state. M11 remains incomplete until an independent operator validates its artifacts in D9.

**Demo:** a governed support-ticket task delegates one bounded subtask, encounters a partial external failure and follows authorized reconciliation/compensation. **Decision:** no new workflow engine or third connector; concentrate on closing operational evidence.

### D9 — Independent operating proof and MVP release (weeks 41–45)

**Implementation/integration:** weeks 41–44. **Dedicated QA and fixes:** week 45; execute the exit tests below, resolve blockers, rerun regressions and record the gate decision.

**Lead:** E coordinates acceptance; C owns restore/load operations; D owns the pilot/docs; A/B close compatibility, feedback and release findings.

1. Finish M14 conformance against the completed M03/M13 contracts before the final M15 pilot.
2. Finish M10 governed feedback/candidate comparison and release; finish M11 independent install/upgrade/restore validation.
3. Run M12 workload/SLO/alert/owner-loss/storage-loss drills only after the supported deployment artifact is ready; publish achieved limits and RPO/RTO.
4. Finish M15 independent second-use-case pilot and docs/site/paper/video alignment with measured behavior.
5. Complete M16 independent security review, fix material findings, rebuild exact artifacts and make the release decision.

**Order:** conformance and install/restore in the first part of D9; operating measurements and independent pilot next; final security/regression gate last. Book reviewers before D9 and start review of stable components in earlier milestones. The five-week milestone includes four weeks of integration, conformance and operating/pilot work, then week 45 for final QA, fixes and acceptance. This allocation assumes bounded findings; material redesign consumes reserve or moves the release.

**Exit evidence:** **all M01–M16, V01–V24 and the complete MVP definition of done pass**. Publish retained installation, migration, recovery, pilot and security-review evidence tied to exact artifacts. External reviewer/operator unavailability blocks the relevant gate; it is not a pass.

**Demo:** another operator installs, upgrades, restores and runs both examples, including interruption/reconciliation, using only the published instructions. **Decision:** release within the measured operating envelope or remain a release candidate with explicit blockers.

## Package allocation and dependency accounting

Numbers are **base PW**, using each original package's midpoint. Multiple entries mean explicit preparation/completion slices; their sum equals the package midpoint. Every package appears and no effort is counted twice. Package-complete claims are governed by dependencies and the detailed exit criteria above, not by preparatory work.

| Package | Base midpoint PW | Delivery allocation |
| --- | ---: | --- |
| B01 | 1.5 | D1: 1.5 |
| B02 | 2.5 | D2: 2.5 |
| B03 | 4 | D2: 4 |
| B04 | 4 | D1: 1; D3: 3 |
| B05 | 4 | D1: 1; D3: 3 |
| B06 | 3 | D3: 3 |
| B07 | 4 | D2: 3; D3: 1 |
| B08 | 2.5 | D3: 2.5 |
| B09 | 4 | D4: 4 |
| B10 | 3 | D4: 3 |
| B11 | 2.5 | D1: 1; D2: 1; D3: 0.5 |
| B12 | 2.5 | D1: 1; D2: 0.5; D3: 0.5; D4: 0.5 |
| B13 | 2.5 | D2: 2; D3: 0.5 |
| B14 | 2.5 | D4: 1; D5: 1.5 |
| B15 | 2.5 | D5: 2.5 |
| B16 | 4 | D1: 0.5; D5: 3.5 |
| M01 | 4 | D6: 4 |
| M02 | 4 | D6: 4 |
| M03 | 4 | D8: 4 |
| M04 | 4 | D7: 4 |
| M05 | 3 | D6: 3 |
| M06 | 3 | D6: 3 |
| M07 | 5 | D7: 5 |
| M08 | 4 | D8: 4 |
| M09 | 3 | D6: 1; D7: 2 |
| M10 | 2.5 | D7: 1; D9: 1.5 |
| M11 | 4 | D7: 1; D8: 1; D9: 2 |
| M12 | 4 | D9: 4 |
| M13 | 4 | D8: 4 |
| M14 | 2.5 | D7: 1; D9: 1.5 |
| M15 | 2.5 | D8: 1; D9: 1.5 |
| M16 | 2.5 | D9: 2.5 |

The first prototype precedes B03 only as a disposable test fixture. B11/B12 begin with existing exposure controls and close only after their new dependencies. M14 preparation precedes M03/M13, but conformance completion follows them. Within D8, M08 precedes M13; within D9, M11 precedes M12 and M14 precedes the final M15 pilot. These constraints require weekly task scheduling; aggregate capacity alone does not prove a resource-leveled critical path.

## Five-week delivery cadence and ticket standard

| Week within each milestone | Main activity | Required checkpoint |
| --- | --- | --- |
| 1 | Refine child tickets, settle contracts/dependencies, prepare tests and fixtures, start implementation | Scope, owners, implementation/QA effort split and test plan recorded |
| 2 | Implement the first integrated slice; unit/contract tests run with each change | Working progress demo and risk review |
| 3 | Finish core implementation and failure-path integration; run continuous tests | Close dependency gaps and prepare release/profile fixtures |
| 4 | Complete scope, integration, docs and candidate artifacts; run pre-QA checks | Second demo; feature scope frozen and candidate ready for QA |
| 5 | Dedicated regression, integration, security and applicable browser/performance/recovery QA; fix and retest | Exact-candidate evidence, zero unresolved release blockers, milestone acceptance decision |

QA is performed by the **same five-person team**. E coordinates the test matrix; A–D reproduce/fix defects, add regression tests, review changes and verify documentation. Independent operator/security-review requirements still apply at their named gates. Automated testing and review happen throughout weeks 1–4; the fifth week is reserved for integrated qualification and fixes, not the first testing activity. New features wait for the next milestone. Any fix changes the candidate and triggers the affected checks again. If blockers remain at the end of week 5, the gate stays closed and dependent dates are reforecast; no automatic approval or shortened QA.

At the week-2 and week-4 reviews and the week-5 acceptance review, show a functioning public path, retained failure evidence and the remaining blockers. Report **accepted base PW**, reserved capacity consumed and actual availability, alongside completed child tickets; never convert raw ticket counts or story points into architectural coverage.

Keep implementation tickets to **1–2 developer days** as specified in [the existing roadmap](roadmap.md). Treat package estimates as budgets including review/integration, not promises to issue a fixed number of tickets. Before a milestone starts, refine its first two weeks only into fully specified child issues; retain the rest as an ordered package checklist to avoid hundreds of speculative tickets.

Every child issue must include:

1. D milestone and B/M parent package, owner, dependencies and bounded scope.
2. Exact API/module/schema affected, compatibility/migration implications and supported profile.
3. Observable acceptance criterion, including a failure/denial case where applicable.
4. Test commands and retained evidence location; measure affected behavioral-code coverage under repository rules.
5. Documentation/example changes, explicit exclusions and a 1–2 day estimate.
6. Dedicated PR, review requirements and release-gate linkage. Merge authority remains with the repository owner under the user's explicit instruction.

Initial D1 tickets should cover the profile/ownership ADR, schema fixtures, connector capability experiment, lost-receipt harness, trace ownership deny test/fix, unsafe-profile refusal, approval UX contract and updated estimates. Split any item that exceeds two days. These are proposed slices, **not claims that GitHub issues or milestones have already been created**.

### Handling slips and deciding what to fund

- Reforecast after D1 feasibility, D3 protected-action proof and D5 Beta acceptance, then at each MVP gate. Calculate remaining base work plus **remaining** uncertainty reserve divided by measured effective capacity; compare that workload requirement with the remaining five-week milestone slots and preserve every full QA week. Do not repeatedly add 25% to work that already includes reserve.
- If a gate slips, stop dependent rollout; use capacity on independent tests/docs/adapters or resolve the blocker. Preserve the same safety/evidence gates.
- If funding is constrained, make D5 Beta the stopping point: 25 planned weeks / 125 gross staffed PW. It is a bounded reference slice, not the MVP defined by the report. Do not advertise strong workload identity, hardened isolation or independent operational proof before D6–D9.
- If implementation finishes early, deepen testing and prepare the next backlog. Keep the dedicated QA week and the five-week milestone boundary; do not fill headroom with additional features.
- Before any dates become commitments, reconcile this expanded EGI program with the older 1.5-developer monthly roadmap and identify which existing work is complete, overlapping or separately funded. Both plans must not consume the same five people simultaneously.

## Definition of plan completion and next action

This planning change is complete when the package mapping, capacity arithmetic, dependencies, links and document formatting are validated and a reviewable PR exists. It does not execute the roadmap or create a release. The [gap analysis sections 13–15](CALIBER_REFERENCE_ARCHITECTURE_GAP_ANALYSIS.md#13-end-to-end-validation-strategy) remain the authoritative scenario and release-acceptance map.

**Next execution step:** name A–E, confirm the five-full-time-person assumption and shared scope, schedule domain/operator/security-review access, then turn D1 into 1–2 day child issues. Keep D2–D9 as gated planning budgets until implementation evidence supports more precise commitments.
