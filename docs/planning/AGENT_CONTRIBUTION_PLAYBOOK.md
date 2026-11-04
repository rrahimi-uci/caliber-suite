# Contributing to CALIBER with a coding agent

This guide applies to any human or coding agent working on CALIBER, regardless of editor or model. [AGENTS.md](../../AGENTS.md) defines repository-wide quality and merge rules; the [contribution guide](../../CONTRIBUTING.md) covers setup. [Project #3](https://github.com/users/rrahimi-uci/projects/3) tracks work, and every canonical task has a current execution brief. Read these before accepting a task. A tool-specific instruction file should point back here instead of creating a competing policy.

## 1. Choose and claim a task

1. Start with an open task in Project #3's **Ready tasks** view, or one explicitly assigned by the coordinator. The `Release` and `Delivery cycle` fields describe destination and scheduling. `Readiness=Ready` only says the recorded producing issues are closed and no start decision is pending. Inspect those producers on current `main`; do not infer release acceptance from a closed issue.
2. Read the parent epic, current execution brief, native blocked-by edges, pending human inputs, and linked PRs. Render a local copy with `node docs/planning/render-task-brief.mjs <issue-number>`. Check the files and public consumers before changing a shared interface. A planned test path is a deliverable, not evidence that it exists.
3. Check the issue assignee, Project **Status**, linked branches/PRs and active worktrees. A project view does not atomically reserve work. Record one accountable owner and set Status to **In Progress** before substantial edits. A contributor without project-edit permission can record the claim on the issue with their branch/PR; a maintainer updates the board. If several agents share credentials, the coordinator must assign disjoint tasks and record branch/PR ownership. Recheck before pushing. If someone else owns the task, coordinate on the existing PR or choose another task.
4. If a product decision is marked `start`, stop coding the dependent task until the named owner supplies it. An `acceptance` decision permits independent deterministic implementation but does not authorize a live trial or release signoff. Keep the unresolved decision in [#608](https://github.com/rrahimi-uci/caliber-suite/issues/608). Do not invent approval, dates, thresholds, credentials, customer data or reviewer conclusions.

## 2. Prepare a reviewable slice

Create a dedicated branch or isolated worktree from current `origin/main`. Never edit `main` directly. Confirm the task's expected outcome, exact boundaries, dependencies, acceptance criteria, tests, documentation and evidence owner. A broad task can be split into focused PRs; keep its parent open until every required unit is complete. Coordinate changes to shared migrations, runtime, models, API contracts and generated clients before parallel implementation. Preserve an existing supported consumer until replacement compatibility is proven.

Use a brief handoff to another agent or collaborator:

```text
Issue and release/cycle:
Outcome users will observe:
Producing issues and exact merged contracts:
Files/interfaces owned; neighboring owners to coordinate with:
Implementation boundaries and exclusions:
Positive, denial, failure, race/restart and compatibility tests:
Required CI jobs, documentation and evidence fragment:
Pending start or acceptance decisions:
Branch/PR and accountable integrator:
```

This handoff references the current issue brief instead of copying an older chat plan. If actual code contradicts the issue, record the concrete mismatch and correct the issue/consumers through the same PR; do not silently substitute another architecture.

## 3. Implement, test and report evidence

Implement the smallest complete behavior for the slice. Add meaningful tests for the intended result and material negative paths. Use deterministic fixtures for ordinary tests and real PostgreSQL/service or installed browser checks where the task requires them. Measure the affected code's coverage as required by `AGENTS.md`; a focused `--no-cov` run is not coverage evidence. Follow [the execution guide](AGENT_EXECUTION_GUIDE.md) and the exact commands in the task brief. Read [.github/workflows/ci.yml](../../.github/workflows/ci.yml) for affected jobs, run `git diff --check`, and inspect generated documentation, migrations and compatibility artifacts when touched.

Record commands **and their results**, test IDs, measured coverage, candidate SHA, relevant artifact hashes and known limitations in the task's evidence fragment. A required check that did not run is unexecuted. Label a failure as implementation, baseline, environment or external dependency only after investigation. Fix failures caused by the PR and rerun the affected check. Do not weaken assertions or mark a required test skipped to obtain green CI.

## 4. PR, review, merge and handoff

Commit and push the task branch. Open a focused PR using the repository's [exact template](../../.github/PULL_REQUEST_TEMPLATE.md), with the goal, changed behavior, design decisions, validation commands/results, coverage, risks, compatibility, migration/deployment/docs implications, and linked issue/control. Request the owner and Copilot reviewers when GitHub permits; if author/reviewer identity prevents a request, record the actual API result rather than claiming review. Self-review the complete diff, address material comments on the same PR, and keep its description current.

The agent may merge **through the PR** only when the task's acceptance criteria, meaningful tests and required coverage pass; all applicable local and PR CI checks have run successfully; the PR is conflict free; review findings are resolved; and branch protection and any required independent review are satisfied. A job omitted by a documented path filter for an unaffected component is outside that PR's required suite. A skipped, failing or unfinished **required** job blocks merge. An agent's self-review is never an independent person's approval. If a genuine blocker remains, leave the PR open or draft with the exact reason and next required action.

After merge, verify `origin/main` includes the change, sync the local worktree, check integration health where relevant, and update the issue/project status and evidence. Close a task only when its entire acceptance contract is met. For a larger assignment, continue independent slices while another PR is under review; dependent work starts from the newly integrated `main` once its producer merges.

## 5. When to write an agent skill

The repository instructions, issue brief and tests are the durable contract. A reusable **contributor skill** is useful when the same specialized workflow recurs across tasks and has stable inputs, commands and outputs. Examples could be migration parity, installed SDK compatibility, or release evidence assembly. First prove the workflow on real PRs; then use the [workflow recipe template](AGENT_WORKFLOW_RECIPE_TEMPLATE.md) for a reviewed, versioned procedure with prerequisites, exact steps, failure handling, tests and one example. Keep any Codex, Copilot or other tool wrapper thin and refer it back to this repository guidance. A skill cannot grant merge authority, relax CI, approve a security finding or change product scope.

CALIBER also has **product skills** exposed to its users. Those are runtime artifacts governed by the product's own versioning, evaluation and release rules. They are distinct from contributor instructions and must follow the relevant feature task and tests.

## What still needs an orchestrator

These documents make tasks executable and handoffs consistent; they do not atomically assign tasks or judge every PR. To run several agents unattended, use a coordinator that selects tasks from live readiness, records one owner per task, serializes shared-file work, checks PR/CI/review state, and reconciles Project #3 after merges. Start with the documented human-visible claims and evidence, then automate those transitions only after the workflow is reliable. Do not treat the number of agents as a capacity estimate or move a release gate from HOLD to PASS from issue state alone.
