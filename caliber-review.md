# CALIBER Paper Review

## Review basis

**Current manuscript:** *CALIBER: Per-Family Governance for Releasing AI-Agent Resources*

**Review date:** 2026-08-03

**Implementation revision inspected:** `b6b2b472bd9670c431babc562bfc70e68336fbd9`

**Review standard:** top-tier systems-paper submission, not product documentation

**Reviewer confidence:** 4.5/5

The baseline review is based on the complete LaTeX source, the original 52-page compiled paper,
the bibliography, generated implementation statistics, selected implementation
paths, focused tests, build diagnostics, and current primary documentation for
the closest systems. It is not a prose-only review.

## Post-review remediation status

**Updated 2026-08-03.** The score below is the baseline assessment of the draft
that was reviewed. The following submission-blocking implementation and manuscript
issues have since been corrected in the working tree:

- prompt creation and version authoring are non-live by default and cannot rotate
  an alias;
- direct promotion, rollback, assistant publication, existing-version Apply, and
  newly registered prompt candidates use a durable release operation with
  `prepared -> applying -> applied | failed | reconcile_required` states;
- the exact outgoing/incoming versions, actor, scopes, evidence, and operation ID
  are committed before the alias effect, retries are operation-ID idempotent, and
  incomplete effects are exposed through list/reconcile endpoints;
- the UI performs authoring and promotion as two explicit requests;
- the release and queue algorithms, title, abstract, limitations, protocol,
  availability statement,
  and current MLflow/LangSmith/Langfuse comparison now match the inspected code;
- focused verification covers commit-before-effect, HTTP retry idempotency,
  provider failure, reconciliation, Apply recovery, migrations, prompt routes,
  async route offloading, and UI behavior;
- final repository validation passed 5,881 backend tests (9 environment-gated
  skips, 93.45% coverage), 1,549 frontend tests, and TypeScript typechecking;
- the current source uses `\documentclass{scrreprt}`, includes a table of
  contents, starts every top-level section on a new page, and reproduces a
  71-page PDF with a 225-word abstract, no overfull boxes, and no undefined
  references or citations.

The paper is **not yet submission-ready**. Quantitative evaluation and the operator
study remain unrun, per-family capability obligations are not mechanically enforced,
Apply remains operator confirmation rather than independent approval, the manuscript
remains over-length, and `paper/` is still untracked. Those unresolved items still
prevent a defensible acceptance recommendation; a new score should follow completed
evaluation rather than code repair alone.

## Overall decision

**Score: 47/100 — Reject in the current form; encourage a major revision.**

The problem is important, the implementation is unusually broad, the diagrams
are strong, and the paper is commendably candid about several limitations. The
current draft nevertheless falls below the bar for a systems venue for four
decisive reasons:

1. The empirical evaluation is entirely unrun.
2. The implementation does not satisfy some of the paper's central governed-
   release contracts across all prompt paths.
3. The two main algorithms overstate or misrepresent the implementation's
   transaction ordering and delivery semantics.
4. The novelty comparison is factually too broad: MLflow, LangSmith, and
   Langfuse already provide important parts of prompt versioning, live-target
   indirection, promotion, rollback, and access control.

This is a strong foundation for a technical report and a potentially valuable
systems paper after the claims, implementation, evaluation, and related-work
positioning are brought into alignment. It is not submission-ready today.

## Dimension scores

