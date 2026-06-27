# Aria: Governance Starter Kit from Intent

## Demo objective

Give Aria ONE sentence and watch it autonomously plan and stand up a whole
governance scaffold — a faithfulness **judge**, an **eval dataset** to score
against, AND a human **review queue** — in a single plan. You only confirm the
parameters it asks for. This is the flagship "one sentence → whole governance
setup" showcase.

## Feasibility & substitutions

Read [`../ARIA-AUTONOMY.md`](../ARIA-AUTONOMY.md) and [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ `judge.create`, `eval_dataset.create`, and `review_queue.create` are all
  real Aria capabilities (TIER_MUTATE) — this whole kit lives inside Aria's
  registry.
- ⚠️ The default **HeuristicPlanner** keys off **domain words** in the goal — the
  canonical intent therefore says "judge", "eval dataset", and "review queue"
  literally. (Natural phrasing like "test set" / "spot-check" needs an LLM planner.)
- ⚠️ Because `review_queue.create` and `review_queue.add_items` share the
  **"review queue"** domain, the planner proposes **both**. There are no traces
  yet, so **deny** the `add_items` interaction to skip it (or supply `trace_ids`
  if you already have traces). The kit is complete without it.
- ⚠️ Aria creates the **empty** test set; example rows are added separately
  (not an Aria capability) — add them after, then run Evaluations.
- ⚠️ **Execution gap (verified):** the shipped planner proposes the steps but
  leaves their **inputs empty**, and the interaction answer (`{approved, choice,
  value}`) can't inject them — so today you **create each artifact via its own
  route** with the `assets/` specs, while Aria's plan is the orchestration
  record. True hands-off execution needs an LLM planner (not wired). See
  [`../ARIA-AUTONOMY.md`](../ARIA-AUTONOMY.md) §Execution status. `autonomy:
  ask_each` pauses on every mutate step; `approve_plan` auto-runs them.

## Prerequisites

- A configured model provider; Aria Plans available (sidebar **Plans**).

## Recipe (UI-first, with API fallbacks)

1. **State the intent.** Sidebar **Plans** → new goal. Paste the canonical
   intent from [`assets/intent.md`](assets/intent.md):
   *"Stand up our governance starter kit: a judge for answer faithfulness, an
   eval dataset to score against, and a review queue for human checks."*
   Set autonomy = **ask_each**.
   - API: `POST /aria/plans {goal, autonomy:"ask_each"}` → returns a `draft` plan.
2. **Review the plan.** Aria decomposes the intent into the create steps —
   `judge.create`, `eval_dataset.create`, `review_queue.create` (plus a
   `review_queue.add_items` step you'll deny). Confirm they appear (see
   [`assets/expected-plan.json`](assets/expected-plan.json)).
   - API: `GET /aria/plans/{plan_id}`.
3. **Approve.** Approve the plan shape.
   - API: `POST /aria/plans/{plan_id}/approve`.
4. **Execute the plan + create the three artifacts.** Approve→execute drives the
   plan (approve each create step; **deny** `review_queue.add_items` — no traces
   yet). Because the answer can't carry the payload, create each artifact via its
   own route using the specs:
   - Judge → `POST /judges` with
     [`assets/judges/answer-faithfulness.judge.json`](assets/judges/answer-faithfulness.judge.json).
   - Test set → `POST /eval-datasets` with
     [`assets/datasets/release-candidates.dataset.json`](assets/datasets/release-candidates.dataset.json).
   - Review queue → `POST /review-queues` with
     [`assets/review-queues/governance-review.queue.json`](assets/review-queues/governance-review.queue.json).
   - API: `POST /aria/plans/{plan_id}/execute`; `GET …/interactions`;
     `POST /aria/interactions/{id}/answer {approved:true}` (creates) /
     `{approved:false}` (add_items). No `inputs` field — see the execution-gap note.
5. **Verify.** Open **Judges** → `AnswerFaithfulness` is active; **Test Sets** →
   `release-candidates-eval` exists; **Review Queues** → `governance-review`
   exists with its four-question schema (faithful / citation_ok / tone / notes);
   the plan shows the three create steps completed.
6. **(Follow-up)** Add example rows to the dataset and run it from
   **Evaluations** with `Judge.AnswerFaithfulness` (SCN-01 / SCN-10), then
   enqueue the hard/low-confidence traces into `governance-review`
   (Review Queues → Add items, or `POST /review-queues/{id}/items {trace_ids}`).

## Demo evidence to capture

- The plan id + its decomposed steps (three creates + the denied add_items).
- The three accepted interaction answers (judge spec, dataset spec, review-queue
  question schema) and the one denied (add_items).
- The created judge id + dataset id + queue id, shown live in Judges / Test Sets
  / Review Queues.

## Done when / gate

- The plan decomposed from the intent alone and the three create steps completed.
- A real active judge `AnswerFaithfulness`, a real test set
  `release-candidates-eval`, and a real review queue `governance-review` (with
  its question schema) all exist — a whole governance kit from one sentence.
