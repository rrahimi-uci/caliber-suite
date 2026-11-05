# Role contracts and collaboration

Read the mandatory [engineering policy](../engineering-policy.md) first. This guide defines
who is accountable, what each role may decide, and what a usable handoff contains. The selected
[canonical workflow](../README.md#command-routing) defines execution order; the
[GitHub contract](../github/project-integration.md) defines authoritative state and supported
operations. Roles do not add commands, CLI payload fields, execution tools or permissions.

## Choose the role by the question it must answer

| Role | Accountable question | Primary deliverable | Does not own |
| --- | --- | --- | --- |
| [Coordinator](coordinator.md) | Who owns each slice, and can the results be integrated safely? | Bounded assignments, integration decisions and verified progress | Product scope decisions, another person's approval |
| [Architect](architect.md) | What design and contracts can meet the agreed outcome? | Grounded design/ADR, interfaces, failure semantics and decision gaps | Schedule commitments or feature implementation |
| [Planner](planner.md) | What executable tasks and dependencies deliver that design within capacity? | GitHub task contracts, dependencies, readiness and feasible delivery slices | Invented requirements or architectural shortcuts |
| [Developer](developer.md) | Does the assigned implementation fulfill its complete contract? | Tested implementation PR and candidate-specific evidence | Redefining acceptance, silently taking neighboring work |
| [Reviewer](reviewer.md) | Is the implementation correct against its contracts and safe to integrate? | Evidence-based findings and explicit review coverage | Feature fixes within a review-only assignment |
| [Critic](critic.md) | Is the integrated approach suitable for the intended users and operators? | Fit-for-purpose assessment, demonstrated weaknesses and bounded investigations | Treating preferences or hypotheses as proven defects |
| [Bug basher](bug-basher.md) | What breaks in realistic and adversarial supported journeys? | Journey matrix and reproducible, deduplicated bugs | Product fixes or declaring release readiness |
| [QA](qa.md) | What acceptance is actually proven for this exact candidate? | Acceptance/evidence matrix; READY or NOT READY for release-check | Granting missing approvals, publication or deployment |

The coordinator is a responsibility within an assigned workflow, not an eighth slash command or
an automatically spawned agent. A single agent may perform several roles sequentially. State the
active role and scope when switching; a review-only assignment does not become development just
because the same agent can code. Record self-review honestly. Another agent can independently
inspect evidence, but an agent using the author's GitHub identity is not an independent GitHub
approver. A required named human or eligible reviewer cannot be replaced by role labels.

## Decision rights and ownership

| Decision or record | Accountable role | Required consultation or evidence |
| --- | --- | --- |
| Product outcome, risk acceptance, required external trial | Existing named product/security/operator decision owner | Architect/Planner/QA provide options and impact; agents cannot impersonate the owner |
| Technical design within agreed scope | Architect | Developer feasibility, Reviewer risk, QA testability; escalate unresolved product tradeoffs |
| Task decomposition and readiness proposal | Planner | Producing dependencies, Architect contracts, QA acceptance and confirmed capacity |
| Assignment, shared-file ownership and integration order | Coordinator | Current claims/PRs, worker identities, dependency and conflict checks |
| Local implementation choice within the contract | Developer | Existing patterns, tests, compatibility and owned interfaces |
| Review finding and verified resolution | Reviewer | Reproduction/code path and fixing candidate; implementer supplies the fix |
| Fitness concern or design investigation | Critic | Intended use, effective runtime evidence and explicit uncertainty |
| Reproducible exploratory defect | Bug basher | Isolated journey, expected/actual result and evidence |
| Acceptance evidence and candidate verdict | QA | All applicable gates, actual approvals and complete scope |
| PR merge | Authorized integrator under current policy and assignment | Current head/checks/protections; a role title alone grants no merge authority |

The accountable role must ensure the record exists and is correct; another authorized contributor
may perform the GitHub write. Use existing fields and transitions. A suggested state in a report
is not a successful remote update. Re-read after writes and record failures explicitly.

Explicit task restrictions remain binding. If the assignment says the owner will merge, deliver
the validated PR for owner review and leave it open. Do not call that merged completion. If
authority is genuinely contradictory, continue reversible independent work and resolve the
contradiction before the disputed action; never interpret a role contract as an override.

## Common entry contract

Before taking responsibility, identify the following from the assignment and live GitHub context.
Read the complete issues, not only titles or board fields. Inspect the relevant implementation
after scope discovery. A role may investigate a missing input; it must not pretend the input exists.

- Target and issue/control URLs, intended user/operator outcome and acceptance criteria.
- Explicit action limits: read-only, documentation, implementation, PR, merge, external trials.
- Current base/candidate SHA, linked PRs, architecture and supported configurations.
- Producing dependencies and the actual integrated interfaces they supply; parentage alone is
  not a dependency, and a closed issue alone is not implementation evidence.
- Accountable worker and coordinator, owned files/contracts, adjacent owners and test resources.
- Required tests, QA/approval gates, evidence destination and pending human decisions by phase.

Missing acceptance that affects implementation correctness, unknown ownership of shared files,
or an unresolved start decision blocks dependent implementation. Record the precise gap and
owner, then continue independent authorized work. Acceptance-only decisions remain acceptance
blockers without automatically preventing safe offline development.

## Assignment and handoff envelope

Use this copyable record in the linked issue/PR or authorized handoff. Replace every bracketed
field with evidence or a specific missing-input statement. This is prose, **not a new CLI JSON
schema**. Add the specialized output required by the relevant role.

```text
Role / worker identity / coordinator:
Assignment and target / issue or control URLs:
Expected observable outcome / acceptance criterion IDs:
Action limits / excluded scope:
Base SHA / candidate SHA / artifact digests when relevant:
Dependencies / producing contracts / integration order:
Owned paths and interfaces / neighboring owners / branch or worktree:
Evidence: criterion -> source or test -> exact command -> environment -> result -> URL:
Finding or decision: demonstrated fact / hypothesis / approved tradeoff:
Known gaps / pending human input / owner / start or acceptance phase:
Actual delivery state: investigated, local-only, pushed, PR CI passed, merged,
                      deployed, or live verified (evidence for each claimed state):
Next receiving role / concrete requested action / exit criteria:
```

The receiving role checks scope, identity and evidence before accepting the handoff. If a result
is incomplete, return the precise missing criterion and next action; preserve completed work.
Do not treat a worker's prose summary as proof that tests ran, changes were integrated or bugs
were fixed. Commands without results are plans. Green checks for an old SHA are historical
evidence, not a pass for the current candidate.

## Collaboration and disagreement

- The coordinator establishes bounded ownership before parallel implementation. Use isolated
  worktrees/branches when the environment supports them; otherwise serialize edits. A shared
  GitHub login, role name or disjoint-looking paths does not prove exclusive ownership.
- Shared contracts, migrations, generated clients and integration fixtures require coordination
  even when workers edit different files. Read the producer's integrated contract before claiming
  the consumer is complete.
- The developer supplies a fix; the reviewer verifies the material finding; QA rechecks affected
  acceptance. Bug basher reproductions and critic findings feed those roles without bypassing them.
- Resolve disagreements by identifying the violated requirement, actual code path and observable
  counterexample. A preference is not a defect. An unresolved product choice goes to its named
  owner with options, consequences and the smallest question needed to proceed.
- Keep severities consistent with the [GitHub finding definitions](../github/project-integration.md#findings-evidence-and-access-failures).
  Severity, confidence and whether a finding blocks acceptance are separate statements.
- Follow [SECURITY.md](../../SECURITY.md) for sensitive evidence. Never paste credentials, private
  data or exploit details into public issue output merely to make a report reproducible.

## Quality of a role output

A useful output lets the next role act without guessing the owner, contract, affected paths,
expected result or verification method. Prefer exact evidence and bounded conclusions over long
generic checklists. Mark an irrelevant check as not applicable with its reason; unavailable or
unexecuted required evidence remains a gap. None of these instructions certifies execution by
itself; deterministic tests, actual review and verified GitHub state supply that evidence.

These contracts apply to any live release or cycle. Worked examples are illustrative teaching
cases, not findings, commitments, shipped guarantees or a local backlog. Store real scope,
priority, scheduling, decisions and progress in GitHub.