| Dimension | Score | Rationale |
| --- | ---: | --- |
| Problem significance | **9/10** | Governed change management for prompts, tools, skills, workflows, and retrieval assets is important and timely. The trace-to-release framing is useful. |
| Novelty and positioning | **5/15** | Cross-family governance is promising, but the claimed empty ecosystem position is contradicted by current prompt-management systems. The “availability, not inheritance” result is presently a design observation, not a demonstrated research result. |
| Architecture and technical soundness | **7/20** | State ownership and limitation boundaries are thoughtful, but central queue and release algorithms do not match important implementation paths, and some invariants are stronger than the mechanisms justify. |
| Implementation depth | **8/10** | The implementation is substantial and contains real mechanisms, migrations, tests, typed IR, multiple resource families, and explicit extension seams. Breadth alone does not validate the paper's claims. |
| Empirical evaluation | **2/20** | The protocol exists, but every quantitative result is unmeasured. There is no baseline comparison, user study, fault-injection campaign, security evaluation, or measured quality-latency-cost result. |
| Reproducibility and artifact quality | **7/10** | The paper builds, counts regenerate byte-identically, source organization is excellent, and the protocol is detailed. However, the entire `paper/` tree is untracked in the inspected checkout, the protocol has internal inconsistencies, and the paper does not package a runnable experiment command. |
| Writing and organization | **5/10** | The prose is intelligent and unusually honest, but the manuscript is repetitive and much too long for a conference paper. The abstract is approximately 489 words and the complete PDF is approximately 25,244 words. |
| Visual and typesetting quality | **4/5** | The figures, tables, semantic color system, and print legibility are strong. Some tables remain dense, and the PDF build still reports font/PDF-string warnings. |
| **Total** | **47/100** | **Reject; major revision required.** |

## Summary of the paper

CALIBER proposes a control plane for changing heterogeneous resources that
influence AI-agent behavior. Its organizing object is a governed asset with a
typed definition, history, authority, evidence, and—where meaningful—a live
target. A governance chain connects a production signal to evidence, a
candidate, measurement, a human decision, release, and subsequent traces. The
paper's main architectural thesis is that a shared layered substrate makes
capabilities available but does not automatically confer them on every resource
family; guarantees must therefore be wired and stated per family.

The paper further describes database-arbitrated queues, external-store
boundaries, gate semantics, human Apply decisions, rollback checkpoints,
containment boundaries, and a large implementation over MLflow. It provides an
unexecuted measurement protocol for serving overhead, queue behavior, judge
agreement, rollback, and record-based reconstruction.

## Strongest aspects

### 1. The problem framing is compelling

The transition from “what happened?” to “may this change go live, on what
evidence, approved by whom, and how can it be undone?” is a useful systems
framing. It is more specific and actionable than a generic LLMOps claim.

### 2. The per-family guarantee table is valuable

Table 1 is one of the strongest artifacts in the paper. It refuses to pretend
that prompts, test sets, judges, tools, and knowledge bases have identical
liveness, evidence, or rollback semantics. This is useful engineering guidance
even if the paper currently overstates it as a novel negative result.

### 3. State ownership and limitations are discussed honestly

The paper distinguishes relational metadata, object bytes, and MLflow-owned
state; names dual-write boundaries; distinguishes containment from isolation;
and explicitly states that project scoping is not verified multi-tenancy. This
is substantially better than the usual systems paper that hides its weakest
boundaries in implementation detail.

### 4. The artifact is organized and visually polished

The LaTeX source is modular, figures are vector-based, counts are generated from
source, and the build checks undefined citations/references and float failures.
The visual system is consistent and the architecture diagrams are readable.

### 5. The authors do not fabricate results

Marking unrun measurements as unmeasured is scientifically correct. The honesty
does not compensate for missing evaluation, but it prevents a worse problem.

## Major concerns

### M1 — The governed-release contract is bypassed by ordinary prompt paths

**Severity: submission blocking.**

The paper states that the governance chain carries a measured candidate through
an enforced gate, human decision, audited release, and recorded rollback target
(`paper/sections/00-abstract.tex:10-18`). Appendix B goes further: “Every release
path, regardless of family” must record the outgoing target, incoming version,
evidence, principal/scope, and audit row *before* the external effect
(`paper/appendix/b-api-surface.tex:37-54`).

The inspected prompt implementation contradicts this contract:

- `create_prompt` defaults `target_alias` to `prod` and immediately calls the
  shared registration helper (`caliber/src/caliber/routes/prompts.py:1248-1283`).
- `create_prompt_version` defaults `promote` to `True`, again rotating `prod`
  during ordinary authoring (`caliber/src/caliber/routes/prompts.py:1354-1396`).
- The helper registers the version and best-effort rotates the alias without a
  release checkpoint, evidence bundle, or release audit row
  (`caliber/src/caliber/routes/prompts.py:254-343`).
