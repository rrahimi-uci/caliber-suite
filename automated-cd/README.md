# CALIBER automated engineering workflow

This is an opt-in coordinator for Project #3 tasks. It reads the complete live
Project through the repository's existing `.agents` GitHub adapter, selects
eligible Ready tasks, claims them, runs bounded model roles in isolated Git
worktrees, records review and QA cycles, and prepares an open pull request.
**It never merges or deploys.** The repository owner performs final review and
merge.

The implementation is designed for a trusted, persistent coordinator host with
Node.js 20+, Python 3.11+, Git, authenticated `gh`, and authenticated Claude
Code CLI. It does not run on ephemeral GitHub hosted runners because interrupted
state and worktrees must survive process restart. Ordinary tests use fakes and
make no model or GitHub calls.

## Setup

1. Install the Python package: `python3.11 -m pip install -e './automated-cd[test]'`.
2. Authenticate `gh` with repository and Project read/write access, and
   authenticate `claude` on the coordinator host. Keep credentials in the host
   credential store; no token is stored in this directory.
3. Copy `config/workflow.toml` to a private configuration file if needed. Set
   `AUTOCD_CONFIG` to its absolute path and `AUTOCD_REPO_ROOT` to the checkout.
   The cron entry also changes into that checkout before invoking Python.
4. Set a persistent private `AUTOCD_STATE_DIR` outside the repository. Add the
   exact issue numbers, reviewed path ownership, and task-specific CI and
   coverage commands under `issue_checks` and `issue_paths`. Without
   `issue_checks`, eligible work is visible to planning but is not executed.
5. Run `caliber-autocd plan` for a read-only eligibility preview and
   `caliber-autocd status` for current state. `caliber-autocd run` executes
   configured tasks immediately even when `workflow.enabled=false`. Set
   `enabled=true` for cron.

Example issue configuration:

```toml
[issue_checks]
"123" = ["caliber/.venv/bin/python -m pytest caliber/tests/test_example.py --cov=caliber.example --cov-branch --cov-report=term-missing --cov-fail-under=95 -q"]

[issue_paths]
"123" = ["caliber/src/caliber/example.py", "caliber/tests/test_example.py", "docs/example.md"]
```

Use an actual issue contract and executable checks. The base checks run the
repository's docs, lint, type, backend and PostgreSQL scenario CI parity. Add
SDK, UI, migration, packaging or other affected checks to the issue list.
The default `checks.setup` builds the full CALIBER test environment inside
each new worktree; plan disk, network and install time accordingly. The host
must provide Docker/PostgreSQL for required reference-profile checks. A
`ci-local.sh` SKIP is treated as an unexecuted required gate even if the
script exits zero.
Each command executes with an argument vector, never through a shell; shell
operators are rejected. The orchestrator fails QA if no coverage command is
configured. QA must also inspect the measured result and acceptance proof.

## Daily cron and manual runs

The cron host calls a minute-level `tick`; the coordinator uses `daily_time`
and IANA `timezone` to start at most one scheduled run per local calendar day.
This handles daylight-saving skips and repeats without relying on `CRON_TZ`.
Add this entry to the trusted host's crontab, replacing the paths:

```cron
* * * * * /absolute/path/to/caliber-suite/automated-cd/scripts/cron-entry.sh /absolute/path/to/caliber-suite /absolute/path/to/python >> /private/persistent/state/cron.log 2>&1
```

The Python path must point to the environment where `caliber-autocd` is
installed. Export `AUTOCD_CONFIG` and `AUTOCD_STATE_DIR` in that cron
environment. `caliber-autocd run` starts a manual run immediately; `tick`
honors `enabled` and the daily schedule. The default is disabled. The single
host lock rejects overlap unless `allow_overlapping_runs=true`; enabling
overlap does not create a distributed lock and is unsafe with a shared GitHub
identity unless another coordinator enforces exclusive claims.

## State and safety

`workflow.sqlite3` stores run/task phases and an append-only event history.
Worktrees live under `AUTOCD_STATE_DIR/worktrees`, never inside `main`.
Interruption during editing, review, QA or PR creation blocks that task for
manual reconciliation; the coordinator does not replay an ambiguous write.
An interrupted `ci_wait` phase rechecks the exact PR head, required checks,
mergeability and unresolved review threads on the next invocation. The local
lock and repository `.agents` transition lock serialize cooperating local
writers; another clone or human remains a remote race. The coordinator rereads
the Project after each claim and stops if the claim is not visible.

The model roles receive no Bash tool. Developers may use file reading/editing
tools in their task worktree; Planner, Reviewer and QA receive read-only tools.
The Python coordinator alone runs Git and GitHub commands. Configured issue
paths enforce ownership when supplied; tasks without explicit path ownership
run serially. A large diff is refused instead of silently truncating review.
The coordinator never calls `gh pr merge` or pushes `main`.

## PR handoff

After review and QA pass, the coordinator commits and pushes the task branch,
opens a PR using the repository's exact template headings, requests the
owner and GitHub Copilot when GitHub permits, and waits for required CI.
GitHub normally rejects requesting the PR author as a reviewer; that attempt
is recorded as unavailable rather than claimed as a review. Copilot may also
be unavailable. When required checks pass, the PR is mergeable, and no review
thread is unresolved, the coordinator marks the PR ready for the owner's
final review. It leaves the PR open. An owner approval is not fabricated by a
model self-review.

## Recovery and troubleshooting

- `caliber-autocd status` shows unfinished tasks. Inspect the SQLite `events`
  table for ordered decisions, commands, review cycles and errors.
- For `blocked_after_interruption`, inspect the retained worktree, branch,
  Project claim and remote PR before restarting. Never delete the lock or
  worktree solely because a process exited.
- For `waiting_for_ci_or_review`, rerun `tick` or `run` after CI finishes. A
  required skipped, pending or failed check is not a pass.
- A failed or skipped required check, requested changes, or CI wait exceeding
  `ci_timeout_seconds` blocks the task and records a Project blocker when
  GitHub is available. Correct it on the same PR branch, rerun review/QA and
  update the PR; the coordinator will not replay an uncertain edit itself.
- If Project access fails, fix `gh` authentication or Project permissions;
  there is no local-backlog fallback.
- If the owner cannot be requested because they authored the PR, final owner
  review occurs directly in GitHub. Any independent approval required by
  branch protection still needs an eligible reviewer.
- Model errors, stalls and timeouts retain worktrees for investigation. No
  automatic replay of a mutating agent step occurs.

The state database and worktrees may contain private issue content or source.
Restrict directory access and back them up under the organization's policy.
See [architecture and failure semantics](docs/architecture.md).
