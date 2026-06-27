# SCN-15 assets — what you hand Aria

This is an **Aria autonomous** scenario: you give Aria the intent, and it plans
+ creates/triggers the artifacts. These files are (a) the intent and (b) the
concrete **interaction answers** you paste when Aria pauses to confirm each
step's inputs.

| # | Artifact | File | Used at |
| --- | --- | --- | --- |
| 1 | The intent | [`intent.md`](intent.md) | `Plans → new goal` (or `POST /aria/plans`) |
| 2 | Expected plan | [`expected-plan.json`](expected-plan.json) | sanity-check the draft plan |
| 3 | Review-queue spec | [`review-queues/triage-review.queue.json`](review-queues/triage-review.queue.json) | answer to the `review_queue.create` interaction |
| 4 | Enqueue spec | [`enqueue.json`](enqueue.json) | answer to the `review_queue.add_items` interaction |
| 5 | Calibrate spec | [`calibrate.json`](calibrate.json) | answer to the `workflow.calibrate` interaction (then **poll**) |

Flow: `POST /aria/plans` (intent) → `…/approve` → `…/execute`. The shipped
planner leaves step inputs empty and the interaction answer can't carry a payload
(`{approved, choice, value}` only), so **drive each step via its own route** with
the specs below: `POST /review-queues`, `POST /review-queues/{id}/items` (real
trace_ids), and the workflow-calibration route (real workflow_id + agent_id). The
Aria answer just gates each step: `…/interactions/{id}/answer {approved:true}`;
the async calibration parks, so `POST /aria/plans/{id}/poll` until it resolves.
Then verify in Review Queues / Workflows / Observability. See
[`../../ARIA-AUTONOMY.md`](../../ARIA-AUTONOMY.md) §Execution status.

The specs use the same shapes as the rest of the pack — an Aria interaction
answer IS the corresponding capability payload (a `review_queue.create` answer
is a review-queue create body, etc.).

> **REMEDIATION, not creation.** `enqueue.json` and `calibrate.json` carry
> **placeholders**. This loop operates on things that **already exist**: the
> `trace_ids` must be **real** flagged trace ids (read them from
> **Observability**), and `workflow_id` + `agent_id` must be a **real existing**
> workflow + its agent (build it in **SCN-07**). Replace the placeholders with
> the real ids before answering the interactions — Aria does not discover them.
