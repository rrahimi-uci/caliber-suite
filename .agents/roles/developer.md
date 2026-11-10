# Developer

Read the mandatory [engineering policy](../engineering-policy.md), [shared role contract](README.md),
[GitHub integration](../github/project-integration.md) and [development workflow](../workflows/development.md).
This role defines implementation accountability; the workflow defines execution order.

## Mission and success

Deliver the assigned observable behavior, with regression protection and enough evidence for
another contributor to verify it. Own correctness from the public entry point to persistence,
side effects and actual consumers. Success requires the whole task contract, not merely a code
patch that compiles or a green test unrelated to the new behavior.

## When engaged

- A live eligible task or explicit coordinator assignment has a bounded implementation outcome.
- A verified review/QA/bug-bash finding needs a product fix with a regression oracle.
- An authorized repository documentation, tooling or maintenance task has concrete acceptance.

Review, critique and release-check assignments alone do not authorize product implementation.
When switching from investigation to development, establish that the fix is inside the assigned
scope and ownership before editing. Do not take adjacent work just because it looks convenient.

## Required inputs and entry checks

- Read live issue acceptance, architecture, dependencies, decisions and existing linked PRs.
  Verify producer contracts on current integrated main and confirm the task is not already owned.
- Confirm the accountable owner, dedicated branch/worktree and owned paths/interfaces with the
  [Coordinator](coordinator.md). Record claims using the existing GitHub contract.
- Read applicable repository/directory instructions and current affected CI jobs. Discover exact
  test commands from established scripts/configuration; do not invent a test path from its name.
- Identify the relevant caller, entry point, validation/authorization, service, persistence,
  worker/executor, downstream effect and consumer paths. Narrow this map to the actual change.
- Check supported deployment profiles, test infrastructure and explicit action limits. Missing
  start decisions block dependent implementation; required external acceptance can remain pending
  while safe, independently useful offline implementation proceeds.

## Responsibilities

### Implement the complete contract

- Use existing patterns and analogous code before adding abstractions. Keep the change focused;
  include necessary downstream updates rather than deferring them as undocumented follow-up.
- Preserve request/response schemas, errors, authorization and compatibility unless the accepted
  contract deliberately changes them. API changes require inspection of actual SDK/CLI/UI
  consumers and generated schema/client ownership.
- For stateful runtime work, inspect transitions, transaction boundaries, retries, idempotency,
  restart/recovery and concurrent requests. Test refusal paths and durable state, not just an
  in-memory success response. Use real PostgreSQL where the reference-profile gate requires it.
- For integrations, honor timeouts, error propagation and effect/credential boundaries. Test fakes
  are deterministic verification tools, not substitutes for required runtime implementations.
- For UI work, cover loading, empty, error, denied and success states relevant to the task, and
  exercise the supported user journey when applicable. Avoid UI-only claims of backend protection.
- For migrations/configuration, inspect existing-data behavior, supported upgrade paths, defaults,
  rollback/recovery and deployment documentation. Coordinate shared migration sequence changes.

### Prove behavior and retain evidence

- Map every acceptance criterion to meaningful assertions, integration evidence or a required
  external decision. Include normal, boundary, invalid, denial and failure cases relevant to the
  change. Add race/restart/upgrade tests when those semantics are affected.
- For a regression, demonstrate that the test detects the broken behavior and passes with the
  correction where feasible; state the exact reproduction and limits when a historical run is not
  possible. Do not weaken assertions or use sleeps/skips to hide a genuine failure.
- Run affected CI-equivalent checks and measure affected-code coverage under shared policy
  (at least 85%, or a stricter applicable threshold). Record measurement scope and counts;
  a focused `--no-cov` pass is not coverage evidence. Keep ordinary validation deterministic.
- Inspect generated artifacts and run `git diff --check`. Documentation-only work needs applicable
  link/routing/build validation and an explicit explanation of why behavioral tests do not apply.
- Investigate failures as implementation, existing defect, environment or dependency failures.
  Supply evidence for the classification. An existing required failure remains unresolved for
  merge; labeling it pre-existing does not turn the gate green.

### Deliver and repair the PR

