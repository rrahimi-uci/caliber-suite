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
reviewed_on: 2026-10-14
version_applicability: current main branch docs contract
tags:
  - review
  - release
  - rollback
  - jobs
---

# Review and release flows

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

## 4. Related docs

- [Trust and governance](../use/trust-and-governance.md)
- [Evaluation and test sets](../use/evaluation-and-test-sets.md)
- [Calibration](../use/calibration.md)
- [Operations runbook](../runbook.md)
