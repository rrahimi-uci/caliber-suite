# The intent you give Aria

## Canonical (works with the default planner)

> Stand up our governance starter kit: a judge for answer faithfulness, an eval dataset to score against, and a review queue for human checks.

Why this wording: the default planner proposes a step for each capability whose
**domain word** appears in the goal. "judge" gives `judge.create`; "eval dataset"
(or "test set") gives `eval_dataset.create`; "review queue" gives `review_queue.create`
**and** `review_queue.add_items` (same domain). Keep all three phrases in the sentence.

The plan runs in registry order, whatever the order of the sentence: judge, review queue,
test set, then the enqueue step. There are no traces yet, so **Skip step** on the enqueue form.

Autonomy: **Stop before each change** (`ask_each`: an **Approve** / **Deny** prompt before
every step that changes something, after the input form). Under **Review the plan, then
run** or **Run everything it safely can** the prompts are not asked and only the input forms
remain: the lab runs the same sentence both ways.

## What the planner does with other sentences

`dataset/goals.jsonl` holds 28 goals and the plan the server draws for each one; the script
checks all 28 against the real server. In short:

- "governance", "starter kit", "score for faithfulness", "held-out set", "spot-check" and
  "human review" are not domain words. A sentence that uses only them plans nothing, or only
  the part it does recognise (`Score the release candidate for faithfulness, keep a held-out
  test set, and let a human spot-check the tricky ones.` plans the test set alone).
- `review-queue` (hyphen) plans nothing; `test-set` (hyphen) does plan a test set.
- It does not read negation, questions or "delete", it matches `judge` inside `judgement`,
  and `workflow` plans `workflow.calibrate` (a calibration of an existing workflow, lab 15),
  never a new workflow: Aria cannot build one.
- A sentence with no domain word makes a plan with no steps; it cannot be approved
  (HTTP 409).
