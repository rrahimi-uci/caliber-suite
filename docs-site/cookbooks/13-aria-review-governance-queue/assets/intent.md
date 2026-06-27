# The intent you give Aria

## Canonical (works with the default HeuristicPlanner)

> Set up a review queue for human labeling of agent replies for safety, citation, and tone.

Why this wording: the default planner proposes a step for each capability whose
**domain word** appears in the goal. The phrase **"review queue"** maps to
**both** `review_queue.create` **and** `review_queue.add_items` (same domain).
Keep the literal phrase "review queue" in the sentence.

Autonomy: **ask_each** (pauses on every mutate step so you confirm each input).

## Natural-language variant (needs an LLM planner)

> Stand up human grading of our agent's answers for safety, citations, and tone.

Reads better, but relies on an LLM planner to map "human grading" →
`review_queue.create`. With the default heuristic planner, use the canonical
wording.

## Note on the second step

Because "review queue" also triggers `review_queue.add_items`, Aria will pause to
enqueue traces. If you have no flagged traces yet, **deny** that interaction —
the queue is still created and you enqueue later.
