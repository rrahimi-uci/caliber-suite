# CALIBER automated engineering workflow

This is a coordinator for the complete CALIBER Project #3, including Beta,
Pilot and Preview work. It reads the live Project, orders executable tasks by
`Execution order`, verifies completed native dependencies, promotes a Planned
task through the governed Ready transition when its start gates pass, claims it,
runs bounded model roles in an isolated Git worktree, records review and QA
cycles, and opens a draft pull request. It never merges or deploys, and never
bypasses GitHub protection.

The initial `daily_done_target` is **10**. This measures merged, Completed Task
issues in Project Done across releases on the configured local day;
opened PRs and model approvals do not count. A target is not a guarantee: tasks
still require explicit start decisions, complete dependencies, review, QA, CI and
owner merge. Counting uses the complete Project snapshot plus issue closure time
and merged closing-PR evidence.

GitHub Actions starts the workflow at 4:00 AM `America/Los_Angeles` on a hosted
runner. A manual `workflow_dispatch` starts an extra run from GitHub. The runner
saves SQLite state as an Actions artifact so published PRs can be reconciled
on a later run. Worktrees do not persist across runners; an interrupted edit
before PR creation is blocked for inspection. The run reports a failed Actions
status if any task blocks, while still uploading its state artifact. Unreviewed
candidate source is not uploaded to public Actions artifacts. Ordinary tests
use fakes and make no model or GitHub calls.

## Setup

1. Add repository Actions secrets `ANTHROPIC_API_KEY`, `AUTOCD_GH_TOKEN`,
   and `AUTOCD_STATE_KEY`. Generate `AUTOCD_STATE_KEY` as 32 random bytes
   encoded in base64; retain it securely because rotation without migrating
   the state makes the existing artifact unreadable.
   `AUTOCD_GH_TOKEN` must be a classic token owned by `rrahimi-uci` with `project`
   and `public_repo` scopes (GitHub may also report `read:project`). It needs
   write access to this repository and user Project #3. The runner verifies
   the token owner, scopes, repository push permission, and Project read
   before dispatching work. The default
   `GITHUB_TOKEN` cannot access user Projects. Do not commit either secret or
   use an admin-wide token.
2. Review `config/workflow.toml`. Project scope `0` covers the whole Project;
   a positive `project_view_number` restricts work to one supported release-label
   view. The checked-in schedule is 4:00 AM Pacific and the daily Done target is ten.
3. Add issue-specific commands under `issue_checks` when the issue requires
   checks beyond the full backend suite and required PR CI. Add `issue_paths`
   only for reviewed, disjoint ownership that permits parallel task execution.
   Without path ownership, tasks run serially. Changes to `.github`, `.agents`,
   `automated-cd`, and other policy files stop before an unattended PR merge.
   UI, SDK, shell, configuration and other unsupported component changes
   require explicit issue-specific commands; the coordinator fails QA if they
   are absent. Root `ARCHITECTURE.md` uses the standard docs checks; other root
   Markdown files still require explicit checks unless already allowlisted.
   The checked-in `max_tasks_per_run=10` also caps daily
   attempts across scheduled and manual runs, so failed attempts reduce that
   day's possible Done count.
   Claude calls currently have no per-call dollar cap for scheduled and manual
   runs while task spend is being measured. This does not change the normal
   task-attempt or five-hour execution bounds.
4. Run **Automated CD execution** with `workflow_dispatch` in GitHub Actions
   to start immediately, or let the daily schedule start it. The Actions log
   records the plan, task results and PR monitoring. Set `enabled=false` to
   pause scheduled dispatch while retaining read-only planning.

Example issue configuration:

```toml
[issue_checks]
"123" = ["caliber/.venv/bin/python -m pytest caliber/tests/test_example.py --cov=caliber.example --cov-branch --cov-report=term-missing --cov-fail-under=95 -q"]

[issue_paths]
"123" = ["caliber/src/caliber/example.py", "caliber/tests/test_example.py", "docs/example.md"]
```

