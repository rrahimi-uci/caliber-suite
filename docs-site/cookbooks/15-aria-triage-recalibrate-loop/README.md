# Aria: Triage & Recalibrate Loop

## Demo objective

Give Aria ONE sentence about weak workflow runs and watch it plan and (1) create
a **review queue**, (2) **enqueue the flagged traces**, and (3) kick off an
**asynchronous workflow calibration** — then **park** the plan on the background
job and **resume itself** once the job finishes. You enter the ids and schema when
Aria asks and confirm each change. This showcases Aria's first **async**
capability and the **governance -> remediation** loop.

## Feasibility & substitutions

Read [`../ARIA-AUTONOMY.md`](../ARIA-AUTONOMY.md) and [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ `review_queue.create`, `review_queue.add_items`, and `workflow.calibrate`
  are real Aria capabilities (TIER_MUTATE).
- ⚠️ This is a **remediation** loop — it operates on things that **already
  exist**. `review_queue.add_items` needs **real existing** `trace_ids`;
  `workflow.calibrate` needs an **existing** `workflow_id` + `agent_id`. Aria
  does **not** create the subject workflow or its traces — **build that in
  SCN-07 first**, then enter the ids in the plan's input forms.
- ⚠️ The default **HeuristicPlanner** keys off **domain words** in the goal: the
  phrase **"review queue"** proposes **both** `review_queue.create` and
  `review_queue.add_items`; the word **"workflow"** proposes
  `workflow.calibrate`. The canonical intent keeps both literal needles. (No LLM
  planner is wired; a natural phrasing without those words plans nothing.)
- ✅ **Inputs are collected in the plan.** Each step pauses on an **"Aria needs
  information"** form (**Continue plan** / **Skip step**) listing the fields the
  planner could not fill; your answers are merged into the step and validated
  before it runs. The planner links `review_queue.add_items`' `queue_id` to the
  queue the first step creates, so that form only asks for `trace_ids`. See
  [`../ARIA-AUTONOMY.md`](../ARIA-AUTONOMY.md) §Execution status.
- 🆕 `workflow.calibrate` is the **first ASYNC capability**. When Aria runs it,
  the handler **enqueues** a real workflow-calibration job and returns an
  `AsyncJobHandle`; the step **parks in `waiting_job`** and the plan stays
  **`running`** (it does **not** pause for a human). The plan worker polls the
  job automatically (about every 10 seconds) and maps the refinement job to
  `done` when it reaches `candidate_ready` or `applied`, or to `failed` on
  `rejected`/`failed`/`error`. On `done` the step finishes and the plan **resumes
  + completes** on its own; the candidate is **not** applied by the plan.
  (`POST …/poll` does the same on demand; the **Plans** UI has no Poll button.)
- ⚠️ The default evaluator and LLM are fake (`eval_provider` and `llm_provider`
  default to `fake`), so in a default install the calibration job scores
  candidates with canned numbers; configure a real provider for real results.
- Autonomy **Stop before each change** (`ask_each`) adds an **Approve** / **Deny**
  prompt before each mutate step; the async calibration still parks and resumes.

## Prerequisites

- An **existing workflow with recent runs/traces** to remediate — build the
  Support Triage Copilot in **SCN-07** and run it so you have flagged traces.
- The **flagged trace ids** (read from **Observability**) and the target
  **`workflow_id` + `agent_id`** (from that SCN-07 workflow).
- The operator role, a configured model provider, and a refinement worker for
  the calibration job; Aria Plans available (sidebar **Integrations and preview
  › Plans**, a Preview page).

## Recipe (UI-first, with API fallbacks)

1. **State the intent.** Sidebar **Plans** → **Goal**. Paste the canonical
   intent from [`assets/intent.md`](assets/intent.md):
   *"Our workflow's recent runs look weak — set up a review queue for the
   flagged traces and kick off a workflow calibration."*
   Set **Autonomy** = **Stop before each change**, then click **Decompose goal**.
   - API: `POST /aria/plans {goal, autonomy:"ask_each"}` → returns a `draft` plan.
2. **Review the plan.** Aria decomposes the intent into three steps —
   `review_queue.create`, `review_queue.add_items`, and `workflow.calibrate`.
   Confirm all three appear (see [`assets/expected-plan.json`](assets/expected-plan.json)).
   - API: `GET /aria/plans/{plan_id}`.
3. **Approve.** Click **Approve plan**.
   - API: `POST /aria/plans/{plan_id}/approve`.
4. **Execute the plan + supply each step's inputs.** Click **Execute**, then work
   through the forms in order, using the specs (with REAL ids):
   - Review queue → enter `name` and `questions` from
     [`assets/review-queues/triage-review.queue.json`](assets/review-queues/triage-review.queue.json)
     → **Continue plan** → **Approve**.
   - Enqueue → enter the **real flagged** `trace_ids` (a JSON array) from
     [`assets/enqueue.json`](assets/enqueue.json) → **Continue plan** →
     **Approve**. (`queue_id` is linked from the first step.)
   - Calibrate → enter the **real** `workflow_id` + `agent_id` from
     [`assets/calibrate.json`](assets/calibrate.json) → **Continue plan** →
     **Approve**.
   - API: `POST /aria/plans/{plan_id}/execute`; `GET …/interactions`;
     `POST /aria/interactions/{id}/answer {"inputs": {…}}` for each form, then
     `{"approved": true}` for each permission prompt.
5. **Park + wait for the async job.** Once you approve the `workflow.calibrate`
   step, the calibration job is **enqueued** and that step parks in
   **`waiting_job`** (the plan stays `running`). Nothing more to click: the plan
   worker advances it when the refinement job reaches a terminal state.
   - UI: refresh the plan page; the step shows *waiting job* until the job
     finishes.
   - API: `POST /aria/plans/{plan_id}/poll` advances it on demand — repeat until
     the `workflow.calibrate` step reads `done`. The resolver maps the
     refinement job to `done` at `candidate_ready`/`applied` and to `failed` at
     `rejected`/`failed`/`error`; on `done` the plan **resumes and completes**
     automatically.
6. **Verify.** Open **Review Queues** → `weak-runs-triage` exists with the four
   triage questions and the flagged traces enqueued; open **Workflows** /
   **Observability** → the workflow-calibration job reached `candidate_ready` (or
   `applied`); the plan shows all three steps completed.
7. **(Follow-up)** Have reviewers answer the queue's questions (answers write
   back onto the traces), then compare the recalibrated workflow against its
   baseline runs and feed the hard cases into its eval dataset (SCN-01 / SCN-10).

## Demo evidence to capture

- The plan id + its three decomposed steps.
- The three inputs you entered (question schema, trace ids, workflow + agent ids).
- The `workflow.calibrate` step in **`waiting_job`**, the worker/poll resuming it,
  and the step flipping to **`done`** as the plan resumes.
- The created review queue id + enqueued item count + the finished calibration
  job, shown live in Review Queues / Workflows / Observability.

## Done when / gate

- The plan decomposed from the intent alone and contains all three capabilities.
- A real review queue `weak-runs-triage` exists with the flagged traces enqueued.
- The `workflow.calibrate` step parked on an async job and, once the job
  reached `candidate_ready`/`applied`, the plan **resumed with all three steps
  completed**.
