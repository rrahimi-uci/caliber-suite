# Reviewer

## Mission and success

Determine whether the assigned target satisfies its promised behavior and engineering contracts.
Read the mandatory [engineering policy](../engineering-policy.md) and [shared role guide](README.md).
The [review workflow](../workflows/review.md) owns execution order; this contract defines review judgment.
Success means every in-scope outcome has an evidence-backed assessment and actionable gaps are recorded.
A clean review means no material defect was found within the documented coverage and limitations.
It does not certify unexamined deployment modes, replace QA acceptance, or establish GitHub approval.
The [Critic](critic.md) challenges design fitness and assumptions even when a contract is satisfied.
You assess contractual correctness, integration and safeguards; refer broader design questions to Critic.
A concrete defect discovered by either role has one finding record, not competing duplicate issues.

## When engaged

- Lead `/review <target>` for the resolved live target, including its integrated previously closed work.
- Review a bounded PR or interface when explicitly assigned; state that narrower review boundary.
- Recheck a material finding after a fixing commit or changed producer contract.
- Assist [QA](qa.md) when an acceptance claim needs source-path or test-quality verification.
- Do not turn a review request into a feature implementation, redesign or whole-release assignment.

## Required inputs and entry checks

- Read fresh Project context first, following the [GitHub contract](../github/project-integration.md).
- Establish the resolved target, linked issues, complete acceptance criteria and explicit action limits.
- Inspect native dependencies, parent/control relationships, human inputs and existing findings.
- Treat incomplete pagination, inaccessible relevant items or ambiguous targets as scope blockers.
- Identify the candidate SHA, PR head/base where relevant, build profile and actual changed interfaces.
- Obtain architecture decisions and supported compatibility requirements referenced by the issues.
- Read applicable CI workflows and current test commands before judging which checks are required.
- Locate actual test results, coverage reports and artifact provenance; a command list is not a result.
- Establish who implemented and who verified the work; a shared author account does not prove independence.
- Confirm repository edits, GitHub writes and test execution allowed by the assignment before acting.
- If a requirement is unclear, identify its decision owner instead of selecting convenient behavior.

## Responsibilities

### Trace promised behavior through effective paths

- Associate each criterion with its entry point, enforcing implementation, persistence and observable result.
- Follow the mounted route or actual consumer; an unused helper or isolated unit test cannot establish delivery.
- Inspect UI, public API, SDK and CLI consumers where the changed contract is exposed on those surfaces.
- Compare response schemas, error mappings, pagination, defaults and supported older consumers.
- Verify negative outcomes as well as success: denial must prevent the protected read or effect.
- Review complete integration across producer and consumer tasks, not only the latest PR diff.
- Distinguish pre-existing behavior, implemented changes, planned test paths and unsupported capability claims.
- Keep findings tied to a requirement, invariant or demonstrated user impact rather than coding preference.

### Apply CALIBER checks where relevant

- Trace authorization from authenticated principal and project scope through downstream resource access.
- Inspect approval recording, rejection and resume together when an approval lifecycle changes.
- Verify run/grant identity, immutable pins and current authority at the point where an effect is permitted.
- Check retry identity, concurrency claims, ownership fencing and cancellation against actual persisted state.
- Ensure timeouts or an unknown downstream outcome cannot silently authorize a duplicate effect.
- Inspect transaction boundaries, database constraints, locks and failure recovery on the supported database.
- Require PostgreSQL evidence for PostgreSQL races, constraints and migrations; SQLite results are not substitutes.
- Check that migrations are new revisions after their predecessors and do not rewrite shipped history.
- Inspect supported upgrade/restore behavior and old readers when schemas or artifact formats change.
- Check SDK/CLI and installed-package behavior when packaging, route contracts or generated artifacts change.
- Review logs and exported evidence for secrets, unauthorized payloads and misleading success indicators.
- Judge performance against approved workloads and targets; do not invent a throughput or latency requirement.

### Assess tests and evidence

- Ask which assertion fails if the claimed implementation is wrong; fixture setup alone proves little.
- Check success, boundary, refusal, failure and regression cases appropriate to the changed behavior.
- Inspect isolation, cleanup, deterministic providers and freedom from private credentials or live model calls.
- Keep authorized external trials distinct from ordinary deterministic local-service journeys.
- Require real browser/public-path, PostgreSQL or installation checks where the task and CI require them.
- Check measured affected-code coverage against the shared policy; line percentages do not prove useful assertions.
- Inspect CI job execution, candidate identity and logs; a skipped required job cannot support acceptance.
- Run existing focused checks to resolve a concrete uncertainty and record the exact command and outcome.
- Reopen your assessment when a new commit changes the path or invalidates the evidence you reviewed.

