Claude Repository Instructions

Autonomy and Merge Authority

* You are explicitly authorized to independently implement assigned work, create branches, commit and push changes, open pull requests, review your work, and merge your own pull requests into main without waiting for human approval, provided every requirement and validation gate in these instructions is satisfied.
* A separate human approval is not required before merging. Once the implementation is complete, the PR documentation is complete, all applicable required checks pass, and all identified review issues are resolved, you have authority to merge the PR and continue with the next task.
* Do not stop merely because a PR has been opened or is awaiting review. Unless there is a genuine blocker that requires human input, continue through validation, merge the PR yourself, synchronize with main, and proceed with the remaining work.
* Never push or commit directly to main. All code changes must reach main through a pull request.
* Never bypass branch protection, required checks, repository security controls, or other enforced safeguards in order to merge.
* Never merge a PR with a failing, skipped, incomplete, or unresolved required validation check.
* Never represent your own review as approval from another person or claim that a human reviewed or approved work when they did not.

Implementation Standard

Before opening or merging a pull request:

* Read and understand the issue, specification, acceptance criteria, relevant architecture, existing implementation, and affected tests.
* Inspect surrounding code before changing it. Preserve established repository conventions unless the task explicitly requires changing them.
* Implement the smallest complete change that fully satisfies the requirements. Avoid unrelated refactors or scope expansion.
* Identify dependencies and downstream effects across components before modifying shared interfaces, schemas, APIs, contracts, or infrastructure.
* Add or update documentation whenever behavior, interfaces, configuration, operational procedures, or developer workflows change.

Testing and Coverage

Every behavioral change must be supported by tests that demonstrate its correctness and guard against regression.

Tests must cover, as applicable:

* expected behavior and acceptance criteria;
* important edge cases and boundary conditions;
* error and failure paths;
* regressions being fixed;
* changed contracts or interfaces;
* interactions between affected components.

A PR without adequate tests for changed behavior is incomplete and must not be merged.

Maintain greater than 95% test coverage for affected code unless the repository defines a stricter requirement. Measure coverage using the repository’s established tooling; do not infer or assume coverage.

Do not artificially increase coverage by adding meaningless tests, excluding legitimate production code, weakening assertions, or changing coverage configuration simply to satisfy the threshold.

Required Validation

Read .github/workflows/ci.yml and any other applicable workflow or repository instruction files before determining the required validation suite.

Run the CI-equivalent checks for every affected area before merging. At minimum, run all applicable:

* unit and integration tests;
* end-to-end/browser tests;
* lint and formatting checks;
* static analysis and type checking;
* builds and packaging checks;
* backend checks;
* SDK and plugin SDK checks;
* CLI checks;
* UI checks;
* documentation checks;
* API/schema/contract checks;
* database and migration validation;
* Docker/Compose checks;
* security-related checks defined by the repository.

When a change spans multiple components, run ./test-all.sh when that is the repository’s comprehensive validation path.

Always run:

git diff --check

Also inspect generated documentation/assets, generated code, lockfiles, schema changes, and migration state whenever relevant.

After merging, synchronize with the latest main and perform an appropriate post-merge verification before beginning dependent work.

Deterministic and Offline Tests

Keep ordinary validation deterministic and offline wherever the repository permits it.

Tests should not depend on:

* private credentials or secrets;
* external network availability;
* live third-party services;
* live model/API calls;
* undocumented machine-specific state;
* uncommitted local files or data.

Use mocks, fixtures, fakes, recorded deterministic data, or repository-supported test infrastructure where appropriate.

Pull Request Requirements

Every PR must clearly document:

* the problem or requirement being addressed;
* changed behavior;
* important implementation/design decisions;
* tests added or updated;
* every significant validation command executed and its result;
* measured coverage where applicable;
* known limitations;
* remaining risks;
* migration or compatibility considerations;
* deployment implications;
* documentation implications.

For documentation-only changes, explicitly explain why behavioral tests are not applicable and list the documentation, link, formatting, or build checks used instead.

Keep each PR focused and independently understandable. Do not combine unrelated changes merely to reduce the number of PRs.

Self-Review Requirements

Before merging, perform a deliberate review of the complete PR diff as though reviewing another engineer’s work.

Check specifically for:

* correctness;
* incomplete acceptance criteria;
* regressions;
* security and privacy issues;
* unsafe input handling;
* concurrency or state-management problems;
* API/contract compatibility;
* migration safety;
* error handling;
* missing tests;
* weak assertions;
* maintainability;
* unnecessary complexity;
* dead or duplicated code;
* documentation gaps.

Record material issues you discover and resolve them before merging.

Do not approve your own PR in a way that implies independent or human review. Self-review is a quality gate, not a substitute for claiming third-party approval.

Merge Decision

You are authorized to merge the PR into main yourself when all of the following are true:

1. The acceptance criteria are completely satisfied.
2. The implementation is complete and appropriately scoped.
3. Required tests have been added or updated.
4. Required coverage thresholds are satisfied.
5. All applicable tests pass.
6. Linting and formatting checks pass.
7. Type checking and static analysis pass.
8. Required builds succeed.
9. Contract, migration, documentation, UI/browser, Compose, and other applicable checks pass.
10. git diff --check passes.
11. CI-required checks are green.
12. The PR body accurately documents the implementation and validation.
13. Your self-review is complete.
14. Every material issue discovered during review has been resolved.
15. There are no unresolved merge conflicts or known blockers that make merging unsafe.

When all applicable conditions above are satisfied, merge the PR yourself. Do not wait for additional human approval unless repository protections technically require it or the task explicitly requires human review.

After merging:

1. Verify that the merge completed successfully.
2. Synchronize the local repository with the latest main.
3. Confirm main remains healthy.
4. Delete or clean up the completed branch when appropriate.
5. Update the relevant issue, plan, or tracking document.
6. Continue immediately with the next assigned unit of work.

Blockers and Escalation

Do not weaken requirements, disable tests, bypass safeguards, or silently reduce scope to make a PR mergeable.

If validation cannot be completed because of a genuine external blocker:

* keep the PR in draft/unmerged state;
* document exactly what is blocked;
* include the failing command and relevant output;
* explain what has already been investigated;
* identify what information, permission, infrastructure, or decision is required to proceed.

Escalate only when progress genuinely requires human input, unavailable credentials/permissions, an architectural/product decision not resolved by existing requirements, or an external dependency outside your control.

Core Operating Rule

Complete → Test → Validate → Self-review → PR → Verify CI → Merge to main → Verify main → Continue.

Opening a PR is not completion. Waiting for unnecessary approval is not completion. For assigned autonomous work, continue until the requested scope is implemented, validated, merged into main, and verified, or until a genuine documented blocker requires human intervention.
