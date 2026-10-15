# The intent you give Aria

## Canonical (works with the default HeuristicPlanner)

> Create a judge for answer faithfulness and an eval dataset to run it on.

Why this wording: the default planner proposes a step for each capability whose
**domain word** appears in the goal. "judge" → `judge.create`; "eval dataset" →
`eval_dataset.create`. Keep both literal needles in the sentence.

Autonomy: **Stop before each change** (`ask_each`: an **Approve** / **Deny** prompt before every mutate step, after the input form).

## Natural-language variant (only partly understood)

> Set up faithfulness scoring for our support answers, with a test set to run it on.

This reads better, but the default heuristic planner recognises "test set"
(→ `eval_dataset.create`) and not "faithfulness scoring" (a judge step needs the
literal word "judge"), so it plans only the test-set step. Use the canonical
wording.
