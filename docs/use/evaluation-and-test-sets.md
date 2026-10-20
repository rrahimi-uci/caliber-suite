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
reviewed_on: 2026-10-14
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
A test set is built by hand on the Test Sets page, or loaded with **Import from file**: a JSON Lines file or a
JSON array in which each row has an `input` object and may have `expected`, `tags` and `weight`.

Inside a project, creating or changing a test set, its examples or a judge needs `reviewer`, `editor` or
`owner`, and running an evaluation needs the same roles. A `viewer` can read but gets 403 on these, and a person
outside the project gets 404. When a result is produced by a simulated provider (the shipped default) it is
recorded with `provider` and `simulated`, the comparison shows "Simulated evaluation — not evidence", and a
banner at the top of the app says evaluation scores are canned, not measurements.

## 2. Common tasks

| You want to... | Read this next |
| --- | --- |
| curate representative examples | [Test sets architecture](../11-test-sets/architecture.md) |
| run a scorecard or judge-backed evaluation | [Evaluation architecture](../14-evaluation/architecture.md) |
| connect evaluation to calibration | [Calibration](../use/calibration.md) |
| connect evaluation to runtime review and release | [Review and release flows](../use/review-and-release-flows.md) |

## 3. Judges and how far to trust them

A custom judge is an LLM call, so a run that uses one needs a model. Evaluations check only the LLM
provider: on the simulated default (`CALIBER_LLM_PROVIDER=fake`) `POST /evaluations` answers 400
`evaluation requires a real LLM provider` and stores nothing, and the Judges alignment check returns every row
as an error. A judge's instructions may use only `{{ inputs }}`, `{{ outputs }}`, `{{ expectations }}`,
`{{ conversation }}` and `{{ trace }}`; any other variable is refused when you save it.

Before a judge blocks anything, check it against people: **Judges**, **Try it**, **Human alignment** (or
`POST /judges/{id}/alignment`, 1 to 100 labelled rows). It reports `agreement_rate`, `cohen_kappa` and the
confusion counts (the judge's pass is the positive class). Rows the judge errors on are left out of the numbers,
so read `scored` next to `n`. The product returns numbers for any sample size; five rows can clear a 0.8
agreement bar by luck, so set a minimum yourself. **Import labels** passes every review answer except the
pass/fail one to the judge as `{{ expectations }}`, so keep a reviewer's explanation out of that queue. Cookbook 10,
Trustworthy Evaluation, works the whole loop on a 50-row test set.

## 4. Common failure modes

| Symptom | First thing to check |
| --- | --- |
| Scores are high but production still looks weak | the evidence set may not represent the live workload |
| A judge looks inconsistent | inspect custom-judge setup and agreement expectations |
| A good score did not change production | evaluation is evidence, not apply/release |
| Evaluations answer 400 `evaluation requires a real LLM provider` | the provider is simulated; configure a real model |
| An evaluation answers 400 `has no examples to score` | the test set is empty |
| Alignment reports `scored` below `n` | the judge errored on some rows (a missing field, no model); fix those rows |

## 5. Related docs

- [Trust and governance](../use/trust-and-governance.md)
- [Calibration](../use/calibration.md)
- [Review and release flows](../use/review-and-release-flows.md)
- [QA plan](../13-qa-plan/architecture.md)