Use an actual issue contract and executable checks. The base checks run the
repository's docs, lint, type and backend smoke CI parity as an isolated task
user. Changed production Python also triggers the full backend suite with
measured coverage unless an issue-specific coverage command is configured. The
fallback uses four pytest-xdist workers and the repository's load-group
serialization, matching the full-suite CI topology with a bounded worker count.
The selected local checks, including full backend coverage for changed
production Python, must pass before a PR is published. The coordinator verifies
the candidate snapshot remains unchanged when it publishes the PR. Required
exact-head PR CI remains the merge gate. The full backend CI job runs after a
main push; it is not a PR-head gate. A pending PR-only job is never a pass.
PostgreSQL scenario and other container-backed gates run in PR CI without the
coordinator secrets. Add SDK, UI, migration, packaging or other affected
checks to the issue list when they can run without host Docker access.
The default `checks.setup` builds the full CALIBER test environment inside
each new worktree and seeds pip because `ci-local.sh` installs SDK/plugin
dependencies through `python -m pip`. Plan runner time and install time
accordingly. Before review, the coordinator synchronizes generated docs for
changed docs/backend sources and includes the generated files in the candidate.
QA repeats the strict docs sync and rejects any further candidate change.
Generated site files require UI parity as well as docs validation. The GitHub
runner provides Docker/PostgreSQL to its separate PR CI jobs. A
`ci-local.sh` SKIP is treated as an unexecuted required gate even if the
script exits zero.
Issue #821 also runs its focused route/migration regressions and the backend
core check because it changes verification contracts and a nonstandard
changelog path. The PostgreSQL PR job will select #821's planned, named
two-session verification transition test from `test_routes_verification_queue.py`.
The default per-command local QA timeout is 7,200 seconds and is configurable
with `AUTOCD_QA_COMMAND_TIMEOUT_SECONDS`; timeout evidence records the duration
without exposing the isolated worker's command line.
Each command executes with an argument vector, never through a shell; shell
operators are rejected. For changed production Python files, the orchestrator
uses a configured coverage command or runs the full backend suite with coverage.
It emits a fresh coverage JSON report and requires greater than
95% measured line-plus-branch coverage for each changed production Python
file; a command covering only unrelated files fails. QA must also inspect
the measured result and acceptance proof.

## GitHub schedule and manual runs

`.github/workflows/automated-cd-run.yml` owns the 4:00 AM Pacific schedule
and manual dispatch. GitHub's timezone-aware schedule tracks daylight saving
time; GitHub may delay a scheduled start during high load. No local cron job
is installed or required. The coordinator allows one scheduled dispatch run
per local date. An Actions concurrency group serializes scheduled and manual
invocations. The runner monitors published PRs for up to five hours, then
uploads the state database; a later run resumes PR reconciliation. After each
completed PR, the runner selects another eligible task until the target, backlog
or runner time limit is reached. Manual
`workflow_dispatch` uses the same guarded engine. `caliber-autocd plan` is
still available for diagnosis without task or Project mutation, but production
starts in GitHub.
For a measured owner-only manual trial, set `trial_tasks` from 1 through 5.
This permits that many **additional attempts** even if the Pacific-day normal
ten-attempt cap was already used; it does not change the next scheduled run's
cap or guarantee that the attempts finish as Done. Its temporary effective
Done ceiling is derived from the live completed count so earlier completions
cannot shorten the requested measurement. Set `retry_issue` as well
to start with one reconciled blocked pre-PR task, then refill from the live
executable Project backlog. The workflow logs a bounded report of coordinator
elapsed time, claimed and completed tasks, Claude call counts, and Claude's
reported dollar estimate. A missing call cost makes the per-Done average
unknown, rather than treating it as zero. The report includes failed attempts
and planner spend; invoice charges may differ from Claude's estimate. No
prompts, review findings, or raw model output are printed in the report.
The preflight plan reads restored state: `eligible` and `selected` exclude
prior blocked issues and today's attempted issues, and `remaining_attempts`
reflects the daily cap. `requires_explicit_retry` identifies Ready issues that
need a guarded manual dispatch with `retry_issue` after claim/branch/PR
reconciliation. The plan never authorizes a retry by itself.
If `pending_tasks` is nonzero, the plan conservatively selects nothing until
the execution engine reconciles those tasks. The plan does not perform that
recovery step.

## State and safety

`workflow.sqlite3` stores run/task phases and an append-only event history.
It can include model output and QA command excerpts. The runner encrypts it
with an authenticated per-repository key before uploading the 90-day Actions
artifact and decrypts it on the next run. Only one known pre-encryption run
artifact is accepted for migration. Worktrees live in a separate temporary
worker directory, never inside `main` or the private state directory.
Blocked tasks are marked Blocked in the Project. Inspect the state artifact,
issue claim and remote PR/branch before deciding whether to retry. An uncommitted
candidate is lost when its runner ends; encrypted state artifacts contain
audit data that may include sensitive snippets, not candidate patches.
Interruption during editing, review, QA or PR creation blocks that task for
manual reconciliation; the coordinator does not replay an ambiguous write.
An interrupted `ci_wait` phase rechecks the exact PR head, required checks,
mergeability and unresolved review threads on the next invocation. The Actions
concurrency group, runner-local lock, and repository `.agents` transition lock
serialize cooperating writers; another clone or human remains a remote race. The coordinator rereads
the Project after each claim and stops if the claim is not visible.

