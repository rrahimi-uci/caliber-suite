# Beta and single-tenant MVP execution plan

Planning baseline: 26 October 2026, main `5b2fb2a408fad887f1c8704e311b46b09da0f15f`.
Production-code baseline remains `4729508db7b948e4cb555d5b365dade2d5c458af`; subsequent report changes are not feature delivery.

This is the executable reconciliation of [map-gap.md](../../map-gap.md), the
[EGI delivery plan](../CALIBER_EGI_DELIVERY_PLAN.md), current source and existing GitHub work.
[Project #3](https://github.com/users/rrahimi-uci/projects/3) is the sole active delivery board.
The [machine-readable manifest](beta-mvp-execution.json) records every epic/task, gap, dependency and validation oracle.
GitHub child issues contain the complete agent briefs. Repository changes in this planning PR remain proposed until owner merge;
GitHub issue/project metadata has been applied separately. No Beta/MVP feature is implemented by this plan.

Post-merge contract correction: [review findings and resolutions](EXECUTION_CONTRACT_CORRECTIONS.md)
against main `9687bc99e19f47c0f9ac19af0da238323cf4848e`. The first structural validator did not detect
contradictory child pseudocode and acceptance oracles. Corrected briefs now cover those findings directly;
offline negative tests protect the reviewed contracts, while live checks compare every brief field.
Neither validator substitutes for source review or future feature validation.

## Release boundary and source precedence

1. Owner's single-tenant decision: one configured organization per installation, multiple projects/users.
2. This plan and linked child issues: corrected implementation contracts, task-level dependency graph and acceptance.
3. ADRs 0001–0004 and existing implementation: preserve supported behavior; evolve contracts readers-first.
4. Historical gap/delivery documents: rationale, scenario inventory and estimate provenance, not conflicting execution instructions.

The `G01`–`G22` namespace here is **map-gap.md**, not the older analysis's `G01`–`G12`.
Required new organization/project/region/residency fields remain. Shared-customer tenant routing, universal legacy
tenant-column retrofit, per-customer shared-hosting key separation and cross-tenant quotas are excluded.
OIDC/SCIM, regional cells, Hub, memory, federation, catalog and autonomy are retained, not optional scope reductions.

| Gate | Required outcome | Not a claim of |
| --- | --- | --- |
| Beta, D1–D5 | Minimal governed reference architecture; one supported connector; human approval; pinned admission/context; fenced effects and unknown recovery; acceptance/replay; evaluated release; joined evidence; public SDK/UI/CLI; install/upgrade/restore smoke and independent boundary review | Full autonomy, arbitrary executable trust, general connector safety, shared hosting or full enterprise MVP |
| Core proof, D6–D9 | Leased credentials, signed promotion, protected audit, isolated execution, knowledge lifecycle, durable budgets, second connector/use case, conformance and independently measured operations | A separate MVP release or completion of D10–D15 |
| Full retained MVP, D10–D15 | Governance/autonomy, routing/contracts/channel, enterprise identity, residency cells, Hub, memory/federation, certified ecosystem, companion traceability and final independent security/release evidence | Unqualified book completeness or shared-customer tenancy certification |

V01–V35 remain the scenario framework. **V28-identity is required**: OIDC/SCIM, project and isolated-installation
boundaries, and deprovisioning of sessions/PATs/memberships/grants/leases/approval authority within 60 seconds.
**V28-shared-tenancy is explicitly excluded**, not a passing test or a skipped required check.

## Existing work reused

D1 ADR/schema, downstream fixture, disposable dispatch/reconciliation prototypes, inventory, narrow trace filter,
profile refusals, approval UX and harness work remain closed: #516, #519, #521, #529, #532, #541, #565–#572, #625–#627.
Their existence does not complete the production packages. In particular, the narrow #569 trace test did not cover
name/numeric aliases; B11 now owns canonical exclusive experiment identity and first-claim provenance.

Reuse existing workflow/compiler/worker, project authorization, approval quorum, legacy effect ledger, release
claim/heartbeat/complete methods, EvalProvider, StorageBackend, SDK conventions, deployment artifact and migration chain.
Do not recreate a queue, fixture, registry or budget authority merely because a new package has a different name.
Already-closed duplicate monthly issues remain closed. Valid open overlapping work is reused, not deleted.

## Settled architecture decisions

| Boundary | Implementation decision | Required negative/fault proof |
| --- | --- | --- |
| Admission | Canonical requested intent is separate from resolved immutable pins. Resolve policy and enabled rule bundle before first insert; DB-level immutability and unique scoped admission key | Alias changes preserve original retry pins; changed intent conflicts; bulk SQL cannot mutate manifest |
| Authority | Exact run/grant identity, typed subject/workload, current membership/revocation and explicit action mapping | No newest-grant fallback; foreign/expired/revoked grant cannot dispatch |
| Effects | Logical key includes durable occurrence, not only arguments. Separate intent digest; seven-state record; private transport behind dispatcher | Legitimate repeated action differs from retry; timeout/404 cannot prove failure; recover abandoned dispatched attempts |
| Approval | Decision recording is separate from atomic approval consumption plus local dispatch claim | Changed intent, expiry, revoked approver and concurrent claims cannot authorize a second effect |
| Ownership/events | Captured owner/token/expiry checked in the transition transaction. Durable SQL outbox/inbox, at-least-once delivery and deduplication | Both handoff crash windows, failed consumer rollback, queue saturation and stale/future tokens |
| Context/memory | Authorize before load; immutable context provenance; reference-profile refusal of unowned durable memory until M20 | No foreign payload or memory-to-instruction promotion; deletion cannot remain active under legal hold |
| Acceptance/replay | Domain observation, not model self-report; historical replay read-only; counterfactual is a separate authorized/budgeted run | False-success receipt rejected; replay calls no live provider/effect and does not mutate original SQL |
| Evaluation | Existing release queue gets a real consumer; one authoritative fenced completion, not exactly one external attempt | Worker crash after external evaluation preserves orphan-attempt provenance; critical failure blocks all release paths |
| Evidence/audit | Hash membership/metadata/links; redact before hashing exported view; version-aware safe verifier; signed archive checkpoints later | Missing/swapped records, unsafe archives, unauthorized cached views and archive truncation detected |
| Isolation | Beta trusted-path refusal; later container network-none with broker-mediated stdio capabilities, no ambient secrets | No false HTTP_PROXY-on-Unix-socket guarantee, host mounts, raw sockets or global container cleanup |
| Budgets | One durable reservation family from B08 through M08; integer micro-USD/tokens, idempotent settlement and conservative unknown usage | Concurrent overspend, retry double-charge, starvation and fake run foreign keys rejected |
| Regions | Global metadata coordinator owns cell generation/expiring dispatch leases; resident payload stays in cell; replacement waits for authoritative prior-lease invalidity | Partitioned/stale cell cannot authorize new work; in-flight effects reconcile; recovery stays inside permitted residency |
| Federation | Pin A2A v0.3.0 JSON-RPC behind protocol-neutral task contracts; local authority/acceptance governs adoption | Lost acknowledgement remains unknown; peer cannot widen scope; SSRF/signature/hash/replay failures denied |
| Autonomy | ADR0004 roles, human owner, independent deterministic acceptance; cap at L2 without it. Promotion requires time AND decision count; incidents deduplicated for demotion | Different model pins alone do not prove independence; L5 never overrides explicit policy/legal human-review requirements |

A2A's versioned wire schema is pinned for reproducibility, not claimed to be the latest standard:
[official v0.3.0 schema](https://github.com/a2aproject/A2A/blob/v0.3.0/specification/json/a2a.json),
tag object `210f03d426e2f2fa92000e14ef0de3b7ba15aee5`. The M19 task vendors the resolved commit/license and validates fixtures.
Reuse-first decisions retain current infrastructure by default. Temporal comparison and extraction-library evaluation
are bounded evidence tasks, not authorization to rewrite the runtime or import an unverified dependency.

## Dependency and concurrency policy

The manifest and native GitHub blocked-by edges form an acyclic task graph. Parent-child links express ownership,
not prerequisites; a consumer depends on the producing child, not on completion of a mutually dependent epic.

Key chains:

- Pure B03 principal/grant/policy contracts plus B07 rule loader #726 → B02 immutable admission → B03 worker/resume integration → B07 context. The loader is D2; #736 remains D3 consumption, never manifest backfill.
- B13 lease/outbox + B05 proposal/receipt + B06 approval validator → B04 dispatcher → atomic approval consumption → bounded adapters → acceptance/replay → evidence/public journey → Beta gate.
- Beta investment gate → M01/M02/M05/M06/M09 foundations → isolation/knowledge/trajectory → budgets/child tasks/second connector → conformance/restore/pilot → D9 proof.
- D9 proof → governance/contracts/routing/autonomy → retained M17 identity → regional cells → Hub and memory; federation follows regional/child-task contracts → catalog/companion traceability → final security/release gate.

Safe parallel starting work includes B02 immutable storage, pure B03 principal contracts, B07 rule-bundle loader, B13 worker/lease inventory,
B11 trace-alias correction and B12 request/memory restrictions. After Beta, credential leases and protected audit
can progress independently; in D10 routing and contract work can progress alongside governance's producer slice.
Memory and federation are independent after their own prerequisites; they need not serialize solely because both are D14.

Use at most two substantial implementation streams with the third person integrating/reviewing. P1 is backend/runtime,
P2 platform/security, P3 integration/UI/SDK/testing; these are capacity lanes, not invented assignees. Concurrent tasks
editing shared migration heads, models.py, runtime.py, server.py or generated client types require an explicit integration
lock/landing order even without a semantic dependency. Do not parallelize five adapter edits in runtime.py blindly.

Project fields: Stage, Package, historical Role, Item type, Priority, Readiness, Lane, Gap IDs and Execution order.
The old effort field is renamed **Historical base PW**, not presented as a current estimate. Saved views are
Execution index, Ready tasks, Beta, MVP and Release gates.
Filter `Item type=Task`, `Readiness=Ready`; check native blockers again before starting. Execution order is milestone
plus dependency depth, not a duration estimate. Readiness is a planning snapshot and must be refreshed when dependencies close.
Milestone controls aggregate evidence; external readiness does not block unrelated offline development.

## Agent task and validation contract

Every child issue contains objective/rationale, release/parent/gaps, exact scope/files/interfaces, settled constraints,
non-goals, prerequisite links, implementation guidance, unit/integration/public-path tests, failure oracle, docs and DoD.
New file/test paths are planned deliverables, not evidence of current implementation. Existing symbol signatures must be
checked when landing; if source drift changes the contract, update the issue and dependents instead of inventing a substitute.

Each behavior change includes meaningful success, boundary, denial/error and regression tests. SQL ownership/race/migration
checks use PostgreSQL16. Ordinary checks use scripted providers/fake clocks and deterministic fixtures. Installed artifacts,
browser journeys, real-provider quality trials and independent human reviews are separate evidence classes.
Greater than95% affected behavioral-code coverage is measured with established tooling; coverage never substitutes for
the failure oracle. All applicable CI-equivalent suites and `git diff --check` pass; cross-component work uses `./test-all.sh`.
Documentation-only/coordination work records why runtime coverage is not applicable and runs docs/link/metadata checks.
**Required skipped checks are not passes.** No critical/high security finding is silently waived to release.

The milestone control alone owns `docs/reports/egi/Dk-acceptance.md`. Tasks supply evidence fragments with exact SHA,
artifact hashes, test IDs/results, scope exceptions and unresolved blockers. No competing report writers or fabricated dates.
All repository changes use focused branches/PRs and the existing full template. Request owner/Copilot review where supported;
GitHub inability to request self-review is recorded, not disguised as approval. **Only the owner merges PRs.**

## Calendar and genuine external inputs

The previous 122-week/211-base-PW plan was a broader-scope sizing model. It is preserved as historical context, not shortened
by deleting M17. GitHub D1–D15 due dates are unset. [#608](https://github.com/rrahimi-uci/caliber-suite/issues/608) owns:

- Named team availability and task assignments; measured capacity and integration ownership.
- Domain owner, independent Beta/final security reviewer, pilot operator and independent adapter author bookings.
- Domain quality thresholds/corpus, real-provider authorization/spend cap, and operating/SLO/RPO/RTO targets.
- Retained-scope effort reforecast with reserved QA, followed by owner-approved calendar PR and matching milestone dates.

These are real staffing/acceptance inputs, not unresolved choices about tenant architecture, effect semantics or event durability.
No due date or live trial result is invented. Book-side publication remains an explicitly coordinated external PR, not permission
for arbitrary changes outside Caliber. No future GitHub Release/tag is created during planning.

## Traceability index

<!-- GENERATED_INDEX_START -->
58 existing epic/slice issues updated; 279 new child issues plus retained task #641. No existing issue was closed as obsolete: valid overlapping work was reused; already-closed duplicates and completed D1 work remain closed. Seven empty historical milestones and Project #4 were closed, not deleted.

| Report gap | Release coverage | Reused epics | Child tasks |
| --- | --- | --- | ---: |
| G01 Scope/roadmap | MVP; all milestone controls | [#607](https://github.com/rrahimi-uci/caliber-suite/issues/607) | 6 |
| G02 Admission/API identity | Beta + MVP | [#517](https://github.com/rrahimi-uci/caliber-suite/issues/517), [#606](https://github.com/rrahimi-uci/caliber-suite/issues/606) | 14 |
| G03 Protected execution | Beta + MVP | [#518](https://github.com/rrahimi-uci/caliber-suite/issues/518), [#520](https://github.com/rrahimi-uci/caliber-suite/issues/520), [#526](https://github.com/rrahimi-uci/caliber-suite/issues/526), [#534](https://github.com/rrahimi-uci/caliber-suite/issues/534), [#559](https://github.com/rrahimi-uci/caliber-suite/issues/559) | 30 |
| G04 Recovery/connector | Beta + MVP | [#520](https://github.com/rrahimi-uci/caliber-suite/issues/520), [#522](https://github.com/rrahimi-uci/caliber-suite/issues/522), [#545](https://github.com/rrahimi-uci/caliber-suite/issues/545) | 21 |
| G05 Approvals | Beta + MVP | [#523](https://github.com/rrahimi-uci/caliber-suite/issues/523), [#548](https://github.com/rrahimi-uci/caliber-suite/issues/548), [#589](https://github.com/rrahimi-uci/caliber-suite/issues/589) | 15 |
| G06 Ownership/events | Beta + MVP | [#522](https://github.com/rrahimi-uci/caliber-suite/issues/522), [#536](https://github.com/rrahimi-uci/caliber-suite/issues/536), [#537](https://github.com/rrahimi-uci/caliber-suite/issues/537), [#606](https://github.com/rrahimi-uci/caliber-suite/issues/606) | 23 |
| G07 Acceptance/replay | Beta + MVP | [#527](https://github.com/rrahimi-uci/caliber-suite/issues/527), [#562](https://github.com/rrahimi-uci/caliber-suite/issues/562) | 9 |
| G08 Release evaluation | Beta + MVP | [#528](https://github.com/rrahimi-uci/caliber-suite/issues/528), [#551](https://github.com/rrahimi-uci/caliber-suite/issues/551), [#552](https://github.com/rrahimi-uci/caliber-suite/issues/552), [#554](https://github.com/rrahimi-uci/caliber-suite/issues/554), [#592](https://github.com/rrahimi-uci/caliber-suite/issues/592) | 19 |
| G09 Release compatibility | MVP | [#544](https://github.com/rrahimi-uci/caliber-suite/issues/544), [#551](https://github.com/rrahimi-uci/caliber-suite/issues/551), [#606](https://github.com/rrahimi-uci/caliber-suite/issues/606) | 16 |
| G10 Deployment identity | Beta + MVP | [#517](https://github.com/rrahimi-uci/caliber-suite/issues/517), [#524](https://github.com/rrahimi-uci/caliber-suite/issues/524), [#598](https://github.com/rrahimi-uci/caliber-suite/issues/598) | 21 |
| G11 Identity/secrets | Beta + MVP | [#518](https://github.com/rrahimi-uci/caliber-suite/issues/518), [#543](https://github.com/rrahimi-uci/caliber-suite/issues/543), [#598](https://github.com/rrahimi-uci/caliber-suite/issues/598) | 20 |
| G12 Profile/isolation | Beta + MVP | [#533](https://github.com/rrahimi-uci/caliber-suite/issues/533), [#534](https://github.com/rrahimi-uci/caliber-suite/issues/534), [#535](https://github.com/rrahimi-uci/caliber-suite/issues/535), [#549](https://github.com/rrahimi-uci/caliber-suite/issues/549) | 16 |
| G13 Traces/audit/evidence | Beta + MVP | [#530](https://github.com/rrahimi-uci/caliber-suite/issues/530), [#531](https://github.com/rrahimi-uci/caliber-suite/issues/531), [#538](https://github.com/rrahimi-uci/caliber-suite/issues/538), [#539](https://github.com/rrahimi-uci/caliber-suite/issues/539), [#547](https://github.com/rrahimi-uci/caliber-suite/issues/547) | 16 |
| G14 Knowledge/context | Beta + MVP | [#524](https://github.com/rrahimi-uci/caliber-suite/issues/524), [#525](https://github.com/rrahimi-uci/caliber-suite/issues/525), [#546](https://github.com/rrahimi-uci/caliber-suite/issues/546), [#553](https://github.com/rrahimi-uci/caliber-suite/issues/553), [#592](https://github.com/rrahimi-uci/caliber-suite/issues/592), [#602](https://github.com/rrahimi-uci/caliber-suite/issues/602) | 27 |
| G15 Memory | Beta + MVP | [#533](https://github.com/rrahimi-uci/caliber-suite/issues/533), [#601](https://github.com/rrahimi-uci/caliber-suite/issues/601) | 7 |
| G16 Budgets/performance | Beta + MVP | [#526](https://github.com/rrahimi-uci/caliber-suite/issues/526), [#550](https://github.com/rrahimi-uci/caliber-suite/issues/550), [#558](https://github.com/rrahimi-uci/caliber-suite/issues/558), [#559](https://github.com/rrahimi-uci/caliber-suite/issues/559), [#604](https://github.com/rrahimi-uci/caliber-suite/issues/604) | 32 |
| G17 SDK/API/extensions | Beta + MVP | [#526](https://github.com/rrahimi-uci/caliber-suite/issues/526), [#539](https://github.com/rrahimi-uci/caliber-suite/issues/539), [#540](https://github.com/rrahimi-uci/caliber-suite/issues/540), [#560](https://github.com/rrahimi-uci/caliber-suite/issues/560), [#561](https://github.com/rrahimi-uci/caliber-suite/issues/561), [#595](https://github.com/rrahimi-uci/caliber-suite/issues/595), [#603](https://github.com/rrahimi-uci/caliber-suite/issues/603), [#606](https://github.com/rrahimi-uci/caliber-suite/issues/606) | 40 |
| G18 Deployment/DR | Beta + MVP | [#542](https://github.com/rrahimi-uci/caliber-suite/issues/542), [#555](https://github.com/rrahimi-uci/caliber-suite/issues/555), [#556](https://github.com/rrahimi-uci/caliber-suite/issues/556), [#557](https://github.com/rrahimi-uci/caliber-suite/issues/557), [#558](https://github.com/rrahimi-uci/caliber-suite/issues/558), [#594](https://github.com/rrahimi-uci/caliber-suite/issues/594) | 26 |
| G19 Testing/evidence | Beta + MVP | [#535](https://github.com/rrahimi-uci/caliber-suite/issues/535), [#542](https://github.com/rrahimi-uci/caliber-suite/issues/542), [#552](https://github.com/rrahimi-uci/caliber-suite/issues/552), [#562](https://github.com/rrahimi-uci/caliber-suite/issues/562), [#563](https://github.com/rrahimi-uci/caliber-suite/issues/563), [#564](https://github.com/rrahimi-uci/caliber-suite/issues/564), [#607](https://github.com/rrahimi-uci/caliber-suite/issues/607) | 29 |
| G20 Governance/autonomy | Beta + MVP | [#518](https://github.com/rrahimi-uci/caliber-suite/issues/518), [#523](https://github.com/rrahimi-uci/caliber-suite/issues/523), [#548](https://github.com/rrahimi-uci/caliber-suite/issues/548), [#604](https://github.com/rrahimi-uci/caliber-suite/issues/604), [#605](https://github.com/rrahimi-uci/caliber-suite/issues/605), [#632](https://github.com/rrahimi-uci/caliber-suite/issues/632) | 33 |
| G21 Replaceability/OSS | Beta + MVP | [#540](https://github.com/rrahimi-uci/caliber-suite/issues/540), [#558](https://github.com/rrahimi-uci/caliber-suite/issues/558), [#560](https://github.com/rrahimi-uci/caliber-suite/issues/560), [#561](https://github.com/rrahimi-uci/caliber-suite/issues/561), [#602](https://github.com/rrahimi-uci/caliber-suite/issues/602), [#603](https://github.com/rrahimi-uci/caliber-suite/issues/603), [#607](https://github.com/rrahimi-uci/caliber-suite/issues/607) | 38 |
| G22 Regions/federation | Beta + MVP | [#517](https://github.com/rrahimi-uci/caliber-suite/issues/517), [#599](https://github.com/rrahimi-uci/caliber-suite/issues/599), [#600](https://github.com/rrahimi-uci/caliber-suite/issues/600), [#606](https://github.com/rrahimi-uci/caliber-suite/issues/606) | 26 |

### Beta epics and child index

| Milestone / package | Epic | Implementation child issues |
| --- | --- | --- |
| D2 / B02 | [#517](https://github.com/rrahimi-uci/caliber-suite/issues/517) | [#656](https://github.com/rrahimi-uci/caliber-suite/issues/656), [#676](https://github.com/rrahimi-uci/caliber-suite/issues/676), [#680](https://github.com/rrahimi-uci/caliber-suite/issues/680), [#683](https://github.com/rrahimi-uci/caliber-suite/issues/683), [#687](https://github.com/rrahimi-uci/caliber-suite/issues/687), [#691](https://github.com/rrahimi-uci/caliber-suite/issues/691), [#695](https://github.com/rrahimi-uci/caliber-suite/issues/695) |
| D2 / B03 | [#518](https://github.com/rrahimi-uci/caliber-suite/issues/518) | [#657](https://github.com/rrahimi-uci/caliber-suite/issues/657), [#664](https://github.com/rrahimi-uci/caliber-suite/issues/664), [#671](https://github.com/rrahimi-uci/caliber-suite/issues/671), [#684](https://github.com/rrahimi-uci/caliber-suite/issues/684), [#688](https://github.com/rrahimi-uci/caliber-suite/issues/688) |
| D2 / B07 | [#524](https://github.com/rrahimi-uci/caliber-suite/issues/524) | [#692](https://github.com/rrahimi-uci/caliber-suite/issues/692), [#696](https://github.com/rrahimi-uci/caliber-suite/issues/696), [#699](https://github.com/rrahimi-uci/caliber-suite/issues/699), [#704](https://github.com/rrahimi-uci/caliber-suite/issues/704), [#710](https://github.com/rrahimi-uci/caliber-suite/issues/710), [#726](https://github.com/rrahimi-uci/caliber-suite/issues/726) |
| D2 / B11 | [#530](https://github.com/rrahimi-uci/caliber-suite/issues/530) | [#672](https://github.com/rrahimi-uci/caliber-suite/issues/672), [#677](https://github.com/rrahimi-uci/caliber-suite/issues/677), [#658](https://github.com/rrahimi-uci/caliber-suite/issues/658), [#665](https://github.com/rrahimi-uci/caliber-suite/issues/665) |
| D2 / B12 | [#533](https://github.com/rrahimi-uci/caliber-suite/issues/533) | [#659](https://github.com/rrahimi-uci/caliber-suite/issues/659), [#666](https://github.com/rrahimi-uci/caliber-suite/issues/666), [#667](https://github.com/rrahimi-uci/caliber-suite/issues/667), [#947](https://github.com/rrahimi-uci/caliber-suite/issues/947) |
| D2 / B13 | [#536](https://github.com/rrahimi-uci/caliber-suite/issues/536) | [#660](https://github.com/rrahimi-uci/caliber-suite/issues/660), [#668](https://github.com/rrahimi-uci/caliber-suite/issues/668), [#673](https://github.com/rrahimi-uci/caliber-suite/issues/673), [#685](https://github.com/rrahimi-uci/caliber-suite/issues/685) |
| D3 / B04 | [#520](https://github.com/rrahimi-uci/caliber-suite/issues/520) | [#724](https://github.com/rrahimi-uci/caliber-suite/issues/724), [#733](https://github.com/rrahimi-uci/caliber-suite/issues/733), [#741](https://github.com/rrahimi-uci/caliber-suite/issues/741), [#750](https://github.com/rrahimi-uci/caliber-suite/issues/750), [#759](https://github.com/rrahimi-uci/caliber-suite/issues/759), [#768](https://github.com/rrahimi-uci/caliber-suite/issues/768), [#774](https://github.com/rrahimi-uci/caliber-suite/issues/774), [#641](https://github.com/rrahimi-uci/caliber-suite/issues/641) |
| D3 / B05 | [#522](https://github.com/rrahimi-uci/caliber-suite/issues/522) | [#700](https://github.com/rrahimi-uci/caliber-suite/issues/700), [#705](https://github.com/rrahimi-uci/caliber-suite/issues/705), [#711](https://github.com/rrahimi-uci/caliber-suite/issues/711), [#717](https://github.com/rrahimi-uci/caliber-suite/issues/717), [#725](https://github.com/rrahimi-uci/caliber-suite/issues/725), [#734](https://github.com/rrahimi-uci/caliber-suite/issues/734), [#742](https://github.com/rrahimi-uci/caliber-suite/issues/742), [#751](https://github.com/rrahimi-uci/caliber-suite/issues/751), [#760](https://github.com/rrahimi-uci/caliber-suite/issues/760) |
| D3 / B06 | [#523](https://github.com/rrahimi-uci/caliber-suite/issues/523) | [#706](https://github.com/rrahimi-uci/caliber-suite/issues/706), [#712](https://github.com/rrahimi-uci/caliber-suite/issues/712), [#718](https://github.com/rrahimi-uci/caliber-suite/issues/718), [#735](https://github.com/rrahimi-uci/caliber-suite/issues/735), [#743](https://github.com/rrahimi-uci/caliber-suite/issues/743) |
| D3 / B07 | [#525](https://github.com/rrahimi-uci/caliber-suite/issues/525) | [#719](https://github.com/rrahimi-uci/caliber-suite/issues/719), [#736](https://github.com/rrahimi-uci/caliber-suite/issues/736) |
| D3 / B08 | [#526](https://github.com/rrahimi-uci/caliber-suite/issues/526) | [#713](https://github.com/rrahimi-uci/caliber-suite/issues/713), [#720](https://github.com/rrahimi-uci/caliber-suite/issues/720), [#744](https://github.com/rrahimi-uci/caliber-suite/issues/744), [#752](https://github.com/rrahimi-uci/caliber-suite/issues/752), [#761](https://github.com/rrahimi-uci/caliber-suite/issues/761), [#769](https://github.com/rrahimi-uci/caliber-suite/issues/769), [#729](https://github.com/rrahimi-uci/caliber-suite/issues/729), [#730](https://github.com/rrahimi-uci/caliber-suite/issues/730), [#728](https://github.com/rrahimi-uci/caliber-suite/issues/728), [#727](https://github.com/rrahimi-uci/caliber-suite/issues/727) |
| D3 / B11 | [#531](https://github.com/rrahimi-uci/caliber-suite/issues/531) | [#737](https://github.com/rrahimi-uci/caliber-suite/issues/737), [#745](https://github.com/rrahimi-uci/caliber-suite/issues/745) |
| D3 / B12 | [#534](https://github.com/rrahimi-uci/caliber-suite/issues/534) | [#738](https://github.com/rrahimi-uci/caliber-suite/issues/738), [#746](https://github.com/rrahimi-uci/caliber-suite/issues/746) |
| D3 / B13 | [#537](https://github.com/rrahimi-uci/caliber-suite/issues/537) | [#689](https://github.com/rrahimi-uci/caliber-suite/issues/689), [#693](https://github.com/rrahimi-uci/caliber-suite/issues/693), [#697](https://github.com/rrahimi-uci/caliber-suite/issues/697) |
| D3 / B06 | [#589](https://github.com/rrahimi-uci/caliber-suite/issues/589) | [#731](https://github.com/rrahimi-uci/caliber-suite/issues/731), [#739](https://github.com/rrahimi-uci/caliber-suite/issues/739), [#747](https://github.com/rrahimi-uci/caliber-suite/issues/747), [#753](https://github.com/rrahimi-uci/caliber-suite/issues/753), [#762](https://github.com/rrahimi-uci/caliber-suite/issues/762) |
| D4 / B09 | [#527](https://github.com/rrahimi-uci/caliber-suite/issues/527) | [#779](https://github.com/rrahimi-uci/caliber-suite/issues/779), [#784](https://github.com/rrahimi-uci/caliber-suite/issues/784), [#791](https://github.com/rrahimi-uci/caliber-suite/issues/791), [#799](https://github.com/rrahimi-uci/caliber-suite/issues/799), [#805](https://github.com/rrahimi-uci/caliber-suite/issues/805), [#812](https://github.com/rrahimi-uci/caliber-suite/issues/812) |
| D4 / B10 | [#528](https://github.com/rrahimi-uci/caliber-suite/issues/528) | [#701](https://github.com/rrahimi-uci/caliber-suite/issues/701), [#707](https://github.com/rrahimi-uci/caliber-suite/issues/707), [#714](https://github.com/rrahimi-uci/caliber-suite/issues/714), [#721](https://github.com/rrahimi-uci/caliber-suite/issues/721), [#732](https://github.com/rrahimi-uci/caliber-suite/issues/732), [#740](https://github.com/rrahimi-uci/caliber-suite/issues/740) |
| D4 / B12 | [#535](https://github.com/rrahimi-uci/caliber-suite/issues/535) | [#754](https://github.com/rrahimi-uci/caliber-suite/issues/754), [#763](https://github.com/rrahimi-uci/caliber-suite/issues/763), [#770](https://github.com/rrahimi-uci/caliber-suite/issues/770) |
| D4 / B14 | [#538](https://github.com/rrahimi-uci/caliber-suite/issues/538) | [#818](https://github.com/rrahimi-uci/caliber-suite/issues/818), [#826](https://github.com/rrahimi-uci/caliber-suite/issues/826) |
| D5 / B14 | [#539](https://github.com/rrahimi-uci/caliber-suite/issues/539) | [#833](https://github.com/rrahimi-uci/caliber-suite/issues/833), [#840](https://github.com/rrahimi-uci/caliber-suite/issues/840), [#845](https://github.com/rrahimi-uci/caliber-suite/issues/845) |
| D5 / B15 | [#540](https://github.com/rrahimi-uci/caliber-suite/issues/540) | [#850](https://github.com/rrahimi-uci/caliber-suite/issues/850), [#857](https://github.com/rrahimi-uci/caliber-suite/issues/857), [#863](https://github.com/rrahimi-uci/caliber-suite/issues/863), [#869](https://github.com/rrahimi-uci/caliber-suite/issues/869), [#873](https://github.com/rrahimi-uci/caliber-suite/issues/873), [#877](https://github.com/rrahimi-uci/caliber-suite/issues/877) |
| D5 / B16 | [#542](https://github.com/rrahimi-uci/caliber-suite/issues/542) | [#881](https://github.com/rrahimi-uci/caliber-suite/issues/881), [#884](https://github.com/rrahimi-uci/caliber-suite/issues/884), [#886](https://github.com/rrahimi-uci/caliber-suite/issues/886), [#888](https://github.com/rrahimi-uci/caliber-suite/issues/888), [#890](https://github.com/rrahimi-uci/caliber-suite/issues/890), [#851](https://github.com/rrahimi-uci/caliber-suite/issues/851) |
| D5 / B16-Upgrade | [#594](https://github.com/rrahimi-uci/caliber-suite/issues/594) | [#702](https://github.com/rrahimi-uci/caliber-suite/issues/702), [#708](https://github.com/rrahimi-uci/caliber-suite/issues/708), [#715](https://github.com/rrahimi-uci/caliber-suite/issues/715), [#722](https://github.com/rrahimi-uci/caliber-suite/issues/722) |
| D5 / B15-Support | [#595](https://github.com/rrahimi-uci/caliber-suite/issues/595) | [#755](https://github.com/rrahimi-uci/caliber-suite/issues/755), [#764](https://github.com/rrahimi-uci/caliber-suite/issues/764), [#771](https://github.com/rrahimi-uci/caliber-suite/issues/771) |

### MVP epics and child index

| Milestone / package | Epic | Implementation child issues |
| --- | --- | --- |
| D6 / M01 | [#543](https://github.com/rrahimi-uci/caliber-suite/issues/543) | [#661](https://github.com/rrahimi-uci/caliber-suite/issues/661), [#669](https://github.com/rrahimi-uci/caliber-suite/issues/669), [#674](https://github.com/rrahimi-uci/caliber-suite/issues/674), [#678](https://github.com/rrahimi-uci/caliber-suite/issues/678), [#681](https://github.com/rrahimi-uci/caliber-suite/issues/681), [#686](https://github.com/rrahimi-uci/caliber-suite/issues/686) |
| D6 / M02 | [#544](https://github.com/rrahimi-uci/caliber-suite/issues/544) | [#748](https://github.com/rrahimi-uci/caliber-suite/issues/748), [#756](https://github.com/rrahimi-uci/caliber-suite/issues/756), [#765](https://github.com/rrahimi-uci/caliber-suite/issues/765), [#772](https://github.com/rrahimi-uci/caliber-suite/issues/772), [#775](https://github.com/rrahimi-uci/caliber-suite/issues/775), [#780](https://github.com/rrahimi-uci/caliber-suite/issues/780) |
| D6 / M05 | [#547](https://github.com/rrahimi-uci/caliber-suite/issues/547) | [#662](https://github.com/rrahimi-uci/caliber-suite/issues/662), [#670](https://github.com/rrahimi-uci/caliber-suite/issues/670), [#675](https://github.com/rrahimi-uci/caliber-suite/issues/675), [#679](https://github.com/rrahimi-uci/caliber-suite/issues/679), [#682](https://github.com/rrahimi-uci/caliber-suite/issues/682) |
| D6 / M06 | [#548](https://github.com/rrahimi-uci/caliber-suite/issues/548) | [#757](https://github.com/rrahimi-uci/caliber-suite/issues/757), [#766](https://github.com/rrahimi-uci/caliber-suite/issues/766), [#773](https://github.com/rrahimi-uci/caliber-suite/issues/773), [#776](https://github.com/rrahimi-uci/caliber-suite/issues/776), [#781](https://github.com/rrahimi-uci/caliber-suite/issues/781) |
| D6 / M09 | [#551](https://github.com/rrahimi-uci/caliber-suite/issues/551) | [#749](https://github.com/rrahimi-uci/caliber-suite/issues/749), [#758](https://github.com/rrahimi-uci/caliber-suite/issues/758), [#767](https://github.com/rrahimi-uci/caliber-suite/issues/767) |
| D7 / M04 | [#546](https://github.com/rrahimi-uci/caliber-suite/issues/546) | [#785](https://github.com/rrahimi-uci/caliber-suite/issues/785), [#792](https://github.com/rrahimi-uci/caliber-suite/issues/792), [#800](https://github.com/rrahimi-uci/caliber-suite/issues/800), [#806](https://github.com/rrahimi-uci/caliber-suite/issues/806), [#813](https://github.com/rrahimi-uci/caliber-suite/issues/813), [#819](https://github.com/rrahimi-uci/caliber-suite/issues/819) |
| D7 / M07 | [#549](https://github.com/rrahimi-uci/caliber-suite/issues/549) | [#690](https://github.com/rrahimi-uci/caliber-suite/issues/690), [#694](https://github.com/rrahimi-uci/caliber-suite/issues/694), [#698](https://github.com/rrahimi-uci/caliber-suite/issues/698), [#703](https://github.com/rrahimi-uci/caliber-suite/issues/703), [#709](https://github.com/rrahimi-uci/caliber-suite/issues/709), [#716](https://github.com/rrahimi-uci/caliber-suite/issues/716), [#723](https://github.com/rrahimi-uci/caliber-suite/issues/723) |
| D7 / M09 | [#552](https://github.com/rrahimi-uci/caliber-suite/issues/552) | [#820](https://github.com/rrahimi-uci/caliber-suite/issues/820), [#827](https://github.com/rrahimi-uci/caliber-suite/issues/827), [#834](https://github.com/rrahimi-uci/caliber-suite/issues/834), [#841](https://github.com/rrahimi-uci/caliber-suite/issues/841) |
| D7 / M10 | [#553](https://github.com/rrahimi-uci/caliber-suite/issues/553) | [#821](https://github.com/rrahimi-uci/caliber-suite/issues/821), [#846](https://github.com/rrahimi-uci/caliber-suite/issues/846) |
| D7 / M11 | [#555](https://github.com/rrahimi-uci/caliber-suite/issues/555) | [#786](https://github.com/rrahimi-uci/caliber-suite/issues/786), [#793](https://github.com/rrahimi-uci/caliber-suite/issues/793), [#801](https://github.com/rrahimi-uci/caliber-suite/issues/801) |
| D7 / M14 | [#560](https://github.com/rrahimi-uci/caliber-suite/issues/560) | [#777](https://github.com/rrahimi-uci/caliber-suite/issues/777), [#782](https://github.com/rrahimi-uci/caliber-suite/issues/782), [#787](https://github.com/rrahimi-uci/caliber-suite/issues/787), [#794](https://github.com/rrahimi-uci/caliber-suite/issues/794) |
| D7 / M10 | [#592](https://github.com/rrahimi-uci/caliber-suite/issues/592) | [#828](https://github.com/rrahimi-uci/caliber-suite/issues/828), [#835](https://github.com/rrahimi-uci/caliber-suite/issues/835), [#842](https://github.com/rrahimi-uci/caliber-suite/issues/842) |
| D8 / M03 | [#545](https://github.com/rrahimi-uci/caliber-suite/issues/545) | [#788](https://github.com/rrahimi-uci/caliber-suite/issues/788), [#795](https://github.com/rrahimi-uci/caliber-suite/issues/795), [#802](https://github.com/rrahimi-uci/caliber-suite/issues/802), [#807](https://github.com/rrahimi-uci/caliber-suite/issues/807), [#814](https://github.com/rrahimi-uci/caliber-suite/issues/814) |
| D8 / M08 | [#550](https://github.com/rrahimi-uci/caliber-suite/issues/550) | [#778](https://github.com/rrahimi-uci/caliber-suite/issues/778), [#783](https://github.com/rrahimi-uci/caliber-suite/issues/783), [#789](https://github.com/rrahimi-uci/caliber-suite/issues/789), [#796](https://github.com/rrahimi-uci/caliber-suite/issues/796), [#803](https://github.com/rrahimi-uci/caliber-suite/issues/803) |
| D8 / M11 | [#556](https://github.com/rrahimi-uci/caliber-suite/issues/556) | [#808](https://github.com/rrahimi-uci/caliber-suite/issues/808), [#815](https://github.com/rrahimi-uci/caliber-suite/issues/815) |
| D8 / M13 | [#559](https://github.com/rrahimi-uci/caliber-suite/issues/559) | [#809](https://github.com/rrahimi-uci/caliber-suite/issues/809), [#816](https://github.com/rrahimi-uci/caliber-suite/issues/816), [#822](https://github.com/rrahimi-uci/caliber-suite/issues/822), [#829](https://github.com/rrahimi-uci/caliber-suite/issues/829), [#836](https://github.com/rrahimi-uci/caliber-suite/issues/836) |
| D8 / M15 | [#562](https://github.com/rrahimi-uci/caliber-suite/issues/562) | [#823](https://github.com/rrahimi-uci/caliber-suite/issues/823), [#830](https://github.com/rrahimi-uci/caliber-suite/issues/830), [#837](https://github.com/rrahimi-uci/caliber-suite/issues/837) |
| D9 / M10 | [#554](https://github.com/rrahimi-uci/caliber-suite/issues/554) | [#852](https://github.com/rrahimi-uci/caliber-suite/issues/852), [#858](https://github.com/rrahimi-uci/caliber-suite/issues/858), [#864](https://github.com/rrahimi-uci/caliber-suite/issues/864) |
| D9 / M11 | [#557](https://github.com/rrahimi-uci/caliber-suite/issues/557) | [#824](https://github.com/rrahimi-uci/caliber-suite/issues/824), [#831](https://github.com/rrahimi-uci/caliber-suite/issues/831), [#838](https://github.com/rrahimi-uci/caliber-suite/issues/838), [#843](https://github.com/rrahimi-uci/caliber-suite/issues/843) |
| D9 / M12 | [#558](https://github.com/rrahimi-uci/caliber-suite/issues/558) | [#847](https://github.com/rrahimi-uci/caliber-suite/issues/847), [#853](https://github.com/rrahimi-uci/caliber-suite/issues/853), [#859](https://github.com/rrahimi-uci/caliber-suite/issues/859), [#865](https://github.com/rrahimi-uci/caliber-suite/issues/865), [#870](https://github.com/rrahimi-uci/caliber-suite/issues/870), [#874](https://github.com/rrahimi-uci/caliber-suite/issues/874), [#878](https://github.com/rrahimi-uci/caliber-suite/issues/878) |
| D9 / M14 | [#561](https://github.com/rrahimi-uci/caliber-suite/issues/561) | [#844](https://github.com/rrahimi-uci/caliber-suite/issues/844), [#848](https://github.com/rrahimi-uci/caliber-suite/issues/848), [#854](https://github.com/rrahimi-uci/caliber-suite/issues/854) |
| D9 / M15 | [#563](https://github.com/rrahimi-uci/caliber-suite/issues/563) | [#860](https://github.com/rrahimi-uci/caliber-suite/issues/860), [#866](https://github.com/rrahimi-uci/caliber-suite/issues/866) |
| D10 / M23 | [#604](https://github.com/rrahimi-uci/caliber-suite/issues/604) | [#810](https://github.com/rrahimi-uci/caliber-suite/issues/810), [#817](https://github.com/rrahimi-uci/caliber-suite/issues/817), [#825](https://github.com/rrahimi-uci/caliber-suite/issues/825), [#832](https://github.com/rrahimi-uci/caliber-suite/issues/832), [#839](https://github.com/rrahimi-uci/caliber-suite/issues/839) |
| D10 / M24 | [#605](https://github.com/rrahimi-uci/caliber-suite/issues/605) | [#790](https://github.com/rrahimi-uci/caliber-suite/issues/790), [#797](https://github.com/rrahimi-uci/caliber-suite/issues/797), [#804](https://github.com/rrahimi-uci/caliber-suite/issues/804), [#811](https://github.com/rrahimi-uci/caliber-suite/issues/811) |
| D10 / M25 | [#606](https://github.com/rrahimi-uci/caliber-suite/issues/606) | [#855](https://github.com/rrahimi-uci/caliber-suite/issues/855), [#861](https://github.com/rrahimi-uci/caliber-suite/issues/861), [#867](https://github.com/rrahimi-uci/caliber-suite/issues/867), [#871](https://github.com/rrahimi-uci/caliber-suite/issues/871), [#875](https://github.com/rrahimi-uci/caliber-suite/issues/875), [#879](https://github.com/rrahimi-uci/caliber-suite/issues/879), [#882](https://github.com/rrahimi-uci/caliber-suite/issues/882) |
| D10 / M27 | [#632](https://github.com/rrahimi-uci/caliber-suite/issues/632) | [#663](https://github.com/rrahimi-uci/caliber-suite/issues/663), [#798](https://github.com/rrahimi-uci/caliber-suite/issues/798), [#849](https://github.com/rrahimi-uci/caliber-suite/issues/849), [#856](https://github.com/rrahimi-uci/caliber-suite/issues/856), [#862](https://github.com/rrahimi-uci/caliber-suite/issues/862), [#868](https://github.com/rrahimi-uci/caliber-suite/issues/868), [#872](https://github.com/rrahimi-uci/caliber-suite/issues/872), [#876](https://github.com/rrahimi-uci/caliber-suite/issues/876), [#880](https://github.com/rrahimi-uci/caliber-suite/issues/880) |
| D11 / M17 | [#598](https://github.com/rrahimi-uci/caliber-suite/issues/598) | [#883](https://github.com/rrahimi-uci/caliber-suite/issues/883), [#885](https://github.com/rrahimi-uci/caliber-suite/issues/885), [#887](https://github.com/rrahimi-uci/caliber-suite/issues/887), [#889](https://github.com/rrahimi-uci/caliber-suite/issues/889), [#891](https://github.com/rrahimi-uci/caliber-suite/issues/891), [#892](https://github.com/rrahimi-uci/caliber-suite/issues/892), [#893](https://github.com/rrahimi-uci/caliber-suite/issues/893), [#894](https://github.com/rrahimi-uci/caliber-suite/issues/894), [#895](https://github.com/rrahimi-uci/caliber-suite/issues/895) |
| D12 / M18 | [#599](https://github.com/rrahimi-uci/caliber-suite/issues/599) | [#896](https://github.com/rrahimi-uci/caliber-suite/issues/896), [#897](https://github.com/rrahimi-uci/caliber-suite/issues/897), [#898](https://github.com/rrahimi-uci/caliber-suite/issues/898), [#899](https://github.com/rrahimi-uci/caliber-suite/issues/899), [#900](https://github.com/rrahimi-uci/caliber-suite/issues/900), [#901](https://github.com/rrahimi-uci/caliber-suite/issues/901) |
| D13 / M21 | [#602](https://github.com/rrahimi-uci/caliber-suite/issues/602) | [#902](https://github.com/rrahimi-uci/caliber-suite/issues/902), [#904](https://github.com/rrahimi-uci/caliber-suite/issues/904), [#906](https://github.com/rrahimi-uci/caliber-suite/issues/906), [#908](https://github.com/rrahimi-uci/caliber-suite/issues/908), [#910](https://github.com/rrahimi-uci/caliber-suite/issues/910), [#912](https://github.com/rrahimi-uci/caliber-suite/issues/912), [#914](https://github.com/rrahimi-uci/caliber-suite/issues/914), [#916](https://github.com/rrahimi-uci/caliber-suite/issues/916) |
| D14 / M19 | [#600](https://github.com/rrahimi-uci/caliber-suite/issues/600) | [#903](https://github.com/rrahimi-uci/caliber-suite/issues/903), [#905](https://github.com/rrahimi-uci/caliber-suite/issues/905), [#907](https://github.com/rrahimi-uci/caliber-suite/issues/907), [#909](https://github.com/rrahimi-uci/caliber-suite/issues/909), [#911](https://github.com/rrahimi-uci/caliber-suite/issues/911), [#913](https://github.com/rrahimi-uci/caliber-suite/issues/913) |
| D14 / M20 | [#601](https://github.com/rrahimi-uci/caliber-suite/issues/601) | [#918](https://github.com/rrahimi-uci/caliber-suite/issues/918), [#920](https://github.com/rrahimi-uci/caliber-suite/issues/920), [#922](https://github.com/rrahimi-uci/caliber-suite/issues/922), [#923](https://github.com/rrahimi-uci/caliber-suite/issues/923) |
| D15 / M16 | [#564](https://github.com/rrahimi-uci/caliber-suite/issues/564) | [#930](https://github.com/rrahimi-uci/caliber-suite/issues/930), [#931](https://github.com/rrahimi-uci/caliber-suite/issues/931), [#932](https://github.com/rrahimi-uci/caliber-suite/issues/932), [#933](https://github.com/rrahimi-uci/caliber-suite/issues/933), [#934](https://github.com/rrahimi-uci/caliber-suite/issues/934) |
| D15 / M22 | [#603](https://github.com/rrahimi-uci/caliber-suite/issues/603) | [#915](https://github.com/rrahimi-uci/caliber-suite/issues/915), [#917](https://github.com/rrahimi-uci/caliber-suite/issues/917), [#919](https://github.com/rrahimi-uci/caliber-suite/issues/919), [#921](https://github.com/rrahimi-uci/caliber-suite/issues/921) |
| D15 / M26 | [#607](https://github.com/rrahimi-uci/caliber-suite/issues/607) | [#924](https://github.com/rrahimi-uci/caliber-suite/issues/924), [#925](https://github.com/rrahimi-uci/caliber-suite/issues/925), [#926](https://github.com/rrahimi-uci/caliber-suite/issues/926), [#927](https://github.com/rrahimi-uci/caliber-suite/issues/927), [#928](https://github.com/rrahimi-uci/caliber-suite/issues/928), [#929](https://github.com/rrahimi-uci/caliber-suite/issues/929) |
<!-- GENERATED_INDEX_END -->

## Final critic pass and evidence boundary

The final pass checks gap coverage, implemented-work reuse, release coherence, explicit child scope, task DAG and native links,
observable tests/acceptance, documentation, metadata and historical-item disposition. Corrections include immutable policy pins,
at-least-once outbox semantics, fail-closed approval, occurrence-aware effects, canonical trace aliases, no 404-as-final-failure,
non-waivable critical gates, signed archive checkpoints, safe sandbox transport, versioned evidence and retained identity scope.

The PR validation record contains executed checks and live GitHub reconciliation counts. This review validates the **plan**;
it does not certify that future feature tests already pass, that staffing is booked, or that Beta/MVP is implemented.

Re-run `node docs/planning/validate-execution-plan.mjs` for local manifest/gap/full-DAG validation.
With authorized read access, add `--live` to compare issue scope, metadata, native hierarchy/dependencies,
project fields/readiness and milestones. Both modes are read-only; live checks are not an ordinary CI dependency.
