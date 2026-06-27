# Aria Autonomous Track — what Aria can build end-to-end from intent

Scenarios **12–15** are a different kind of demo: you give **Aria** a single
clear **intent** and it autonomously **plans → (you approve) → executes** to
create real artifacts, pausing only to confirm each step's parameters. This doc
is the ground truth for that track (verified in
`caliber/src/caliber/assistant/{capabilities,plans,executor}.py` and
`routes/aria_plans.py`). Read [`FEASIBILITY.md`](FEASIBILITY.md) for the rest of
the platform.

## The honest boundary

Aria's autonomous reach is **exactly its capability registry** — today that is
**evaluation & governance scaffolding**. It can create judges, test sets, and
review queues, enqueue traces, and trigger a workflow calibration. It **cannot**
yet autonomously author prompts, skills, tools, or build workflows/KBs (those are
the manual Build scenarios 01–11). The four Aria scenarios are designed to live
**entirely inside** the real registry.

## ⚠️ Execution status — what works today vs full autonomy (verified)

There is one important gap between "Aria plans from intent" and "Aria executes
hands-off", and the four scenarios are written around it honestly:

- ✅ **Planning from intent works.** `POST /aria/plans {goal}` decomposes the
  intent into the right capability **steps** (the default `HeuristicPlanner` is
  deterministic and registry-driven). This is the autonomous-orchestration
  showcase and it is real + demoable.
- ⚠️ **The shipped planner does not fill step _inputs_.** `HeuristicPlanner`
  emits each step with `inputs={}` (it proposes *which* capability, not its
  arguments). The interaction-answer body is only `{approved, choice, value}`
  (`extra="forbid"`) and `answer()` records it on the interaction but **never
  merges it into `step.inputs`**; there is no PATCH/UI surface to inject inputs
  either. So an auto-run `judge.create` would call its handler with `{}` →
  validation error. **Filling step inputs is the job of an LLM-backed planner**
  (the `Planner` Protocol slot exists; it is **not wired** in this build).

So, to actually create the artifacts, use one of:

1. **Operator-assisted (works today):** let Aria decompose the intent + show the
   plan, then create each planned capability via **its own route** using the
   spec in this scenario's `assets/` — `POST /judges`, `POST /review-queues`,
   `POST /eval-datasets`, `POST /review-queues/{id}/items`,
   `POST /workflow-calibration/...`. *Aria plans; you (or a one-line script)
   run the creates.* The `assets/` specs are exactly those create payloads.
2. **Full hands-off autonomy:** wire an LLM planner that populates
   `PlannedStep.inputs` (or extend the interaction answer to carry per-step
   inputs). Until then, treat "execute the plan" as path (1).

Every Aria scenario's `assets/` therefore doubles as **capability create
payloads** (usable via the routes today) and as the **inputs an LLM planner
would fill** for true autonomy.

## Capability registry (the only things Aria can do)

| Capability | Tier | Required inputs | Notes |
| --- | --- | --- | --- |
| `judge.list` | read | — | list active judges |
| `judge.create` | mutate | `name`, `instructions` | instructions must reference ≥1 of `{{ inputs }}`/`{{ outputs }}`/`{{ expectations }}`; optional `description`, `model`, `feedback_value_type` (bool\|int\|float\|str), `tags` |
| `review_queue.list` | read | — | list active queues |
| `review_queue.create` | mutate | `name`, `questions` | question item: `{key, title, type∈[pass_fail,categorical,numeric,text], options[], required, target∈[feedback,expectation]}`; optional `description`, `reviewers` |
| `eval_dataset.create` | mutate | `name` | creates an **empty** versioned test set; example rows are added **separately** (not an Aria capability) |
| `review_queue.add_items` | mutate | `queue_id`, `trace_ids` | enqueues **existing** traces; optional `experiment_id`, `assigned_to` |
| `workflow.calibrate` | mutate · **async** | `workflow_id`, `agent_id` | enqueues a calibration job; the plan **parks** (`waiting_job`) → **poll** until done |

## How a plan is built (default planner)

The default **`HeuristicPlanner`** is deterministic and registry-driven: it
proposes a step for **every non-read capability whose domain word appears in the
goal**, where the domain is the key before the dot:

| Domain needle in the goal | Capabilities proposed |
| --- | --- |
| `judge` | `judge.create` |
| `eval dataset` (or `eval_dataset`) | `eval_dataset.create` |
| `review queue` | `review_queue.create` **and** `review_queue.add_items` |
| `workflow` | `workflow.calibrate` |

So craft the intent to contain the literal needles. Each scenario's
`assets/intent.md` gives a canonical (needle-bearing) intent and a
natural-language variant that would need an LLM planner. Step **inputs** are
partial after planning — they're confirmed/refined via **interactions**.

## Plan lifecycle + autonomy

```
POST /aria/plans {goal, autonomy}      -> draft (decomposed steps)
POST /aria/plans/{id}/approve          -> approved
POST /aria/plans/{id}/execute          -> runs until it pauses / completes / fails
GET  /aria/plans/{id}/interactions     -> pending interaction(s)
POST /aria/interactions/{iid}/answer {approved, inputs}  -> resume
POST /aria/plans/{id}/poll             -> advance an async (waiting_job) step
```

**Autonomy dial** (`gate_decision`): `read` always runs; **`gated` always
pauses** (the non-negotiable floor); `safe`/`mutate` **auto-run** under
`approve_plan`/`auto_guarded` but **pause under `ask_each`**. Use **`ask_each`**
in a demo so you confirm (or deny) every mutate step. Denying an interaction
marks that step **skipped** (e.g. skip `review_queue.add_items` when you have no
traces yet).

UI: sidebar **Plans** → `/aria/plans` (`AriaPlans.tsx`).

## The four Aria scenarios

| # | Folder | Intent decomposes to | Showcases |
| --- | --- | --- | --- |
| 12 | `12-aria-evaluation-harness` | `judge.create` + `eval_dataset.create` | judge + test set from one sentence |
| 13 | `13-aria-review-governance-queue` | `review_queue.create` (+ `add_items`) | a rich human-review label schema |
| 14 | `14-aria-governance-starter-kit` | `judge.create` + `eval_dataset.create` + `review_queue.create` (+ `add_items`) | **flagship** — a whole eval+governance scaffold in one plan |
| 15 | `15-aria-triage-recalibrate-loop` | `review_queue.create` + `add_items` + `workflow.calibrate` | the **async** capability + the triage→remediate loop (operates on existing ids; depends on SCN-07) |

Each scenario folder holds the usual 5 contract files plus an `assets/` with the
intent, the expected plan, and the concrete **interaction answers** (the
judge/queue/dataset/enqueue/calibrate payloads you paste when Aria pauses).