- Alias-rotation exceptions are swallowed, while `alias_changed` is computed
  from API availability and requested inputs rather than actual success
  (`caliber/src/caliber/routes/prompts.py:320-342`).

Thus an operator can create or edit a prompt and move the live target without
traversing the claimed refinement gate or satisfying the paper's release record.
The paper acknowledges that a release verdict is advisory, but this is more than
an advisory verdict: it is an alternate release path outside the stated durable
contract.

**Required correction:** make authoring non-live by default; route every live-
target mutation through one release state machine; mechanically prevent direct
alias changes from bypassing intent, checkpoint, actor, audit, and completion
state. If the bypass is intentionally retained, narrow the paper's contribution
to the refinement/Apply path and stop claiming a general governed-release control
plane.

### M2 — Algorithm 2's transaction order is the reverse of the implementation

**Severity: submission blocking.**

Algorithm 2 calls its ordering “the whole mechanism”: it commits the outgoing
target, provenance anchor, and audit row before rotating the external live target
(`paper/images/alg-promote.tex:9-42`; `paper/sections/06-components.tex:122-130`).
Its crash invariant says the release is either complete or the prior target is
recorded and restorable.

The implementation instead performs the external effect first:

- The Apply route opens a SQL session and eventually commits at
  `caliber/src/caliber/routes/jobs.py:226-294`.
- Before that commit, the prompt-alias Apply path calls MLflow alias rotation at
  `caliber/src/caliber/apply.py:527-538` and only afterward adds the checkpoint
  and audit rows at `caliber/src/caliber/apply.py:548-628`.
- The production promoter registers the prompt version and rotates the alias at
  `caliber/src/caliber/promoter.py:159-203`; the caller creates the durable SQL
  checkpoint only after the promoter returns.
- The direct alias endpoint also rotates first and commits its audit afterward
  (`caliber/src/caliber/routes/prompts.py:1577-1653`).

A process/database failure after MLflow rotation but before SQL commit can
therefore leave production changed with no committed local checkpoint or audit
row. This is precisely the “external effect without local record” state the
algorithm claims to exclude.

**Required correction:** implement a durable release-intent state machine such
as `prepared -> applying -> applied | reconcile_required`, commit the outgoing
target and intent first, perform the idempotent external operation, then commit
completion. A reconciler must settle incomplete intents. Alternatively, change
the algorithm and claims to describe the weaker external-first reality and
evaluate the resulting failure window.

### M3 — Queue mutual exclusion is conflated with exactly-once execution

**Severity: submission blocking.**

The conditional claim update can establish that two workers do not concurrently
own the same queued row. It does not by itself establish exactly-once execution
across crashes. Algorithm 1's own invariant is only “at most one worker observes
a given row in running state at any instant” (`paper/images/alg-claim.tex:15-19`),
yet the paper and protocol elevate this into exact-once execution
(`paper/appendix/c-protocol.tex:50-69`).

The implementation also does not have one queue discipline:

- Tool-calibration jobs use a select followed by a conditional update and have
  `claimed_at`/`claimed_by`, but no lease in the claim shown at
  `caliber/src/caliber/orchestrator/calibration_drain.py:145-177`.
- Stale refinement jobs are marked **failed**, not returned to `queued` for
  another worker (`caliber/src/caliber/orchestrator/janitor.py:1-21`).
- Workflow runs do use lease expiry and requeue
  (`caliber/src/caliber/orchestrator/workflow_run_worker.py:560-637`).
- Workflow restart is inherently replay-oriented. The effect ledger explicitly
  documents that crash recovery is at-least-once and that an `in_progress`
  external effect may be indeterminate
  (`caliber/src/caliber/workflows/effect_ledger.py:1-70`).

The paper's generic `FOR UPDATE SKIP LOCKED` pseudocode is not the literal
implementation of all five “durably arbitrated” loops. E2(f)'s expectation that a
mid-claim kill resumes on another replica is false for at least the refinement
janitor path, which marks the job failed.

