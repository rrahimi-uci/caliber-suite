# Aria: Human-Review Queue from Intent

> **Prerequisite flags:** `CALIBER_FEATURE_COOKBOOKS=true` opens the Cookbooks gallery and `CALIBER_FEATURE_ARIA=true` for **Plans** and the `/aria/*` API (both off by default).

## Demo objective

Give Aria one sentence and have it plan and create a real human-**review
queue** — with a safety/citation/tone label schema — that reviewers can
immediately start scoring traces against.

## Feasibility & substitutions

Read [`../ARIA-AUTONOMY.md`](../ARIA-AUTONOMY.md) and [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ `review_queue.create` is a real Aria capability (TIER_MUTATE) with a rich
  question schema (`pass_fail | categorical | numeric | text`; target
  `feedback` or `expectation`).
- ✅ **Inputs are collected in the plan.** The planner proposes the steps but
  cannot know your question schema, so `review_queue.create` pauses on an
  **"Aria needs information"** form (**Continue plan** / **Skip step**); your answer
  is merged into the step and validated before it runs. See
  [`../ARIA-AUTONOMY.md`](../ARIA-AUTONOMY.md) §Execution status.
- ✅ The intent mentions **"review queue"**, so the default HeuristicPlanner
  proposes **both** `review_queue.create` **and** `review_queue.add_items`
  (same domain). The planner links `add_items`' `queue_id` to the create step, so
  that form only asks for `trace_ids` (plus optional `experiment_id` /
  `assigned_to`). If you have no flagged traces yet, click **Skip step** (or
  **Deny** its approval prompt) to skip it (queue-first, enqueue-later); the queue
  still gets created.
- Autonomy **Stop before each change** (`ask_each`) adds an **Approve** / **Deny**
  prompt before each mutate step; **Review the plan, then run** runs them straight
  after the input form.

## Prerequisites

- Aria Plans enabled (`CALIBER_FEATURE_ARIA=true`; sidebar **Integrations › Plans**) and the operator role. No model provider is needed to create the queue.

## Recipe (UI-first, with API fallbacks)

1. **State the intent.** With `CALIBER_FEATURE_ARIA=true` set, sidebar **Plans** → **Goal**. Paste the canonical intent
   from [`assets/intent.md`](assets/intent.md):
   *"Set up a review queue for human labeling of agent replies for safety,
   citation, and tone."* **Autonomy** = **Stop before each change**, then click
   **Decompose goal**.
   - API: `POST /aria/plans {goal, autonomy:"ask_each"}`.
2. **Review the plan.** Confirm two steps: `review_queue.create` and
   `review_queue.add_items` (see [`assets/expected-plan.json`](assets/expected-plan.json)).
   - API: `GET /aria/plans/{plan_id}`.
3. **Approve.** Click **Approve plan**.
   - API: `POST /aria/plans/{plan_id}/approve`.
4. **Execute the plan + create the queue.** Click **Execute**. On the
   `review_queue.create` form, enter the queue from
   [`assets/review-queues/agent-reply-review.queue.json`](assets/review-queues/agent-reply-review.queue.json)
   (`name`, optional `description`, and `questions` as a JSON array) and click
   **Continue plan**, then **Approve** the step. For `review_queue.add_items`,
   either enter real flagged `trace_ids` (see
   [`assets/enqueue.json`](assets/enqueue.json)) and **Approve**, or click
   **Skip step** if you have no traces yet.
   - API: `POST /aria/plans/{plan_id}/execute`; `GET …/interactions`;
     `POST /aria/interactions/{id}/answer {"inputs": {name, questions, …}}`, then
     `{"approved": true}` for the permission prompt (or `{"approved": false}` on the
     input interaction to skip `add_items`).
5. **Verify.** Open **Review Queues** → `agent-reply-review` is active with the
   safety/severity/citation_ok/tone_ok/reviewer_notes questions.
6. **(Follow-up)** Enqueue real traces (here or via Review Queues) and have
   reviewers answer; answers write back onto each trace (SCN-10).

## Demo evidence to capture

- The plan id + its two decomposed steps.
- The question schema you entered for `review_queue.create`.
- The created queue id, shown live in Review Queues; whether add_items was
  answered (enqueued) or skipped.

## Done when / gate

- The plan decomposed from the intent alone; `review_queue.create` completed.
- A real active queue `agent-reply-review` exists with the question schema.
- `review_queue.add_items` is completed (traces supplied) or skipped —
  both acceptable.
