# Milestone critique workflow

Lead: [Critic](../roles/critic.md).

Read the [engineering policy](../engineering-policy.md) and
[GitHub integration contract](../github/project-integration.md). Honor explicit assignment limits.
The command's first operation is live Project context; repository inspection follows that read.
If context is unavailable, incomplete or ambiguous, report the blocker and do not infer scope
from a local plan or stale chat. Follow the linked issues and their complete acceptance contracts.

1. Read the intended users, operational constraints, required outcomes, decisions, prior findings and
   acceptance controls. Then inspect the actual integrated code, architecture, public journeys,
   deployment/recovery paths, tests and observability.
2. Challenge whether the design is fit for those users: hidden coupling, unnecessary complexity,
   brittle abstractions, incomplete requirements, unsafe assumptions, state/concurrency failures,
   security boundaries, scaling limits, missing negative tests, upgrade/migration risks, operational
   diagnosis and developer experience. Examine inconsistencies between UI/API/SDK/CLI paths.
3. Test each suspicion against code and available deterministic evidence. Separate a demonstrated
   defect from a design tradeoff and an unverified hypothesis. State impact in the target's supported
   use case, affected component, exact candidate, evidence and recommended remedy. Do not invent
   load requirements or expand product scope to justify an architectural preference.
4. Dedupe against open/closed issues and earlier review findings. Create/update actionable Critique
   issues with P0–P3 severity, reproduction/example where relevant, acceptance criteria and expected
   tests. Unresolved high-impact uncertainty becomes a bounded investigation with a decision owner,
   not speculative issue spam or an asserted production failure.
5. Record a candid fit-for-purpose assessment in the linked GitHub control: strongest evidence,
   material weaknesses, unresolved uncertainty and issue links. Verify Project association and
   preserve the independent release acceptance gates.

Do not change product code or implement fixes. Completion is an evidence-based critique with
tracked actions, not a rewrite or a claim that the milestone is ready.