**Required correction:** replace the single generic story with a per-loop table:
claim predicate, ownership field, lease/heartbeat, recovery action, delivery
semantics, external-effect policy, and idempotency key. Claim concurrent
mutual exclusion where established; use at-least-once, at-most-once replay, or
indeterminate-effect language where appropriate. Test crash points, not only
contention.

### M4 — The closest-system comparison is materially inaccurate

**Severity: submission blocking for novelty.**

The manuscript says tracking/observability systems are “silent about what may
change,” have no artifact to version or live target, and that the release concern
is unoccupied (`paper/sections/11-comparison.tex:20-50,85-95`). That is not
accurate as of the review date:

- [MLflow Prompt Registry](https://mlflow.org/docs/latest/genai/prompt-registry/index.html)
  provides immutable prompt versions, aliases, lineage with tracing/evaluation,
  and documented rollback/lifecycle workflows. CALIBER itself depends on these
  mechanisms.
- [LangSmith prompt management](https://docs.langchain.com/langsmith/manage-prompts)
  provides prompt commits, staging/production environment pointers, promotion,
  rollback history, prompt owners, permissions, and update webhooks.
- [Langfuse prompt management](https://langfuse.com/docs/prompt-management/overview)
  and [version control](https://langfuse.com/docs/prompt-management/features/prompt-version-control)
  provide versioned prompts, deployment labels, client-side live retrieval,
  rollback, environment labels, trace linkage, and protected production labels.

These systems may still lack CALIBER's heterogeneous resource model, enforced
candidate-advancement gate, cross-resource evidence chain, or unified release
record. Those narrower differences could be defensible. The present table gives
whole categories a “none” governance model and therefore reads as a straw-man
comparison.

**Required correction:** compare feature-by-feature and version-by-version using
dated primary sources. Separate prompt lifecycle, heterogeneous asset lifecycle,
evidence binding, approval authority, enforced policy, rollback, audit export,
and cross-family impact. Claim the delta CALIBER actually adds, not an empty
ecosystem.

### M5 — The central “negative result” is currently a false dichotomy

**Severity: major.**

The paper argues that a uniform contract is either vacuous or excludes useful
families (`paper/sections/05-architecture.tex:19-43`). This overlooks a standard
third design: a meaningful shared base contract plus explicit capability
interfaces or a discriminated union. For example:

- every governed asset can implement `Identity + Version/History + Authority +
  Audit`;
- releasable assets implement `LiveTarget + Promote + Rollback`;
- evaluatable assets implement `EvidenceConsumer + Score`;
- evidence assets implement `EvidenceProvider`;
- calibratable assets implement `CandidateGenerator`.

That design neither forces a test set to expose `promote` nor reduces the shared
contract to “has an identifier.” In fact, the paper's own optional facets are
already close to this capability model.

The insight remains useful: layer adjacency does not prove a family wired the
available control. To become a research contribution, however, it needs a formal
capability model, conformance obligations, and evidence that the model catches
real missing wiring or reduces extension errors. A prose table alone does not
establish the stronger architectural result.

### M6 — The human decision is not an independent approval boundary

**Severity: major.**

The prose invokes authority separation and at points suggests distinct approval
scope. The implementation's Apply route requires `operator`, creates a
`CaliberApprovalRequest` already in `approved` state, and records the same actor as
`approved_by` (`caliber/src/caliber/routes/jobs.py:210-294`). Its own docstring
states that votes, quorum, and the earlier approval-governance flow were removed.

This does establish a deliberate human click after an automated gate. It does not
establish separation of duties, independent approval, or requester-versus-approver
control. The paper partly admits this under the single-environment limitation, but
the stronger governance language elsewhere remains misleading.

**Required correction:** either enforce author/requester != approver with the
existing sibling `approver` scope, or consistently call the step an
operator-confirmed Apply action rather than approval/separation of duty.

### M7 — The evaluation is absent, and parts of the protocol are inconsistent

**Severity: submission blocking.**

The paper correctly states that all quantitative evidence is absent. For a systems
paper, architecture plus a future protocol is not enough. The missing evidence
includes the claims most likely to fail in production: release crash consistency,
queue delivery semantics, serving overhead, gate validity, rollback observation,
and reconstructability.

The protocol also needs correction before execution:

- E2 asks for exactly-once invocation when the mechanisms support different
  delivery semantics and some external outcomes are explicitly indeterminate.
- E4(k) gives the investigator only the CALIBER database and object storage
  (`paper/appendix/c-protocol.tex:106-116`), while the architecture says MLflow
  owns prompt versions, aliases, traces, and assessments
  (`paper/sections/05-architecture.tex:177-181`). Prompt releases cannot be fully
  reconstructed under that access model unless CALIBER exports a self-contained
  evidence bundle.
- “At least 200” candidates has no power analysis, family/task stratification,
  class-prevalence plan, preregistered success threshold, or cost-sensitive error
  analysis.
- Cohen's kappa against a majority label derived from three annotators should be
  supplemented with an adjudicated expert gold set, per-class sensitivity and
  specificity, calibration, and uncertainty under family/task clustering.
- There is no baseline against Git+CI, MLflow alone, LangSmith, or Langfuse.
- There is no operator study for the paper's central value claim of reviewability.
- There is no fault-injection matrix across the two dual-write boundaries.

### M8 — The paper is not currently a committed/reproducible artifact

**Severity: major for artifact evaluation.**

At the inspected revision, `git ls-files paper` returns zero and `git status`
shows the entire `paper/` directory as untracked. The Availability section says
the paper source and evaluation harness are contained in the project repository
(`paper/tex/main.tex:85-94`), which is not true of the inspected Git revision.

The generated statistics do correctly identify implementation revision
`b6b2b472b`, and regenerating both generated files produced byte-identical hashes.
That is a strong practice. It must be completed by committing the paper, pinning
the experiment environment, and providing one executable reproduction entrypoint.

### M9 — The manuscript is substantially over-length and repetitive

**Severity: major for conference submission.**

The authoritative build is 52 single-column pages and approximately 25,244 PDF
words. The README itself estimates about 27 body pages in the two-column form,
roughly twice a common 13-page systems limit. The abstract is approximately 489
words, far above the usual 150–250-word range.

The same messages recur in the abstract, introduction, architecture, components,
capabilities, design decisions, comparison, limitations, and conclusion:

- availability is not inheritance;
- containment is not isolation;
- a gate differs from a verdict;
- rollback is restore rather than reconstruction;
- the evaluation is unrun.

Repetition currently substitutes for evidence. Keep the thesis once, prove it,
and move the component inventory, detailed decision log, configuration notes, and
family-by-family mechanics to an appendix or separate technical report.

### M10 — Current product claims need current citations

**Severity: moderate.**

The bibliography is broad and compiles cleanly, but current software capability
claims often cite a 2018 MLflow paper or repository homepages. A 2018 lifecycle
paper cannot support 2026 Prompt Registry, alias, cache, optimization, and lineage
features. Similarly, competitor feature-absence claims need dated documentation
or a reproducible feature audit—not a generic project citation.

Use peer-reviewed papers for conceptual antecedents and versioned official
documentation or tagged source revisions for current product capabilities.

## Evaluation redesign

The revised paper should organize evaluation around falsifiable claims rather
than around a general feature inventory.

### RQ1 — Does every live-target mutation satisfy the governance contract?

Build an automatically generated inventory of every route, assistant capability,
and internal caller that can mutate a live target. For each, assert:

1. durable release intent exists before the external effect;
2. outgoing and incoming immutable targets are recorded;
3. actor and effective scope are recorded;
4. evidence/gate state is recorded or an explicit emergency-policy reason exists;
5. completion or reconciliation state is durable;
6. rollback is possible or explicitly unsupported by that family.

The success criterion should be 100% coverage, not a sampled percentage.

### RQ2 — Is release state crash-consistent?

Inject process death and database/provider failure at every boundary:

- before intent commit;
- after intent commit but before external effect;
- during external effect;
- after external effect but before completion commit;
- during event publication;
- during rollback at the same points.

For each case, assert the permitted terminal states and measure reconciliation
time. No run may leave a changed live target that is absent from the durable
release state.

### RQ3 — What are the delivery semantics of each background loop?

Evaluate each loop separately under contention, lease expiry, heartbeat loss,
database failover, and process death. Report concurrent-ownership violations,
replays, duplicates suppressed by idempotency keys, indeterminate effects,
failed jobs, recovery latency, and throughput. Do not aggregate loops with
different semantics into one exact-once number.

### RQ4 — Does CALIBER improve release review and recovery?

Use at least two baselines:

- Git + CI/evaluation scripts + code deployment;
- the strongest prompt-lifecycle baseline available in MLflow, LangSmith, or
  Langfuse.

Run a within-subject operator study with realistic incidents. Measure time and
accuracy for:

- identifying the currently live resource;
- finding evidence for a change;
- determining who authorized it;
- reconstructing prior state;
- rolling back correctly;
- detecting a release that bypassed policy.

Report learning effects, participant background, task order randomization, and
confidence intervals. Reviewability is the central value claim and needs human
evidence.

### RQ5 — Does the per-family capability model prevent missing wiring?

Define machine-checkable capability declarations and mutation tests. Ask
developers to add a small new family under:

- the current documentation/table-only approach;
- the declared capability/conformance approach.

Measure missing authorization/audit/gate hooks, implementation time, and review
defects. At minimum, demonstrate that a deliberately omitted hook fails a static
or dynamic conformance test.

### RQ6 — What is the operational cost?

Retain E1, but report hardware, database tuning, connection pools, data size,
cache TTL, offered-load method, trial count, confidence intervals, and cold/warm
behavior. Measure alias-change propagation time to clients—not just server lookup
latency—because cache TTL determines how quickly a release or rollback becomes
observable.

### RQ7 — How effective is the gate?

Use sealed, family-stratified expert labels; separate judge development,
calibration, threshold selection, and test data. Report per-family confusion
matrices, calibration, confidence intervals, false-advance and false-withhold
costs, inter-rater agreement, and off-distribution degradation. The release
policy must be selected before the sealed test is opened.

### RQ8 — What security boundary is actually measured?

Add adversarial tests for malicious local tools, compromised MCP servers,
retrieval-delivered prompt injection, scope-confused assistant capabilities, and
cross-project access. Report containment separately from isolation and do not turn
local-path tests into multi-tenant security claims.

## Recommended implementation changes before rewriting the paper

### Priority 0 — Restore correctness of the central contract

1. Make prompt creation/versioning non-live by default.
2. Centralize all alias/live-target changes behind one release service.
3. Persist a release intent and exact outgoing target before any external effect.
4. Add durable `prepared`, `applying`, `applied`, `failed`, and
   `reconcile_required` states.
5. Make retries idempotent using a release operation ID, not only `(asset,
   version)` assumptions.
6. Add a reconciler and operator-visible incomplete-release queue.
7. Fix `alias_changed` so it reports observed success, not requested intent.
8. Generate and test a complete inventory of live-target mutation call sites.
9. Decide whether Apply means operator confirmation or independent approval;
   name and enforce it consistently.

### Priority 1 — Correct the queue model

1. Document and test delivery semantics per loop.
2. Remove the generic exact-once claim.
3. Align the pseudocode with literal SQL used by each loop.
4. Test crashes after external side effects and before ledger settlement.
5. Surface indeterminate effects as a first-class result in the paper.

### Priority 2 — Make per-family guarantees executable

1. Define capability interfaces such as `Versioned`, `Evaluatable`,
   `Releasable`, `Rollbackable`, and `EvidenceProvider`.
2. Declare each family's capability set in code.
3. Generate Table 1 from those declarations.
4. Add conformance tests for required scopes, audit, evidence, gate, and rollback
   behavior.
5. Fail CI when a route mutates a declared live target outside the release
   service.

## Recommended paper rewrite

### Revised thesis

Avoid the unsupported “unoccupied ecosystem” and false all-or-nothing contract
claims. A stronger and more defensible thesis would be:

> CALIBER integrates release governance for heterogeneous AI-agent resources over
> a shared typed substrate, while representing and mechanically checking the
> family-specific capabilities and guarantees that cannot be uniform.

This thesis becomes publishable only after the capability declarations,
conformance checks, crash-consistent release path, and evaluation exist.

### Revised contribution list

Limit the main paper to four contributions:

1. A typed capability model for heterogeneous agent resources.
2. A crash-consistent governance chain from evidence to live-target mutation.
3. An implementation demonstrating the model across a small, representative set
   of families—not an inventory of every feature.
4. An evaluation of contract coverage, failure recovery, operator
   reconstructability, and overhead against credible baselines.

Queue implementation, UI breadth, deployment topologies, configuration, and the
full nine-family matrix can remain in the technical report or artifact appendix.

### Related-work rewrite

Create a dated comparison that distinguishes:

- prompt versioning;
- environment/live labels;
- rollback;
- access control;
- evidence binding;
- enforced pre-release policy;
- independent approval;
- audit/reconstruction export;
- heterogeneous asset families;
- cross-family impact analysis.

Mark “unknown/not verified” separately from “absent.” Do not group MLflow,
LangSmith, Langfuse, and Phoenix into one cell when their current capabilities
differ.

### Length target

For a conference version:

- 180–230-word abstract;
- 12–13 pages of body, subject to venue rules;
- at most four main figures and four main tables;
- architecture, claim, implementation, and evaluation in the body;
- component catalogue, decision log, API surface, configuration notes, and
  extended family matrix in appendices or the technical report.

### Title

The current title is accurate only if all live release paths are governed. After
the contract is repaired, it can remain. If the implementation remains scoped to
the refinement path, use a narrower title such as:

> **CALIBER: Per-Family Governance for Releasing AI-Agent Resources**

## Claim-by-claim correctness matrix

| Claim | Evidence checked | Assessment |
| --- | --- | --- |
| Generated implementation counts match the inspected tree | Regenerated `paper/generated/stats.tex` and `implementation-table.tex`; SHA-256 hashes unchanged | **Verified** |
| Paper compiles without undefined references/citations or oversized floats | Forced four-pass `pdflatex`/BibTeX build and `make check` | **Verified**, with remaining warnings |
| A candidate-ready Apply path creates a human action, checkpoint, and audit record | `routes/jobs.py`, `apply.py`, focused tests | **Verified for this path** |
| Every prompt live-target mutation follows the same governed release contract | Prompt create/version/default-promotion and direct alias paths | **False** |
| Outgoing target/checkpoint is committed before external alias rotation | Algorithm 2 versus `apply.py`, `promoter.py`, and `routes/prompts.py` | **False** |
| The advancement gate is unbypassable for releases | Direct prompt create/version/alias routes | **False system-wide; true only inside the refinement state machine** |
| One claim-and-lease algorithm describes the durable loops | Calibration, refinement janitor, workflow-run worker | **False; mechanisms and recovery differ** |
| Conditional claim implies exact-once execution | Algorithm, effect ledger, recovery code | **Unsupported/incorrect** |
| Human Apply is independent approval/separation of duty | `routes/jobs.py` mints a born-approved row for the same operator | **False as an independence claim; true as a human confirmation** |
| Current ecosystem lacks prompt live targets and release paths | Current MLflow, LangSmith, Langfuse primary documentation | **False** |
| Quantitative evaluation has been executed | Results table and evaluation section | **False and correctly disclosed** |
| The complete release is reconstructable from the E4(k) access bundle | Protocol excludes MLflow although MLflow owns critical prompt/trace state | **Protocol internally inconsistent; untested** |
| Paper source is contained in the inspected Git revision | `git ls-files paper` and `git status` | **False in this checkout; paper is untracked** |

## Minor comments

1. Replace sweeping statements such as “production LLM-agent systems are
   observable but not governable” with scoped, evidence-backed language.
2. The abstract should not enumerate nearly every mechanism; state problem,
   approach, two contributions, measured result, and limitation.
3. “AI-agent resources” is understandable but less standard than “AI agent
   resources” or “agent resources”; choose one convention consistently.
4. The statement that the resolver is the “only CALIBER code” on the serving
   path needs a precise deployment/API definition and implementation reference.
5. The claim that a team adopting CALIBER “almost certainly already runs MLflow”
   is unsupported and unnecessary.
6. Report the exact venue and page policy before maintaining two layouts.
7. The bibliography should include access/version dates tied to comparison rows,
   not only generic project access dates.
8. Avoid calling the empty evaluation table a contribution. The protocol is an
   artifact; results are the contribution.
9. “No worker tier” is a deployment choice, not inherently a research result.
10. Give the threat model a dedicated table: adversary, trusted component,
    protected asset, enforced boundary, residual risk, and evidence.

## Verification record

The following checks were performed during this review:

- `git rev-parse HEAD` ->
  `b6b2b472bd9670c431babc562bfc70e68336fbd9`.
- Worktree inspection found a pre-existing deleted report and the complete
  `paper/` directory untracked. No unrelated file was restored or modified.
- Forced paper build: four `pdflatex` passes plus BibTeX completed; 52 pages;
  zero overfull boxes; three underfull boxes; no undefined citations or
  references; no oversized floats.
- PDF inspection: approximately 25,244 extracted words; approximately 489 words
  in the abstract; embedded Type 1 fonts; no Type 3 fonts.
- Build log inspection found 18 `hyperref` PDF-string warnings, font-size
  substitutions, and an undefined small-caps-italic shape. These are polish
  issues, not correctness failures.
- Generated statistics were regenerated. Both generated file hashes were
  unchanged.
- Seven focused central-path tests passed in 7.70 seconds: Apply, prompt-alias
  rollback visibility, two claim-race tests, two janitor tests, and workflow
  lease recovery.
- A broader focused selection was not counted as a pass: it was interrupted
  after its partial JUnit report recorded 224 tests with zero failures; 44 of
  the collected tests had not completed. Only the seven terminal tests above
  are used as passing evidence.
- Current primary documentation for MLflow, LangSmith, and Langfuse was checked
  on 2026-08-03 for the related-work assessment.
- The LaTeX skill's documented bundled Tectonic binary was absent. The system
  Tectonic executable then failed because the manuscript forces the `pdftex`
  hyperref driver. The repository's declared `pdflatex`/BibTeX build path was
  therefore used and passed.

## Acceptance checklist for a resubmission

A revised paper should not be submitted until all of the following are true:

- [ ] Every live-target mutation is inventoried and forced through one governed
      release service.
- [ ] Durable intent/checkpoint precedes the external effect; completion and
      reconciliation are durable.
- [ ] Crash injection covers every release and rollback boundary.
- [ ] Gate and approval claims match all alternate paths and actual scopes.
- [ ] Queue semantics are stated and tested per loop; “exactly once” is removed
      unless formally and empirically established end to end.
- [ ] Table 1 is generated from machine-checkable capability declarations and
      conformance tests.
- [ ] The closest-system comparison is rebuilt from current primary sources.
- [ ] Results are populated for governance coverage, crash consistency,
      concurrency/recovery, operator reviewability, gate validity, and overhead.
- [ ] E4 reconstruction includes every authoritative store or a self-contained
      export bundle.
- [ ] The paper and experiment harness are committed at the revision named in
      the manuscript.
- [ ] A one-command artifact reproduction path is documented and tested in a
      clean environment.
- [ ] The main paper is reduced to the target venue's limit and the abstract is
      below 250 words.
- [ ] Build warnings affecting metadata/font substitution are resolved.

## Final assessment

CALIBER is not failing because the project lacks engineering effort. It is
failing because the paper's strongest guarantees are broader and cleaner than
the implementation paths that currently exist, while the empirical section has
not yet tested the mechanisms most likely to break those guarantees.

The best revision strategy is therefore not to add more features or more prose.
First make the governed-release contract singular, unavoidable, and
crash-consistent. Then represent per-family guarantees as executable capability
contracts. Finally evaluate the resulting system against the strongest current
prompt-lifecycle baselines and with real operators. If those three steps succeed,
the work can become a credible and distinctive systems contribution.
