# The intent you give Aria

## Canonical (works with the default HeuristicPlanner)

> Our workflow's recent runs look weak — set up a review queue for the flagged traces and kick off a workflow calibration.

Why this wording: the default planner proposes a step for each capability whose
**domain word** appears in the goal. The phrase **"review queue"** maps to
**both** `review_queue.create` **and** `review_queue.add_items`; the word
**"workflow"** (calibration) maps to `workflow.calibrate`. Keep both literal
needles in the sentence.

Autonomy: **Stop before each change** (`ask_each`: an **Approve** / **Deny** prompt before every mutate step, after the input form).

## Natural-language variant (not understood by the default planner)

> Our latest runs aren't great — get a human to look at the bad ones and then tune the pipeline.

This reads better, but the default heuristic planner has no mapping from "get a
human to look at the bad ones" to the review-queue steps or "tune the pipeline"
to `workflow.calibrate` (the sentence contains neither "review queue" nor
"workflow"), so it plans nothing. Use the canonical wording.

## Remember: this loop remediates EXISTING things

`review_queue.add_items` needs **real existing** `trace_ids` and
`workflow.calibrate` needs an **existing** `workflow_id` + `agent_id`. Aria does
not create the subject workflow or its traces — build that in **SCN-07** first,
then enter the ids in the input forms when Aria pauses (see [`enqueue.json`](enqueue.json) and
[`calibrate.json`](calibrate.json)).
