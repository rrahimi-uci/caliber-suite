# Architecture workflow

Lead: [Architect](../roles/architect.md).

Read the [engineering policy](../engineering-policy.md) and
[GitHub integration contract](../github/project-integration.md). Honor explicit assignment limits.
The command's first operation is live Project context; repository inspection follows that read.
If context is unavailable, incomplete or ambiguous, report the blocker and do not infer scope
from a local plan or stale chat. Follow the linked issues and their complete acceptance contracts.

1. Resolve the target from live context. Read its issues, release controls, requirements, native
   dependencies, architectural work, human inputs and current progress. Identify the required user
   outcome, supported deployment and what lies outside scope.
2. Inspect [ARCHITECTURE.md](../../ARCHITECTURE.md), relevant ADRs and actual code/tests. Trace affected
   UI/API/SDK/CLI paths, services, data models, persistence, integrations, deployment and security
   boundaries. Check concurrency, scaling, observability and technical debt. For CALIBER, verify
   standalone ASGI/MLflow ownership and path-specific guarantees; do not infer product guarantees
   from a configuration switch, fake provider, or diagram.
3. Describe current and desired behavior, affected components, API/interface contracts, data flows,
   schemas, state transitions, migrations, failure/recovery semantics, authorization, performance,
   observability, testing and deployment implications. Include Mermaid diagrams where useful.
   Record alternatives and why the selected approach fits the live requirements.
4. Identify producing dependencies and integration boundaries. Specify observable acceptance and
   negative/fault oracles, compatibility constraints, rollout/rollback needs and unresolved decisions
   with accountable owners. Distinguish blocking decisions from optional improvements.
5. Store durable architecture/ADRs in the appropriate existing repository location through a PR,
   following shared validation and merge rules. Dedupe and create/update linked architecture issues
   in GitHub, attaching document/PR references and decision provenance. Put work scheduling and
   implementation progress in GitHub, not a second Markdown roadmap.
6. Re-read the Project and report architecture links, issue links, approved/blocked contracts and
   readiness for planning. Completion means sufficient detail for a planner to decompose without
   inventing architecture. It does not mean features are implemented or a release is ready.

Do not implement features. Architecture documentation changes remain subject to the PR policy.
