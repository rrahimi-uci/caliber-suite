# GitHub Project integration contract

Read the mandatory [engineering policy](../engineering-policy.md). The associated Project is
[CALIBER Project #3](https://github.com/users/rrahimi-uci/projects/3); repository identity is
`rrahimi-uci/caliber-suite`. The association is discovered configuration, not a fixed set of
release names. GitHub Project and linked issues own all planning and execution state.

## Read first and resolve scope

Every command first runs `node .agents/bin/engineering.mjs context <command> <target>` from the
repository root, then inspects the repository. Read the returned canonical policy/workflow/role
paths and complete issues, native blocked-by relationships, parent/sub-issues, linked PRs,
acceptance controls and human decisions. Paginate discovery; partial results cannot establish
scope or readiness. Draft/redacted items of relevant or unknown scope are exposed as
`scope-uninspectable` blockers; resolve them before release acceptance. A native dependency must
be closed as COMPLETED; not-planned or unknown closure reasons cannot authorize dependent work.
Refresh before writes and after integration. On read failure, unknown target,
ambiguous identity or schema drift, stop dependent actions and report the concrete blocker.

Targets resolve from live Release options, existing Delivery cycle values or native milestone
names. Qualify ambiguity as `release:<name>`, `cycle:<name>` or `milestone:#<number>`, passing the
whole target as one safely quoted argument. Never infer that Release and native milestone have
the same name or that a cycle covers its entire release. Empty or nonexistent targets are errors.
Historical milestones may have no executable Task items; inspect their history rather than
creating artificial work. Controls use `Gate`/`Control` kinds or `milestone-control` labels and
must not be selected as ordinary feature tasks.

Use `node .agents/bin/engineering.mjs schema` to discover field types, IDs and select options
live. The configured field names reflect the observed association; runtime field/option IDs
must come from discovery. Schema does not enumerate repository labels. Discover those separately:

```sh
gh label list --repo rrahimi-uci/caliber-suite --limit 300 --json name
```

If the limit is reached, obtain all pages before drawing conclusions. Existing Project views
filter labels. The issue helper validates configured task/epic, priority and release/stage labels
against live labels before creation and does not invent labels. A newly introduced Release value
requires a reviewed configuration mapping update; workflow files must remain release-neutral.

| Concept | Observed field/type | Meaning |
| --- | --- | --- |
| Execution status | Status / single select | Todo, In Progress, Done |
| Workflow phase | Readiness / text | Planned, Ready, In Progress, In Review, QA, Blocked, Done |
| Destination | Release / single select | Discover options; never hardcode one destination |
| Timebox | Delivery cycle / text | Existing committed cycle; blank means unscheduled |
| Scheduling priority | Priority / text | P0, P1, P2, P3 |
| Work classification | Item type / text | Task, Epic and existing acceptance/coordination kinds |
| Decision gates | Human input / text | Evidence-based start/acceptance classification |
| Native milestone | Linked issue milestone | Repository milestone identity, independent of Release |

At framework discovery on 2026-11-04, all Delivery cycle values were blank. No cycle dates or
new cycle commitments are established by this framework. Preserve unscheduled work. A planner
may explicitly commit cycles through reviewed `gh` field edits after capacity, requirements,
dependencies and QA capacity are checked. The helper resolves existing cycles; it does not
create cycle values or dates. Re-read the Project for current scheduling state.

## Issue creation and deliberate updates

Before writing, search both open and closed repository issues and Project items by outcome,
affected component, reproduction and existing acceptance criteria. The helper detects normalized
title/fingerprint matches, not all semantic duplicates. A match returns the existing issue without
mutation, including closed issues. Multiple matches are an ambiguity to resolve, not a reason to
create another issue. Preserve existing valid work and evidence.

The `issue` operation creates validated planned work. Despite the broader create/update workflow,
it is not an automatic overwrite operation. A planner may make an explicit reviewed `gh issue edit`
or Project edit after reading current content and checking the diff. Keep unrelated sections,
metadata, evidence and dependencies; re-read after editing. Repair a partial write on the existing
issue instead of retrying by creating another. Updates to Project state are not atomic transactions.

Required JSON fields are `title`, `kind`, `objective`, `context`, nonempty arrays `acceptance`,
`tests`, `components`, text `complexity` and `parallelization`, `priority` (P0–P3), and
`dependencies` (array of distinct existing issue numbers, possibly empty). Allowed kinds are
Task, Bug, Review, Critique, Architecture and Epic. Metadata retains that kind; Project Item type
is Epic for an Epic and Task for the other kinds so existing views continue to work.

Optional fields:

- `milestone`: an open native milestone number in the resolved target; inferred only when the
  target has exactly one milestone. Select explicitly when ambiguous.
- `release`: an existing Release value consistent with target and milestone; inferred only when
  unambiguous. `cycle`: an existing consistent Delivery cycle; omit it to preserve blank scheduling
  when there is no existing cycle, or when the helper can resolve one unambiguously. Multiple
  possible cycles require explicit selection.
- `owned_paths`: repository-relative ownership paths. Omitted/empty/unknown ownership requires
  serial execution. Disjoint paths suggest possible parallelism; shared contracts and integrations
  still require coordinator review.
- `human_input`: defaults to `Not assessed`, which blocks readiness. Set `None pending` only after
  verifying no pending decisions. Use the existing decision IDs and phase syntax, for example
  `H02: start` or `H03: acceptance` (comma-separated for several). Start/unknown inputs block coding;
  acceptance-only inputs can permit implementation but remain acceptance blockers. Put detailed
  decision owner, evidence and resolution in issue context; do not invent a decision ID.

This illustrative payload must be replaced with a verified task and evidence before applying.
Native milestone/release are omitted here only to show inference for an unambiguous live target;
Delivery cycle is intentionally unscheduled. `Not assessed` deliberately keeps it out of Ready.

```json
{
  "title": "Add regression coverage for the confirmed SDK pagination defect",
  "kind": "Task",
  "objective": "Protect the verified pagination contract with a deterministic regression test.",
  "context": "Replace with the source issue URL, exact reproduction, candidate SHA and architecture references.",
  "acceptance": ["The regression fails on the confirmed broken behavior and passes on the accepted implementation."],
  "tests": ["Run the relevant SDK pagination tests and applicable SDK CI-equivalent checks; record commands and results."],
  "components": ["sdk/caliber-sdk/tests/"],
  "complexity": "Small; confirm against the actual reproduction before committing capacity.",
  "parallelization": "One owner; serialize with changes to the affected pagination contract.",
  "priority": "P2",
  "dependencies": [],
  "owned_paths": ["sdk/caliber-sdk/tests/"],
  "human_input": "Not assessed"
}
```

Save the reviewed payload to a temporary JSON file outside the repository task backlog:

```text
node .agents/bin/engineering.mjs issue "<resolved target>" --file /tmp/caliber-task.json
node .agents/bin/engineering.mjs issue "<resolved target>" --file /tmp/caliber-task.json --apply
```

The first command is a dry run. Inspect the proposed issue, milestone, fields, labels and
native dependencies before authorized apply. The second makes writes. Re-read the issue and
Project to confirm all updates. New work starts Status Todo, Readiness Planned, never implicitly
Ready. A helper success is not acceptance evidence.

## Phase transitions

```text
node .agents/bin/engineering.mjs transition "<resolved target>" <issue-number> <phase> --file /tmp/caliber-transition.json
```

Omit `--file` only where no payload is needed. Append `--apply` only after reviewing the dry run
within the assignment's authority. The exact phases and payloads are:

| Phase | Status / Readiness | JSON payload and preconditions |
| --- | --- | --- |
| planned | Todo / Planned | `{}`; open, unclaimed work; do not reset another owner's active work |
| ready | Todo / Ready | `{"evidence":["Verified dependency and decision evidence URL or text"]}`; eligible task with resolved start gates |
| claim | In Progress / In Progress | `{"owner":"<GitHub login>","branch":"feat/<task>"}`; eligible Ready task, dedicated branch |
| review | In Progress / In Review | `{"evidence":["PR URL and actual validation evidence"]}`; claimed work and an open linked PR |
| qa | In Progress / QA | `{"evidence":["Candidate and QA evidence"]}`; claimed work |
| blocked | In Progress / Blocked | `{"reason":"Exact blocker, evidence and next required action/owner"}`; open work |
| done | Done / Done | `{"evidence":["Merged PR, acceptance, main SHA and CI evidence"]}`; already closed as COMPLETED |

Use actual values in these payloads. The evidence array is stored context, not machine proof of
quality. The helper does not execute tests, inspect all CI/approval gates or close issues. The
agent must verify acceptance, exact-head CI, merge and post-merge health before closing an
implementation issue and recording Done. Closing as not planned is not completed work. A control
issue needs its own acceptance proof, not merely closed children. The blocked phase uses the
existing In Progress status; there is no new Blocked status option or invented state machine.

The linked-PR connection uses closing references. A complete-task PR should use `Closes #<number>`;
a partial slice must not close the whole task. For partial PRs maintain an explicit issue/PR link,
assignee and active status, and inspect it manually; `Refs #<number>` alone is not discoverable as
a closing PR by the helper. Do not mislabel a partial PR solely to satisfy a review transition.

Applied transitions refresh the full Project snapshot immediately before authorization, then
recheck the issue state/update timestamp. Project-field edits need this refresh because they may
not change the issue timestamp. The remaining remote race requires coordinator reconciliation.

Apply operations serialize cooperating local worktrees through a shared local lock, but other
clones/actors remain concurrent. Re-read after claiming and obtain coordinator confirmation of
exclusive ownership before coding. Stale snapshots, partial writes or ownership conflicts require
reconciliation. Never remove a lock until checking the active coordinator and remote write state.

## Findings, evidence and access failures

P0 means critical immediate impact; P1 means must fix for the target; P2 means should fix;
P3 means improvement. Scope-specific acceptance may make a lower-severity finding blocking too.
Every finding needs impact, candidate/environment, affected component, source/test or reproduction
evidence, expected versus actual behavior, actionable remedy, acceptance/test oracle and target
association. Follow [SECURITY.md](../../SECURITY.md) for sensitive evidence. Dedupe before creating
Bug/Review/Critique issues; record hypotheses as investigations rather than established defects.

With read-only access, report the exact missing write and issue/control links. If authorized issue
comments are possible, record claim/blocker/evidence there for the coordinator, but do not claim
Project reconciliation succeeded. No Project access means no authoritative workflow context;
report that blocker rather than producing an independent local plan. Never store credentials,
claim a dry run mutated GitHub, or claim the CLI launched agents or certified a release.
