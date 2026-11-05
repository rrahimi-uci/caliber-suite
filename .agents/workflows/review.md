# Review workflow

Lead: [Reviewer](../roles/reviewer.md); QA assists with acceptance evidence.

Read the [engineering policy](../engineering-policy.md) and
[GitHub integration contract](../github/project-integration.md). Honor explicit assignment limits.
The command's first operation is live Project context; repository inspection follows that read.
If context is unavailable, incomplete or ambiguous, report the blocker and do not infer scope
from a local plan or stale chat. Follow the linked issues and their complete acceptance contracts.

1. Read the target's promised outcomes, complete issue contracts, dependencies, controls and linked
   PRs, including closed work. Then inspect architecture and effective runtime paths on the candidate.
   Review the milestone/cycle as a whole; the latest PR is only one input.
2. Map every required outcome to code, tests, integration and CI evidence. Inspect correctness,
   architecture compliance, maintainability, security/privacy, authorization, performance, concurrency,
   state/error handling, API compatibility, migrations, regressions and interactions across components.
   Look for missing requirements, test assertions that cannot catch faults, and required checks that
   did not execute. Run relevant existing deterministic checks to resolve concrete uncertainty.
3. Classify each substantiated finding using the shared P0–P3 definitions. Include candidate SHA,
   affected component, source/test references, reproduction or concrete counterexample, expected
   behavior, impact, suggested remediation and test/acceptance oracle. Do not report a suspicion as
   a verified bug or infer safe behavior from the name of a helper.
4. Search existing issues and Project items, including closed findings. Create an actionable Review
   issue or explicitly update the matching issue with fresh evidence. Preserve required target,
   milestone and Project association; apply priority/readiness truthfully. Follow private security
   reporting guidance for sensitive vulnerability details.
5. Publish the review result in the relevant GitHub issue/control with covered outcomes, candidate,
   verified evidence, findings and gaps. Re-read the Project to verify writes. Resolved findings need
   verification on the fixing candidate; independent approval must have actual eligible provenance.

Do not implement feature fixes. A review report is not a release decision or a fabricated GitHub
approval; it is complete when target coverage and actionable findings are recorded with evidence.
