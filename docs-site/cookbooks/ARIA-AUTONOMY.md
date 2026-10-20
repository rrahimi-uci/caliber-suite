# Aria Autonomous Track — what Aria can build end-to-end from intent

> **Disabled by default.** Aria and its **Plans** page are a preview area and are off
> by default; this whole track needs `CALIBER_FEATURE_ARIA=true` on the server (and
> `CALIBER_FEATURE_COOKBOOKS=true` to install the recipes from the gallery). While it
> is off, the `/aria/*` and `/assistant/*` routes answer 404
> (`reason_code: feature_disabled`).

Scenarios **12–15** are a different kind of demo: you give **Aria** a single
clear **intent** and it **plans → (you approve) → executes** to create real
artifacts, pausing to collect each step's inputs and, depending on the autonomy
level, to confirm each change. This doc is the ground truth for that track
(verified in `caliber/src/caliber/assistant/{capabilities,plans,executor}.py`,
`routes/aria_plans.py`, and the **Plans** UI in `caliber-ui/src/pages/AriaPlans.tsx`
and `components/aria/planView.tsx`) and **each statement here was run**: every lab
has a script (`sdk/caliber-sdk/examples/cookbooks/cookbook_12..15_*.py`) and a
Playwright spec (`caliber-ui/e2e/cookbooks/labs/12..15-*.spec.ts`) that exercise it
against a live server, and a dataset of goals (`assets/dataset/goals.jsonl`) that the
shipped planner is checked against (`caliber/tests/test_cookbook_aria_labs.py`).
Read [`FEASIBILITY.md`](FEASIBILITY.md) for the rest of the platform.

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
- ✅ **Only references are wired, never a name.** The planner links a required
  field to an earlier step only when it is a reference to that step's result: a field
  ending `_id`. Today that is `review_queue.add_items.queue_id`, wired from
  `review_queue.create`. A `name` is the identity of what a step is about to create,
  so every create step asks for its own (it used to take the previous step's, so a
  test set was silently created under the judge's name and its form never asked).
  `workflow.calibrate` takes no wired input: its `workflow_id` and `agent_id` are
  existing things you type.
- ✅ **Skipping or denying a step skips what depends on it.** Denying (or
  **Skip step** on the form of) `review_queue.create` marks `review_queue.add_items`
  skipped too (`skipped because step 1 (review_queue.create) was skipped`) without
  asking about it; the plan then completes. It used to leave the plan `paused` with
  no question to answer and no way to finish.
- ✅ **A failed step fails the plan and stops it; nothing is undone.** The failure text
  names the field (`invalid input: instructions: instructions must reference at least
  one evaluation variable: ...`) or the refusal (`workflow 'WF-x' not found (HTTP 404)`),
  steps after it stay `pending`, and artifacts made by earlier steps stay made. A failed
  plan cannot be run again: state the goal again. Empty or missing required text and an
  empty `questions` / `trace_ids` array are refused when you answer the form (409, the
  question stays open) instead of failing the plan.
- ✅ **Execution is hands-off after the answers.** Entering the inputs is the
  only manual work; you do not create each artifact through its own route. The
  `assets/` specs in each scenario are the values you enter in those forms (or
  POST as `inputs`).
- ⚠️ **No LLM planner is wired, with a real provider too.** The `Planner` Protocol slot exists; the
  default is the heuristic planner, and `POST /aria/plans` uses it **whatever
  `CALIBER_ASSISTANT_ENGINE` is**. Run live on a `--real-llm` stack (engine `openai`): the 26 to 28 goals of
  each lab's dataset draw the same plans as on the simulated stack, and `"Set up faithfulness scoring for our support
  answers, with a test set to run it on."` still draws only the test set step. Phrasing must contain
  the literal domain words below (the heuristic planner also recognises "test set" for
  `eval_dataset.create`, but not "faithfulness scoring" for a judge or "human grading" for a review
  queue). The real engine acts only in the **Ask Aria** chat; see
  [What a real provider changes](#what-a-real-provider-changes-verified-live).

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

Async steps: `workflow.calibrate` parks the plan in `waiting_job`: the plan stays
`running`, the step carries the job's id, nothing is pending for a human, and a
parked plan cannot be cancelled or edited (only drafts can). The plan
worker advances parked plans automatically (about every 10 seconds,
`aria_plan_worker_interval_seconds`, while background tasks are enabled);
`POST /aria/plans/{id}/poll` does the same on demand and is safe to repeat. Poll only
**reads** the job: with the workers off the job stays `queued` and the plan stays
parked. The **Plans** page re-reads a running plan every five seconds, so it settles
without a click. The plan ends where the job ended: `candidate_ready` or `applied`
completes the step (the candidate is **not** applied), `rejected`, `failed` or `error`
fails it with `<status>: <the job's error_message>`.

**What the simulated default shows.** Aria passes only `workflow_id` and `agent_id`, so
the calibration runs with the default objective (maximize quality; a candidate must beat
the baseline by 0.02) and budget (20 test set cases, 3 candidates). A fake executor gives a candidate
the same outputs as the baseline, so on the simulated provider the job always ends
`rejected` (`quality delta +0.000 < epsilon 0.02`) and the plan `failed`.

**What a real provider shows (run live, lab 15).** The refinement worker replays 20 gate cases through
the real model and the lab's two tools (about 2 to 3 minutes), generated one candidate (a duplicate
grounding guardrail) and **rejected it, in both recorded runs, for the same reason**: quality scored
0.000 for the baseline and the candidate because the gate rows' `expected` has no key the quality
scorer reads (`output`, `final_output`, `answer`, `text`, `contains`, `exact`, `regex`,
`json_subset`), and with the default objective a candidate that only moves completion, tool
adherence or safety (by a row, in either direction, from run to run) cannot clear the gate. The plan
parked, was resumed by the worker and ended `failed` with the job's reason. The completing path
(`candidate_ready` -> plan `completed`) is covered by unit tests (`test_aria_async.py`) and was **not
reached live**. A first real attempt exposed a product defect, fixed: on the default model a
tool-using workflow agent could not run at all (chat completions refuse function tools with a
reasoning effort), so every replay errored.

UI: sidebar **Operate › Plans** → `/aria/plans`
(`AriaPlans.tsx`). Buttons: **Decompose goal**, **Approve plan**,
**Execute** / **Resume**, **Continue plan** / **Skip step** (input form),
**Approve** / **Deny** (permission), **Accept** / **Reject** (confirm).

## The four Aria scenarios

| # | Folder | Intent decomposes to | Showcases |
| --- | --- | --- | --- |
| 12 | `12-aria-evaluation-harness` | `judge.create` + `eval_dataset.create` | judge + test set from one sentence |
| 13 | `13-aria-review-governance-queue` | `review_queue.create` + `review_queue.add_items` | a rich human-review label schema |
| 14 | `14-aria-governance-starter-kit` | `judge.create` + `review_queue.create` + `eval_dataset.create` + `review_queue.add_items` | **flagship** — a whole eval+governance scaffold in one plan (the fourth step is skipped: no traces yet) |
| 15 | `15-aria-triage-recalibrate-loop` | `review_queue.create` + `add_items` + `workflow.calibrate` | the **async** capability + the triage→remediate loop (operates on existing ids; the lab creates its own workflow, agent and 12 flagged traces with two scripts in its `assets/`, and needs the background workers) |

Each scenario folder holds the usual 5 contract files plus an `assets/` with the
intent, the expected plan, a dataset of goals (`dataset/goals.jsonl`: 26 to 28 sentences
and the plan the planner draws for each, including the ones it gets wrong), the sample
data the lab loads, and the concrete **values you enter** when Aria asks for each step's
inputs (the judge/queue/dataset/enqueue/calibrate payloads).

> The product's default evaluator and LLM are fake (`eval_provider` and
> `llm_provider` default to `fake`). Creating the artifacts in these scenarios needs no
> provider. On the simulated stack, running a judge in **Evaluations** returns `400 evaluation requires a real
> LLM provider` until one is configured, and the calibration job that `workflow.calibrate`
> enqueues is rejected (above). The assistant engine is `fake` too (a deterministic heuristic,
> `GET /assistant/config`); the Plans page does not call it on any stack.

