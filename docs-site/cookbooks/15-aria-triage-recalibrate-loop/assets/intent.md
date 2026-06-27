# The intent you give Aria

## Canonical (works with the default HeuristicPlanner)

> Our workflow's recent runs look weak — set up a review queue for the flagged traces and kick off a workflow calibration.

Why this wording: the default planner proposes a step for each capability whose
**domain word** appears in the goal. The phrase **"review queue"** maps to
**both** `review_queue.create` **and** `review_queue.add_items`; the word
**"workflow"** (calibration) maps to `workflow.calibrate`. Keep both literal
needles in the sentence.

Autonomy: **ask_each** (pauses on every mutate step so you confirm each input).

## Natural-language variant (needs an LLM planner)

> Our latest runs aren't great — get a human to look at the bad ones and then tune the pipeline.

This reads better but relies on an LLM planner to map "get a human to look at
the bad ones" → `review_queue.create` + `review_queue.add_items` and "tune the
pipeline" → `workflow.calibrate`. With the default heuristic planner, use the
canonical wording.

## Remember: this loop remediates EXISTING things

`review_queue.add_items` needs **real existing** `trace_ids` and
`workflow.calibrate` needs an **existing** `workflow_id` + `agent_id`. Aria does
not create the subject workflow or its traces — build that in **SCN-07** first,
then supply the ids when Aria pauses (see [`enqueue.json`](enqueue.json) and
[`calibrate.json`](calibrate.json)).
