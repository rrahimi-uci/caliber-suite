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
reviewed_on: 2026-10-14
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

How calibration starts, and what it leaves you with, differs by asset family:

| Asset | Start it | What comes back | Making it live |
| --- | --- | --- | --- |
| Prompt | the Prompts page, or `POST /prompts/calibration/runs` | a refinement job that stops at `candidate_ready` | **Apply** moves the live alias; prompt rollback restores the earlier version |
| Skill | Skills, **Runs**, **Calibrate** (`POST /skills/{skill_id}/calibrate`) | a refinement job at `candidate_ready`; a skill has no test set, so the evidence is canned unless a real provider is configured | **Review & apply**, then **Apply to skill**; a rollback restores the earlier content as a new version |
| Workflow | `POST /workflows/{workflow_id}/calibration/runs` | a refinement job that replays the baseline and each candidate; the run carries **Simulated evaluation** and **Structural score** markers when they apply | **Apply** on the calibration run |
| Tool | the **Fixtures** stage, **Run calibration** (`POST /tools/{tool_id}/calibrate`) | a pass rate over the saved fixtures; there is no candidate | nothing to apply |
| Knowledge base | `POST /knowledge-bases/{knowledge_base_id}/calibrate` | a synchronous retrieve-and-judge run summary; there is no candidate job | nothing to apply |

Applying a job (`POST /jobs/{job_id}/apply`) works only on a job at `candidate_ready` and answers 409 for any
other status. The evidence on a job records `provider` and `simulated`, so a score from the fake provider is
labelled as canned wherever it is shown. See [Review and release flows](review-and-release-flows.md).

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