## What a real provider changes (verified live)

Every lab has a "Run it with a real provider" section, a `--real-llm` stack
(`scripts/cookbook-env.sh up labNNh --flags cookbooks,aria --real-llm`, your own `OPENAI_API_KEY`)
and a script and Playwright spec that detect the mode with `GET /readiness`. What was run:

| Where | What a real provider unlocks | Verified result (will vary) |
| --- | --- | --- |
| Plans (all labs) | **Nothing**: planning, forms, approvals, gating and the executor do not use a model | The planner's goals datasets draw identically (28/28, 26/26, 28/28, 28/28) |
| Lab 12 | Evaluations run the created judge over the 30 rows; a grounded and a careless answerer prompt are told apart | 30/30 rows judged; grounded 0.97, careless 0.00 |
| Lab 13 | A real judge is compared with the 12 human `safety` labels | agreement 0.92, kappa 0.82, 4 of 4 unsafe replies caught |
| Lab 14 | Evaluations run the judge over the 30 policy cases, sliced by tag | 30/30 rows judged, 0.97 faithful, the 5 `high-risk` rows sliced apart |
| Lab 15 | The calibration job runs for real (2 to 3 minutes) | `rejected`, plan `failed`: quality 0.000 for baseline and candidate (see above) |
| Ask Aria (all labs) | The chat answers and **acts**: its tools are the capability registry, and a mutating capability (`judge_create`, `review_queue_create`, ...) is offered only in a `build` session whose approval mode is `auto_all` (UI label **Legacy auto**) or `full_autonomy`; read tools in every session; gated tools never | `judge_create` / `review_queue_create` ran and made the artifact in `auto_all`; in a default session (**Ask first**) the model was offered read tools only and nothing was created |

