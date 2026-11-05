# Development workflow

Lead: [Developer](../roles/developer.md); a coordinator owns integration and delegates Reviewer/QA.

Read the [engineering policy](../engineering-policy.md) and
[GitHub integration contract](../github/project-integration.md). Honor explicit assignment limits.
The command's first operation is live Project context; repository inspection follows that read.
If context is unavailable, incomplete or ambiguous, report the blocker and do not infer scope
from a local plan or stale chat. Follow the linked issues and their complete acceptance contracts.

1. Resolve the assigned scope and read live Ready, In Progress and blocked work, priorities, native
   dependencies, human inputs, complete issue contracts, linked PRs and architecture. Then inspect
   current main, worktrees, code, tests and applicable CI. Verify actual merged producer contracts.
   A control/Gate or epic is not a substitute for an eligible implementation task.
2. Coordinate existing work before choosing another task. Select dependency-ready, unclaimed tasks;
   do not take a task merely because the GitHub login matches a shared account. Record owner and
   dedicated branch with a claim transition, then re-read the issue and Project. Confirm exclusive
   ownership with the coordinator before editing: GitHub writes do not provide an atomic lock.
3. Use 3–4 agents only when the environment actually provides delegation and work is independent.
   Give each an isolated branch/worktree from current integrated main and the handoff below.
   Serialize shared files, contracts, migrations and generated clients; empty owned paths mean
   serial execution. If subagents are unavailable, use the same workflow sequentially. The CLI
   does not launch models, assign locks or run a fleet.
4. In each task, inspect affected public paths and analogous implementations, implement the smallest
   complete change, add meaningful regression and failure tests, run the issue/CI validation and
   measure affected-code coverage under the policy. Check lint, format, types, builds, integration,
   browser, migrations, docs and packaging where applicable. Inspect generated outputs and run
   `git diff --check`. Investigate failures; never weaken tests or revise acceptance silently.
5. Self-review the complete diff; coordinator reviews delegated diffs and evidence. Open/update a
   focused PR using the exact template, link the issue, and record the review transition with the
   PR and validation evidence. Request eligible reviewers, resolve material findings on the same
   PR, rerun affected checks and keep the description current. Report actual reviewer provenance.
6. Record QA with evidence when applicable. Verify the latest PR head, required CI, review decisions,
   mergeability and all policy gates before merging through the PR. Honor explicit no-commit,
   no-push or other task limits. Unresolved required gates leave the PR unmerged; work may continue
   on independent eligible tasks within the assignment.
7. After merge, verify the expected change on current main, synchronize the owned worktree, run
   appropriate integration/post-merge checks and confirm main health. Record acceptance evidence
   on the issue. Close it only when its entire contract is met; then use Done to reconcile the
   Project. The helper requires an issue closed as COMPLETED but does not prove acceptance, inspect CI or close it.
8. Refresh context after each integration, re-evaluate dependencies and continue eligible assigned
   tasks until development scope is complete or genuinely blocked. Record new required work via
   deduplicated GitHub issues. Report exact remaining blockers and accountable decisions; a release
   acceptance decision requires the release-check workflow even after implementation is complete.

## Delegation handoff

```text
Target and issue URLs; bounded expected outcome:
Acceptance criteria and explicit action limits:
Producing issues/contracts and integrated main SHA:
Owned paths/interfaces; adjacent owners; integration order:
Compatibility, migration and excluded-work boundaries:
Required positive, denial, failure, race/restart and regression tests:
Required CI jobs, measured coverage, docs and evidence destination:
Pending start/acceptance decisions and accountable owner:
Implementation owner, branch/worktree, PR, reviewer and integration owner:
```

A lost worker, failed claim or conflicting owner requires reconciliation before retry. Preserve
other contributors' edits; never reset or clean their worktree to make integration easier.