The model roles receive no Bash tool. Untrusted issue/task text is passed as a
user prompt, below the trusted role policy. Developers may use file reading/editing
tools in their task worktree; Planner, Reviewer and QA receive read-only tools.
The hosted runner creates an unprivileged Unix user for model, setup and local
QA subprocesses. They run without the Project token and cannot read the
coordinator user's process environment. Claude Code 2.1.314 or newer is
required so file tools remain inside the working directory. Docker-backed
checks run as separate PR CI jobs without coordinator credentials.
The Python coordinator alone runs Git and GitHub commands. Configured issue
paths enforce ownership when supplied; tasks without explicit path ownership
run serially. The reviewer receives the complete source diff up to 250,000
characters. If generated docs make the full patch larger, the reviewer receives their exact
paths, byte sizes and SHA-256 hashes and can inspect the files with read-only
tools; strict docs and UI checks validate generation. QA and staging still
compare the complete candidate patch byte for byte. A source diff above
250,000 characters or a total patch above 64 million characters is refused
instead of silently truncating review.
The coordinator never pushes `main` directly. Automatic merging uses
`gh pr merge --squash --match-head-commit` without `--admin`; GitHub protection
remains authoritative. If `main` advances beyond the reviewed PR head, the
task waits for a new candidate and review/QA. This can reduce throughput below
the target when several PRs overlap.

## PR handoff

After review and QA pass, the coordinator commits and pushes the task branch,
opens a draft PR using the repository's exact template headings, requests
GitHub Copilot and the owner when available, and waits for required CI. The
checked-in configuration keeps `auto_merge=false`, so the PR remains open for
owner review and merge. A model self-review is never represented as an
independent GitHub approval; the coordinator still observes the PR and
reconciles Project Done only after a verified owner merge.

## Recovery and troubleshooting

- The Actions log shows the plan and monitor results. For a blocked run, dispatch
  the GitHub workflow with `diagnose_state=true` to print a bounded summary of
  the latest run and any task claimed within that run from the encrypted state.
  It reports fixed failure
  codes, the day's claim count, review/QA counts, and whether a PR or head was
  pushed. An early run failure is visible even when no new task was claimed;
  model text and command output remain in the encrypted artifact. Diagnosis
  needs `AUTOCD_STATE_KEY`
  but does not need the model or Project token. It does not claim tasks or upload
  a new state artifact, and cannot be combined with `retry_issue`.
- For a private incident investigation, temporarily add repository secret
  `AUTOCD_DIAG_KEY` containing a separate base64-encoded 32-byte key, then
  dispatch with `diagnose_state=true`, `export_incident=true`, and
  `incident_issue=<number>`. The runner
  uploads a one-day `caliber-autocd-incident` artifact containing only an
  AES-GCM encrypted bundle of that issue's latest attempt and up to 50 relevant evidence
  events. Decrypt it with the locally retained temporary key, then delete the
  artifact and secret. Never put review findings or command output in Actions
  logs, issue comments, or an unencrypted artifact.
- For `blocked_after_interruption`, inspect the Project claim, remote branch,
  and any PR before restarting. The runner worktree is ephemeral. Blocked task
  IDs remain excluded from later runs until the state and Project are manually
  reconciled; rerunning the scheduler does not replay an ambiguous write.
- After verifying the blocked attempt has no pushed head or PR and its branch
  is absent remotely, clear the old claim and return the Project task to
  unassigned Todo/Ready through a reviewed Project update. For an immediate
  retry, dispatch the GitHub workflow with `retry_issue` set to that one issue
  number; the Pacific-day attempt cap still applies. If the cap is exhausted,
  the next 4:00 AM Pacific scheduled run may select the reconciled task after
  the day resets. It requires the persisted blocked pre-PR attempt, no pushed
  head or PR, an absent remote branch, a live Ready unclaimed Project task, an
  owner-authored `Engineering workflow: **ready**` transition comment after the
  blocked event, and no claim on the new day. The worker rechecks the old branch
  before creating its worktree. A task left Blocked in Project, or a stale Ready
  field left by failed claim/block writes, is never replayed automatically.
- For `waiting_for_ci_or_review` or `waiting_for_merge`, the hosted monitor or
  next scheduled/manual run rechecks the PR. A
  required skipped, pending or failed check is not a pass.
- A failed or skipped required check, requested changes, or CI wait exceeding
  `ci_timeout_seconds` blocks the task and records a Project blocker when
  GitHub is available. Correct it on the same PR branch, rerun review/QA and
  update the PR; the coordinator will not replay an uncertain edit itself.
- If Project access fails, fix `gh` authentication or Project permissions;
  there is no local-backlog fallback.
- If GitHub blocks the merge because of a required independent review, the PR
  remains open until an eligible reviewer approves it. The coordinator cannot
  invent that approval or bypass protection.
- After a merge, `merged_unreconciled` retains the task until the merge commit
  is confirmed on `main`, the issue is closed as Completed, and Project fields
  persist as Done. A failed Project read or update is retried on the next run.
- Model errors, stalls and timeouts retain audit events and the remote branch.
  No automatic replay of a mutating agent step occurs.

The state artifact may contain private issue content or source. Limit Actions
artifact access under the repository's security policy.
See [architecture and failure semantics](docs/architecture.md).
