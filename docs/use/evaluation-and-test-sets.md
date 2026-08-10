---
audience:
  - system-user
  - evaluator
  - developer
doc_type: how-to
product_area: evaluation
stability: ga
prerequisites:
  - A CALIBER deployment with evaluation access
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - evaluation
  - test-sets
  - judges
  - qa
---

# Evaluation and test sets

Use this page when you need the practical trust loop: assemble evidence, run
scorecards, inspect judges and per-example results, and decide what still needs
review before live change.

## At a glance

| Task | Start here | Deep reference |
| --- | --- | --- |
| define the evidence base | create or inspect a test set | [Test sets architecture](../11-test-sets/architecture.md) |
| score a candidate | run evaluation against that evidence | [Evaluation architecture](../14-evaluation/architecture.md) |
| inspect judge behavior | understand the scorer and custom judge surfaces | [Evaluation architecture](../14-evaluation/architecture.md) |
| connect runtime QA to engineering evaluation | compare both trust loops explicitly | [QA plan](../13-qa-plan/architecture.md) |

## 1. What this path is for

CALIBER separates evidence from rollout. Evaluation tells you how something
performed against a known set of cases. It does not release or apply a change.

## 2. Common tasks

| You want to... | Read this next |
| --- | --- |
| curate representative examples | [Test sets architecture](../11-test-sets/architecture.md) |
| run a scorecard or judge-backed evaluation | [Evaluation architecture](../14-evaluation/architecture.md) |
| connect evaluation to calibration | [Calibration](../use/calibration.md) |
| connect evaluation to runtime review and release | [Review and release flows](../use/review-and-release-flows.md) |

## 3. Common failure modes

| Symptom | First thing to check |
| --- | --- |
| Scores are high but production still looks weak | the evidence set may not represent the live workload |
| A judge looks inconsistent | inspect custom-judge setup and agreement expectations |
| A good score did not change production | evaluation is evidence, not apply/release |

## 4. Related docs

- [Trust and governance](../use/trust-and-governance.md)
- [Calibration](../use/calibration.md)
- [Review and release flows](../use/review-and-release-flows.md)
- [QA plan](../13-qa-plan/architecture.md)
