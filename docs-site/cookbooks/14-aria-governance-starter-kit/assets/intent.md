# The intent you give Aria

## Canonical (works with the default HeuristicPlanner)

> Stand up our governance starter kit: a judge for answer faithfulness, an eval dataset to score against, and a review queue for human checks.

Why this wording: the default planner proposes a step for each capability whose
**domain word** appears in the goal. "judge" → `judge.create`; "eval dataset" →
`eval_dataset.create`; "review queue" → `review_queue.create`. Keep all three
literal needles in the sentence.

Note: "review queue" matches BOTH `review_queue.create` and
`review_queue.add_items` (they share the domain), so the planner proposes both.
There are no traces yet, so click **Skip step** on the `add_items` form.

Autonomy: **Stop before each change** (`ask_each`: an **Approve** / **Deny** prompt before every mutate step, after the input forms).

## Natural-language variant (not understood by the default planner)

> Get our new support answers governed end to end: score them for faithfulness, keep a held-out test set, and let a human spot-check the tricky ones.

This reads better, but the default heuristic planner only recognises "test set"
(→ eval_dataset); "score for faithfulness" has no judge needle and "human
spot-check" no review-queue needle, so it plans only the test-set step. Use the
canonical wording.
