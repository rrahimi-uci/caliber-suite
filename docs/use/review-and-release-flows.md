---
audience:
  - system-user
  - operator
  - evaluator
  - developer
doc_type: how-to
product_area: governance
stability: ga
prerequisites:
  - A change candidate, review question, or release decision
reviewed_on: 2026-10-15
version_applicability: current main branch docs contract
tags:
  - review
  - release
  - rollback
  - jobs
---

# Review and release flows

> **Disabled by default: release-candidate signoff and Workspace releases.** Review, Apply, rollback, the live
> board, the release timeline and release operations are core and always on. The release-candidate signoff
> (record-only) and Workspace releases (experimental) are off by default; enable with
> `CALIBER_FEATURE_RELEASE_CANDIDATES=true` and restart CALIBER. See
> [Release-candidate signoff and Workspace releases](#release-candidate-signoff-and-workspace-releases) below
> and [Feature flags](../start/alpha-scope.md#feature-flags).

Use this page when you need the practical path from candidate evidence to human
review, explicit apply, release, or rollback.

## At a glance

| Question | Start here | Deep reference |
| --- | --- | --- |
| How does evidence turn into an apply decision? | follow the explicit review path | [The refinement loop](../refinement-loop.md) |
| Where do jobs and apply targets show up? | inspect the release surfaces | [REST API HTTP reference](../api/reference.md) |
| What if release or apply is stuck? | use the on-call path | [Operations runbook](../runbook.md) |
| What if runtime and offline trust disagree? | compare the trust surfaces | [Trust and governance](../use/trust-and-governance.md) |

## 1. What this path is for

CALIBER deliberately separates candidate generation, review, and live apply.
That is true for prompts, workflows, and other governed assets even though each
asset family has different semantics.

## What Apply does

Apply is `POST /jobs/{job_id}/apply` (SDK `jobs.apply(job_id)`, and the **Apply** button in the page). It is
the explicit human decision, and these are its rules:

- **Who may.** The caller needs `caliber.operator` and, for a job whose target belongs to a project, `owner` or
  `editor`. A person outside the project gets 404 and a member with another role gets 403, before anything is
  claimed. There is no second approver: the approval record Apply creates (already approved, naming the caller)
  is an audit and rollback anchor, not a vote.
- **Which jobs.** Only a job at `candidate_ready`. Any other status answers 409.
- **Intent first.** The claim (`candidate_ready` to `applying`) and the approval record are committed before
  the promotion runs. If the promotion fails before any release operation exists, the job returns to
  `candidate_ready` and the approval record is withdrawn, so you can retry. If a release operation does exist,
  the job stays `applying` until the release reconciler settles it.
- **Simulated releases say so.** The response carries `simulated`, which is `true` when the promoter is the fake
  one, and the audit rows record it.
- **Sending it back.** `POST /jobs/{job_id}/request-changes` returns a `candidate_ready` job to the candidate
  stage with your notes. It needs `owner`, `editor` or `reviewer`.

## Release-candidate signoff and Workspace releases

These two areas are **off by default**. Enable them with `CALIBER_FEATURE_RELEASE_CANDIDATES=true` and
restart CALIBER; turning the flag on means you accept their alpha behavior, below.

| Area | Routes it gates | What it does in this build |
| --- | --- | --- |
| Release-candidate signoff (record-only) | `/releases/candidates*` and `/releases/report-jobs*`, and the candidate section of the Releases page | Assembles criteria, evidence and waivers, computes a readiness score on the server, and records a go or no-go signoff with a rationale. A signoff **records a decision** and deploys or rolls back nothing. A waiver's `expires_at` is honoured every time the candidate is evaluated (a lapsed waiver stops clearing its blocker), and a signed candidate cannot be evaluated, waived or signed again. Scores and evidence references are entered by people: CALIBER does not read them from Evaluations or check that the artifact exists |
| Workspace releases (experimental) | `/projects/{id}/releases*` (and `caliberctl workspace release`) | The default-off owner worker executes a complete release-local pinned evaluation when explicitly enabled; incomplete plans fail closed. Per-example evidence, expired-attempt recovery, and matching-evidence enforcement for governed decisions/break-glass are implemented; broader production apply remains a later Beta slice |

While the flag is off, a call to any of those routes answers HTTP 404 with `reason_code: feature_disabled`,
and the Releases page shows only the live board, the timeline and release operations. Those are core and not
behind the flag, and neither are `GET /releases/live`, `GET /releases/timeline` and
`GET /releases/operations`. `GET /capabilities` reports the flag as `features.release_candidates`, so a client
can check it first. The path that governs a release end to end is Apply and rollback, above.

## 2. Common tasks

| You want to... | Read this next |
| --- | --- |
| inspect whether a candidate is ready for review | [Evaluation and test sets](../use/evaluation-and-test-sets.md) |
| understand the per-asset live-control boundary | [The refinement loop](../refinement-loop.md) |
| recover from a stuck release, lost job, or rollback | [Operations runbook](../runbook.md) |

## 3. Common failure modes

| Symptom | First thing to check |
| --- | --- |
| Evidence exists, but nobody can apply it | review and apply are separate from evaluation |
| A release is stuck in an uncertain state | use the runbook instead of retrying ad hoc |
| A rollback path is unclear | inspect the asset-specific control path first |
| The Releases page has no candidate section, or `/releases/candidates` answers 404 with `reason_code: feature_disabled` | `CALIBER_FEATURE_RELEASE_CANDIDATES` is off (the default); set it to `true` and restart |

## 4. Related docs

- [Trust and governance](../use/trust-and-governance.md)
- [Evaluation and test sets](../use/evaluation-and-test-sets.md)
- [Calibration](../use/calibration.md)
- [Operations runbook](../runbook.md)
