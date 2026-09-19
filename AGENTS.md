Codex Repository Instructions

Autonomy and Merge Authority

* You are explicitly authorized to independently execute assigned issues, specifications, implementation plans, and development tasks from start to completion.
* You may inspect and modify the repository, create branches, commit and push changes, open pull requests, perform self-review, respond to review findings, and merge your own pull requests into main without waiting for human approval, provided every applicable requirement and validation gate in these instructions is satisfied.
* A separate human approval is not required before merging. When implementation is complete, the PR documentation is complete, all applicable required checks pass, and all identified review issues are resolved, you have authority to merge the PR yourself.
* Do not stop merely because you have implemented the code, committed changes, pushed a branch, or opened a pull request. For autonomous assignments, continue through validation, PR creation, CI verification, merge, post-merge verification, and subsequent assigned work.
* Never push or commit directly to main. All code changes must reach main through a pull request.
* Never bypass branch protection, required status checks, security controls, repository policies, or other enforced safeguards in order to merge.
* Never merge a PR containing a failing, skipped, incomplete, unresolved, or intentionally bypassed required check.
* Never represent your own review as approval from another engineer or claim that a human reviewed or approved work when they did not.

Repository and Task Discovery

Before changing code:

* Read the repository-level and directory-specific instruction files applicable to the files you will modify.
* Read .github/workflows/ci.yml and other applicable CI/CD workflows before determining the required validation suite.
* Read the assigned issue, specification, implementation plan, acceptance criteria, and related documentation completely.
* Inspect the existing implementation, tests, interfaces, schemas, dependencies, and relevant history when needed to understand intended behavior.
* Search the repository for existing patterns and analogous implementations before introducing new abstractions.
* Identify affected components and downstream consumers before changing shared APIs, schemas, contracts, persistence models, configuration, or infrastructure.

Repository-specific instructions take precedence where they impose stricter requirements.

Implementation Standard

Implement the smallest complete change that fully satisfies the assigned requirements.

Do not:

* silently reduce scope;
* skip difficult acceptance criteria;
* introduce unrelated refactors;
* rewrite working components without a task-driven reason;
* leave temporary implementations presented as complete;
* suppress errors instead of addressing their cause;
* weaken existing safeguards merely to make validation pass.

Prefer existing repository patterns and abstractions over introducing unnecessary new ones.

When the task naturally consists of multiple independently reviewable units, divide it into focused slices and complete each through the full PR lifecycle before proceeding when doing so reduces integration risk.

Update documentation whenever behavior, interfaces, configuration, deployment requirements, architecture, or developer workflows change.

Testing Requirements

Every behavioral change must include tests that demonstrate correctness and protect against regression.

Tests must cover, where applicable:

* acceptance criteria;
* expected behavior;
* important edge and boundary cases;
* error and failure paths;
* regressions being fixed;
* changed contracts and interfaces;
* state transitions;
* interactions between affected components;
* backward compatibility where required.

A PR without adequate tests for changed behavior is incomplete and must not be merged.

Do not treat existing tests passing as sufficient evidence when the new or changed behavior itself is not exercised.

Coverage Requirements

Maintain greater than 95% test coverage for affected code unless the repository defines a stricter requirement.

Measure coverage using the repository’s established tooling. Do not estimate or assume coverage.

Do not satisfy coverage requirements by:

* adding meaningless tests;
* weakening assertions;
* excluding legitimate production code from coverage;
* marking meaningful paths as untestable without justification;
* deleting valuable tests;
* changing coverage configuration solely to make the threshold pass.

Coverage is a quality signal, not a substitute for meaningful behavioral testing.

Required Validation

Run the CI-equivalent validation for every affected component.

At minimum, run all applicable:

