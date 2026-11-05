# CALIBER plan audit — 4 November 2026

> **Archived planning record — retired 4 November 2026.** [GitHub Project #3](https://github.com/users/rrahimi-uci/projects/3) and its linked Issues are the sole current planning authority for scope, milestones, dependencies, priorities, acceptance criteria and progress.
> Everything below records historical planning and architectural rationale, including statements that formerly called this document or its manifest canonical. Those statements are no longer operational instructions. Preserve evidence and issue links; do not synchronize this archive back to GitHub or infer current readiness from it.
> Use the repository agent entry points and canonical `.agents/` workflows for current work. See [archive tooling and migration notes](PLANNING_ARCHIVE.md).

## Historical record

The release sequence is useful, but the previous plan mixed implementation completion, release acceptance, future product decisions and calendar promises. The corrected [manifest](beta-mvp-execution.json) contains 58 epics, 296 tasks, 15 D-controls and 22 gap categories. Five already-open GitHub tasks are now canonical: #1051, #1086–#1088 and #1110. The first four belong to Preview 2; #1110 is Beta CI maintenance and cannot block release when the complete required suite already passes. No feature was silently dropped and no issue was closed by this audit.

The review covered every manifest task's scope, files, tests, acceptance and dependencies, every epic decision, control and current GitHub relationship. Source/test inspection checked the risky integration boundaries listed below against main `189e1113a121e73555df15badd61e415d9796ab0`. This is a planning and contract audit, not an independent security review or a certification that all existing implementations meet revised requirements. Closed delivery history remains closed; any newly demonstrated runtime gap needs an explicit regression issue or reopening decision.

## Findings and corrections

| Finding | Consequence | Corrected contract |
| --- | --- | --- |
| D-controls did not require the preceding control; #609 required closing the whole-program #608 staffing issue. | Later releases could appear accepted while earlier guarantees were unproven, and future staffing could deadlock Preview 1. | D2–D15 now explicitly depend on their predecessor. D1 requires its own foundation evidence and H01, not closure of every future decision. |
| Five open tasks were outside the canonical plan; #1051 overlaps already-merged governed-context work. | Board totals hid required integration work and encouraged duplicate implementation. | Four D3 tasks are required B04 children with exact remaining caller ownership. #1110 is a B16 nonblocking maintenance child. #1051 audits existing paths first; EP-017/081 ownership is not duplicated in #1088. |
| #823 consumed the support-ticket result before #837 defined its acceptance predicate. | Agents would invent incompatible success rules. | #814 produces recovery state, #837 defines the predicate, #823 implements the scenario and #830 verifies the joined public path. |
| Identity #883 depended on governance UI #880; memory #918 depended on Hub UI #916; feedback API #852 depended on a live trial/UI. | Artificial UI/external blockers serialized independent backend work. | Identity follows the actual organization/grant producers; memory follows identity/revocation; feedback API follows its backend protocol. Their release gates still require the complete UI/trial evidence. |
| #847/#865 could approve targets after observing performance; the independent pilot preceded a proven envelope. | A result could define its own passing threshold. | Approve units/workload/SLO/RPO/RTO/cost targets before measurement. #843 follows #838/#847; #860 follows the scenario predicate and measured operational envelope. |
| #772 used a broad random canary proportion tolerance; #670 allowed missing object storage to skip required checks; #723 relied on test order. | Flaky or unexecuted tests could appear green. | Exact hash/weight boundary fixtures; owned isolated object-storage fixture with startup failure; per-test sandbox ownership/cleanup with isolated and shuffled runs. |
| #796 used a queue-health snapshot for a capacity decision. | Concurrent producers could exceed the cap. | Transactional admission/reservation, refusal at `>= cap`, and an already-authorized idempotent retry at capacity; PostgreSQL races prove the boundary. Observability remains a read model. |
| #809 could include retry attempt scope in child identity. | A retry could create a second child and reserve budget twice. | Stable parent/step/logical-occurrence identity; retries preserve the same child and authority. |
| #703/#776 could imply direct broker or compensation dispatch; #795 referenced a legacy ledger. | A new transport could bypass common approval/authority or test the wrong path. | Protected writes use the common dispatcher/approval and current production effect records; stop does not grant compensation authority. |
| #813 deferred read denial until asynchronous deletion; #824 could expose database credentials in process arguments. | Revoked data could remain readable and credentials could leak. | Deny reads immediately at logical DELETE, then drain physical cleanup; holds apply to retained archive evidence only. Pass restore credentials via protected environment/file. |
| #838 forced downgrade rather than describing a supported rollback; #899 assumed uniform tenant/use-case binding columns. | Unsafe migration reversal or a fictitious schema retrofit. | Supported previous binary or certified coordinated restore; preserve actual resource binding keys and version/digest CAS. |
| #892/#893 were broad identity tasks without a bounded provider/profile handoff. | An agent could build an arbitrary login/SCIM subset and claim enterprise support. | Ordered OIDC units, issuer/subject identity, scripted negative cases, explicit SCIM discovery/Users/Groups/CRUD/PATCH/filter/page profile, atomic refusals and administrator-owned mappings. H07 supplies the actual IdP/connector. |
| Hub extraction conflated spans with factual verification and used inconsistent verdict/gate language. | Extracted content could authorize its own release. | #904/#906/#908/#910 share `verified`, `contradicted`, `insufficient`; missing evidence may persist only as insufficient. Ontology #910 precedes final gate #908; independent evidence and critical-field rules are required. |
| #857/#869 invented an `accepted` run lifecycle status; #811 left promotion/waiver outcomes vague. | New incompatible state machines or critical-control bypass. | Preserve completed lifecycle plus `acceptance.status=accepted`; enumerate promotion outcomes and deny critical waivers. |
| #909/#917/#871 referred to unnamed counts of errors/cases. | A low-cost agent would guess the required cases. | Enumerated named failure cases and observable refusal/no-effect expectations in each task. |
| #928 recursively ran pytest; #929/#866 relied on vague grep evidence; #933 could tag before validation; #934 implied ready before final decision. | Self-referential tests and unaudited claims could publish a release. | One external acceptance runner; explicit provenance/link/claim checks; build/test/sign exact candidate bytes, PASS before tag/publication, and HOLD while evidence remains incomplete. |
| 133 original tasks repeated scope as acceptance, including 80 open tasks. | “Implemented the scope” was not an observable completion test. | All 80 open instances now have concrete positive/negative outcome criteria. Every task has dependencies, ordered units, boundaries, test targets/commands, QA, evidence ownership and stop conditions. Closed historical acceptance is not retroactive recertification. |
| Readiness was stale for 50 original tasks; a coding blocker and a future trial approval looked identical. | Work was hidden or started without a product decision. | Closed → Done; pending start decision → Needs input; open producer → Blocked; otherwise Ready. Separate Human input field and `decision:pending` label retain pending acceptance decisions. |
| Multiple children could overwrite the same D acceptance report. | Parallel agents could lose evidence or claim another task's acceptance. | Each child owns a numbered fragment; only the control owns the consolidated SHA-bound PASS/HOLD report. |

The bounded SCIM contract uses the protocol's discovery, resource operations, errors and filtering concepts from [RFC 7644](https://www.rfc-editor.org/rfc/rfc7644.html) and resource schema rules from [RFC 7643](https://www.rfc-editor.org/rfc/rfc7643.html). These references do not imply that every optional feature or every identity provider is supported; the declared profile and actual selected connector must be tested.

Subsequent cadence update: the [product roadmap](PRODUCT_ROADMAP.md) extends the three-week preview pattern through Pilot and MVP. The duration ranges below record the audit baseline; the new cadence requires a fresh capacity calculation and does not inherit those dates.

## Scope, sequencing and timeline critique

Keep Alpha → Preview 1 → Preview 2 → Preview 3 → Beta → Pilot → MVP. Each Preview has two development weeks plus one QA/bug-bash/fix/release week. At the unconfirmed historical 1.8 planned person-weeks per week, a Preview has about 3.6 base person-weeks of development capacity. Issue count is not an estimate. Existing closed work reduces implementation demand but still needs joined acceptance on the release candidate; the new D3 follow-ups and route/refuse decision can consume a cycle. Do not compress the QA week to hide unfinished work.

The [roadmap](PREVIEW_TO_MVP_RELEASE_TRAIN.md) retains a 6–10 week Preview-3-to-Beta range, conditional on remaining work, independent review and live-trial bookings. The main D5 report is HOLD, not a release promise. Tests or documents prepared for an independent reviewer are not that reviewer's sign-off.

The retained Beta-to-Pilot estimate of 36–44 weeks includes M01–M15 implementation and integration, not merely user testing. Reserve a real 4–6 week controlled operator trial after the operating envelope and restore path are proven. The retained Pilot-to-MVP estimate of 72–96 weeks includes identity, regional cells, Hub, memory, federation, catalog, governance and autonomy. Calling that a minimum product obscures its breadth. These historical effort-based ranges are useful budget warnings, not validated forecasts or due dates.

H16 requires an explicit product/funding choice before committing post-Beta dates. The recommended product discussion is whether a smaller supported single-installation release meets the first users' need, with enterprise breadth tracked separately. Its exact scope and duration require actual pilot requirements and a fresh remaining-effort estimate; this audit does not fabricate a short estimate or silently reclassify retained requirements. H01 supplies named capacity, measured throughput and the integration owner. No calendar dates are set until that reforecast.

Milestone controls now state concrete outcomes and cumulative QA. Producing task edges permit safe early work across administrative milestone labels; acceptance still occurs in order. Two parallel implementation streams and one integration/review stream are assumptions, not three confirmed engineers. Shared runtime, models, server routes, migrations and generated-client work must be coordinated. Large identity, revocation, regional and final-artifact tasks have ordered independently tested PR units; the acceptance container remains open until every unit is complete.

## Human decisions

The canonical decision register below is also published in [#608](https://github.com/rrahimi-uci/caliber-suite/issues/608). None is represented as approved. Start inputs block implementation; acceptance inputs block the named configuration/trial/release while allowing deterministic preparatory work. H16 is a planning commitment decision rather than a Beta feature gate.

| ID / owner | Input required | Needed by |
| --- | --- | --- |
| H01 — Release owner | Confirm named near-term staffing, integration owner, capacity and first Preview kickoff; reforecast later phases at each release. | D1 |
| H02 — Domain owner and release owner | Approve Beta corpus/critical thresholds, real-provider scope, spend cap and abort rule before the live trial. | D5 |
| H03 — Release owner and independent security reviewer | Name/book the independent Beta reviewer and accept resolved-finding evidence. | D5 |
| H04 — Product owner | Choose routed or refused for each EP-009/010/013/051/059/070/071/072/075/076/077; routed choices need exact connector contracts and producer tasks. | D3 |
| H05 — Domain owner and operations owner | Approve Pilot load/SLO/RPO/RTO/cost/alert targets with units and workload before measurement; do not derive requirements from results. | D6 |
| H06 — Release owner and independent operators | Book independent operator/user, two use cases, approved data and a 4–6 week Pilot trial with severity/abort/support rules. | D7 |
| H07 — Identity administrator | Select the supported IdP, issuer/client/redirects, SCIM connector profile and explicit group-to-project-role mappings. | D10 |
| H08 — Operations and data owner | Approve allowed residency/recovery locations, reference host capacity and regional RPO/RTO targets before the drill. | D11 |
| H09 — Domain owner | Approve frozen Hub corpus, typed ontology/normalization, blocking fields and independent validation thresholds. | D12 |
| H10 — Data owner | Approve memory consent basis, purpose allowlist, TTL and archive-retention/hold policy. | D13 |
| H11 — Security and integration owner | Approve peer identities/keys, origins, local project mappings and permitted federation skills. | D13 |
| H12 — Release owner | Supply an independently authored adapter with pinned source/license and author evidence; a team stub cannot satisfy certification. | D14 |
| H13 — Book repository owner | Authorize the exact cross-repository patch and confirm whether publication is required for the final companion claim. | D14 |
| H14 — Release owner and independent final reviewer | Approve final independent security findings, residual lower risks and PASS/HOLD on the exact candidate artifact hashes. | D15 |
| H15 — Domain owner | Approve the Pilot real-provider held-out protocol, thresholds, spend cap and abort rule. | D6 |
| H16 — Product and release owner | Before committing post-Beta dates, explicitly fund the retained enterprise Pilot/MVP scope or approve a separately specified reduced-scope product release. Do not call the retained 72–96 week enterprise program a minimum product or silently remove features. | D5 reforecast; does not block Beta feature acceptance |

## Verification and limits

The offline validator checks every task's handoff, release mapping, human references, required scope and the complete task/epic/control DAG. Regression tests reject the corrected unsafe contracts and missing producing edges. The live validator checks GitHub briefs, native parents/blockers, labels, project fields and controls against the manifest. These are plan-integrity tests, not proof that planned runtime features are implemented.

GitHub issue states, external approvals and CI can change after a snapshot. Refresh them at task kickoff and release cut. The versioned corrections use a PR; live Project #3 metadata is synchronized as requested. Repository changes remain unmerged for the owner's final review under the supplied stricter policy. The PR records exact executed validation and any remaining CI/environment limitations.
