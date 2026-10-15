---
audience:
  - system-user
  - developer
doc_type: how-to
product_area: skills
stability: ga
prerequisites:
  - A CALIBER deployment with skill access
reviewed_on: 2026-10-15
version_applicability: current main branch docs contract
tags:
  - skills
  - packaging
  - prompts
  - assistant
---

# Skills

Use this page for the practical path around skills: package them, test their
selection behavior, and understand how they shape assistant or workflow output.

## At a glance

| Task | Start here | Deep reference |
| --- | --- | --- |
| package a reusable instruction asset | define the skill surface and tests | [Skills architecture](../04-skills/architecture.md) |
| verify the right skill is selected | run trigger/selection coverage | [Skills architecture](../04-skills/architecture.md) |
| use a skill with Aria | inspect the assistant flow (Aria is off by default; enable with `CALIBER_FEATURE_ARIA`) | [Aria assistant](../use/aria-assistant.md) |
| use a skill in examples | follow a cookbook or SDK recipe (the cookbooks are off by default; enable with `CALIBER_FEATURE_COOKBOOKS`) | [SDK recipes](../sdk/cookbooks.md) |

## The skill journey, step by step

This is the loop the alpha supports end to end for a skill, with each step as a page action and as an
SDK call. `caliber/caliber-ui/e2e/skill-journey.spec.ts` drives the page steps (it creates the skill with
`POST /skills`, the endpoint the wizard saves to, rather than filling the wizard) and
`caliber/tests/test_sdk_skill_journey.py` drives the SDK steps, both on the shipped defaults.

| # | Step | In the page (Skills) | In the SDK | What you should see |
| --- | --- | --- | --- | --- |
| 1 | Author | **Build Skill**, then fill the wizard; or open a skill and edit it on the **Author** stage | `skills.create(name, content=..., owner=...)` | version 1 in the version history |
| 2 | Preview | **Render Preview**: enter variables as JSON, **Render Skill** | `skills.render(skill_id, variables={...})` | the content with variables filled in, and any left unresolved |
| 3 | Check selection | **Trigger Tests**: **Check selection** for one message, or build cases on **Scenario Sets** and run them all from **Trigger Tests** | `skills.test_selection(skill_id, query)` | whether the skill would be chosen, with a score and a reason |
| 4 | Calibrate | **Runs**: **Calibrate** | `skills.calibrate(skill_id)` | "Calibration queued", then a run under **Calibration runs** |
| 5 | Review | **Runs**: **Review & apply** on a run at `candidate_ready` | `jobs.wait(job_id)`, then read `job.candidate` and `job.eval_results` | the diff, the baseline, candidate and change scores, the gate result, and a banner when the evaluation is simulated |
| 6 | Apply | **Apply to skill** (disabled when the evaluation gate failed) | `jobs.apply(job_id)` | the skill is now the candidate, as the next version |
| 7 | Roll back | **Author**: **Roll back** on the live version in the version history | `skills.rollback(skill_id)` | the earlier content is current again, as a new version |

Three things to know before you trust the result.

- **Skill evidence is not measured by default.** A skill calibration has no test set. With the default fake
  providers the scores are canned and the run says so: the review shows "Simulated evaluation — not
  evidence" and `0 evaluation examples`, and `job.eval_results["simulated"]` is `true`. Apply anyway only
  if you have judged the diff yourself.
- **Rolling back never rewrites history.** A skill is one row with a version counter, so rolling back
  restores the earlier content as a **new** version. After apply and rollback you have versions 1, 2 and 3,
  and version 3 has the same text as version 1.
- **Roles decide who can do what.** Anyone in the project can read, render and preview. Recording runs and
  baselines needs `reviewer` or above, calibrating needs `reviewer` or above, and applying a candidate needs
  `editor` or `owner`. Creating, importing and binding a skill also need `editor` or `owner` (plus the
  global `caliber.operator` scope). Changing a skill's content directly, and rolling it back, also need the global
  `caliber.admin` scope. A member whose role is too weak gets a permission error (403), and a person outside the
  project sees "not found" (404) for every one of these. See
  [Auth and project scoping](../build/auth-and-project-scoping.md).

## 1. What skills are for in CALIBER

Skills are reusable instruction assets. They let you keep repeatable behavioral
guidance separate from the rest of the workflow or assistant logic.

## 2. Common tasks

| You want to... | Read this next |
| --- | --- |
| author or update a skill | [Skills architecture](../04-skills/architecture.md) |
| verify trigger behavior | [Skills architecture](../04-skills/architecture.md) |
| understand how the assistant chooses or applies a skill (Aria is off by default) | [Aria assistant](../use/aria-assistant.md) |
| package skill logic for developer workflows | [SDK recipes](../sdk/cookbooks.md) |

## 3. Common failure modes

| Symptom | First thing to check |
| --- | --- |
| The skill exists but is not used | trigger cases or selection conditions are too weak |
| A skill package is installed but not active | installation and enablement are separate steps |
| Output changed after calibration | inspect the evidence loop and packaged instructions together |

## 4. Related docs

- [Aria assistant](../use/aria-assistant.md)
- [Trust and governance](../use/trust-and-governance.md)
- [Skills architecture](../04-skills/architecture.md)
- [SDK recipes](../sdk/cookbooks.md)
