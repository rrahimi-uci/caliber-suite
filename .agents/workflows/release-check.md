# Release-check workflow

Lead: [QA](../roles/qa.md); use Reviewer and Critic evidence with actual required independence.

Read the [engineering policy](../engineering-policy.md) and
[GitHub integration contract](../github/project-integration.md). Honor explicit assignment limits.
The command's first operation is live Project context; repository inspection follows that read.
If context is unavailable, incomplete or ambiguous, report the blocker and do not infer scope
from a local plan or stale chat. Follow the linked issues and their complete acceptance contracts.

1. Resolve the precise target and read all required issues, predecessor guarantees, release controls,
   human inputs, open bugs, review/critique findings and bug-bash results. Reconcile implementation
   state with actual PRs and main. Do not substitute a Release field for a native milestone or
   assume that a delivery cycle grants acceptance to its whole destination release.
2. Identify one exact integrated candidate SHA and artifact set/digests. Read the applicable
   architecture, release procedures, CI workflows and acceptance criteria. If the candidate,
   authoritative scope or required gate set cannot be established, return NOT READY.
3. Build an evidence matrix in the linked GitHub acceptance control. For every required outcome,
   record issue/criterion, candidate/artifact identity, test or review gate, environment, command,
   result, CI/artifact/evidence URL and accountable verifier. Include cumulative predecessor
   guarantees and revalidation of affected earlier outcomes on this candidate.
4. Verify meaningful unit/integration/regression tests, affected-code coverage, lint/format/types,
   browser/public API/SDK/CLI journeys, security, contracts, migrations/upgrade/restore, builds,
   installed packages, deployment/configuration, documentation and operational checks as required.
   Check main CI and other required workflows, not only PR CI. A helper analysis, closed issue,
   old report, green badge or agent assertion does not prove a gate.
5. Verify no incomplete required scope, P0/P1 defects or unresolved release-blocking review/critique
   findings remain. Lower-priority deferrals need the authority and evidence required by the
   acceptance contract; agents cannot silently remove them. Validate bug-bash coverage and results.
6. Verify all required independent approvals, authorized trials and product/security/operator
   decisions with named identity, provenance and candidate applicability. Agent self-review or
   a shared author account cannot substitute for independent approval. Do not fabricate a trial,
   approve on behalf of a person or infer permission from elapsed time.
7. Return exactly **READY** or **NOT READY** as the verdict, followed by target, candidate and
   evidence links. READY requires every applicable gate to have executed and passed, all required
   approvals to exist and the acceptance control to be satisfied. Unknown, missing, stale,
   skipped-required, failed or ambiguous evidence means NOT READY (record HOLD where the existing
   control uses that term). Link every blocker to an existing or deduplicated new issue/control;
   include evidence and the precise action/owner needed. If GitHub writes are blocked, identify
   the control URL and unsaved blocker explicitly.
8. Re-read the live control and Project to confirm the recorded decision. A required repository
   evidence report goes through a PR; if that changes the final candidate, re-evaluate applicable
   gates and provenance for the new SHA. Never silently reuse approval for different artifact bytes.

Do not implement features, create tags, publish artifacts, deploy or close unrelated issues.
READY is the verified readiness of the named candidate, not authorization for a separate release
operation. If new changes land, their candidate needs another check.
