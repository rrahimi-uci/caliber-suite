---
audience:
  - system-user
  - evaluator
  - developer
doc_type: how-to
product_area: calibration
stability: ga
prerequisites:
  - An evidence set or runtime question
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - calibration
  - candidates
  - optimization
  - governance
---

# Calibration

Use this page for the practical calibration path: choose the asset family,
launch candidate generation, inspect the evidence, and move to explicit human
review or apply only where the product supports it.

## At a glance

| Task | Start here | Deep reference |
| --- | --- | --- |
| understand whether calibration exists for an asset | check the per-asset model | [Calibration architecture](../15-calibration/architecture.md) |
| generate better candidates | launch the asset-specific loop | [Calibration architecture](../15-calibration/architecture.md) |
| inspect evidence and targets | connect back to evaluation and release | [Evaluation and test sets](../use/evaluation-and-test-sets.md), [Review and release flows](../use/review-and-release-flows.md) |
| recover a lost or stuck job | use the operator runbook | [Operations runbook](../runbook.md) |

## 1. What calibration is for

Calibration is proposal generation backed by evidence. It is not hidden
autopilot. Different asset families expose different candidate-generation and
apply semantics, so always reason about the target asset first.

## 2. Common tasks

| You want to... | Read this next |
| --- | --- |
| calibrate prompts or skills | [Calibration architecture](../15-calibration/architecture.md) |
| understand how evaluation feeds the loop | [Evaluation and test sets](../use/evaluation-and-test-sets.md) |
| inspect the live-control boundary after calibration | [Review and release flows](../use/review-and-release-flows.md) |

## 3. Common failure modes

| Symptom | First thing to check |
| --- | --- |
| Calibration produced a candidate but nothing went live | apply/release is a separate control path |
| The candidate improved one slice but hurt another | the evidence set or judge mix is incomplete |
| The job vanished or stalled | use the runbook recovery path instead of retrying blindly |

## 4. Related docs

- [Evaluation and test sets](../use/evaluation-and-test-sets.md)
- [Review and release flows](../use/review-and-release-flows.md)
- [Trust and governance](../use/trust-and-governance.md)
- [Calibration architecture](../15-calibration/architecture.md)
