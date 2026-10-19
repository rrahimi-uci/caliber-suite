# SCN-13 assets

Files for [the lab](../README.md). This is an **Aria autonomous** scenario: you give Aria the intent and it plans and creates the artifacts. The files are the sentence, the data the lab checks it against, a seeder for the traces to review, and the values you enter in the plan's **Aria needs information** forms.

| File | What it is | How it is used |
| --- | --- | --- |
| [`intent.md`](intent.md) | The canonical sentence and what the planner does with other phrasings | `Plans > Goal`, or `POST /aria/plans {goal}` |
| [`expected-plan.json`](expected-plan.json) | The plan the canonical sentence must draw | Sanity check; the script asserts it |
| [`dataset/goals.jsonl`](dataset/goals.jsonl) | 26 goals: `{id, goal, expected_steps, outcome, note}` | The script draws a plan for each (draft only, then cancels it) and compares the steps |
| [`dataset/agent-replies.jsonl`](dataset/agent-replies.jsonl) | 30 flagged agent replies: `{id, flag, tags, user_message, reply, review}` | What is queued, and the labels a reviewer gives (`review`) |
| [`seed_traces.py`](seed_traces.py) | Writes the 30 replies to MLflow as traces, prints `{row id: trace id}` | Run by the script and by the Playwright spec; needs `mlflow` and `MLFLOW_TRACKING_URI` |
| [`review-queues/agent-reply-review.queue.json`](review-queues/agent-reply-review.queue.json) | The `review_queue.create` form values (`name`, `description`, `reviewers`, `questions`) | The queue's input form; the same shape is a `POST /review-queues` body |
| [`enqueue.json`](enqueue.json) | The `review_queue.add_items` form values (`trace_ids`, `assigned_to`) | The enqueue form; the queue's id is wired, not typed |

The `notes` key in the form specs is for readers: strip it before posting a file as `inputs` (the script does).

Flow: `POST /aria/plans` (intent) -> `.../approve` -> `.../execute`. The create step pauses on an `input` question; answer it with `.../interactions/{id}/answer {"inputs": {...}}` (the form's **Continue plan**) and, under **Stop before each change**, approve it. The enqueue step then asks for `trace_ids` only: its `queue_id` is wired from the create step. Then review in **Review Queues** (or `POST /review-queues/{id}/items/{item}/submit`). See [`../../ARIA-AUTONOMY.md`](../../ARIA-AUTONOMY.md).
