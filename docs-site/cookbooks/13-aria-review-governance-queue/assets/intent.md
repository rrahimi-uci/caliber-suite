# The intent you give Aria

## Canonical (works with the default planner)

> Set up a review queue for human labeling of agent replies for safety, citation, and tone.

Why this wording: the default planner proposes a step for each capability whose
**domain word** appears in the goal. "review queue" gives `review_queue.create` **and**
`review_queue.add_items` (same domain). Keep the words "review queue" (with a space) in
the sentence.

Autonomy: **Stop before each change** (`ask_each`: an **Approve** / **Deny** prompt
before every step that changes something, after the input form).

## What the planner does with other sentences

`dataset/goals.jsonl` holds 26 goals and the plan the server draws for each one; the
script checks all 26 against the real server. In short:

- Any sentence with `review queue` plans both the queue and its enqueue step, however it is
  phrased. If you have no traces yet, **Skip step** (or **Deny**) the enqueue; the queue is
  still created. Skipping the queue's own form skips the enqueue with it.
- `review-queue` (hyphen) and `reviewqueue` plan nothing; neither does "human review" or
  "human grading": the planner looks for the exact words, there is no language model behind it.
- It does not read negation (`Do not create a review queue...` still plans one), a question
  (`What is a review queue?`) or a request to delete one: there is no delete capability.
- A sentence with no domain word makes a plan with no steps; it cannot be approved
  (HTTP 409).
