# The intent you give Aria

## Canonical (works with the default HeuristicPlanner)

> Create a judge for answer faithfulness and an eval dataset to run it on.

Why this wording: the default planner proposes a step for each capability whose
**domain word** appears in the goal. "judge" → `judge.create`; "eval dataset" →
`eval_dataset.create`. Keep both literal needles in the sentence.

Autonomy: **ask_each** (pauses on every mutate step so you confirm each input).

## Natural-language variant (needs an LLM planner)

> Set up faithfulness scoring for our support answers, with a test set to run it on.

This reads better but relies on an LLM planner to map "test set" → eval_dataset
and "faithfulness scoring" → judge. With the default heuristic planner, use the
canonical wording.