Two things the real engine does **not** change: plan creation (the heuristic planner, always) and the
gates (a plan still pauses on `gated` steps and, under **Stop before each change**, on each change; the
chat has no approve or publish tool). What a model *does* decide in the chat is whatever the message
leaves open: asked to create a judge, it chose `model: openai:/gpt-4o-mini` itself, which the Plans form
path never does.

## What the planner gets wrong (documented, not fixed)

It is rules, so these are properties, each pinned by a goal in the datasets:

- It needs the exact words: `faithfulness scoring`, `evaluation dataset`, `review-queue`,
  `human review`, `pipeline` and `tune` plan nothing (or only the part it recognises).
- It does not read negation (`Do not create a judge ...` plans one), questions
  (`What is a judge?`), or verbs with no capability (`delete`, `roll back`, `rebuild`).
- It matches words inside other words (`judgement` plans a judge) and a domain word whatever
  the sentence is about (`a workflow outage` plans a calibration).
- A review queue always brings its enqueue step; skip it when you have no traces. A lone
  `queue` is not a review queue.
- `workflow` plans a calibration of an existing workflow, never a new workflow: Aria cannot
  build one.

## Other limits worth knowing

- `review_queue.add_items` does not check that a trace id exists: a made-up id is queued and
  fails (502) when a reviewer submits it. Aria does not discover traces.
- A review queue created outside a workspace is visible to its owner only; its `reviewers`
  list is a label, not access (lab 13). Plans are private to their owner too (another
  account gets 404, or 403 when answering an interaction by id).
- A plan runs with its **owner's** scopes: an owner without `caliber.operator` fails at
  `execute`, before the first question, and creates nothing.
