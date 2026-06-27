# Aria: Triage & Recalibrate Loop

## Demo objective

Give Aria ONE sentence about weak workflow runs and watch it autonomously plan
and (1) create a **review queue**, (2) **enqueue the flagged traces**, and (3)
kick off an **asynchronous workflow calibration** — then **park** the plan on
the background job and **resume itself** once the job completes. You only confirm
the parameters it asks for. This showcases Aria's first **async** capability and
the **governance -> remediation** loop.

## Feasibility & substitutions

Read [`../ARIA-AUTONOMY.md`](../ARIA-AUTONOMY.md) and [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ `review_queue.create`, `review_queue.add_items`, and `workflow.calibrate`
  are real Aria capabilities (TIER_MUTATE).
- ⚠️ This is a **remediation** loop — it operates on things that **already
  exist**. `review_queue.add_items` needs **real existing** `trace_ids`;
  `workflow.calibrate` needs an **existing** `workflow_id` + `agent_id`. Aria
  does **not** create the subject workflow or its traces — **build that in
  SCN-07 first**, then supply the ids as interaction answers.
- ⚠️ The default **HeuristicPlanner** keys off **domain words** in the goal: the
  phrase **"review queue"** proposes **both** `review_queue.create` and
  `review_queue.add_items`; the word **"workflow"** proposes
  `workflow.calibrate`. The canonical intent keeps both literal needles. (A
  natural phrasing needs an LLM planner.)
- 🆕 `workflow.calibrate` is the **first ASYNC capability**. When Aria runs it,
  the handler **enqueues** a real workflow-calibration job and returns an
  `AsyncJobHandle`; the step **parks in `waiting_job`** and the plan stays
  **`running`** (it does **not** pause for a human). You then **`POST
  …/poll`** until the MLflow resolver maps the `refinement_job` to
  `done`/`failed`. On `done`, the step finishes and the plan **resumes +
  completes** on its own.
- ⚠️ **Execution gap (verified):** the shipped planner proposes the steps but
  leaves their **inputs empty**, and the interaction answer (`{approved, choice,
  value}`) can't inject them — so today you **drive each step via its own route**
  with the `assets/` specs, while Aria's plan is the orchestration record. True
  hands-off execution needs an LLM planner (not wired). See
  [`../ARIA-AUTONOMY.md`](../ARIA-AUTONOMY.md) §Execution status. `autonomy:
  ask_each` pauses on every mutate step; the async calibration still parks + polls.

## Prerequisites

- An **existing workflow with recent runs/traces** to remediate — build the
  Support Triage Copilot in **SCN-07** and run it so you have flagged traces.
- The **flagged trace ids** (read from **Observability**) and the target
  **`workflow_id` + `agent_id`** (from that SCN-07 workflow).
- A configured model provider; Aria Plans available (sidebar **Plans**).

## Recipe (UI-first, with API fallbacks)

1. **State the intent.** Sidebar **Plans** → new goal. Paste the canonical
   intent from [`assets/intent.md`](assets/intent.md):
   *"Our workflow's recent runs look weak — set up a review queue for the
   flagged traces and kick off a workflow calibration."*
   Set autonomy = **ask_each**.
   - API: `POST /aria/plans {goal, autonomy:"ask_each"}` → returns a `draft` plan.
2. **Review the plan.** Aria decomposes the intent into three steps —
   `review_queue.create`, `review_queue.add_items`, and `workflow.calibrate`.
   Confirm all three appear (see [`assets/expected-plan.json`](assets/expected-plan.json)).
   - API: `GET /aria/plans/{plan_id}`.
3. **Approve.** Approve the plan shape.
   - API: `POST /aria/plans/{plan_id}/approve`.
4. **Execute the plan + drive each step via its route.** Approve→execute drives
   the plan (approve each mutate step). Because the answer can't carry the
   payload, perform each step via its own route using the specs (with REAL ids):
   - Review queue → `POST /review-queues` with
     [`assets/review-queues/triage-review.queue.json`](assets/review-queues/triage-review.queue.json).
   - Enqueue → `POST /review-queues/{id}/items` with the **real flagged**
     `trace_ids` from [`assets/enqueue.json`](assets/enqueue.json).
   - Calibrate → the workflow-calibration route (the one Aria's
     `workflow.calibrate` enqueues) with the **real** `workflow_id` + `agent_id`
     from [`assets/calibrate.json`](assets/calibrate.json).
   - API: `POST /aria/plans/{plan_id}/execute`; `GET …/interactions`;
     `POST /aria/interactions/{id}/answer {approved:true}` (gates the step — no
     `inputs` field).
5. **Park + poll the async job.** Once you answer the `workflow.calibrate`
   interaction, the calibration job is **enqueued** and that step parks in
   **`waiting_job`** (the plan stays `running`). **Poll** to advance it:
   - API: `POST /aria/plans/{plan_id}/poll` — repeat until the
     `workflow.calibrate` step reads `done`. The resolver maps the
     `refinement_job` to `completed`/`failed`; on `completed` the plan
     **resumes and completes** automatically. (In the UI, the plan shows the
     step "waiting on a job"; refresh/poll until it clears.)
6. **Verify.** Open **Review Queues** → `weak-runs-triage` exists with the four
   triage questions and the flagged traces enqueued; open **Workflows** /
   **Observability** → the workflow-calibration job **completed**; the plan
   shows all three steps completed.
7. **(Follow-up)** Have reviewers answer the queue's questions (answers write
   back onto the traces), then compare the recalibrated workflow against its
   baseline runs and feed the hard cases into its eval dataset (SCN-01 / SCN-10).

## Demo evidence to capture

- The plan id + its three decomposed steps.
- The three interaction answers (question schema, trace ids, workflow + agent ids).
- The `workflow.calibrate` step in **`waiting_job`**, the **poll** call(s), and
  the step flipping to **`done`** as the plan resumes.
- The created review queue id + enqueued item count + the completed calibration
  job, shown live in Review Queues / Workflows / Observability.

## Done when / gate

- The plan decomposed from the intent alone and contains all three capabilities.
- A real review queue `weak-runs-triage` exists with the flagged traces enqueued.
- The `workflow.calibrate` step parked on an async job and, after polling, the
  job **completed** and the plan **resumed with all three steps completed**.
