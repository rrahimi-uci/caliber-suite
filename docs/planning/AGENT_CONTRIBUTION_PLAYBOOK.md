# Contributing to CALIBER with a coding agent

This guide applies to people and coding agents using any editor or model, across every CALIBER development cycle. [AGENTS.md](../../AGENTS.md) is the repository-wide quality and merge contract; the [contribution guide](../../CONTRIBUTING.md) covers setup. [Project #3](https://github.com/users/rrahimi-uci/projects/3) tracks planned work. A tool-specific instruction file should point here rather than define a competing policy.

## 1. Establish the assigned scope and current state

An assignment can be one issue, several linked tasks, an epic, or a whole milestone. Deliver **that assigned scope** through its acceptance and PR lifecycle. A task assignment does not silently authorize unrelated milestone work; a milestone assignment is not complete after its first task or PR. The requester, current issue/specification, release plan and `AGENTS.md` define the boundaries. Record a material change to those boundaries in the issue and PR so the next contributor sees the same contract.

1. Start with an open, unclaimed Ready task in Project #3, or work explicitly assigned by a coordinator. Check the issue, parent epic when present, acceptance criteria, producing dependencies, native blocked-by edges, human-input phase, linked PRs, assignee and current `main`. `Readiness=Ready` records that known producers are closed and no start decision is pending; verify their merged contracts. A board status is a planning signal, not proof that code or release evidence exists.
2. Compare the relevant code, tests, public contracts and generated artifacts with the issue. Classify the assigned work as complete, partial, missing, obsolete, duplicated or incorrectly tracked **using evidence**. For a multi-task or milestone assignment, inspect the whole assigned plan and its critical path; for a focused issue, inspect affected components and downstream consumers rather than unrelated modules.
3. Read applicable repository and directory instructions, architecture, analogous implementations and [.github/workflows/ci.yml](../../.github/workflows/ci.yml). For a canonical planning task, follow the [execution guide](AGENT_EXECUTION_GUIDE.md) and use `node docs/planning/render-task-brief.mjs <issue-number>` to render its current brief. Other issues use their own issue/specification contract. A planned test path is a deliverable, not evidence that the test exists.
4. Check Project **Status**, issue assignee, linked branches/PRs and active worktrees. Record one accountable implementation owner and set **In Progress** before substantial edits. Without project-edit permission, record the claim and branch/PR on the issue and ask a maintainer to update the board. A board view is not an atomic claim service; agents sharing a GitHub identity need a coordinator to assign disjoint work. Recheck ownership before pushing.
5. Resolve a named `start` decision before coding dependent behavior. An `acceptance` decision may allow deterministic implementation but cannot be inferred as approval for a trial or release. Use the decision tracker named by the relevant issue or release plan. Do not invent dates, thresholds, customer consent, credentials or reviewer conclusions.

## 2. Plan and coordinate independent work

For a broad assignment, map required outcomes to reviewable slices. Record producing tasks, critical-path work, shared files and interfaces, integration order, required tests, evidence owners and release controls. Plan enough to avoid conflicting implementations, then implement. Re-evaluate the map whenever a producer merges or new evidence changes readiness.

Use parallel agents when independent work and available tools make it worthwhile. Backend, UI, API, migrations, SDK, infrastructure, security review, tests and documentation can be separate lanes **when their contracts and ownership are clear**. A coordinator owns the dependency map, task allocation, shared interface decisions, integration, conflict resolution and final verification. Delegation does not transfer accountability for the assigned outcome.

Give each contributor a written handoff:

```text
Issue(s), assigned scope and target cycle/milestone:
User-visible outcome and acceptance criteria:
Producing issues, exact merged contracts and current main SHA:
Owned files/interfaces and neighboring owners:
Implementation boundaries, compatibility and excluded work:
Positive, denial, failure, race/restart and regression tests:
Required local/PR CI jobs, coverage, docs and evidence path:
Pending start or acceptance decisions and accountable decision maker:
Branch/PR, review owner and integration owner:
```

Establish a shared interface before parallel changes depend on it. Do not give two agents overlapping ownership of a migration, schema, generated client or shared runtime path without an explicit integrator and serialization plan. An independent reviewer or test agent can inspect a separate workstream, but its findings need verification and resolution; a review agent's report is not an approval unless GitHub records the required review. When a dependency merges, recheck newly unblocked work and dispatch an independent slice if it reduces elapsed time without increasing integration risk.

## 3. Implement production-quality changes continuously

Create a dedicated branch or isolated worktree from current `origin/main`; never edit `main` directly. For each slice, inspect the surrounding implementation and existing tests, implement the smallest complete solution, add meaningful tests, run focused validation, investigate failures, fix root causes and review the final diff. Keep the PR independently understandable. Preserve supported readers and consumers before changing writers or public contracts.

Required behavior must not be represented by a placeholder, temporary hack, commented-out path, unresolved task-critical TODO or test fake used as a runtime substitute. Mocks and fakes are appropriate for deterministic tests when they exercise the real contract. Evaluate correctness, edge and refusal paths, error handling, authorization, privacy, security, performance, concurrency, compatibility, observability, maintainability and user experience where affected. Update documentation and configuration examples when behavior or developer workflows change.

If code contradicts the issue, record the concrete mismatch and reconcile the issue or consumers through a reviewable change. Do not silently narrow acceptance criteria or invent a replacement architecture. A large issue may use several focused PRs; keep the issue open until all required units and evidence are complete.

## 4. Test, validate and record evidence

Test during implementation, after material fixes and after integration, not only at the end. Run the affected unit, integration, end-to-end/browser, regression, contract, migration, lint, formatting, type, static-analysis, build, packaging, security, docs and Compose checks defined by the issue and CI workflow. Use `./test-all.sh` when the change spans components and that is the repository's comprehensive path. Always run `git diff --check`. Inspect generated docs/source, lockfiles, schemas, migrations, fixtures and snapshots when touched.

Every behavioral change needs tests for its acceptance criteria and material negative paths. Measure affected-code coverage using established tooling and meet the threshold in `AGENTS.md`; a focused `--no-cov` run is not coverage evidence. Keep ordinary tests deterministic and offline. Use actual PostgreSQL, services, installed packages or browser journeys when the task or CI requires those integration gates. Existing tests passing is insufficient if the changed behavior is untested.

Record the candidate SHA, commands **and results**, test IDs, measured coverage, relevant artifact hashes, known limitations and unresolved inputs at the issue's evidence path. Distinguish local-only, PR-CI passed, merged, deployed and live-verified states. A required check that did not run is unexecuted; a path-filtered job for an unaffected component is outside that PR's required suite. Investigate failures as implementation, baseline, environment or external dependency, fix change-caused failures and rerun affected and broader checks as appropriate. Never weaken assertions, suppress errors or skip a required check to get a green badge.

## 5. Review, integrate and merge through PRs

Open a focused PR using the repository's [exact template](../../.github/PULL_REQUEST_TEMPLATE.md). Explain goal, changed behavior, design decisions, tests and command results, coverage, risks, compatibility, migrations, deployment and docs implications, and linked issue/control. Request owner and Copilot review when GitHub permits; if the author identity prevents a request, record the actual result. Keep the PR body current as work changes.

The coordinator reviews a delegated diff and tests against the acceptance contract rather than trusting an agent's completion claim. Check compatibility with other workstreams, security/privacy, failure handling, data migration and conflict resolution. Resolve every material finding on the same PR, rerun affected validation and verify the **exact head SHA**. Self-review is required but is not independent approval.

An authorized agent may merge **through the PR** when the assigned slice is complete, meaningful tests and required coverage pass, every applicable local and PR CI job is green, the PR is conflict free, review findings are resolved, and branch protection and any required independent review are satisfied. A skipped, failing or unfinished **required** job blocks merge. Do not push to `main`, bypass protections or treat a simulated review as approval. If a genuine blocker remains, leave the PR open or draft with the exact reason and next required action.

After merge, verify `origin/main` contains the expected commit, synchronize the local worktree, run appropriate post-merge validation and check main health. Update issue/project status and evidence. Close a task only after its entire acceptance contract is met. Build dependent work from integrated `main`, not a stale producer branch. For a broader assignment, re-evaluate the remaining critical path and newly unblocked independent tasks, then repeat the cycle until its assigned scope is complete.

## 6. Keep GitHub and release evidence truthful

Keep the issue, linked PR, dependencies, assignee, Project fields and milestone control aligned with observed state. Record newly discovered required work before calling a scope complete. Mark an issue obsolete, duplicate or already complete only after checking code and evidence; preserve the links explaining that decision. Distinguish a task merged to `main` from a release gate passing. A closed issue or old report from another SHA does not establish acceptance for a new candidate.

When the assignment is a **milestone or release**, finish with a fresh audit on one exact integrated candidate. Verify every required outcome and issue, end-to-end user journey, service integration, migration and configuration, error and refusal path, security/reliability finding, build/package, CI result, documentation, and release evidence. Inspect task-critical TODOs and placeholders. Use independent review or test agents where useful, resolve their material findings, and rerun affected validation. Record actual required approvals, trial results and artifact hashes; unresolved gates mean HOLD and must not be reported as release-ready. A release is complete only when its declared acceptance control passes and the validated changes are on healthy `main`.

## 7. Add reusable agent workflows selectively

The repository instructions, issue brief and tests remain the durable contract. A contributor skill is useful when a specialized workflow recurs with stable inputs, commands and outputs, such as migration parity, installed SDK compatibility or release evidence assembly. First prove it on real PRs; then use the [workflow recipe template](AGENT_WORKFLOW_RECIPE_TEMPLATE.md) for a reviewed, versioned procedure with prerequisites, exact steps, failure handling, tests and a worked example. Keep Codex, Copilot or other tool wrappers thin and pointed at this repository guidance. A skill cannot grant merge authority, relax CI, approve a security finding or change product scope.

CALIBER also has **product skills** exposed to its users. Those are runtime artifacts governed by the product's own versioning, evaluation and release rules; they are separate from contributor instructions.

Documents alone do not atomically assign work or judge PRs. Unattended multi-agent operation needs a coordinator that selects tasks from live readiness, records one owner per task, serializes shared-file work, checks PR/CI/review state and reconciles Project #3 after merges. Automate those transitions only after the documented, human-visible workflow is reliable.