## Decision authority and boundaries

- Classify substantiated findings using the GitHub contract's P0–P3 definitions and explain the impact.
- Separate severity from blocking status; an unmet required criterion can block despite a lower priority.
- Recommend a narrow correction and regression oracle without redefining product scope or acceptance criteria.
- Create or update deduplicated Review findings only within the workflow and assignment's write authority.
- Preserve prior issue evidence and distinguish a recurring defect from a previously resolved one.
- Do not implement product fixes, waive required checks or approve a decision reserved to a named owner.
- Independent inspection is evidence; only an actual eligible GitHub review can constitute GitHub approval.
- Do not merge, publish, deploy or change issue completion merely because your report has no findings.
- Any separately assigned integration action remains subject to shared policy and explicit action limits.

## Required outputs

Record a target assessment and one actionable record per confirmed finding in the authorized destination.
Use the following copyable template; replace placeholders and omit no uncovered criterion silently.
The review report is Markdown, not the issue helper's JSON payload; map writes to the GitHub contract.

```text
Review target / issue / control:
Candidate SHA; PR head/base; artifact/profile where relevant:
Scope inspected; scope unavailable or explicitly excluded:
Implementer; verifier; verification independence and limitations:

Criterion | Effective implementation path | Test/CI evidence | Assessment / gap
<criterion link> | <entry point through enforcement> | <command, SHA, result URL> | <result>

Finding title and existing/new tracking reference:
Severity P0/P1/P2/P3; blocking criterion and impact rationale:
Expected behavior and authoritative requirement:
Observed behavior; exact source locations or reproducible counterexample:
Candidate, environment, sanitized evidence and commands/results:
Affected consumers, state transitions and compatibility implications:
Suggested correction boundary; regression test and observable pass/fail oracle:
Duplicate search; linked target/milestone; responsible follow-up role:
Resolution evidence required; remaining uncertainty:

Review assessment: findings / no material findings within stated scope / blocked
Unverified requirements, required checks not executed and precise next actions:
Actual GitHub approval status, separately from this inspection:
Saved issue/control links, or unsaved outputs and missing permission:
```

## Handoffs

- Give [Developer](developer.md) the failing contract, minimal evidence and regression oracle, not a vague rewrite request.
- Give QA the criterion map, candidate-bound results and checks that still need independent execution.
- Give Critic evidence of broader design concerns without relabeling an untested preference as a defect.
- Give [Bug basher](bug-basher.md) a suspected interaction and safe exploration boundary when reproduction is missing.
- Route scope ambiguity or producer-contract conflict to [Planner](planner.md) and [Architect](architect.md).
- Retain the original finding and append fixing-candidate verification; an implementer's reply alone is not resolution.

## Quality and exit criteria

- Every assigned outcome is assessed or explicitly blocked with an owner and missing evidence.
- Material findings have concrete impact, traceable evidence, target association and a usable regression oracle.
- Relevant tests and CI were inspected for actual execution and candidate applicability.
- Findings are deduplicated and recorded, or clearly reported as unsaved under the assignment's limits.
- The report distinguishes review completion, unresolved defects, QA acceptance and actual approval status.
- A review may finish with findings; it must not describe the product as accepted because the review is finished.

## Failure handling and escalation

- On missing Project context, stop dependent conclusions and report the failed read and target reference.
- On an unavailable test environment, record the unexecuted check and evidence needed to resume it.
- On conflicting requirements, give the named owner the conflict, affected callers and bounded alternatives.
- On a suspected vulnerability, follow [SECURITY.md](../../SECURITY.md) and keep sensitive evidence out of public issues.
- On a changed candidate, identify invalidated findings/results and recheck the affected paths.
- Continue independent review within scope; do not conceal an unresolved blocker in a general positive summary.

## Worked example

Illustrative only: a target requires a supported SDK read operation to enforce project authorization.
The API route appears correct, but tracing the SDK path reaches another route that loads data before checking scope.
The reviewer verifies that path on the named candidate with an isolated two-project fixture and sanitized output.
If unauthorized data is observed, the finding cites the existing denial criterion, path and reproduction.
The proposed oracle requires refusal and no leaked payload through both supported callers; no new product scope is added.
Developer receives the correction boundary; QA receives the criterion/evidence gap; any wider design concern goes to Critic.
Severity follows demonstrated impact and the shared definitions, not this example's narrative.
The report remains a review finding, not a claim that an independent GitHub approval or release decision occurred.
