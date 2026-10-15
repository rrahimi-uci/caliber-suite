# Aria: Governance Starter Kit from Intent

> **Prerequisite flags:** `CALIBER_FEATURE_COOKBOOKS=true` opens the Cookbooks gallery and `CALIBER_FEATURE_ARIA=true` for **Plans** and the `/aria/*` API (both off by default).

## Demo objective

Give Aria ONE sentence and watch it plan and stand up a whole governance
scaffold — a faithfulness **judge**, an **eval dataset** to score against, AND a
human **review queue** — in a single plan. You enter the judge and queue fields
when Aria asks and confirm each change. This is the flagship "one sentence →
whole governance setup" showcase.

## Feasibility & substitutions

Read [`../ARIA-AUTONOMY.md`](../ARIA-AUTONOMY.md) and [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ `judge.create`, `eval_dataset.create`, and `review_queue.create` are all
  real Aria capabilities (TIER_MUTATE) — this whole kit lives inside Aria's
  registry.
- ⚠️ The default **HeuristicPlanner** keys off **domain words** in the goal — the
  canonical intent therefore says "judge", "eval dataset", and "review queue"
  literally. ("test set" is also recognised for the dataset step; "spot-check"
  and "score for faithfulness" are not recognised — no LLM planner is wired.)
- ✅ **Inputs are collected in the plan.** `judge.create` and `review_queue.create`
  pause on **"Aria needs information"** forms (**Continue plan** / **Skip step**);
  your answers are merged into the steps and validated before they run. See
  [`../ARIA-AUTONOMY.md`](../ARIA-AUTONOMY.md) §Execution status.
- ⚠️ The planner links `name` between steps, so the review queue takes the
  **judge's name** and the test set takes the **queue's** — all three artifacts are
  created as `AnswerFaithfulness`, and the queue and test-set steps do not ask for
  a name. The queue form still asks for `questions` (and optional `description` /
  `reviewers`); the test-set step asks for nothing. For differently named
  artifacts, create them on **Review Queues** / **Test Sets** (specs in `assets/`).
- ⚠️ Because `review_queue.create` and `review_queue.add_items` share the
  **"review queue"** domain, the planner proposes **both**. There are no traces
  yet, so click **Skip step** on the `add_items` form (or supply `trace_ids` if
  you already have traces). The kit is complete without it.
- ⚠️ Aria creates the **empty** test set; example rows are added separately
  (not an Aria capability) — add them after, then run Evaluations.
- Autonomy **Stop before each change** (`ask_each`) adds an **Approve** / **Deny**
  prompt before each mutate step; **Review the plan, then run** runs them straight
  after the input forms.

## Prerequisites

- A configured model provider for the judge to run in **Evaluations** (creating
  the kit needs none). Aria Plans enabled (`CALIBER_FEATURE_ARIA=true`; sidebar
  **Integrations and preview › Plans**, a Preview page) and the operator role.

## Recipe (UI-first, with API fallbacks)

1. **State the intent.** With `CALIBER_FEATURE_ARIA=true` set, sidebar **Plans** → **Goal**. Paste the canonical
   intent from [`assets/intent.md`](assets/intent.md):
   *"Stand up our governance starter kit: a judge for answer faithfulness, an
   eval dataset to score against, and a review queue for human checks."*
   Set **Autonomy** = **Stop before each change**, then click **Decompose goal**.
   - API: `POST /aria/plans {goal, autonomy:"ask_each"}` → returns a `draft` plan.
2. **Review the plan.** Aria decomposes the intent into the create steps —
   `judge.create`, `review_queue.create`, `eval_dataset.create` (plus a
   `review_queue.add_items` step you'll skip). Confirm they appear (see
   [`assets/expected-plan.json`](assets/expected-plan.json)).
   - API: `GET /aria/plans/{plan_id}`.
3. **Approve.** Click **Approve plan**.
   - API: `POST /aria/plans/{plan_id}/approve`.
4. **Execute the plan + create the three artifacts.** Click **Execute**, then
   work through the prompts in order:
   - `judge.create` form → enter the fields from
     [`assets/judges/answer-faithfulness.judge.json`](assets/judges/answer-faithfulness.judge.json)
     → **Continue plan** → **Approve**.
   - `review_queue.create` form → enter `questions` (and optionally `description`)
     from
     [`assets/review-queues/governance-review.queue.json`](assets/review-queues/governance-review.queue.json)
     → **Continue plan** → **Approve**. (Its `name` is linked from the judge step.)
   - `eval_dataset.create` → no form; **Approve**. (Its `name` is linked; reference
     spec: [`assets/datasets/release-candidates.dataset.json`](assets/datasets/release-candidates.dataset.json).)
   - `review_queue.add_items` form → **Skip step** (no traces yet).
   - API: `POST /aria/plans/{plan_id}/execute`; `GET …/interactions`;
     `POST /aria/interactions/{id}/answer {"inputs": {…}}` for each form, then
     `{"approved": true}` for each permission prompt (`{"approved": false}` on the
     input interaction skips `add_items`).
5. **Verify.** Open **Judges** → `AnswerFaithfulness` is active; **Test Sets** →
   an empty `AnswerFaithfulness` test set exists; **Review Queues** →
   `AnswerFaithfulness` exists with its four-question schema (faithful /
   citation_ok / tone / notes); the plan shows the three create steps completed.
6. **(Follow-up)** Add example rows to the dataset and run it from
   **Evaluations** with `Judge.AnswerFaithfulness` (SCN-01 / SCN-10; needs a real
   model provider — the default evaluator and LLM are fake and **Evaluations**
   returns a 400 without one), then enqueue the hard/low-confidence traces into
   the queue (Review Queues → Add traces to review, or
   `POST /review-queues/{id}/items {trace_ids}`).

## Demo evidence to capture

- The plan id + its decomposed steps (three creates + the skipped add_items).
- The judge and queue inputs you entered and the three **Approve** answers.
- The created judge id + dataset id + queue id, shown live in Judges / Test Sets
  / Review Queues.

## Done when / gate

- The plan decomposed from the intent alone and the three create steps completed.
- A real active judge `AnswerFaithfulness`, a real test set and a real review
  queue (both named `AnswerFaithfulness`, taken from the judge step, with the
  queue's question schema) all exist — a whole governance kit from one sentence.
