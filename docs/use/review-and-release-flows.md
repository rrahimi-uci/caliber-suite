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
reviewed_on: 2026-08-10
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
