# Aria: Human-Review Queue from Intent

## Demo objective

Give Aria one sentence and have it autonomously plan and create a real
human-**review queue** — with a safety/citation/tone label schema — that
reviewers can immediately start scoring traces against.

## Feasibility & substitutions

Read [`../ARIA-AUTONOMY.md`](../ARIA-AUTONOMY.md) and [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ `review_queue.create` is a real Aria capability (TIER_MUTATE) with a rich
  question schema (`pass_fail | categorical | numeric | text`; target
  `feedback` or `expectation`).
- ⚠️ The intent mentions **"review queue"**, so the default HeuristicPlanner
  proposes **both** `review_queue.create` **and** `review_queue.add_items`
  (same domain). If you have no flagged traces yet, **deny** the `add_items`
  interaction to skip it (queue-first, enqueue-later); the queue still gets created.
- ⚠️ **Execution gap (verified):** the shipped planner proposes the steps but
  leaves their **inputs empty**, and the interaction answer (`{approved, choice,
  value}`) can't inject them — so today you **create the queue via its own
  route** with the `assets/` spec, while Aria's plan is the orchestration record.
  True hands-off execution needs an LLM planner (not wired). See
  [`../ARIA-AUTONOMY.md`](../ARIA-AUTONOMY.md) §Execution status. `autonomy:
  ask_each` pauses on each mutate step.

## Prerequisites

- Aria Plans available (sidebar **Plans**). (No model provider strictly required
  to create the queue, but the planner/engine uses one if configured.)

## Recipe (UI-first, with API fallbacks)

1. **State the intent.** Sidebar **Plans** → new goal. Paste the canonical intent
   from [`assets/intent.md`](assets/intent.md):
   *"Set up a review queue for human labeling of agent replies for safety,
   citation, and tone."* Autonomy = **ask_each**.
   - API: `POST /aria/plans {goal, autonomy:"ask_each"}`.
2. **Review the plan.** Confirm two steps: `review_queue.create` and
   `review_queue.add_items` (see [`assets/expected-plan.json`](assets/expected-plan.json)).
   - API: `GET /aria/plans/{plan_id}`.
3. **Approve.** `POST /aria/plans/{plan_id}/approve`.
4. **Execute the plan + create the queue.** Approve→execute drives the plan;
   approve the `review_queue.create` step and **deny** `review_queue.add_items`
   (or approve it if you have traces). Because the answer can't carry the
   payload, create the queue via its own route using the spec:
   - Queue → `POST /review-queues` with
     [`assets/review-queues/agent-reply-review.queue.json`](assets/review-queues/agent-reply-review.queue.json).
   - Enqueue (optional) → `POST /review-queues/{id}/items` with
     [`assets/enqueue.json`](assets/enqueue.json).
   - API: `POST /aria/plans/{plan_id}/execute`; `GET …/interactions`;
     `POST /aria/interactions/{id}/answer {approved:true}` (or `{approved:false}`
     to skip add_items). No `inputs` field — see the execution-gap note.
5. **Verify.** Open **Review Queues** → `agent-reply-review` is active with the
   safety/severity/citation_ok/tone_ok/reviewer_notes questions.
6. **(Follow-up)** Enqueue real traces (here or via Review Queues) and have
   reviewers answer; answers write back onto each trace (SCN-10).

## Demo evidence to capture

- The plan id + its two decomposed steps.
- The review_queue.create interaction answer (the question schema).
- The created queue id, shown live in Review Queues; whether add_items was
  answered (enqueued) or denied (skipped).

## Done when / gate

- The plan decomposed from the intent alone; `review_queue.create` completed.
- A real active queue `agent-reply-review` exists with the question schema.
- `review_queue.add_items` is completed (traces supplied) or skipped (denied) —
  both acceptable.
