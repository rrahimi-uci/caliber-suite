# Executing one CALIBER development task

> **Archived planning record — retired 4 November 2026.** [GitHub Project #3](https://github.com/users/rrahimi-uci/projects/3) and its linked Issues are the sole current planning authority for scope, milestones, dependencies, priorities, acceptance criteria and progress.
> Everything below records historical planning and architectural rationale, including statements that formerly called this document or its manifest canonical. Those statements are no longer operational instructions. Preserve evidence and issue links; do not synchronize this archive back to GitHub or infer current readiness from it.
> Use the repository agent entry points and canonical `.agents/` workflows for current work. See [archive tooling and migration notes](PLANNING_ARCHIVE.md).

## Historical record

The [manifest](beta-mvp-execution.json) is the versioned contract. Each GitHub task has a generated **Current execution brief**; collapsed earlier text preserves implementation history. The [audit](PLAN_AUDIT_2026_11.md) explains corrected requirements. Do not combine conflicting historical and current instructions.

## Select and establish the task

1. Select an open task from Project #3's Ready tasks view. `Ready` means its producing issues are closed and no start decision is pending; verify their commits and contracts are actually present on current `main`. It is not release approval. `Done` follows closed issue state; this audit does not recertify completed work.
2. Read repository/directory `AGENTS.md`, the task, parent decision, producer issues, applicable workflows and existing implementation/tests. Start a dedicated branch/worktree from current `main`. Preserve unrelated working changes.
3. Historical handoffs can now be inspected only as archives: `node docs/planning/render-task-brief.mjs --archive 796` (replace 796 with the assigned issue). Check expected outcome, dependencies, files, ordered work units, test targets, commands, evidence path and human inputs before editing.
4. `Needs input` blocks coding for the named start decision. An `acceptance` input permits deterministic fixtures and contract implementation, but blocks the real trial, owner-specific configuration or certification it governs. Never invent issuer URLs, mappings, thresholds, budgets, corpus approval or independent reviews. Record exact missing input in #608. Role names are not confirmed assignees.

Use the [product roadmap](PRODUCT_ROADMAP.md) to distinguish the destination Release from the three-week Delivery cycle. A blank cycle is unscheduled; a cycle assignment never overrides dependencies or human inputs.

## Implement within the boundary

Use each ordered work unit as a reviewable PR slice. A broad issue remains the acceptance container, not a direction to implement every layer in one patch. Do not close it until all units and acceptance evidence are complete. If a unit still exceeds roughly two development days after inspecting its producers, first split its concrete interfaces/tests into subissues and update the parent; this sizing rule is a planning heuristic, not permission to drop scope. Unknown interface behavior is a blocker to resolve with its producer, not an invitation to invent a second service.

The named files are owners and starting points, not permission to rewrite adjacent subsystems. Reuse the existing principal, grants, dispatcher, proposal ledger, budget reservations, resource binding identities and public route conventions. Preserve supported readers before changing writers. A planned test path is a deliverable; an existing test must be read and extended rather than overwritten. Database changes use a new migration after merged predecessors.

At most two implementation streams use the historical capacity assumption; one integrator owns shared models, runtime/dispatcher, server routes, migrations and generated clients. Serialize overlapping edits or agree exact contracts before parallel work. Child tasks write their own evidence fragments; only the D-control issue updates the shared acceptance report. Dependency depth/`Execution order` is a sorting aid, not an effort estimate or permission to ignore blockers.

## Set up and run validation

Follow the checked-in README/Makefile and CI versions. The local CI runner expects `caliber/.venv`, installed using `make -C caliber install-extended`, Node/npm for UI jobs, and Docker for PostgreSQL/Compose. Package jobs may install their own environments. Dependency installation may need network access; ordinary test fixtures must remain deterministic and offline unless the task explicitly owns an authorized external trial.

Run the exact focused commands in the task first, then its listed `scripts/ci-local.sh` jobs. Inspect every PASS/FAIL/SKIP: exit 0 alone is insufficient because unavailable prerequisites can be skipped. For changes spanning components, use `./test-all.sh` where required by the repository, plus applicable integration/contract/install checks. Read workflows again if the branch changes. Do not substitute a mock for required PostgreSQL locking, real sandbox isolation, or installed public-journey evidence.

Every behavioral change needs a meaningful positive case, refusal/error cases and the listed boundary/race/restart regressions. Measure affected production-code coverage with the established tooling and require greater than 95%; document exactly which code and command produced that measurement. Do not exclude real branches or add tautologies to improve the number. Documentation/manual evidence tasks explain why runtime coverage is inapplicable and execute their actual artifact, link, provenance and independent review checks. A Python unit suite does not certify a human trial.

## Complete the handoff

Record the exact SHA, commands/results, measured coverage, artifact hashes where relevant, review findings and unresolved inputs at the task's evidence path. No required skipped/failed check can be called passing. Report known baseline/environment failures precisely. Use the full PR template, link the task and control, and resolve material findings. Follow the [repository agent instructions](../../AGENTS.md) for the objective merge gate. After all applicable checks and review issues are resolved, the authorized agent may merge through the PR; never push directly to `main` or bypass branch protection.

After integration, reconcile issue state and Project #3 readiness with live evidence. Control acceptance is cumulative and uses one exact candidate. Earlier closed tasks and reports from another SHA do not automatically confer PASS. Reserve each Preview's third week for QA, bug bash, fixes and release validation. Missing required evidence means HOLD and moves the release date.

## Maintaining this plan

The former manifest-to-GitHub maintenance workflow is retired. Change current planning in GitHub Project + Issues. The following commands validate the preserved archive only:

```sh
node docs/planning/validate-execution-plan.mjs
node --test docs/planning/test-execution-plan-validator.mjs
git diff --check
```

Live synchronization and manifest parity validation are retired. `--live` and `--snapshot` now fail explicitly. Read current task scope and evidence directly from GitHub; never overwrite it from this archive.
