# Aria: Evaluation Harness from Intent

## Demo objective

Give Aria ONE sentence and watch it autonomously plan and create a real
faithfulness **judge** plus an **eval dataset** to run it on — you only confirm
the parameters it asks for.

## Feasibility & substitutions

Read [`../ARIA-AUTONOMY.md`](../ARIA-AUTONOMY.md) and [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ `judge.create` and `eval_dataset.create` are real Aria capabilities (TIER_MUTATE).
- ⚠️ The default **HeuristicPlanner** keys off **domain words** in the goal — the
  canonical intent therefore says "judge" and "eval dataset" literally. (A
  natural phrasing like "test set" needs an LLM planner.)
- ⚠️ Aria creates the **empty** test set; example rows are added separately
  (not an Aria capability) — add them after, then run Evaluations.
- ⚠️ **Execution gap (verified):** the shipped planner proposes the steps but
  leaves their **inputs empty**, and the interaction answer (`{approved, choice,
  value}`) can't inject them — so today you **create each artifact via its own
  route** with the `assets/` spec, while Aria's plan is the orchestration record.
  True hands-off execution needs an LLM planner (not wired). See
  [`../ARIA-AUTONOMY.md`](../ARIA-AUTONOMY.md) §Execution status. `autonomy:
  ask_each` pauses on every mutate step; `approve_plan` auto-runs them.

## Prerequisites

- A configured model provider; Aria Plans available (sidebar **Plans**).

## Recipe (UI-first, with API fallbacks)

1. **State the intent.** Sidebar **Plans** → new goal. Paste the canonical
   intent from [`assets/intent.md`](assets/intent.md):
   *"Create a judge for answer faithfulness and an eval dataset to run it on."*
   Set autonomy = **ask_each**.
   - API: `POST /aria/plans {goal, autonomy:"ask_each"}` → returns a `draft` plan.
2. **Review the plan.** Aria decomposes the intent into two steps —
   `judge.create` and `eval_dataset.create`. Confirm both appear (see
   [`assets/expected-plan.json`](assets/expected-plan.json)).
   - API: `GET /aria/plans/{plan_id}`.
3. **Approve.** Approve the plan shape.
   - API: `POST /aria/plans/{plan_id}/approve`.
4. **Execute the plan + create the artifacts.** Approve→execute drives the plan
   (approve each mutate step, or deny to skip). Because the answer can't carry
   the payload, create the two artifacts via their own routes using the specs:
   - Judge → `POST /judges` with
     [`assets/judges/answer-faithfulness.judge.json`](assets/judges/answer-faithfulness.judge.json).
   - Test set → `POST /eval-datasets` with
     [`assets/datasets/support-faithfulness.dataset.json`](assets/datasets/support-faithfulness.dataset.json).
   - API: `POST /aria/plans/{plan_id}/execute`; `GET …/interactions`;
     `POST /aria/interactions/{id}/answer {approved:true}` (gates the step — no
     `inputs` field). Full autonomy (planner fills inputs) needs the LLM planner.
5. **Verify.** Open **Judges** → `AnswerFaithfulness` is active; **Test Sets** →
   `support-faithfulness-eval` exists; the plan shows both steps completed.
6. **(Follow-up)** Add example rows to the dataset and run it from
   **Evaluations** on the deterministic graders (Contains expected / Token F1 /
   Non-empty), and tick the `AnswerFaithfulness` judge under **Custom LLM
   judges** so it runs as a `Judge.<id>` scorer for an automatic per-row verdict
   — the same scored-run loop as SCN-01 / SCN-10.

## Demo evidence to capture

- The plan id + its two decomposed steps.
- The two interaction answers (judge spec, dataset spec).
- The created judge id + dataset id, shown live in Judges / Test Sets.

## Done when / gate

- The plan decomposed from the intent alone and both steps completed.
- A real active judge `AnswerFaithfulness` and a real test set
  `support-faithfulness-eval` exist.
