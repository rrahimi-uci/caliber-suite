# SCN-14 assets — what you hand Aria

This is an **Aria autonomous** scenario: you give Aria the intent, and it plans
+ creates the artifacts. These files are (a) the intent and (b) the concrete
**interaction answers** you paste when Aria pauses to confirm each step's inputs.

| # | Artifact | File | Used at |
| --- | --- | --- | --- |
| 1 | The intent | [`intent.md`](intent.md) | `Plans → new goal` (or `POST /aria/plans`) |
| 2 | Expected plan | [`expected-plan.json`](expected-plan.json) | sanity-check the draft plan |
| 3 | Judge spec | [`judges/answer-faithfulness.judge.json`](judges/answer-faithfulness.judge.json) | answer to the `judge.create` interaction |
| 4 | Dataset spec | [`datasets/release-candidates.dataset.json`](datasets/release-candidates.dataset.json) | answer to the `eval_dataset.create` interaction |
| 5 | Review-queue spec | [`review-queues/governance-review.queue.json`](review-queues/governance-review.queue.json) | answer to the `review_queue.create` interaction |

Flow: `POST /aria/plans` (intent) → `…/approve` → `…/execute`. The shipped
planner leaves step inputs empty and the interaction answer can't carry a payload
(`{approved, choice, value}` only), so **create each artifact via its own route**
with the specs below: `POST /judges`, `POST /eval-datasets`, `POST /review-queues`
(deny the `review_queue.add_items` step — no traces yet). The Aria answer just
gates each step: `…/interactions/{id}/answer {approved:true}` (or
`{approved:false}` to skip). Then verify in Judges / Test Sets / Review Queues.
See [`../../ARIA-AUTONOMY.md`](../../ARIA-AUTONOMY.md) §Execution status.

The judge / dataset / review-queue specs use the same shapes as the rest of the
pack — an Aria `judge.create` / `eval_dataset.create` / `review_queue.create`
interaction answer IS the corresponding create payload.