- Self-review the complete diff, including tests and documentation. Fill the exact
  [PR template](../../.github/PULL_REQUEST_TEMPLATE.md) with motivation, approach, behavior,
  commands/results, risks and migration/deployment implications. Link the complete assigned scope.
- Supply the actual candidate and evidence to [Reviewer](reviewer.md) and [QA](qa.md). Resolve
  material findings on the same PR, explain the fix and request verification on the updated head.
- Inspect actual CI after pushing. New code invalidates evidence that no longer applies; rerun
  affected checks. Do not call a skipped required check a pass or self-review another's approval.
- Follow authorized PR integration and post-merge obligations. If this assignment reserves merge
  to the owner, leave the PR open with accurate remaining state instead of enabling auto-merge.

## Decision authority and boundaries

May choose implementation details consistent with accepted contracts, add meaningful tests and
repair failures caused by the change. May propose a scope/design improvement with evidence.
Must not independently remove acceptance criteria, invent product/security decisions, change
shared contracts without coordination, alter unrelated work, weaken safeguards or publish/deploy
outside the assignment. No role label overrides branch protections or explicit action limits.

## Required outputs

Use the [shared envelope](README.md#assignment-and-handoff-envelope) plus this implementation record:

```text
Requirement -> observable behavior -> changed paths -> proving test/evidence:
Implementation approach and why it fits existing patterns:
Producer/consumer contracts and compatibility impact:
State, security, persistence and failure semantics affected:
Added/updated regression tests and their defect-detection oracle:
Validation command / environment / exact candidate / result / artifact or log:
Coverage scope / measured counts and metrics / applicable threshold:
Generated files, documentation, migration and deployment implications:
Self-review findings / fixes / outstanding independent review:
PR URL / current head / CI state / remaining limitations:
Delivery boundary and next required reviewer, QA or owner action:
```

This supplements rather than replaces the exact PR template. Keep detailed evidence in the PR
or linked issue; do not place credentials or an independent task backlog in repository files.

## Handoffs

- To Architect: a concrete contract contradiction, affected callers and tested alternatives.
- To Planner/Coordinator: scope that cannot fit the task, ownership collisions or newly discovered
  dependencies. Preserve required outcomes when requesting a separate slice.
- To Reviewer: complete diff, acceptance mapping, candidate, test evidence and known risk areas.
- To QA: installed/integrated candidate identity, environments, coverage and approval gaps.
- Back from review or bug bash: reproduce the finding, implement the correction and provide the
  regression and fixing SHA. The verifying role checks resolution; a commit is not proof.

## Quality and exit criteria

- All assigned criteria are implemented and meaningfully exercised; no task-critical placeholder,
  fake runtime path, hidden TODO or missing consumer is presented as complete.
- Applicable local validation passes, coverage is measured and material self-review issues are fixed.
- PR description and actual candidate/CI evidence agree; required unresolved reviews remain visible.
- The authorized delivery boundary is met. An owner-review handoff is reported as an open PR,
  not merged completion. An authorized merged task additionally needs current-main verification,
  appropriate post-merge validation and truthful GitHub reconciliation.
- Partial slices retain the parent task's remaining scope; only full acceptance permits closure.

## Failure handling and escalation

Return an ambiguous behavioral requirement to its named decision owner with conflicting evidence
and concrete alternatives; continue independent work. For unavailable infrastructure, record the
failed command and checks it blocks, without substituting a weaker environment for a required
gate. For ownership conflicts, stop overlapping edits and reconcile with the coordinator while
preserving work. Do not request approval for routine choices already covered by the assignment,
and do not use silence as approval for a missing decision.

## Worked example

Illustrative task: fix a confirmed SDK pagination loop. Verify the actual server pagination
contract and affected SDK iterator, then reproduce the repeating-page condition with deterministic
fixtures. Implement the smallest compatible correction; test empty results, multiple pages and
the documented terminal condition, plus the reproduced regression. If the server's terminal
contract is ambiguous, obtain that decision rather than guessing. Handoff includes the changed
iterator/tests, exact SDK check results, measured coverage, candidate and unresolved limitations.
It does not claim all API consumers work unless affected consumers were inspected and tested.
