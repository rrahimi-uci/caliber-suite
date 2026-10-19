# The intent you give Aria

## Canonical (works with the default planner)

> Our workflow's recent runs look weak - set up a review queue for the flagged traces and kick off a workflow calibration.

Why this wording: the default planner proposes a step for each capability whose
**domain word** appears in the goal. "review queue" gives `review_queue.create` **and**
`review_queue.add_items` (same domain); "workflow" gives `workflow.calibrate`. Keep both in
the sentence.

Autonomy: **Stop before each change** (`ask_each`: an **Approve** / **Deny** prompt before
every step that changes something, after the input form).

## This loop remediates things that already exist

`review_queue.add_items` needs trace ids that exist, and `workflow.calibrate` needs a workflow
and an agent that exist. Aria creates neither: it does not discover traces, and it cannot
build a workflow (`Create a new workflow for returns.` plans a *calibration*, not a workflow).
`setup_subject.py` creates the lab's subject (two tools, a published workflow with its test
set as the deploy gate, an agent) and `seed_traces.py` writes the flagged traces; in a real
deployment they are yours.

## What the planner does with other sentences

`dataset/goals.jsonl` holds 28 goals and the plan the server draws for each one; the script
checks all 28 against the real server. In short:

- Any sentence with `review queue` plans both the queue and its enqueue step; any with
  `workflow` plans a calibration, whatever the sentence is about (`We are in the middle of a
  workflow outage.` plans one). Both can appear in one plan, always in the order queue, enqueue,
  calibrate.
- "pipeline", "tune", "human review", "get a human to look" and "re-run the evaluation" are
  not domain words: `Tune the pipeline and get a human to look at the bad runs.` plans nothing.
  There is no language model behind the planner.
- It does not read negation (`Do not calibrate the workflow, only set up the review queue.`
  still plans the calibration), questions, "rebuild" or "roll back": there is no capability
  for those, and the word `workflow` still plans a calibration.
- A sentence with no domain word makes a plan with no steps; it cannot be approved
  (HTTP 409).
