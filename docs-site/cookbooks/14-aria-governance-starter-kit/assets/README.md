# SCN-14 assets — what you hand Aria

This is an **Aria autonomous** scenario: you give Aria the intent, and it plans
+ creates the artifacts. These files are (a) the intent and (b) the concrete
values you enter in the **"Aria needs information"** forms when Aria pauses.

| # | Artifact | File | Used at |
| --- | --- | --- | --- |
| 1 | The intent | [`intent.md`](intent.md) | `Plans → Goal` (or `POST /aria/plans`) |
| 2 | Expected plan | [`expected-plan.json`](expected-plan.json) | sanity-check the draft plan |
| 3 | Judge spec | [`judges/answer-faithfulness.judge.json`](judges/answer-faithfulness.judge.json) | the `judge.create` input form (**Continue plan**) |
| 4 | Dataset spec | [`datasets/release-candidates.dataset.json`](datasets/release-candidates.dataset.json) | reference only: the planner links the test set's name, so the step asks for nothing |
| 5 | Review-queue spec | [`review-queues/governance-review.queue.json`](review-queues/governance-review.queue.json) | the `review_queue.create` input form — `questions` (and `description`); `name` is linked |

Flow: `POST /aria/plans` (intent) → `…/approve` → `…/execute`. The planner leaves
the judge's and the queue's fields empty, so `judge.create` and
`review_queue.create` pause on `input` interactions; answer each with
`…/interactions/{id}/answer {"inputs": {…}}` (or the form's **Continue plan**).
The executor merges and validates the inputs, then — under **Stop before each
change** — asks for **Approve** / **Deny** (`{"approved": true}`).
`eval_dataset.create` has its `name` linked from the queue step (which took the
judge's), so it only asks for approval; click **Skip step** on the
`review_queue.add_items` form (no traces yet). Then verify in Judges / Test Sets
/ Review Queues.

The judge / queue specs hold the `judge.create` / `review_queue.create` inputs
(the same shapes as `POST /judges` / `POST /review-queues` bodies). Because the
planner links `name` between steps, all three artifacts are created under the
judge's name; use the specs' own names when you create the artifacts yourself.
See [`../../ARIA-AUTONOMY.md`](../../ARIA-AUTONOMY.md) §Execution status.
