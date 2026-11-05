# CALIBER scalability implementation proposal

**Status:** proposed engineering work, not implemented capability or a release commitment.

**Baseline:** `1b4322a2938c459db9af0976472637272ae35e8c`, reviewed 2026-11-05.

**Design:** [scalability architecture and evidence](../architecture/scalability-arch.md).

The architecture's [critique record](../architecture/scalability-arch.md#5-document-critique-and-correction-record) maps corrected proposal defects R01–R10 to the contracts and phase gates below. These are documentation corrections; implementation remains proposed.

This document is the requested implementation breakdown. The [live GitHub Project](https://github.com/users/rrahimi-uci/projects/3) and linked issues remain authoritative for scheduling, ownership, dependencies, acceptance decisions and progress. Before execution, map these slices to existing tasks and create only missing, nonduplicate work. Do not update frozen local release schedules or infer a task is complete from a closed issue without checking its integrated runtime path.

## 1. Delivery strategy and dependency gates

Preserve the current modular application and PostgreSQL queue foundation. Correct failover behavior before enabling parallel workers. Separate process roles before autoscaling. Keep unsupported worker families under one control owner until individually qualified. All new file names and configuration contracts below are proposed unless explicitly described as existing.

```mermaid
flowchart LR
    P0[P0.1 scope and P0.2 evidence] --> P1[P1 safe replica foundation]
    P0 --> P2[P2 ownership and recovery correctness]
    P1 --> P3[P3 independent workflow workers]
    P2 --> P3
    P3 --> P4[P4 schedules delivery and family qualification]
    P4 --> P5[P5 Kubernetes and operational acceptance]
    P5 --> P6[P6 measured optimizations]
    Decisions[P0.3 and P0.4 acceptance decisions] -.-> P3
    Decisions -.-> P4
    Decisions -.-> P5
```

P1/P2 implementation depends on inspected scope, the relevant authority contract and reproducible evidence from P0.1/P0.2, not completion of every P0 decision. P0.3 target approval gates performance acceptance; P0.4 schedule policy gates P4.1 acceptance, and infrastructure decisions gate P5 deployment. Pending decisions therefore do not block independent deterministic safety fixes. Shared edits to `server.py`, configuration, models, migrations and runtime must be serialized. P1.6 is required before P2/P3 production cutover; the existing Pilot [maintenance and migration-preflight task #808](https://github.com/rrahimi-uci/caliber-suite/issues/808) owns that contract. P2 code and tests may develop earlier. P3 requires the applicable P1/P2 integrated contracts. P4 slices may begin against their merged producer contracts; P6 remains optional.

| Phase | Deliverable / release boundary | Gap IDs |
| --- | --- | --- |
| P0 | Agreed support matrix, regression inventory and target-envelope decision record | G01–G12 evidence and decisions |
| P1 | Consistent API replicas, shared state, mutation/migration barriers and webhook intake before role separation; no parallel execution claim | G01, G08–G11 foundations |
| P2 | Correct claims, stale-write exclusion and reachable recovery in current execution paths | G02–G06; G10 publication |
| P3 | Bounded independent workflow workers with migrated effect authority and durable commands | G01, G05, G08, G12 |
| P4 | Durable schedule/wake/delivery behavior and explicit qualification of other worker families | G04, G06, G07, G09 |
| P5 | Measured single-region Kubernetes operating envelope and upgrade/recovery evidence | G11, G12 and end-to-end confirmation |
| P6 | Optional performance optimizations justified by measurements | Residual capacity bottlenecks |

These phases are dependency boundaries, not equally sized calendar cycles. P2 and P4 contain multiple independently reviewable slices. Assign dates only after the current Project capacity and P0 decisions are known; do not fit all correctness work into an arbitrary three-week release.

### Common execution and acceptance contract

Every implementation slice must specify an observable outcome, changed interfaces, owned files, prerequisites and non-goals in its GitHub issue. Use the repository PR template and applicable engineering policy. Keep each PR coherent and update public API/SDK/UI consumers when a contract changes. Preserve old supported readers before enabling new writers. Use new Alembic revisions after merged predecessors, never rewrite shipped migrations.

Run applicable CI-equivalent checks from [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) and [`scripts/ci-local.sh`](../../scripts/ci-local.sh), including real PostgreSQL migration/concurrency tests where required. Run `git diff --check`. Behavioral changes require meaningful tests and measured **greater than 95% affected-code coverage**, with line/branch results and exclusions disclosed. A focused `--no-cov` command is not coverage evidence. Documentation-only slices use docs/link/format checks and explain why behavioral coverage is inapplicable. Use `./test-all.sh` when the change spans components and that is the applicable comprehensive path.

Evidence must identify candidate SHA/image digest, database/version, configuration and enabled families, exact commands, PASS/FAIL/SKIP, scenario results and artifact location. A skipped required PostgreSQL, browser, deployment or fault check is not a pass. Self-review and parallel source inspection are not independent GitHub approval. Observe the current owner-review/merge policy on each PR; this plan grants no additional merge or deployment authority.

## 2. P0 — Establish contracts and reproduce safety gaps

**Outcome:** implementation starts from an inspected topology, explicit correctness oracles and tracked decisions. Approval of performance inputs is a later acceptance gate, not a prerequisite for unrelated safety fixes. Architecture sections 2–3 provide the source baseline, not reproduced cluster evidence.

**Dependencies:** none beyond repository/Project discovery. Performance acceptance requires the named owner decision in [#847](https://github.com/rrahimi-uci/caliber-suite/issues/847); missing approval does not prevent deterministic regression work.

**Slices and likely files:**

| Slice | Work and boundaries | Files / existing tracking |
| --- | --- | --- |
| P0.1 | Record every mounted write/long-running API and enabled worker; classify inline/queued/owner-only, effect boundary, storage, claim and recovery. Distinguish production workflow-node HTTP verifier wiring from agent-step governance and adapters tested with injected contexts. Decide which families remain singleton or disabled in the first scalable profile. | `caliber/src/caliber/server.py`, `routes/`, `orchestrator/`, `assistant/executor.py`, `knowledge/`; architecture inventory; live Project issues |
| P0.2 | Record deterministic reproductions at actual worker/route entry points for G02–G06, with candidate/command/failure evidence on the fixing issue or PR. Merge regression tests with their fixes; reusable green harness changes may land separately. A merged failing test is never a prerequisite. | Existing `test_workflow_run_worker.py`, `test_effect_reconcile.py`, `test_workspace_imports.py`, `test_aria_async.py`, knowledge/release/refinement tests; proposed dedicated PostgreSQL race fixtures |
| P0.3 | Define target-envelope schema and obtain domain/operations approval before load acceptance. Record work mix, resource ceilings, latency/error/cost targets, RPO/RTO and detection time, with units/window/owner/date. | #847, proposed `docs/operate/operating-envelope.md`; retain existing control evidence ownership |
| P0.4 | Record owners and exact acceptance gates for misfire policy, connector risks and infrastructure/upgrade decisions; resolve each before its dependent feature is enabled. Map regressions to existing issue histories without reopening completed work wholesale. | Architecture section 4.8; #789 capacity, #783 retry authority, #896 regional decision; #996/#997 park/reconcile history, #721 release recovery history |

**Migration/configuration:** no production migration. Fixtures need independent PostgreSQL sessions/processes and barriers; do not depend on a developer's local clock or private provider credentials.

**Tests and QA:** demonstrate old-worker return after takeover; renewal racing recovery; two resume calls crossing claim; effect settlement before park and crash before wake; import lost-lease transaction rollback; release replacement poller discovery; knowledge stale activation; Aria continuation crash; refinement janitor versus long evaluation. Record which failures were reproduced versus only statically identified.

**Acceptance:** every enabled family and write route has a disposition; each G02–G06 case has a failing regression oracle or documented evidence limit and assigned fixing slice. Target fields are versioned and complete before performance testing; unapproved numeric targets remain **HOLD**, never substituted with post hoc thresholds. All human decisions have a named role and affected gate.

Verify the dependency contract with target-envelope, schedule and infrastructure decisions still pending: at least the isolated P2 ownership/regression slices remain eligible for implementation, while their dependent performance/deployment acceptance stays blocked. Feature-scope approval is required before enabling or advertising a family, not to investigate its current defect.

**Rollout/rollback:** no runtime rollout. If the baseline changes, rebase the evidence and recheck affected findings. Do not turn this proposal into a second release-status database.

## 3. P1 — Safe replica and deployment foundations

**Outcome:** separate API/control lifecycle and consistent shared state, while keeping execution owner-bound. This phase alone does not qualify multi-pod failover or independent worker throughput; P2 safety fixes remain a gate.

**Dependencies:** P0 route/worker support matrix. Coordinate readers-before-writers changes with P2 migration ownership.

### Implementation slices

| Slice | Expected change and boundary | Likely components/files |
| --- | --- | --- |
| P1.1 Role contract | Add explicit API/control/combined-development modes and role-specific startup/shutdown. API starts no execution-owner loops but retains required notification tasks. Control exposes only health and a private authenticated operational route surface during the bridge, not general user routes. Fail startup on unsafe profile/role combinations. Retain database global owner for control. | `server.py`, `execution_role.py`, `config.py`, `profile.py`, `deploy/caliber/entrypoint.sh`; proposed worker/control entry point |
| P1.2 Request isolation | Classify blocking calls; move whole synchronous units off the event loop with bounded concurrency and per-operation sessions. Preserve responses. Route owner-bound operations to the active control endpoint until P3 commands exist; an owner-sensitive private readiness/discovery mechanism excludes standby from that Service while preserving separate standby health. Keep handler-level fencing on every request. | `routes/assistant.py`, assistant service, `routes/system_effects.py`, other audited route owners; API/SDK contracts only where needed |
| P1.3 Shared configuration and limits | Persist versioned assistant overrides; authorize/audit writes; refresh replicas by committed version. Define shared generic request limits without changing the existing SQL service limiter. Pin execution settings at admission; mutable revocation/kill switches are checked at use and cannot be overridden by an old pin or stale cache. | `routes/assistant.py`, `assistant/`, `rate_limit.py`, `routes/services.py`, `db/models.py`, new migrations; settings UI/client tests |
| P1.4 Storage eligibility | Inventory per-project local references; add a resumable checksum-verified copy/verification tool and safe switch procedure to shared storage. Multi-pod mode refuses unmigrated authoritative local references. Preserve existing content identity and published pins. | `storage/service.py`, `storage/local.py`, `storage/s3.py`, knowledge/storage owners, config/profile validation; proposed migration utility/runbook |
| P1.5 Health and schema rollout | Add dependency-free liveness, role readiness and diagnostic separation. Reuse the existing [migration command and record task #815](https://github.com/rrahimi-uci/caliber-suite/issues/815); wire its Job in P5.1 rather than create another CLI. Define explicit schema reader/writer compatibility checks, or retain maintenance-only upgrade until compatible readers exist. Expose SQL pool budgets/timeouts. | `routes/health.py`, `observability/readiness.py`, `db/migrate.py`, `db/session.py`, `config.py`, entry point; deployment docs |
| P1.6 Mutation barrier and migration exclusion | Reuse [Pilot task #808](https://github.com/rrahimi-uci/caliber-suite/issues/808) for the persisted maintenance barrier, API-only writer enforcement and migration lock. Verify its coverage of callback/dispatch entry points and draining versus quiescence; add only a proven uncovered restore-quarantine or old-writer isolation gap as a focused follow-up. Do not create a parallel lock or barrier. | `server.py` request/service enforcement, domain mutation boundaries, `db/migrate.py`, `db/migrations/env.py`, entry point, config/profile and deployment/restore runbooks |
| P1.7 Webhook intake bridge | Before deploying separated producers with webhooks enabled, persist stable event/recipient obligations and hand off transactionally to the controller dispatcher. Implement database-time publisher acquisition fencing and recipient acknowledgement for this minimum path; P4 extends it. Preserve remote-event loop suppression instead of enabling blind rebroadcast. | `events/outbox.py`, `events/inbox.py`, `events/webhooks.py`, event-producing route/service transactions, `server.py`, models/migrations and cross-process tests |

**Data changes:** additive settings version/audit records; shared-limit records if SQL-backed; storage-copy manifest mapping old reference to verified new reference. Do not mutate published artifact identity during copy. Schema compatibility must describe minimum/maximum supported contract versions and writer features, rather than blindly accept unknown future migrations.

Maintenance state needs a versioned persistent contract and a bootstrap path for schema/restore operations. Document how old binaries lacking enforcement are stopped and isolated. Test old/new writer semantics, not just whether both can read additive columns. Do not run runtime schema migrations while user or background writers continue across an incompatible transition.

P1.7 adds durable event/recipient-version obligations, acquisition epochs/expiry and acknowledgements where existing outbox/webhook records lack them. Define uniqueness on logical event/recipient identity and preserve pending obligations through configuration rollback. Backfill only under an explicit cutover decision; do not infer that historical events with no consumer were delivered to a newly enabled webhook. Reuse the existing B13 outbox/inbox contract [#537](https://github.com/rrahimi-uci/caliber-suite/issues/537); P1.7 owns the new cross-process producer-to-controller bridge, not a second event ledger.

**Tests/load/failure:** two API processes must see the same settings revision and enforce the same aggregate limit. Verify revoked auth and project denial across replicas. Hold an assistant/provider call and demonstrate unrelated health/read requests remain responsive within the P0 target. Lose storage during migration and resume without dangling references. Remove SQL and verify liveness stays healthy while readiness refuses unsafe work. Start two control processes and verify one owner; exercise real reconciliation/attestation endpoints before and after owner replacement, including stale routing refusal. Assert API role constructs no execution loops, cross-replica notifications still work, and unsafe development multi-pod configuration refuses startup. Verify supported old/new schema overlap or the explicit maintenance refusal path.

**Acceptance:** zero replica-dependent configuration results after the documented propagation bound; aggregate admitted requests never exceed configured shared limits; every authoritative stored reference is verified shared or explicitly unsupported in multi-pod mode; no schema migration runs from an ordinary replica start; role probes have deterministic success/failure contracts. Required PostgreSQL/profile/route/storage/docs tests pass. P2 is required before declaring owner failover safe.

Also prove: two concurrent migrators cannot both enter migration; loss of the migration-lock connection cannot leave the migrator writing unprotected; draining denies new work/dispatch but can settle existing attempts; quiescence denies ordinary mutations on every surface while diagnostics remain read-only. Unrecorded callbacks get retryable refusal, never a successful acknowledgement. A stale settings cache cannot bypass revocation. P1.6 is a required deployment prerequisite for P2/P3 authority cutover, even when their implementation PRs develop in parallel.

P1.7 gates the first split-role deployment with webhooks enabled: events from separate API and worker producers reach the controller, duplicate transport creates one logical recipient obligation, and restart loses no accepted obligation. Include same-publisher restart/stale settlement and recipient-removal cases from P4.2 now for the webhook bridge. Without these checks, keep the supported combined topology or obtain an explicit disabled-feature scope decision; do not silently regress delivery until P4.

**Rollout:** migrate shared settings/storage first with old readers supported; deploy one API/control pair with explicit routing; exercise role separation, then stage a second API. Maintain one active control owner. Do not switch execution authority yet.

**Rollback:** revert roles to one combined instance only while its schema reader is supported. Preserve shared settings and copied objects; do not restore stale local overrides or delete new objects. If old code cannot interpret new settings/schema, stop admission and use the maintenance rollback procedure rather than run incompatible pods.

## 4. P2 — Ownership and recovery correctness

**Outcome:** stale workers cannot publish authoritative state; recovery reaches eligible interrupted work through actual pollers; ambiguous effects remain conservative. Complete this work even if the first deployment stays single-owner.

**Dependencies:** P0.1 inspected scope, P0.2 regression evidence and the authority specification; no performance/schedule approval is required for these safety fixes. Coordinate the schema contract with P1 and require P1.6 before deployment cutover. This phase keeps independent workflow dispatch disabled until P3 effect-authority integration.

### Implementation slices

| Slice | Expected change and boundaries | Likely files |
| --- | --- | --- |
| P2.1 Claim contract | Add acquisition epochs/history and database-time expiry to existing work models; shared conditional claim/renew/recover helpers bounded by available slots. Never renew an expired epoch. During the owner-bound bridge, capture and validate parent controller epoch for child renewal/execution; parent loss must stop heartbeat even if provider code is blocked. Preserve domain state machines and validate heartbeat/TTL constraints. | `db/models.py`, new migrations, `ownership.py`, `execution_role.py`, proposed reusable work-claim module; worker claim/heartbeat sites |
| P2.2 Workflow writes | Capture immutable attempt authority on claim. Fence execution callbacks, checkpoint writes, terminal events, execution budget settlement and publication; stop dispatch on lost renewal. Apply the architecture's operation-to-authority matrix to operator/lifecycle changes. Retain global effect fencing alongside run checks until P3. Replace recovery select-then-unconditional-update with expiry/epoch/status guarded updates; emit events from changed rows. | `orchestrator/workflow_run_worker.py`, `workflows/run_events.py`, runtime/checkpoint owners, budget/effect call sites |
| P2.3 Resume and wake | Atomically compare waiting/checkpoint version for manual/event/timer transitions. Add post-park recheck and idempotent parked-run sweep so settlement/wake crashes cannot strand work. | `routes/workflow_runs.py`, workflow worker, `effects/reconcile.py`, checkpoint/state models |
| P2.4 Artifact and effect boundary | Publish immutable attempt objects and fence SQL pointers; route/refuse runtime output writes. Token-guard compatibility ledger retries/completion or explicitly prohibit that path in the scalable profile. Preserve stable logical effect identity. | `workflows/runtime.py`, `workflows/effect_ledger.py`, `storage/service.py`, `storage/s3.py`, worker artifact writers; governed context/profile |
| P2.5 Other-family failover | Fix import rowcount-before-commit with whole-transaction rollback; release evaluator expired-running selection and periodic attempt renewal; knowledge guarded recovery/activation; refinement stage settlement versus janitor; Aria durable continuation and exclusive step attempt. Retain calibration ambiguity policy. Fence prompt-release reconciliation settlement under controller takeover, or disable it in the failover topology; singleton concurrency is not a safety proof. | `workspace_imports.py`, `workspace_import_worker.py`, `workspace_import_materializer.py`, `workspace_release_service.py`, `orchestrator/workspace_release_evaluator.py`, `knowledge/worker.py`, `knowledge/service.py`, `orchestrator/worker.py`, `eval_stage.py`, `janitor.py`, `assistant/executor.py`, `orchestrator/release_reconciler.py`, `release_operations.py` |
| P2.6 Checkpoint capability | Define supported resumable graph shapes. Persist the execution frontier/crossing values/branch state needed by a supported checkpoint, or reject unsupported placements before effects. Preserve effect identity and checkpoint format compatibility; do not infer arbitrary graph recovery from a valid node-local snapshot. | `workflows/runtime.py`, plan validation/compiler owners, workflow worker checkpoint creation/resume, checkpoint model/schema; runtime and worker integration tests |

P2.5 is several PRs, not one broad rewrite. Fix each actual producer/consumer path and its regression before extracting more shared abstractions. A family not completed must be disabled in the supported failover topology; retaining one owner does not by itself fix stale-thread recovery bugs.

**Migrations:** add nullable authority/attempt fields and indexes; backfill only safely classifiable non-running records. Drain or explicitly classify active/unknown work before changing writer mode. Add state/version constraints after data validation. Keep old columns during compatibility. Never assign a fresh authority to an ambiguous in-flight effect to make migration convenient.

Use the P1.6 mutation barrier for the cutover; remove legacy writers that lack epoch predicates before enforcing new ownership semantics. Reader compatibility is not permission for an old worker to keep writing. The migration must refuse to enable the new mode while incompatible writers or unclassified legacy attempts remain.

**Required tests and fault scenarios:**

- PostgreSQL barriers: A claims, pauses; expiry and B takeover; A resumes progress/checkpoint/terminal/publication. Assert **zero** accepted stale writes and unchanged B authority.
- Renewal committed before expiry prevents recovery. Once expiry is observed, old-epoch renewal changes **zero** rows and recovery may proceed. If a newer epoch has already been acquired, stale recovery changes **zero** rows and emits no event. Update the legacy `test_renew_lease_extends_lease_when_held` fixture, which currently renews an expired lease; preserve only explicitly supported development compatibility outside the new profile.
- Keep A's provider blocked while its heartbeat connection remains available, then transfer controller ownership to B. A's child renewals change **zero** rows; B can recover within the declared lease/sweep bound; releasing A cannot publish stale state.
- Resume/resume, resume/claim, cancel/complete and timer/resume: one valid transition; no cleared newer claim; checkpoint input matches the winning version.
- Parked runs with no live execution lease remain approvable/resumable by authorized actors; stale execution callbacks remain rejected. A recovered run without a selected checkpoint must not silently replay an ambiguous external effect. An explicitly selected missing, corrupt or out-of-lineage checkpoint must fail closed, never fall back to entry-point execution.
- Compare uninterrupted and restarted `source → gate → join` with a bypass `source → join` input, plus selected/skipped branches and nested supported waits. Outputs and completed-effect counts must match; unsupported topologies must be refused before an effect. Include checkpoint format upgrade/old-data refusal tests.
- Settle before park; crash after settlement commit before wake; restart normal pollers. Every safely resumable parked run is discovered within the configured sweep interval plus execution budget; repeated sweeps add no duplicate transition.
- Lose import ownership after materialization flush: **zero** committed revision/pin changes. Release evaluator replacement `tick()` must discover expired running work and create a new provenance-preserving attempt; a slow valid evaluation renews beyond its initial lease.
- Knowledge stale worker cannot activate a version; old refinement evaluation cannot overwrite janitor/retry state; Aria completed-wait crash resumes the persisted continuation once under its claim.
- Prompt-release reconciliation pause/takeover/resume cannot commit stale intent or settlement; otherwise the family remains disabled with that limitation documented.
- Artifact upload succeeds/SQL fails and the reverse reachable boundaries: no committed pointer to absent/unverified content, no stale overwrite of published content; orphan collection excludes active/published references.

**Acceptance:** all above negative oracles pass on real PostgreSQL at normal worker/route seams. Existing effect/approval and development compatibility suites remain green. Unknown effects and interrupted calibration do not auto-replay. Record measured affected-code coverage. No family is marked qualified merely because its helper claim test passes.

**Rollout:** keep one active execution owner; migrate readers then writers; enable new guards on a small workload, inspect stale-rejection/recovery metrics, then expand. Drain legacy running records before enforcing non-null new authority. No active-active worker switch in this phase.

**Rollback:** pause admission and new claims; drain or classify active attempts. Revert only to a binary that understands the authority metadata and preserves fencing. Do not drop epochs, attempt history or unknown-effect records. If prior code could accept stale writes, the safe rollback is disabled execution plus a forward fix, not returning to unsafe automatic recovery.

## 5. P3 — Independent workflow workers and bounded admission

**Outcome:** API replicas durably admit work; workflow-worker replicas execute independent runs under per-run authority; the controller retains scheduling/reconciliation. Horizontal capacity comes from independent work, not parallel execution of one run without a design.

**Dependencies:** P1 and P2 gates; supported effect disposition inventory; #783 retry authority and [#789](https://github.com/rrahimi-uci/caliber-suite/issues/789) typed capacity bucket contract. Coordinate with existing effect ownership tasks before adding duplicates.

| Slice | Implementation and boundary | Likely files |
| --- | --- | --- |
| P3.1 Authority integration | Persist and validate operation-specific authority domain/work/epoch/history-generation across dispatch, approval, budgets and reconciliation. Remove P2's parent-controller dependency only for qualified independent workers; retain it for controller-owned families. Distinguish expired-owner takeover from live-owner stuck-dispatch expiry and lookup; arbitrate settlement by proposal/attempt version without resend authority. | `ownership.py`, `effects/dispatcher.py`, `effects/records.py`, `effects/reconcile.py`, `effects/governed_context.py`, budget owners, models/migrations |
| P3.2 Worker role | Add non-public worker entry point, bounded claim loop, independent renewal connection capacity, graceful drain and per-family enablement. Retain global controller lease for maintenance. | `server.py` lifecycle extraction, `execution_role.py`, workflow worker, config/profile, entry point |
| P3.3 Durable command admission | Any API replica accepts authorized long-work/operator commands with scoped key/versioned fingerprint and persisted result identity; recheck runtime permissions. Preserve existing workflow canonical-intent checks and original pins. Resolve matching retries before new-work capacity checks; new admission atomically reserves capacity with work/event creation. Specify key tombstones/retention and expired-result behavior. Migrate owner-bound effect operations with explicit API/SDK compatibility. | `routes/system_effects.py`, `routes/workflow_runs.py`, `workflows/admission.py`, `workflows/run_launch.py`, selected assistant routes/services; proposed command model/consumer, OpenAPI/SDK/UI consumers |
| P3.4 Backpressure | Enforce shared deployment/project/connector capacities, queue limits and bounded fairness. Unknown effects retain reservations. Add clear overload responses and capacity-aware worker concurrency. | `budget/reservations.py`, `budget/step_reservation.py`, `effects/dispatcher.py`, config, queue/admission services, metrics |

**Data/config changes:** additive versioned authority references on effect/dispatch attempts; operation keys with canonical fingerprint uniqueness; typed capacity buckets and reservations. Introduce an explicit independent-worker feature gate defaulting off. Define installation history-generation management for restore. Do not repurpose run IDs as connector bucket keys or embed secrets in commands.

**Tests/load/failure:** run separate API/controller/worker processes against PostgreSQL with deterministic HTTP/provider fixtures. Submit the same key/body concurrently through different APIs: one durable work identity. Same key/different body: conflict with no second effect. Pause a worker after dispatch claim but before send, transfer ownership, then resume it; assert the documented connector-specific result and stale settlement refusal. For a connector lacking remote idempotency, assert conservative unknown/refusal rather than a fictitious duplicate-free guarantee. Saturate a shared provider from two projects and verify capacity never exceeds its configured bound. Test scale-down drain, ungraceful death, database disconnect and cancellation with in-flight effects.

Test a matching retry during full-queue overload and after alias/default changes: original operation/pins return, no new capacity is consumed. Concurrent new submissions for the last slot admit at most one; failed admission leaves no reservation leak. Revoked access cannot discover an existing result by guessing a key. Retained keys survive visible-run deletion and cannot create a second operation; test the configured retention/expired-result boundary. Record key/fingerprint compatibility before changing existing workflow callers.

Preserve the same-token dispatcher/reconciler race covered by `test_effect_in_flight.py`, including stuck-dispatch classification and parked-run continuation while the owner remains live. Any additional production workflow-node backend needs configured verifier wiring and route-to-worker-to-effect tests; adapter-only success does not enable it.

**Acceptance:** every supported workflow mutation validates current authority; no stale completion accepted in the fault matrix. Two independent workers demonstrate concurrent processing of distinct runs with conserved run/attempt counts. Throughput and p95/p99 latency meet the **preapproved** envelope for the stated workload/resources; do not invent a universal linear speedup requirement. Queue/provider/database caps hold across replicas; no starvation beyond the configured fairness bound. Legacy response compatibility or versioned migration is tested through API/SDK/UI consumers.

**Rollout:** deploy readers supporting both authority formats; stop new legacy claims; drain/classify old attempts; enable the new dispatcher and one worker under the new contract; add a second worker only after correctness smoke tests. Increase a bounded pool after envelope tests. Other worker families remain control-owned unless P4 qualifies them.

**Rollback:** disable new admission/claims, drain current authority and preserve effects/reservations. Reduce worker replicas to one without removing fencing. Reverting to the old global-owner writer is allowed only after a tested data conversion and no incompatible active attempts; otherwise roll forward. Never force-release unknown provider capacity simply to make rollback complete.

## 6. P4 — Durable schedules, delivery and additional worker families

**Outcome:** scheduling and continuation survive downtime, event consumers have durable handoff semantics, and each supported background family has an explicit concurrency/recovery qualification. Parallelism for every family is not required for the first supported deployment.

**Dependencies:** P2 claim/transition contract and P3 authority/capacity integration. Product decision on misfires/DST is required for schedule acceptance. P2 parked-run sweep remains in place even when durable wake delivery is added.

| Slice | Implementation and boundary | Likely files |
| --- | --- | --- |
| P4.1 Schedule occurrences | Unique UTC occurrence/revision, occurrence-derived enqueue key and run association; persist deferred/admitted/skipped/refused disposition with cursor advance. Commit admission/run association atomically; retry deferred work under bounded backlog/age/fairness without silent skips. Retain single scheduler owner. | `orchestrator/scheduler.py`, `workflows/run_launch.py`, schedule models/new migrations, schedule API/config/tests |
| P4.2 Outbox/inbox wiring | Extend P1.7's publisher fencing and persisted recipient obligations to named consumers/wake processing. All intended recipients must acknowledge or receive audited cancellation before delivery/pruning. Transactional inbox deduplicates SQL work; external delivery retains attempt fencing. | `events/outbox.py`, `events/inbox.py`, `events/webhooks.py`, `server.py`, `effects/reconcile.py`, worker wake consumer, migrations |
| P4.3 Authorized live replay | Run-scoped versioned sequence cursor, consistent snapshot watermark and one SQL replay/live tail; broker hints accelerate bounded polling. Detect overflow/gaps and force resnapshot when needed. Recheck current auth per protected batch, including already-open connections; general ephemeral events remain snapshot-recoverable notifications. | `routes/events_stream.py`, `workflows/run_events.py`, event buses, UI/SDK stream consumers, retention configuration |
| P4.4 Family qualification | Apply the P2/P3 protocol to each selected refinement/knowledge/Aria/import/release family. Keep prompt-release reconciliation, janitor and calibration owner-bound unless separately proven safe. Explicitly disable unsupported families. | Worker inventory in architecture section 2; family-specific tests and deployment flags |

**Migrations:** additive schedule occurrence/cursor records with uniqueness; consumer identity/attempt/processed state where existing inbox/outbox tables are insufficient; replay retention/index policy. Initialize a new schedule cursor at an explicitly chosen cutover instant. Do not manufacture historical missed runs or mark old outbox rows delivered to new consumers without a backfill decision.

Extend the P1.7 recipient-obligation schema rather than create a second webhook ledger. Preserve pending recipients through configuration changes; explicitly version consumer additions/removals and backfill boundaries. A deployment rollback cannot erase pending obligations or make an event eligible for pruning.

**Tests/failure scenarios:** two scheduler ticks plus leader takeover for the same instant produce one logical run; outage spanning many ticks obeys the approved policy and maximum backlog; repeated/skipped DST hours and schedule revisions have unambiguous identities. Crash between consumer reservation and SQL commit rolls back safely; after commit/redelivery causes no duplicate transition. Broker outage does not lose accepted work. Webhook timeout remains retryable/unknown according to contract; receiver idempotency is separate from local delivery state. SSE reconnect to another pod replays only authorized events, and an expired cursor returns an explicit resnapshot outcome. Revoked access cannot read replay history.

For repeated-hour policies that execute both occurrences, two distinct eligible UTC instants must produce distinct runs; replay of either occurrence returns its original run. For publisher restart, hold an old consumer handoff, stop/restart the same publisher object, acquire a new claim, then return the old handoff: it must change zero delivery/release rows.

Also test capacity-full → scheduler restart → capacity-restored: deferred occurrences retain identity, each eligible occurrence gets one run, expired ones receive the approved recorded disposition, and other schedules progress. Test recipient A acknowledged/B failed → remove B → rollback/re-add B: B's obligation survives, A's database transition is not repeated, and premature pruning is impossible. Commit an event during SSE replay/live handoff and overflow the notification queue; SQL tail recovers it or sends explicit resnapshot. Revoke access on another replica with the stream open: no protected batch is authorized after revocation, and reconnect/replay remains denied.

For each family, repeat kill, pause/takeover/resume, long-call renewal, cancellation, poison work and storage-publication tests through the real poller. Calibration must preserve its operator-resolution boundary until safe replay is separately established.

**Acceptance:** one occurrence per schedule/revision/UTC instant; no silently lost eligible continuation; bounded catch-up and event retention; one committed database consumer transition per consumer/event identity under duplicate delivery; zero cross-project unauthorized replay. Every enabled family has documented supported concurrency, recovery limits and passing PostgreSQL/failure evidence. A singleton family is acceptable only after its failover correctness is proven; a disabled family is disclosed as a product-scope limitation.

**Rollout:** wire one durable consumer at a time behind a gate, observe queue age and redelivery, then migrate UI streams. Keep old event readers during the compatibility interval. Enable family-specific worker deployments separately; do not use a single global “distributed” flag to enable all loops.

**Rollback:** stop new consumers/claims while retaining outbox/inbox rows, pending recipient obligations and schedule occurrence/cursor history. Reduce families to their tested singleton mode. Preserve consumed identities so rollback/restart cannot replay external effects accidentally; removal from configuration is not recipient acknowledgement. A schedule policy rollback requires an explicit new cutover, not deletion of occurrence records. SSE can fall back to authorized snapshot polling without treating missed notifications as lost state.

## 7. P5 — Kubernetes, operational qualification and release evidence

**Outcome:** a reproducible supported single-region topology with approved operating limits, tested upgrades and recovery, and operator runbooks. Installing manifests alone is not this milestone.

**Dependencies:** P1–P4 acceptance for every enabled capability; approved #847 target envelope and operations/security infrastructure choices. Disabled families/features and connector limitations must be visible in the release support matrix.

**Files/components:** proposed `deploy/kubernetes/` Deployments/Services, migration Job, ConfigMaps/secret references, probes, resources, disruption/topology policies, NetworkPolicies and optional HPA; `deploy/caliber/Dockerfile`/entrypoint as needed; `observability/`; `docs/operate/` installation, upgrades, recovery and operating-envelope guides; deterministic multi-process/fault/load harness under repository test conventions; applicable CI jobs. Do not commit cluster credentials or vendor-specific secret values.

**Configuration and migration:** pin image digests, schema contract, role/family flags, pool/concurrency budgets, provider limits, object storage, bus and identity inputs. Run one migration Job under the P1.6 database lock/mutation barrier with backup and documented rollback. Verify old/new reader and writer overlap or use maintenance rollout. Keep database and object storage lifecycle outside ephemeral application pod lifecycle. Namespace deletion must not erase authoritative evidence by accident. Declare whether database failover preserves every acknowledged commit; HA service branding is not an RPO-zero guarantee.

### Required scenario matrix

| Scenario | Measurement and pass oracle |
| --- | --- |
| Fixed workload at 1, 2 and approved maximum workers | Record offered/admitted/completed rate, p50/p95/p99 latency, queue growth, resources and database/provider saturation. Meet approved envelope on fixed declared infrastructure; account for each accepted run. |
| Burst and sustained overload | Admission rejects/backpressures within configured bounds; memory, SQL connections and provider capacity remain bounded. Recovery returns queue age to the approved target without manual data repair. |
| Controller death and paused former owner | Standby acquires according to lease contract; old controller cannot settle authoritative mutations. Measured recovery/detection time meets approved targets. |
| Worker SIGKILL, pod eviction and node loss | Accepted work remains durable; safe work resumes; stale effects/unknowns follow contract. No accepted stale write and no unexplained lost run. |
| SQL outage, slow renewal and lossless primary failover | Local authority expires safely; probes do not cause database-driven liveness restart storms; old primary is fenced and all acknowledged commits survive under the tested replication configuration. No new effect is authorized after ownership loss; already-authorized late transport is evaluated against its connector contract. Recovery meets approved RTO. |
| Failover that loses acknowledged writes | Detect/declare the lost interval and quarantine execution before replay. Reconcile against independent admission/effect evidence; absent ledger rows do not mean no remote action. Report any unrecoverable operations and compare actual loss to approved RPO. Do not apply the lossless-failover acceptance claim. |
| Broker and object-store outage | No loss of committed work; bounded retries/backpressure; no bad artifact pointers or silent outbox loss. |
| Provider timeout, success-with-lost-reply and late old-worker return | Supported receipt/idempotency fixtures show one logical committed effect; unsupported guarantees are refused/unknown, never reported as exactly once. |
| Rolling upgrade and incompatible schema | Compatible readers/writers coexist only within tested window. Incompatible configuration refuses readiness/startup with actionable diagnostics. |
| Restore from backup/PITR | Verify CALIBER/MLflow/object/event consistency, new history generation and isolation of old writers. Include an external effect committed after the restored snapshot whose local row is absent: preserve known logical identity, reconcile from independent evidence or keep the scope quarantined. Generation change alone must never authorize replay. Meet approved RPO/RTO, including operator steps and declared data loss. |
| Graceful scale-down and forced termination | Draining stops new claims, finishes or parks safe work within configured grace, and leaves unknown effects accounted for. Correctness also holds when grace is not honored. |
| Authorized streaming and replica churn | Reconnect/replay/resnapshot respects project permissions and bounded retention; secrets/payloads do not leak through telemetry. |

**QA method:** use deterministic local provider/receiver fixtures with independent effect counters, real PostgreSQL and object storage, and process barriers/fault injection. Record fault timing relative to transaction boundaries. Repeated trials and a soak duration come from the preapproved workload/window, not a convenient single run. Live-provider or paid infrastructure trials are separately scoped and budgeted; they do not replace deterministic CI. A kill test is not a substitute for pause-and-resume of a stale process.

**Acceptance:** all zero-tolerance safety oracles above pass; no missing required scenario, skipped environment or unknown enabled-family qualification. Measured performance/cost/recovery results meet the approved envelope without revising targets after the run. Include dashboards/alerts with tested detection, install/upgrade/restore evidence and an independent operator exercise. Publish exact support limits and unresolved connector risks; do not advertise universal HA or exactly-once behavior.

**Rollout:** staging with fixed replicas → bounded operator trial → production canary with limited workload → gradual expansion within measured caps. Enable HPA only after the fixed-replica envelope, drain and overload tests pass. Watch queue age, stale-write rejection, unknown-effect age, scheduler lag, outbox age, SQL pool waits and cost. Rollout pauses automatically or operationally at the approved thresholds.

**Rollback:** freeze admission where necessary, disable HPA, return to the last qualified replica/configuration set, drain or classify active attempts, and use compatible application binaries. Restore only through the history-generation/external-reconciliation runbook; a database restore cannot undo remote effects. Preserve audit/attempt evidence. If safety invariants fail, stop dispatch and roll forward with a fix rather than erase evidence or disable guards.

## 8. P6 — Later scaling optimizations

These are optional and require an observed bottleneck after P5. None substitutes for P2 correctness. The retained MVP [D12 regional/residency-cell work #896](https://github.com/rrahimi-uci/caliber-suite/issues/896) remains required under its own product control; P6 concerns additional capacity sharding beyond that scope.

| Optimization | Trigger / likely changes | Tests, rollout and rollback |
| --- | --- | --- |
| Polling/queue tuning | Database polling load or claim contention exceeds approved budget; add notification hints, bounded batch claims, indexes and retention/vacuum tuning in queue/event modules. | Compare query plans and queue latency under identical workload; broker loss falls back to polling. Gate hints; retain SQL authority. |
| Dedicated heavy-worker pools | Knowledge/refinement resource contention affects workflow/API targets; isolate resource classes and concurrency budgets. | Mixed-workload fairness and provider capacity tests; canary a family; revert to its qualified pool without changing work identity. |
| Read/query optimization | Measured API queries dominate primary load; optimize indexes/pagination/caches first, then consider read replicas for explicitly stale-tolerant queries. | No authority, admission or read-after-write decisions use stale replicas. Test lag; route reads back to primary on rollback. |
| Independent control partitions | Measured scheduler/reconciler backlog exceeds a single controller's bounded capacity; partition by stable ownership domain with durable transfer. | No overlap/gap during transfer, epoch fencing and restore tests; route back only after partition drain. Reuse existing ownership-transfer tasks. |
| External durable broker | SQL queue cost remains unacceptable after tuning; use outbox handoff and consumer deduplication, retain authoritative state/claim protocol. | Lost/duplicate/out-of-order delivery tests; dual-read/write migration needs a separate ADR. Rollback preserves consumer identities. |
| Additional cross-region capacity sharding | A measured bottleneck or new approved locality requirement exceeds the retained D12 cell contract; extend its home-region/history-generation authority rather than creating a second cell model. | Separate architecture, migration, routing and disaster-recovery acceptance. No active-active cross-region claim in this proposal. |

Each optimization needs a new measured hypothesis, explicit acceptance threshold approved before benchmarking, resource/cost comparison and reversible feature gate. Avoid replacing the current engine, ORM or queue solely for architectural fashion.

## 9. Human decisions and engineering handoff

| Decision | Required input | Blocks | Work that can proceed |
| --- | --- | --- | --- |
| Operating envelope, #847 | Domain/operations-approved numeric workload, latency/error/cost/RPO/RTO/detection targets and measurement window | P3 performance acceptance, P5 qualification | Deterministic safety fixes, harness and target template |
| First supported feature/family matrix | Product/engineering select included, singleton and disabled families; disclose limits | P1/P4 release support claims | Inventory and regression fixes |
| Schedule semantics | Product chooses skip/catch-up/coalesce, DST and lateness/backlog bounds | P4 schedule acceptance | Occurrence schema and policy-neutral fixtures |
| Connector risks | Security/product/connector owner confirms receipt/idempotency scope and residual-risk acceptance | Enabling affected external writes | Refusal paths and deterministic adapter tests |
| Infrastructure and restore ownership | Operations/security select database/object service, workload identity, backup ownership and maintenance constraints | Deployment and P5 restore qualification | Portable manifests and offline validation |
| API compatibility and retry retention | Product/API owner approves asynchronous command evolution, supported older consumers, idempotency retention and expired-result behavior | Changing public response/key semantics and P3 admission acceptance | Additive command contract and client fixtures |

The implementing agent should take one dependency-ready slice, inspect its current producer/consumer paths, update its issue with the precise contract and regression oracle, then implement and validate through a focused PR. Do not implement all phases as one change, silently reduce an enabled family's scope, or treat this document's proposed tests as already present. Completion means integrated evidence for the supported topology, with limitations and unresolved decisions explicit.
