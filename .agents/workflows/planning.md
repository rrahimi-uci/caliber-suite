# Planning workflow

Lead: [Planner](../roles/planner.md); consult Architect and QA for contracts and evidence.

Read the [engineering policy](../engineering-policy.md) and
[GitHub integration contract](../github/project-integration.md). Honor explicit assignment limits.
The command's first operation is live Project context; repository inspection follows that read.
If context is unavailable, incomplete or ambiguous, report the blocker and do not infer scope
from a local plan or stale chat. Follow the linked issues and their complete acceptance contracts.

1. Read all target issues, parents, native blockers, acceptance controls, architecture links,
   priorities, statuses and human decisions. Then inspect code/tests, merged PRs and current main
   to classify work as implemented, partial, missing, duplicate, obsolete or incorrectly tracked.
   Verify evidence before changing an issue's state; preserve historical provenance.
2. Resolve architectural gaps before planning dependent implementation. Build a dependency DAG with
   producing contracts, integration points and acceptance gates. Parent/child hierarchy is not
   itself a dependency. Avoid cycles and serialize overlapping schemas, migrations and shared files.
3. Size a feasible slice for approximately two development weeks with 3–4 agents, followed by a
   protected QA/bug-bash/fixes/validation week when using a three-week delivery cycle. These are
   planning assumptions, not staffing or delivery commitments. Use available capacity and measured
   effort; split large scope into successive cycles without dropping required outcomes.
4. For each task specify objective, detailed context/evidence, architecture references, exact
   outcome, implementation boundaries and likely components, dependencies, priority, complexity,
   acceptance criteria, required positive/negative/integration tests, QA, docs/migration implications,
   owned paths and safe parallelization. Include implementation, integration, tests, migration,
   infrastructure, documentation and acceptance work when relevant. Separate start decisions from
   acceptance decisions and identify the decision owner; do not fabricate thresholds or dates.
5. Search open and closed issues plus Project items for semantic duplicates. Use the integration
   helper's validated issue payload and dry run; inspect then apply authorized writes. A duplicate
   result never overwrites the old issue, including a closed one. Make needed explicit `gh` edits
   only after reviewing current content, preserving unrelated evidence and rechecking the result.
6. Record the execution plan in GitHub issues/Project fields: milestones, release/cycle, priorities,
   native blocked-by links, owners/lanes, acceptance and integration order. New issues are Planned.
   Set `human_input` from evidence; default `Not assessed` is not readiness. Move a task to Ready
   only through an evidence-bearing transition after all start gates and dependencies are verified.
   Never create an invented cycle merely to satisfy a helper. An unscheduled task keeps a blank
   Delivery cycle; committing a new cycle through explicit `gh` edits requires verified capacity,
   requirements and dependencies first.
7. Re-read the complete target. Confirm every intended issue/item and field, dependencies, absence
   of duplicates and executable entry tasks. Report GitHub links, feasible parallel lanes, estimated
   capacity and unresolved decisions. The plan is complete when executable work is in GitHub;
   an explanation in chat or local Markdown alone is incomplete.

Do not implement features or weaken acceptance to fit a calendar. Aggregate Gate/control issues
remain acceptance owners, not developer tasks; historical milestones may have no executable tasks.
