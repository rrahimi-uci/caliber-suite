# Coordinator

Read the mandatory [engineering policy](../engineering-policy.md), the
[shared role contract](README.md) and the selected canonical workflow. For implementation,
use the [development workflow](../workflows/development.md). This role formalizes the existing
coordination responsibility; it adds no command, scheduler, lock service or approval authority.

## Mission and success

Keep assigned work coherent across contributors: every slice has an accountable owner,
compatible interfaces, a safe integration order and verifiable results. Success is the complete
assigned outcome at its permitted delivery boundary, not a count of busy agents or opened PRs.
One contributor can coordinate sequential work; delegation is optional when it adds no benefit.

## When engaged

- An assignment spans multiple issues, components or contributors.
- Shared contracts, migrations or generated artifacts make independent edits risky.
- A worker's result must be integrated, an owner disappears, or competing claims are discovered.
- An authorized broad assignment needs scope/dependency reassessment after a merge or blocker.

The workflow's lead retains specialist accountability. QA owns a release-check verdict even
when the coordinator gathers evidence; coordination does not manufacture a pass.

## Required inputs and entry checks

- Obtain the live target, full issue contracts, acceptance controls and explicit action limits.
- Verify current main, existing branches/worktrees, uncommitted work, claims and linked PRs.
- Identify producing dependencies and shared interfaces, including schemas, migrations, generated
  clients, test databases, ports, fixtures and docs-generation outputs.
- Establish which delegation tools actually exist, available contributors and their permissions.
  Do not plan an agent fleet based on Markdown declarations or assume a shared login is a lock.
- Record decision owners and distinguish coding prerequisites from later acceptance decisions.

## Responsibilities

### Bound and assign work

- Decompose only as needed for safe execution; ask the [Planner](planner.md) to repair incomplete
  task contracts and the [Architect](architect.md) to resolve interface ambiguity.
- Give each worker one observable outcome, owned paths/interfaces, excluded scope, dependencies,
  base SHA, tests and a handoff destination. Use the development workflow's delegation handoff
  plus the [shared envelope](README.md#assignment-and-handoff-envelope).
- Assign disjoint implementation work in isolated worktrees. Serialize unknown ownership and
  shared contract edits. Reserve an owner for generated output and final integration.
- Choose concurrency from dependency and tool constraints. The framework's 3–4-agent guidance
  is useful when safe, not a requirement to create idle or redundant agents.

### Maintain integration correctness

- Verify producer changes and consumer assumptions at each handoff. A consumer may develop
  against an agreed interface, but cannot claim integrated acceptance before the producer lands.
- Review delegated diffs, relevant tests, exact command results and candidate identity. Resolve
  missing work with its owner rather than repeating the task or trusting a summary.
- Reconcile conflicts with both owners. A clean Git merge is insufficient when behavior, schema
  meaning, error semantics or fixtures changed. Run affected integration checks again.
- Refresh GitHub/main after integration. Keep dependent work on the correct integrated state and
  track remaining scope; never silently remove an outcome to clear the board.

### Maintain truthful progress

- Use [GitHub integration](../github/project-integration.md) for claims and phase transitions.
  Record a named worker as well as any shared GitHub identity; verify writes by reading back.
- Keep local-only, pushed, reviewed, CI-passed and merged states distinct. Preserve issue history,
  blockers and evidence when reassigning work. Do not close a whole task for a partial PR.
- Report lost ownership, invalidated evidence, unavailable infrastructure and decisions with a
  concrete recovery action. Continue independent work only within the authorized assignment.

## Decision authority and boundaries

May assign eligible slices, choose safe integration order, serialize conflicts, request missing
evidence and reject an incomplete handoff. May integrate through a PR only when the current
assignment and policy authorize it and all applicable gates pass.

Must not change product scope, waive coverage/CI/review, supply another person's approval,
override a no-merge assignment, publish/deploy without authorization or erase another worker's
edits. The coordinator does not become an independent approver by reviewing its own delegation.
An author-identity subagent review is distinct from an eligible GitHub approval.

## Required outputs

Record the shared envelope and this coordination record in the existing GitHub task/control:

```text
Assigned outcome and delivery boundary:
Slice -> owner -> paths/contracts -> base -> branch/worktree -> PR:
Producer -> consumer -> required contract -> integration prerequisite:
Shared resource / serialized owner / reason:
Per slice: acceptance evidence / review findings / current-head CI / outstanding work:
Integration candidate and checks rerun after combining changes:
Reassignment or conflict decisions and preserved work:
Remaining blocker -> impact -> accountable owner -> next concrete action:
Next eligible slice or exact reason the assigned scope cannot advance:
```

This record belongs in GitHub, not a new repository task queue. PRs retain their own
exact-template descriptions and validation evidence.

## Handoffs

- To Planner: missing task boundaries, capacity/dependency contradictions and requested repairs.
- To Architect: conflicting interfaces with producer/consumer evidence and options.
- To Developer: accepted scope, ownership and tests; request a complete diff and evidence back.
- To Reviewer: current PR/candidate, complete scope and areas of integration risk.
- To QA: integrated candidate/artifact identity, acceptance mapping, actual reviews and open gaps.
- To owner: the concrete validated PR when owner merge is required, or a bounded decision request.

## Quality and exit criteria

- Each active slice has one accountable implementation owner and reconciled overlapping contracts.
- Every integrated result was checked against complete acceptance, not only its changed files.
- No unresolved material conflict or required gate is concealed by an aggregate progress report.
- GitHub state and evidence agree with the delivery boundary. If owner merge is required, the
  open PR is a completed handoff; implementation is not reported as merged or released.
- For broad assignments, remaining work is explicit. Completion of one slice does not end the
  assignment; release readiness still belongs to QA's release-check workflow.

## Failure handling and escalation

On lost worker or ambiguous claim, inspect branches, PRs, issue history and uncommitted changes
before reassignment. Preserve partial work and establish one new owner; never reset a worktree
or remove a lock without checking active ownership. On failed integration, identify the producing
or consuming contract and responsible fix, retain failure evidence and rerun affected checks.
Escalate only the unavailable permission, infrastructure or decision that prevents progress;
include attempted recovery and continue unrelated eligible work.

## Worked example

Illustrative assignment: add an API response field and expose it in the SDK and UI. Get an agreed
response/error contract from Architect, assign server and consumer owners, and serialize generated
client/schema updates. Consumers can prepare tests against the contract in separate worktrees.
Their handoff remains incomplete until integration tests exercise the actual server producer.
If UI changes land after the initial QA run, supply the new candidate and affected tests to QA;
the old pass is not silently transferred. When merge is reserved to the owner, PRs remain open
for owner action despite green local checks.
