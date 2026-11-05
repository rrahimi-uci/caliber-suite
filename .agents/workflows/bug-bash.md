# Bug-bash workflow

Lead: [Bug basher](../roles/bug-basher.md); [QA](../roles/qa.md) owns acceptance coverage.

Read the [engineering policy](../engineering-policy.md) and
[GitHub integration contract](../github/project-integration.md). Honor explicit assignment limits.
The command's first operation is live Project context; repository inspection follows that read.
If context is unavailable, incomplete or ambiguous, report the blocker and do not infer scope
from a local plan or stale chat. Follow the linked issues and their complete acceptance contracts.

1. Read intended user/operator journeys, current bugs, acceptance controls, supported configurations,
   release boundaries and required tests. Then inspect implementation, existing fixtures/tests and
   runbooks. Pin the candidate SHA/artifacts, environment and isolated test data.
2. Build a journey matrix including normal flows, invalid inputs, edges/boundaries, unusual sequences,
   retries, interrupted operations, restart/persistence, concurrent requests, authn/authz denial,
   API misuse, integration failure, configuration errors, migrations/upgrades and UX consistency.
   Prioritize supported critical paths and known risky changes. Identify genuinely inapplicable cells.
3. Run relevant existing tests and explore the product through supported UI/API/SDK/CLI surfaces.
   Passing unit tests alone is not a bug bash. Use deterministic fixtures/local services; external,
   destructive, paid or production trials need the existing explicit authorization and limits.
4. For each failure capture reproducible steps, environment, candidate, sanitized logs/screenshots,
   expected versus actual behavior, impact/severity, affected component and suggested regression
   tests. Reproduce and narrow cause; distinguish harness/environment failures from product defects.
5. Dedupe against open/closed issues and Project findings, then create/update Bug issues associated
   with the resolved target and native milestone. Retain evidence and acceptance/test oracles.
   Follow [SECURITY.md](../../SECURITY.md) for sensitive vulnerability reporting.
6. Temporary experiments may run in an isolated worktree. Permanent reproduction/regression tests
   use a focused PR and all applicable policy gates. Do not merge a known-failing required test or
   hide it with a skip; retain an unmerged reproduction branch and issue until the defect is fixed.
   Product fixes are development work and require that assignment.
7. Record the tested journey matrix, exact results, bugs, untested areas and blocked scenarios in
   GitHub. Re-read to verify the result. Retest fixes and affected neighboring journeys on the new
   candidate; never transfer an old pass silently.

Completion means credible exploration and tracked reproducible findings. It does not bypass
missing release checks or certify untested deployment modes.
