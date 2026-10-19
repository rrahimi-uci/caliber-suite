# SCN-15 assets

Files for [the lab](../README.md). This is an **Aria autonomous** scenario: you give Aria the intent and it plans and triggers. Its async step operates on things that already exist, so the assets also include the subject it remediates and the flagged traces.

| File | What it is | How it is used |
| --- | --- | --- |
| [`intent.md`](intent.md) | The canonical sentence and what the planner does with other phrasings | `Plans > Goal`, or `POST /aria/plans {goal}` |
| [`expected-plan.json`](expected-plan.json) | The plan the canonical sentence must draw | Sanity check; the script asserts it |
| [`dataset/goals.jsonl`](dataset/goals.jsonl) | 28 goals: `{id, goal, expected_steps, outcome, note}` | The script draws a plan for each (draft only, then cancels it) and compares the steps |
| [`dataset/support-runs.jsonl`](dataset/support-runs.jsonl) | 30 recorded support runs: `{id, question, reply, tools_called, required_tools, reference, weak, reason}` | The test set the workflow is calibrated against, the 12 flagged traces (`weak`), and the reviewer's labels |
| [`workflows/support-policy-agent.workflow.json`](workflows/support-policy-agent.workflow.json) | The subject workflow: a support agent with two tools, a grounding guardrail and a deploy gate | Imported by `setup_subject.py` (UI: `Agent resources > Workflows > Import`) |
| [`tools/lookup-policy.tool.json`](tools/lookup-policy.tool.json), [`tools/get-order.tool.json`](tools/get-order.tool.json) | The two read tools the agent binds (`caliber.workflows.demo_tools`) | Registered by `setup_subject.py` |
| [`setup_subject.py`](setup_subject.py) | Creates the tools, the test set, the published workflow and an agent; prints their ids | Run by the script and the Playwright spec; by hand for the UI path |
| [`seed_traces.py`](seed_traces.py) | Writes the 12 weak runs to MLflow as traces, prints `{row id: trace id}` | Run by the script and the Playwright spec; needs `mlflow` and `MLFLOW_TRACKING_URI` |
| [`review-queues/triage-review.queue.json`](review-queues/triage-review.queue.json) | The `review_queue.create` form values (`name`, `description`, `reviewers`, `questions`) | The queue's input form; the same shape is a `POST /review-queues` body |
| [`enqueue.json`](enqueue.json) | The `review_queue.add_items` form values (`trace_ids`, `assigned_to`) | The enqueue form; the queue's id is wired, not typed |
| [`calibrate.json`](calibrate.json) | The `workflow.calibrate` form values (`workflow_id`, `agent_id`) | The calibration form; placeholders for the ids `setup_subject.py` prints |

The `notes` key in the form specs is for readers: strip it before posting a file as `inputs` (the script does).

Flow: `POST /aria/plans` (intent) -> `.../approve` -> `.../execute`. Each step pauses on an `input` question; answer it with `.../interactions/{id}/answer {"inputs": {...}}` (the form's **Continue plan**) and, under **Stop before each change**, approve it. Approving `workflow.calibrate` enqueues a refinement job and **parks** the plan (`running`, the step `waiting_job`): the plan worker (or `POST /aria/plans/{id}/poll`) resumes it when the job finishes. See [`../../ARIA-AUTONOMY.md`](../../ARIA-AUTONOMY.md).