* unit tests;
* integration tests;
* end-to-end tests;
* browser/user-journey tests;
* regression tests;
* lint checks;
* formatting checks;
* static analysis;
* type checking;
* builds;
* packaging checks;
* backend checks;
* SDK checks;
* plugin SDK checks;
* CLI checks;
* UI checks;
* documentation builds/checks;
* API/schema/contract checks;
* database and migration validation;
* Docker/Compose checks;
* security-related checks defined by the repository.

When a change spans multiple components, use ./test-all.sh when that is the repository’s comprehensive validation path.

Always run:

git diff --check

Also inspect, when applicable:

* generated documentation;
* generated source code;
* generated assets;
* lockfiles;
* API/schema artifacts;
* database migrations;
* migration state;
* snapshots;
* fixtures;
* dependency changes.

Do not assume local success guarantees CI success. Verify required CI checks on the pull request before merging.

Deterministic and Offline Validation

Keep ordinary validation deterministic and offline wherever the repository permits it.

Ordinary tests should not require:

* private credentials;
* secrets;
* external network availability;
* live third-party services;
* live model calls;
* production systems;
* undocumented machine-specific state;
* uncommitted local files or data.

Use repository-supported mocks, fixtures, fakes, deterministic recordings, local test services, or equivalent test infrastructure where appropriate.

Do not replace deterministic tests with live external dependencies merely because doing so is easier.

Pull Request Requirements

Every PR must clearly document:

* the problem or requirement being addressed;
* relevant acceptance criteria;
* changed behavior;
* important implementation and design decisions;
* tests added or modified;
* every significant validation command executed;
* the result of each validation command;
* measured coverage where applicable;
* known limitations;
* remaining risks;
* backward-compatibility considerations;
* migration implications;
* deployment implications;
* documentation implications.

For documentation-only changes, explicitly explain why behavioral tests are not applicable and list the documentation, link, formatting, or build checks used instead.

Do not claim a validation command passed unless it was actually executed successfully.

Do not omit known failures or limitations from the PR description.

Keep PRs focused and independently understandable. Do not combine unrelated changes merely to reduce the number of pull requests.

Self-Review Requirements

Before merging, review the complete PR diff as though reviewing another engineer’s work.

Specifically inspect for:

* correctness;
* incomplete acceptance criteria;
* unintended behavior changes;
* regressions;
* security vulnerabilities;
* privacy concerns;
* unsafe input handling;
* authorization/authentication problems;
* concurrency issues;
* race conditions;
* state-management errors;
* resource leaks;
* error-handling gaps;
* API/contract compatibility;
* migration safety;
* data-loss risks;
* missing tests;
* weak assertions;
* unnecessary complexity;
* maintainability issues;
* duplicated or dead code;
* documentation gaps.

Resolve every material issue discovered during self-review before merging.

If you leave review comments or record findings on the PR, resolve each material finding before merge.

Self-review is a required quality gate, but must never be represented as independent human approval.

Merge Authorization

You are explicitly authorized to merge your own pull request into main when all applicable conditions below are satisfied:

1. The assigned requirements and acceptance criteria are completely satisfied.
2. The implementation is complete and appropriately scoped.
3. Required tests have been added or updated.
4. Tests meaningfully exercise the changed behavior.
5. Required coverage thresholds are satisfied.
6. All applicable unit, integration, regression, and end-to-end tests pass.
7. Linting and formatting checks pass.
8. Type checking and static analysis pass.
9. Required builds and packaging checks succeed.
10. Contract, schema, migration, documentation, browser/UI, Compose, and other applicable validations pass.
11. git diff --check passes.
12. Required CI/status checks on the PR are green.
13. The PR branch is in a mergeable state with no unresolved conflicts.
14. The PR body accurately documents the implementation and validation.
15. Self-review is complete.
16. Every material issue discovered during implementation, validation, or review has been resolved.
17. No known blocker makes the merge unsafe.

When all applicable conditions are satisfied, merge the PR yourself. Do not wait for additional human approval unless repository protections technically require it or the assigned task explicitly requires independent human review.

Human approval is therefore not the default completion gate. Passing the repository’s required objective validation gates is the default merge gate.

