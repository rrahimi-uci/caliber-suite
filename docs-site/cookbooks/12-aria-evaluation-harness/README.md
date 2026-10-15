# Aria: Evaluation Harness from Intent

> **Prerequisite flags:** `CALIBER_FEATURE_COOKBOOKS=true` opens the Cookbooks gallery and `CALIBER_FEATURE_ARIA=true` for **Plans** and the `/aria/*` API (both off by default).

## Demo objective

Give Aria ONE sentence and watch it plan and create a real faithfulness
**judge** plus an **eval dataset** to run it on — you enter the judge's fields
when Aria asks and confirm each change.

## Feasibility & substitutions

Read [`../ARIA-AUTONOMY.md`](../ARIA-AUTONOMY.md) and [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ `judge.create` and `eval_dataset.create` are real Aria capabilities (TIER_MUTATE).
- ⚠️ The default **HeuristicPlanner** keys off **domain words** in the goal — the
  canonical intent says "judge" and "eval dataset" literally. "test set" is also
  recognised for the dataset step, but a phrase like "faithfulness scoring" does
  not produce a judge step (no LLM planner is wired).
- ✅ **Inputs are collected in the plan.** The planner proposes the steps but
  cannot know a judge's instructions, so `judge.create` pauses on an **"Aria needs
  information"** form (**Continue plan** / **Skip step**); your answer is merged
  into the step and validated before it runs. See
  [`../ARIA-AUTONOMY.md`](../ARIA-AUTONOMY.md) §Execution status.
- ⚠️ The planner links the test set's `name` to the judge step's `name`, so Aria
  creates the test set under the **judge's name** (`AnswerFaithfulness`) and asks
  for nothing else — you only **Approve** that step. For a differently named set,
  create one on **Test Sets** afterwards (the spec in `assets/datasets/`).
- ⚠️ Aria creates the **empty** test set; example rows are added separately
  (not an Aria capability) — add them after, then run Evaluations.
- Autonomy **Stop before each change** (`ask_each`) adds an **Approve** / **Deny**
  prompt before each mutate step; **Review the plan, then run** (`approve_plan`)
  runs them straight after the input form.

## Prerequisites

- A configured model provider for the judge to run in **Evaluations** (creating
  the judge and test set needs none). Aria Plans enabled (`CALIBER_FEATURE_ARIA=true`;
  sidebar **Integrations › Plans**, a Preview page); you need the
  operator role to create judges and test sets.

## Recipe (UI-first, with API fallbacks)

1. **State the intent.** With `CALIBER_FEATURE_ARIA=true` set, sidebar **Plans** → **Goal**. Paste the canonical
   intent from [`assets/intent.md`](assets/intent.md):
   *"Create a judge for answer faithfulness and an eval dataset to run it on."*
   Set **Autonomy** = **Stop before each change**, then click **Decompose goal**.
   - API: `POST /aria/plans {goal, autonomy:"ask_each"}` → returns a `draft` plan.
2. **Review the plan.** Aria decomposes the intent into two steps —
   `judge.create` and `eval_dataset.create`. Confirm both appear (see
   [`assets/expected-plan.json`](assets/expected-plan.json)).
   - API: `GET /aria/plans/{plan_id}`.
3. **Approve.** Click **Approve plan**.
   - API: `POST /aria/plans/{plan_id}/approve`.
4. **Execute the plan + create the artifacts.** Click **Execute**. Aria pauses on
   the `judge.create` step with **Aria needs information**: enter the judge's
   fields from
   [`assets/judges/answer-faithfulness.judge.json`](assets/judges/answer-faithfulness.judge.json)
   and click **Continue plan**, then **Approve** the step. The `eval_dataset.create`
   step needs no input (its name comes from the judge step) — **Approve** it and
   the plan completes. (**Deny**/**Skip step** marks a step skipped.)
   - API: `POST /aria/plans/{plan_id}/execute`; `GET …/interactions`;
     `POST /aria/interactions/{id}/answer {"inputs": {name, instructions,
     feedback_value_type}}`, then `{"approved": true}` for each permission prompt.
5. **Verify.** Open **Judges** → `AnswerFaithfulness` is active; **Test Sets** →
   an empty test set `AnswerFaithfulness` exists; the plan shows both steps
   completed.
6. **(Follow-up)** Add example rows to the dataset and run it from
   **Evaluations** on the deterministic graders (Contains expected / Token F1 /
   Non-empty), and tick the `AnswerFaithfulness` judge under **Custom LLM
   judges** so it runs as a `Judge.<id>` scorer for an automatic per-row verdict
   — the same scored-run loop as SCN-01 / SCN-10. This needs a real model
   provider: the default evaluator and LLM are fake and **Evaluations** returns a
   400 without one.

## Demo evidence to capture

- The plan id + its two decomposed steps.
- The input you entered for the judge and the two **Approve** answers.
- The created judge id + dataset id, shown live in Judges / Test Sets.

## Done when / gate

- The plan decomposed from the intent alone and both steps completed.
- A real active judge `AnswerFaithfulness` and a real test set (named
  `AnswerFaithfulness`, taken from the judge step) exist.
