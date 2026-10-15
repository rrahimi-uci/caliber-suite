# SCN-12 assets — what you hand Aria

This is an **Aria autonomous** scenario: you give Aria the intent, and it plans
+ creates the artifacts. These files are (a) the intent and (b) the concrete
values you enter in the **"Aria needs information"** form when Aria pauses for a
step's inputs.

| # | Artifact | File | Used at |
| --- | --- | --- | --- |
| 1 | The intent | [`intent.md`](intent.md) | `Plans → Goal` (or `POST /aria/plans`) |
| 2 | Expected plan | [`expected-plan.json`](expected-plan.json) | sanity-check the draft plan |
| 3 | Judge spec | [`judges/answer-faithfulness.judge.json`](judges/answer-faithfulness.judge.json) | the `judge.create` input form (**Continue plan**) |
| 4 | Dataset spec | [`datasets/support-faithfulness.dataset.json`](datasets/support-faithfulness.dataset.json) | reference only: the planner links the test set's name to the judge's, so the step asks for nothing |

Flow: `POST /aria/plans` (intent) → `…/approve` → `…/execute`. The planner leaves
the judge's fields empty, so `judge.create` pauses on an `input` interaction;
answer it with `…/interactions/{id}/answer {"inputs": {…}}` (or the form's
**Continue plan**). The executor merges and validates the inputs, then — under
**Stop before each change** — asks for **Approve** / **Deny** before running the
step (`{"approved": true}`). `eval_dataset.create` has its `name` linked from the
judge step, so it only asks for approval and creates an empty test set under the
judge's name. Then verify in Judges / Test Sets.

The judge spec's `name`, `instructions` (and optional `feedback_value_type`,
`description`, `model`, `tags`) are the `judge.create` inputs; the same shape is a
`POST /judges` body. See [`../../ARIA-AUTONOMY.md`](../../ARIA-AUTONOMY.md)
§Execution status.
