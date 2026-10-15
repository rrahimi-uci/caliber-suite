# SCN-15 assets — what you hand Aria

This is an **Aria autonomous** scenario: you give Aria the intent, and it plans
+ creates/triggers the artifacts. These files are (a) the intent and (b) the
concrete values you enter in the **"Aria needs information"** forms when Aria
pauses.

| # | Artifact | File | Used at |
| --- | --- | --- | --- |
| 1 | The intent | [`intent.md`](intent.md) | `Plans → Goal` (or `POST /aria/plans`) |
| 2 | Expected plan | [`expected-plan.json`](expected-plan.json) | sanity-check the draft plan |
| 3 | Review-queue spec | [`review-queues/triage-review.queue.json`](review-queues/triage-review.queue.json) | the `review_queue.create` input form (**Continue plan**) |
| 4 | Enqueue spec | [`enqueue.json`](enqueue.json) | the `review_queue.add_items` input form (`trace_ids`; `queue_id` is linked) |
| 5 | Calibrate spec | [`calibrate.json`](calibrate.json) | the `workflow.calibrate` input form (then the plan **parks** on the job) |

Flow: `POST /aria/plans` (intent) → `…/approve` → `…/execute`. The planner leaves
the queue's schema, the trace ids and the workflow/agent ids empty, so each step
pauses on an `input` interaction; answer each with
`…/interactions/{id}/answer {"inputs": {…}}` (or the form's **Continue plan**).
The executor merges and validates the inputs, then — under **Stop before each
change** — asks for **Approve** / **Deny** (`{"approved": true}`). The
`review_queue.add_items` step has its `queue_id` linked from the create step.
Approving `workflow.calibrate` enqueues the calibration job and the plan parks in
`waiting_job`; the plan worker resumes it when the refinement job reaches
`candidate_ready`/`applied` (`POST /aria/plans/{id}/poll` advances it on demand;
the Plans UI has no Poll button). Then verify in Review Queues / Workflows /
Observability. See [`../../ARIA-AUTONOMY.md`](../../ARIA-AUTONOMY.md) §Execution
status.

The specs use the same shapes as the rest of the pack — an input form's content
IS the corresponding capability payload (the `review_queue.create` inputs are a
review-queue create body, etc.).

> **REMEDIATION, not creation.** `enqueue.json` and `calibrate.json` carry
> **placeholders**. This loop operates on things that **already exist**: the
> `trace_ids` must be **real** flagged trace ids (read them from
> **Observability**), and `workflow_id` + `agent_id` must be a **real existing**
> workflow + its agent (build it in **SCN-07**). Replace the placeholders with
> the real ids before filling the forms — Aria does not discover them.
>
> The default evaluator and LLM are fake, so a default install's calibration job
> scores candidates with canned numbers.
