---
audience:
  - system-user
  - developer
  - evaluator
doc_type: how-to
product_area: prompts
stability: ga
prerequisites:
  - A CALIBER deployment with prompt access
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - prompts
  - release
  - evaluation
  - calibration
---

# Prompts

This page is the task-oriented entry point for using prompt assets in CALIBER.
Use it before the deep prompt architecture page when the question is practical:
author, evaluate, calibrate, release, or recover.

## At a glance

| Task | Start here | Deep reference |
| --- | --- | --- |
| Edit a prompt safely | create or update a prompt revision | [Prompts architecture](../02-prompts/architecture.md) |
| Measure prompt quality | run evaluation against a test set | [Evaluation](../14-evaluation/architecture.md) |
| Generate better candidates | launch prompt calibration | [Calibration](../15-calibration/architecture.md) |
| Make a candidate live | use the explicit release/apply path | [The refinement loop](../refinement-loop.md) |
| Recover from release trouble | use the runbook | [Operations runbook](../runbook.md) |

## 1. What prompts are for in CALIBER

Prompts are governed assets with version history, evaluation context, and an
explicit release boundary. Editing a prompt does not make it live by itself.

That distinction matters because CALIBER separates:

- authoring
- evidence generation
- candidate generation
- live release

## 2. Common tasks

| You want to... | Read this next |
| --- | --- |
| create or revise a prompt | [Prompts architecture](../02-prompts/architecture.md) |
| compare prompt behavior against known examples | [Evaluation and test sets](../use/evaluation-and-test-sets.md) |
| generate improved candidates | [Calibration](../use/calibration.md) |
| understand release, rollback, and review boundaries | [Review and release flows](../use/review-and-release-flows.md) |

## 3. What usually confuses readers

| Symptom | What it usually means |
| --- | --- |
| The prompt changed, but production behavior did not | the change is not live until the explicit release path completes |
| A candidate scored well, but nothing deployed | evaluation or calibration produced evidence, not live apply |
| Prompt behavior regressed after a release | inspect the release path, evidence set, and rollback options |

## 4. Related docs

- [Evaluation and test sets](../use/evaluation-and-test-sets.md)
- [Calibration](../use/calibration.md)
- [Review and release flows](../use/review-and-release-flows.md)
- [Prompts architecture](../02-prompts/architecture.md)
