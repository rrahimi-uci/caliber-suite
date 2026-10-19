# The intent you give Aria

## Canonical (works with the default planner)

> Create a judge for answer faithfulness and an eval dataset to run it on.

Why this wording: the default planner proposes a step for each capability whose
**domain word** appears in the goal. "judge" gives `judge.create`; "eval dataset"
(or "test set") gives `eval_dataset.create`. Keep both words in the sentence.

Autonomy: **Stop before each change** (`ask_each`: an **Approve** / **Deny** prompt
before every step that changes something, after the input form).

## What the planner does with other sentences

`dataset/goals.jsonl` holds 28 goals and the plan the server draws for each one;
the script checks all 28 against the real server. In short:

- Wording with `judge` and `eval dataset` / `test set` / `test-set` / `testset` /
  `eval_dataset` (any case) gives the right steps, in any phrasing.
- "faithfulness scoring" gives no judge step, and "evaluation dataset" gives
  nothing at all: the planner looks for the exact domain words, there is no
  language model behind it.
- It does not read negation (`Do not create a judge...` still plans a judge), it
  matches `judge` inside `judgement`, and a question that contains the word
  (`What is a judge in CALIBER?`) also plans a step. That is why you read the plan
  before you approve it: nothing runs until you do.
- A sentence with no domain word makes a plan with no steps; it cannot be approved
  (HTTP 409).
