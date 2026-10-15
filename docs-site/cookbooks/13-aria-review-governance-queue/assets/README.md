# SCN-13 assets — what you hand Aria

An **Aria autonomous** scenario: you give Aria the intent, and it plans + creates
the review queue. These files are (a) the intent and (b) the concrete values you
enter in the **"Aria needs information"** forms when Aria pauses.

| # | Artifact | File | Used at |
| --- | --- | --- | --- |
| 1 | The intent | [`intent.md`](intent.md) | `Plans → Goal` (or `POST /aria/plans`) |
| 2 | Expected plan | [`expected-plan.json`](expected-plan.json) | sanity-check the draft plan |
| 3 | Review-queue spec | [`review-queues/agent-reply-review.queue.json`](review-queues/agent-reply-review.queue.json) | the `review_queue.create` input form (**Continue plan**) |
| 4 | Enqueue spec (optional) | [`enqueue.json`](enqueue.json) | the `review_queue.add_items` input form — or **Skip step** |

Flow: `POST /aria/plans` (intent) → `…/approve` → `…/execute`. The planner leaves
the queue's name and questions empty, so `review_queue.create` pauses on an
`input` interaction; answer it with `…/interactions/{id}/answer {"inputs": {…}}`
(or the form's **Continue plan**). The executor merges and validates the inputs,
then — under **Stop before each change** — asks for **Approve** / **Deny**
(`{"approved": true}`). `review_queue.add_items` has its `queue_id` linked from
the create step, so its form asks for `trace_ids` only; click **Skip step** (or
answer `{"approved": false}`) to skip it. Then verify in Review Queues.

The queue spec's fields are the `review_queue.create` inputs (the same shape as a
`POST /review-queues` body); `questions` goes into the form as a JSON array. See
[`../../ARIA-AUTONOMY.md`](../../ARIA-AUTONOMY.md) §Execution status.
