# Aria Autonomous Track — what Aria can build end-to-end from intent

Scenarios **12–15** are a different kind of demo: you give **Aria** a single
clear **intent** and it **plans → (you approve) → executes** to create real
artifacts, pausing to collect each step's inputs and, depending on the autonomy
level, to confirm each change. This doc is the ground truth for that track
(verified in `caliber/src/caliber/assistant/{capabilities,plans,executor}.py`,
`routes/aria_plans.py`, and the **Plans** UI in `caliber-ui/src/pages/AriaPlans.tsx`
and `components/aria/planView.tsx`). Read [`FEASIBILITY.md`](FEASIBILITY.md) for
the rest of the platform.

## The honest boundary

Aria's autonomous reach is **exactly its capability registry** — today that is
**evaluation & governance scaffolding** plus two read-only OpenAPI capabilities.
It can create judges, test sets, and review queues, enqueue traces, and trigger a
workflow calibration. It **cannot** yet autonomously author prompts, skills,
tools, or build workflows/KBs (those are the manual Build scenarios 01–11). The
four Aria scenarios are designed to live **entirely inside** the real registry.
`GET /aria/capabilities` returns the live list.

## Execution status — what works today (verified)

- ✅ **Planning from intent works.** `POST /aria/plans {goal}` decomposes the
  intent into the right capability **steps**. The default `HeuristicPlanner` is
  deterministic and registry-driven (no LLM).
- ✅ **Step inputs are collected through typed interactions.** The planner does
  not know a judge's instructions or a queue's questions, so when a step is about
  to run with required fields still missing, the executor pauses the plan and
  raises an **`input`** interaction carrying the capability's JSON Schema. In the
  UI this is an **"Aria needs information"** form with **Continue plan** and
  **Skip step**; over the API it is
  `POST /aria/interactions/{id}/answer {"inputs": {...}}`. The executor merges
  the answer into the step's inputs, validates it against the schema (an unknown
  or wrongly typed field is rejected with a `409` and the interaction stays
  open), and then resumes the plan. Skipping marks the step **skipped**.
- ✅ **Dependencies between steps are wired.** When a step's required field is a
  declared output of an earlier selected step, the planner links it
  (`{"$from_step": …, "path": …}`) instead of asking again — for example
  `review_queue.add_items.queue_id` comes from `review_queue.create`. A linked
  field is not shown in the form.
- ⚠️ **Linking is by field name, so `name` is linked too.** `judge.create` and
  `review_queue.create` both output `name`, and `eval_dataset.create` and
  `review_queue.create` both require it. With the default planner a test set takes
  the **name of the judge** step (12) and, in the three-capability plan, the review
  queue also takes the judge's name and the test set takes the queue's (14). You
  cannot type a different name for a linked step in the form; the dataset step then
  needs no input at all, so its `description`/`tags` are never prompted. Name the
  judge so the shared name reads well, or create a separately named test set on
  **Test Sets**.
- ✅ **Execution is hands-off after the answers.** Entering the inputs is the
  only manual work; you do not create each artifact through its own route. The
  `assets/` specs in each scenario are the values you enter in those forms (or
  POST as `inputs`).
- ⚠️ **No LLM planner is wired.** The `Planner` Protocol slot exists; the default
  is the heuristic planner. Phrasing must contain the literal domain words below
  (the heuristic planner also recognises "test set" for `eval_dataset.create`, but
  not "faithfulness scoring" for a judge or "human grading" for a review queue).

## Capability registry (the only things Aria can do)

| Capability | Tier | Required inputs | Notes |
| --- | --- | --- | --- |
| `judge.list` | read | — | list active judges |
| `judge.create` | mutate | `name`, `instructions` | instructions must reference ≥1 of `{{ inputs }}`/`{{ outputs }}`/`{{ expectations }}`; optional `description`, `model`, `feedback_value_type` (bool\|int\|float\|str), `tags` |
| `review_queue.list` | read | — | list active queues |
| `review_queue.create` | mutate | `name`, `questions` | question item: `{key, title, type∈[pass_fail,categorical,numeric,text], options[], required, target∈[feedback,expectation]}`; optional `description`, `reviewers` |
| `eval_dataset.create` | mutate | `name` | creates an **empty** versioned test set (optional `description`, `tags`); example rows are added **separately** (not an Aria capability) |
| `review_queue.add_items` | mutate | `queue_id`, `trace_ids` | enqueues **existing** traces; optional `experiment_id`, `assigned_to` |
| `workflow.calibrate` | mutate · **async** | `workflow_id`, `agent_id` | enqueues a calibration (refinement) job; the plan **parks** (`waiting_job`) and is resumed when the job reaches `candidate_ready`/`applied` (or fails on `rejected`/`failed`) |
| `openapi_tool.list` | read | — | list published OpenAPI-derived tools (including write-capable ones, for discovery) |
| `openapi_tool.invoke` | read | `tool_id` | invoke one published **read-only** OpenAPI tool with optional `input`; refuses write or approval-gated tools |

