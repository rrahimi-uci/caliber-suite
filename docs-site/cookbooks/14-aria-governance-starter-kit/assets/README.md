# SCN-14 assets

Files for [the lab](../README.md). This is an **Aria autonomous** scenario: you give Aria the intent and it plans and creates the artifacts. The files are the sentence, the data the lab checks it against, and the values you enter in the plan's **Aria needs information** forms.

| File | What it is | How it is used |
| --- | --- | --- |
| [`intent.md`](intent.md) | The canonical sentence and what the planner does with other phrasings | `Plans > Goal`, or `POST /aria/plans {goal}` |
| [`expected-plan.json`](expected-plan.json) | The plan the canonical sentence must draw | Sanity check; the script asserts it |
| [`dataset/goals.jsonl`](dataset/goals.jsonl) | 28 goals: `{id, goal, expected_steps, outcome, note}` | The script draws a plan for each (draft only, then cancels it) and compares the steps |
| [`dataset/release-candidate-cases.jsonl`](dataset/release-candidate-cases.jsonl) | 30 policy cases: `{id, tags, input: {question, evidence}, expected: {answer}}` | Loaded into the test set Aria creates: API `POST /eval-datasets/{id}/examples`, UI `+ Add example` |
| [`judges/answer-faithfulness.judge.json`](judges/answer-faithfulness.judge.json) | The `judge.create` form values (`name`, `feedback_value_type`, `instructions`, `tags`) | The judge's input form; the same shape is a `POST /judges` body |
| [`review-queues/governance-review.queue.json`](review-queues/governance-review.queue.json) | The `review_queue.create` form values (`name`, `description`, `reviewers`, `questions`) | The queue's input form; the same shape is a `POST /review-queues` body |
| [`datasets/release-candidates.dataset.json`](datasets/release-candidates.dataset.json) | The `eval_dataset.create` form values (`name`, `description`, `tags`) | The test set's input form; the same shape is a `POST /eval-datasets` body |

The `notes` key in the form specs is for readers: strip it before posting a file as `inputs` (the script does).

Flow: `POST /aria/plans` (intent) -> `.../approve` -> `.../execute`. Each create step pauses on an `input` question; answer it with `.../interactions/{id}/answer {"inputs": {...}}` (the form's **Continue plan**) and, under **Stop before each change**, approve it (`{"approved": true}`). Each step asks for its own `name`. The enqueue step (`review_queue.add_items`, its `queue_id` wired from the queue step) is skipped here: there are no traces yet. See [`../../ARIA-AUTONOMY.md`](../../ARIA-AUTONOMY.md).
