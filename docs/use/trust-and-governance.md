---
audience:
  - system-user
  - operator
  - evaluator
doc_type: concept
product_area: governance
stability: ga
prerequisites:
  - A CALIBER deployment or design question
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - trust
  - governance
  - evaluation
  - calibration
---

# Trust and governance

This page connects the major trust surfaces in CALIBER so a reader can
understand how evidence, review, and apply fit together without starting from
deep subsystem architecture.

## At a glance

| Surface | What it answers | Start here |
| --- | --- | --- |
| Test sets | What evidence are we scoring against? | [Evaluation and test sets](../use/evaluation-and-test-sets.md) |
| Evaluation | How is quality measured? | [Evaluation and test sets](../use/evaluation-and-test-sets.md) |
| Calibration | How are better candidates proposed? | [Calibration](../use/calibration.md) |
| Workflow/runtime approvals | Who may allow a live run to continue? | [Workflows](../use/workflows.md) |
| Artifact apply / release | Who may move a candidate live? | [Review and release flows](../use/review-and-release-flows.md) |
| QA plan | How do runtime QA and engineering validation relate? | [QA plan](../13-qa-plan/architecture.md) |

## 1. Evidence comes first

CALIBER is designed around measured change, not silent mutation.

The practical reader path is:

1. define or inspect the evidence set
2. run evaluation or calibration
3. inspect the result
4. apply or release only through the explicit control path for that asset family

## 2. Evaluation is not the same as apply

Evaluation tells you how a candidate performed. It does not make the candidate
live by itself.

That distinction matters because different asset families have different live
control paths:

- prompts use explicit alias release semantics
- workflows have their own apply/review path
- runtime approvals govern live executions rather than offline candidate motion

## 3. Calibration is proposal generation, not hidden autopilot

Calibration surfaces generate candidate changes or candidate-ready outcomes using
the evidence loop available for that asset family.

Use the deep references when you need the exact execution model:

- [Calibration](../15-calibration/architecture.md)
- [Refinement loop](../refinement-loop.md)

## 4. Human authority stays explicit

CALIBER separates:

- evidence generation
- candidate generation
- approval or review
- live apply / release

That is why the governance story must be read across several surfaces rather
than only one route family.

## 5. Recommended reader path

| If your question is... | Read this next |
| --- | --- |
| What are we measuring against? | [Evaluation and test sets](../use/evaluation-and-test-sets.md) |
| How are scores, judges, and results represented? | [Evaluation and test sets](../use/evaluation-and-test-sets.md) |
| How does the system generate better candidates? | [Calibration](../use/calibration.md) |
| How do live workflow decisions work? | [Workflows](../use/workflows.md) |
| How do engineering tests relate to runtime trust? | [QA plan](../13-qa-plan/architecture.md) |