The planner only proposes `safe`/`mutate` capabilities, so the four **read**
capabilities are never planned from a goal.

## How a plan is built (default planner)

The default **`HeuristicPlanner`** is deterministic and registry-driven: it
proposes a step for **every non-read capability whose domain word appears in the
goal**, where the domain is the key before the dot (underscores read as spaces),
or whose full key appears, or which matches a synonym:

| Domain needle in the goal | Capabilities proposed |
| --- | --- |
| `judge` | `judge.create` |
| `eval dataset` / `eval_dataset` / `test set` / `test-set` / `testset` | `eval_dataset.create` |
| `review queue` | `review_queue.create` **and** `review_queue.add_items` |
| `workflow` | `workflow.calibrate` |

So craft the intent to contain the literal needles. Each scenario's
`assets/intent.md` gives a canonical (needle-bearing) intent and a
natural-language variant that the heuristic planner only partly understands.
Steps run in registry order.

A goal that matches nothing still creates a plan (`201`, with a `warnings` entry
saying no platform steps matched), but a plan with no steps cannot be approved or
executed: both routes return `409`, and the **Approve plan** button is disabled.

## Plan lifecycle + autonomy

```
POST /aria/plans {goal, autonomy}      -> draft (decomposed steps)
POST /aria/plans/{id}/approve          -> approved   (409 if there are no steps)
POST /aria/plans/{id}/execute          -> runs until it pauses / completes / fails
GET  /aria/plans/{id}/interactions     -> pending interaction(s)
POST /aria/interactions/{iid}/answer {approved | choice | value | inputs}  -> resume
POST /aria/plans/{id}/poll             -> advance an async (waiting_job) step
```

Interaction kinds: **`input`** (missing required fields — answer with `inputs`),
**`permission`** (run this step? — `approved` true/false), **`confirm`** (a
below-gate result — accept/reject) and **`choice`**. Answering resumes execution
automatically; the **Execute**/**Resume** button is only shown while nothing is
pending.

**Autonomy dial** (`gate_decision`), with the labels the UI shows: `read` always
runs; **`gated` always pauses** (the non-negotiable floor); `safe`/`mutate`
**auto-run** under **Review the plan, then run** (`approve_plan`) and **Run
everything it safely can** (`auto_guarded`) but **pause under Stop before each
change** (`ask_each`). Missing inputs are collected at every autonomy level —
the autonomy dial only decides whether you *also* get an **Approve** / **Deny**
prompt before each change. Use **Stop before each change** in a demo so you
confirm (or deny) every mutate step. Denying marks that step **skipped** (e.g.
skip `review_queue.add_items` when you have no traces yet).

Async steps: `workflow.calibrate` parks the plan in `waiting_job`. The plan
worker advances parked plans automatically (about every 10 seconds,
`aria_plan_worker_interval_seconds`, while background tasks are enabled);
`POST /aria/plans/{id}/poll` does the same on demand. The **Plans** UI has no
Poll button — refresh the plan page.

UI: sidebar **Integrations and preview › Plans** → `/aria/plans`
(`AriaPlans.tsx`; a Preview page). Buttons: **Decompose goal**, **Approve plan**,
**Execute** / **Resume**, **Continue plan** / **Skip step** (input form),
**Approve** / **Deny** (permission), **Accept** / **Reject** (confirm).

## The four Aria scenarios

| # | Folder | Intent decomposes to | Showcases |
| --- | --- | --- | --- |
| 12 | `12-aria-evaluation-harness` | `judge.create` + `eval_dataset.create` | judge + test set from one sentence |
| 13 | `13-aria-review-governance-queue` | `review_queue.create` (+ `add_items`) | a rich human-review label schema |
| 14 | `14-aria-governance-starter-kit` | `judge.create` + `review_queue.create` + `eval_dataset.create` (+ `add_items`) | **flagship** — a whole eval+governance scaffold in one plan |
| 15 | `15-aria-triage-recalibrate-loop` | `review_queue.create` + `add_items` + `workflow.calibrate` | the **async** capability + the triage→remediate loop (operates on existing ids; depends on SCN-07) |

Each scenario folder holds the usual 5 contract files plus an `assets/` with the
intent, the expected plan, and the concrete **values you enter** when Aria asks
for each step's inputs (the judge/queue/dataset/enqueue/calibrate payloads).

> The product's default evaluator and LLM are fake (`eval_provider` and
> `llm_provider` default to `fake`), so the calibration job that `workflow.calibrate`
> enqueues scores candidates with canned numbers in a default install, and
> **Evaluations** returns 400 until a real model provider is configured. Creating
> the artifacts in these scenarios needs no provider; running them for real does.
