# The intent you give Aria

## Canonical (works with the default HeuristicPlanner)

> Set up a review queue for human labeling of agent replies for safety, citation, and tone.

Why this wording: the default planner proposes a step for each capability whose
**domain word** appears in the goal. The phrase **"review queue"** maps to
**both** `review_queue.create` **and** `review_queue.add_items` (same domain).
Keep the literal phrase "review queue" in the sentence.

Autonomy: **Stop before each change** (`ask_each`: an **Approve** / **Deny** prompt before every mutate step, after the input form).

## Natural-language variant (not understood by the default planner)

> Stand up human grading of our agent's answers for safety, citations, and tone.

Reads better, but the default heuristic planner has no mapping from "human
grading" to `review_queue.create` and would plan no steps. Use the canonical
wording.

## Note on the second step

Because "review queue" also triggers `review_queue.add_items`, Aria will pause to
enqueue traces (its form only asks for `trace_ids`; `queue_id` is linked from the
create step). If you have no flagged traces yet, click **Skip step** — the queue
is still created and you enqueue later.