Post-Merge Requirements

Merging the PR is not the final step.

After every merge:

1. Verify that the PR merged successfully into main.
2. Synchronize the local repository with the latest remote main.
3. Verify the expected commits and changes are present.
4. Run appropriate post-merge validation when integration with newer main could affect correctness.
5. Confirm main remains healthy.
6. Clean up the completed branch when appropriate.
7. Update the relevant issue, implementation plan, checklist, or tracking document.
8. Re-evaluate remaining work against the original requirements.
9. Continue immediately with the next assigned slice or task when work remains.

Do not continue developing dependent work from a stale pre-merge branch when the intended workflow requires building on the newly merged main.

Multi-Slice and Long-Running Tasks

When an assigned task, issue, specification, or implementation plan contains multiple substantial pieces of work:

1. Understand the complete plan before beginning.
2. Identify dependencies and determine a sensible implementation order.
3. Divide the work into small, coherent, independently reviewable slices.
4. For each slice:
   * create or update the appropriate branch;
   * implement the complete slice;
   * add/update tests;
   * run applicable validation;
   * measure required coverage;
   * perform self-review;
   * open a focused PR;
   * verify CI;
   * resolve failures and review findings;
   * merge the PR into main;
   * synchronize with main;
   * verify repository health.
5. Update progress tracking.
6. Begin the next incomplete slice.
7. Repeat until the complete assigned scope is finished.

Do not stop after the first PR or first successful slice when the assignment explicitly covers additional work.

Failure Handling

When a test or validation check fails:

1. Determine whether the failure is caused by your change, an existing repository issue, environment configuration, or an external dependency.
2. Investigate the root cause.
3. Fix failures caused by the implementation.
4. Re-run the affected validation.
5. Run broader regression validation when the fix could affect other components.
6. Document relevant failures and resolutions in the PR when they materially affect review or risk.

Never:

* disable a legitimate failing test simply to make CI green;
* mark a required check as skipped without valid repository-supported justification;
* weaken assertions merely to make a test pass;
* conceal failures;
* merge while required validation remains unresolved.

Blockers and Escalation

Continue autonomously whenever the next action can be determined safely from the repository, requirements, tests, CI results, or existing documentation.

Do not stop merely because a decision requires investigation.

Escalate only when progress genuinely requires something you cannot resolve autonomously, such as:

* unavailable credentials or permissions;
* repository protection that requires an independent human action;
* an unresolved product or architectural decision for which the requirements provide no defensible answer;
* unavailable infrastructure;
* an external dependency outside your control;
* contradictory requirements that materially affect correctness.

When blocked:

* do not merge;
* keep the PR draft/unmerged where appropriate;
* document the exact blocker;
* include the relevant command and failure output;
* describe what you investigated;
* describe attempted resolutions;
* identify precisely what information, permission, infrastructure, or decision is required to continue.

Do not weaken requirements or invent an answer merely to avoid escalation.

Completion Criteria

An assigned task is complete only when:

* all required behavior is implemented;
* acceptance criteria are satisfied;
* appropriate tests exist;
* required coverage is achieved;
* all applicable validation passes;
* the PR is complete;
* required CI checks are green;
* the change has been merged into main;
* post-merge repository health has been verified;
* relevant documentation/tracking has been updated.

For multi-PR assignments, the assignment is not complete merely because one PR has been merged. Continue until every required slice has completed this lifecycle.

Core Operating Loop

Understand → Slice → Implement → Test → Validate → Measure Coverage → Self-Review → Open PR → Verify CI → Resolve Issues → Merge to main → Sync → Verify main → Update Progress → Continue

Opening a PR is not completion.

Passing local tests alone is not completion.

Waiting for unnecessary human approval is not completion.

For autonomous assignments, continue until the entire assigned scope is implemented, validated, merged into main, and verified — or until a genuine, explicitly documented blocker requires human intervention.
