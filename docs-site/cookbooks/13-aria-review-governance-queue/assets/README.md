# SCN-13 assets — what you hand Aria

An **Aria autonomous** scenario: you give Aria the intent, and it plans + creates
the review queue. These files are (a) the intent and (b) the concrete
**interaction answers** you paste when Aria pauses.

| # | Artifact | File | Used at |
| --- | --- | --- | --- |
| 1 | The intent | [`intent.md`](intent.md) | `Plans → new goal` (or `POST /aria/plans`) |
| 2 | Expected plan | [`expected-plan.json`](expected-plan.json) | sanity-check the draft plan |
| 3 | Review-queue spec | [`review-queues/agent-reply-review.queue.json`](review-queues/agent-reply-review.queue.json) | answer to the `review_queue.create` interaction |
| 4 | Enqueue spec (optional) | [`enqueue.json`](enqueue.json) | answer to the `review_queue.add_items` interaction — or **deny** to skip |

Flow: `POST /aria/plans` (intent) → `…/approve` → `…/execute`. The shipped
planner leaves step inputs empty and the interaction answer can't carry a payload
(`{approved, choice, value}` only), so **create the queue via its own route**:
`POST /review-queues` with the spec below (optionally
`POST /review-queues/{id}/items` with `enqueue.json`). The Aria answer just gates
each step: `…/interactions/{id}/answer {approved:true}` (or `{approved:false}` to
skip add_items). Then verify in Review Queues.

The queue spec IS a `POST /review-queues` body. Full hands-off autonomy needs an
LLM planner to fill step inputs (not wired); see
[`../../ARIA-AUTONOMY.md`](../../ARIA-AUTONOMY.md) §Execution status.
