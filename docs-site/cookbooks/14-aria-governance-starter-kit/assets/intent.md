# The intent you give Aria

## Canonical (works with the default HeuristicPlanner)

> Stand up our governance starter kit: a judge for answer faithfulness, an eval dataset to score against, and a review queue for human checks.

Why this wording: the default planner proposes a step for each capability whose
**domain word** appears in the goal. "judge" → `judge.create`; "eval dataset" →
`eval_dataset.create`; "review queue" → `review_queue.create`. Keep all three
literal needles in the sentence.

Note: "review queue" matches BOTH `review_queue.create` and
`review_queue.add_items` (they share the domain), so the planner proposes both.
There are no traces yet, so **deny** the `add_items` interaction to skip it.

Autonomy: **ask_each** (pauses on every mutate step so you confirm each input).

## Natural-language variant (needs an LLM planner)

> Get our new support answers governed end to end: score them for faithfulness, keep a held-out test set, and let a human spot-check the tricky ones.

This reads better but relies on an LLM planner to map "score for faithfulness" →
judge, "test set" → eval_dataset, and "human spot-check" → review_queue. With
the default heuristic planner, use the canonical wording.
