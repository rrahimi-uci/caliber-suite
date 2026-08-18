# GitHub Copilot repository instructions

- You may work on assigned issues and development tasks, create a branch, and open a pull request. You must never merge a pull request, approve a pull request, push to `main`, or represent your review as human approval.
- Before opening or marking a pull request ready for human review, understand the issue acceptance criteria, implement the smallest complete change, and add or update tests for changed behavior.
- Every PR must include tests that verify the correctness and completeness of the change: cover the new/changed behavior, the edge cases it introduces, and any regression the change fixes. A PR without adequate test coverage is not ready for review.
- Read `.github/workflows/ci.yml` and run the CI-equivalent checks for every affected area. At minimum run the applicable backend, SDK, plugin SDK, CLI, UI, docs, and compose checks described there; use `./test-all.sh` when the change spans components. Also run `git diff --check` and inspect generated docs/assets and migration state.
- Keep validation deterministic and offline. Do not require credentials, network services, model calls, or uncommitted local data for ordinary tests.
- Do not submit a PR as ready if a required test, lint check, type check, build, browser journey, or contract check fails. If validation is blocked, keep the PR draft and state the exact blocker and command output in the PR body.
- The PR body must list changed behavior, tests added or updated, every validation command and result, limitations, risks, and deployment or documentation implications. If a change is documentation-only, explain which documentation/link/build checks replaced unit tests.
- During code review, identify correctness, security, test-coverage, and maintainability issues and leave comments. Never approve or merge the PR.
