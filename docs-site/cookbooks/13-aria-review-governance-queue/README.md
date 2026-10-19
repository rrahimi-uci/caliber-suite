# Aria: Human-Review Queue from Intent

> **Prerequisite flag:** `CALIBER_FEATURE_ARIA=true` (Aria and its **Plans** page are a preview area, off by default; the `/aria/*` routes answer 404 `feature_disabled` without it). `CALIBER_FEATURE_COOKBOOKS=true` is only needed for the Cookbooks gallery card. This lab is outside the supported alpha journey (`docs/start/alpha-scope.md`); the review queues it creates are part of it.

## What this lab demonstrates and the expected outcome

You give Aria one sentence, *"Set up a review queue for human labeling of agent replies for safety, citation, and tone."*, and it draws a plan of two platform steps: `review_queue.create` and `review_queue.add_items`. When you execute it, the first step stops on an **Aria needs information** form for the queue's name and its question schema, the second asks only for the trace ids (the queue's id is wired from the first step, so it is never asked), and, because you chose **Stop before each change**, each stops on an **Approve** / **Deny** prompt before it changes anything. At the end a real queue holds 30 flagged agent replies. A person then reviews 12 of them: each answer is validated against the queue's questions, written onto its trace as an assessment, and becomes a labelled example a judge can be checked against (lab 10).

**The planner is rules, not a model.** It proposes a step for every capability whose domain word appears in the goal, so the plan is deterministic and checked exactly against a dataset of 26 goals. No model is involved anywhere in this lab: a review queue is people's work, and the 12 reviews are the labels recorded in `assets/dataset/agent-replies.jsonl`. On a stack with a real provider ([Run it with a real provider](#run-it-with-a-real-provider)) the script also runs a real safety judge against those 12 labels and checks the Ask Aria chat.

You are done when:

- the script prints `All documented outcomes verified.` and exits 0;
- the planner drew the documented plan for all 26 goals in `assets/dataset/goals.jsonl` (26/26), none of them ran;
- the intent drew `review_queue.create` then `review_queue.add_items`; the enqueue step's only wired input is the queue's id; the enqueue form asked for `trace_ids` only; the plan reads `completed`; the queue is active with five questions and holds 30 traces, all pending and assigned;
- 12 reviews are in (12 completed, 18 pending), every answer is on its trace as an assessment with the reviewer's value, and the `safety` answers are 12 labelled examples (4 unsafe);
- the refusals happened: a review that does not fit the questions is refused (400) and the item stays pending, a reviewed item cannot be reviewed again (409), an account without the operator scope cannot review (403), an operator who is listed as a reviewer but does not own the queue gets 404, a trace id that does not exist is queued and fails when reviewed (502), skipping the queue skips the enqueue step with it, a denied enqueue keeps the queue, a form with no questions or an unknown field is refused (409) and stays open, a categorical question with no options fails the plan, a taken name fails the plan, a plan owned by an account without the operator scope fails at execute;
- the Playwright spec `caliber/caliber-ui/e2e/cookbooks/labs/13-aria-review-queue.spec.ts` passes.

### What is simulated, and what a real provider changes

| Piece | Simulated stack (default) | Real provider (verified, see [below](#run-it-with-a-real-provider)) |
| --- | --- | --- |
| Planning the goal into steps | The rules-based planner | **Unchanged**: no model is wired into planning; all 26 goals draw the same plans on a real stack (26/26, run live) |
| `review_queue.create`, `review_queue.add_items` | Real: a queue and its items are created in the database | Unchanged |
| The traces | Real MLflow traces, written by `assets/seed_traces.py`: a fresh checkout has no agent producing flagged replies | Your agent's traces; you copy the ids of the flagged ones |
| The reviews | The labels recorded in the dataset, submitted by the script | People answer the questions in **Review Queues** (unchanged on a real stack: the 12 reviews are still the dataset's) |
| Judge alignment | Only the labelled examples are checked here (`alignment-examples`); no judge runs | A real judge scores the 12 reviewed replies and is compared with the 12 `safety` labels: agreement 0.92, kappa 0.82 in the recorded run |
| **Ask Aria** (the chat) | Not used here; its engine is `fake` | A real engine answers and, in a `build` session with `auto_all` approval, creates a review queue itself (run live) |

## Prerequisites and setup

A CALIBER server per [`docs/start/alpha-install.md`](../../../docs/start/alpha-install.md) with the Aria flag on and an MLflow server (the stack has one). For a disposable one, `scripts/cookbook-env.sh` starts exactly that stack. From the repo root:

```bash
# olga: an operator who does NOT own the queue. vera (created by the script) must not be an operator.
export CALIBER_OPERATOR_USERS=admin,olga
scripts/cookbook-env.sh up lab13 --flags cookbooks,aria
source .tmp/cookbook-env/lab13/env.sh      # CALIBER_URL, CALIBER_BASE_URL, CALIBER_API, MLFLOW_TRACKING_URI, ...
```

Sign in at `$CALIBER_URL/caliber/` as `admin` / `admin`, then make an access token for the script (Settings > API tokens, or):

```bash
curl -s -c /tmp/lab13.jar -X POST "$CALIBER_API/auth/login" -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"admin"}' >/dev/null
export CALIBER_TOKEN=$(curl -s -b /tmp/lab13.jar -X POST "$CALIBER_API/auth/tokens" \
  -H 'content-type: application/json' -d '{"name":"lab13"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["token"])')
```

The default simulated providers are what this lab is written for; nothing in it calls a model. The 30 traces are written by `assets/seed_traces.py` through the `mlflow` package in the dev virtualenv, against `MLFLOW_TRACKING_URI` (set by `env.sh`); the script and the Playwright spec both run it.

## Dataset and sample files

All under `docs-site/cookbooks/13-aria-review-governance-queue/assets/`.

| File | What it is | Why |
| --- | --- | --- |
| `dataset/goals.jsonl` | 26 goals, one JSON object per line: `{id, goal, expected_steps, outcome, note}` | The planner is checked against it. `outcome` says how the plan relates to what the sentence asked: `as_asked` (15), `unasked` (5), `missed` (1), `nothing` (5, no steps) |
| `dataset/agent-replies.jsonl` | 30 flagged agent replies: `{id, flag, tags, user_message, reply, review: {safety, severity?, citation_ok, tone_ok, reviewer_notes?}}` | What goes into the queue, and the labels a reviewer gives. `flag` is why the reply was queued (`thumbs_down`, `low_judge_score`, `keyword:medication`, `pii_detected`, ...). 12 clean replies; 7 unsafe (medical advice, a legal claim, an address disclosed, a jailbreak followed, a smoking charger called normal, a refund that was never issued, an open campfire); 10 with a wrong citation (a section that does not exist, 35 USD for 50); 6 with bad tone |
| `seed_traces.py` | Writes the 30 replies to MLflow as traces and prints `{row id: trace id}` | A fresh checkout has no agent. Every run writes 30 new traces |
| `review-queues/agent-reply-review.queue.json` | The `review_queue.create` form values: `name`, `description`, `reviewers`, `questions` (a JSON array) | What you type into the queue's form |
| `enqueue.json` | The `review_queue.add_items` form values: `trace_ids`, `assigned_to` | What you type into the enqueue form |
| `intent.md`, `expected-plan.json` | The canonical sentence, and the plan it must draw | Sanity checks for the draft |

The queue's five questions: `safety` (pass/fail, required), `severity` (categorical `low`/`medium`/`high`, optional), `citation_ok` (pass/fail, required), `tone_ok` (pass/fail, required), `reviewer_notes` (text, optional). An unsafe reply carries a severity; a safe one does not.

A few goals worth reading, because they show where the rules stop (all verified):

| Goal | Plan | Why |
| --- | --- | --- |
| `Set up a review queue for human labeling of agent replies for safety, citation, and tone.` | create, add traces | The canonical one. **A review queue always plans both steps**: skip the second if you have no traces yet |
| `Set up a review-queue for tone checks.` | nothing | A hyphen is not a space (and `reviewqueue` is not two words either) |
| `Set up human review for the agent's replies.` | nothing | No domain word |
| `Open a queue so people can double check the judge's verdicts.` | judge | A lone `queue` is not `review queue`, and `judge` plans a judge |
| `Do not create a review queue, just tell me how they work.` | create, add traces | Negation is not read |
| `Can you remove the review queue we made yesterday?` | create, add traces | There is no delete capability, and the words still plan a create |
| `Create a review queue, then calibrate the support workflow with what the reviewers find.` | create, add traces, calibrate | `workflow` plans `workflow.calibrate` (lab 15) |

That is why you read the plan before you approve it: nothing runs until you do.

## Configuration and environment

| Variable | Value used | What it does |
| --- | --- | --- |
| `CALIBER_FEATURE_ARIA` | `true` (set by `--flags aria`) | Opens `/aria/*`, `/assistant/*` and the **Plans** page (404 `feature_disabled` without it) |
| `CALIBER_FEATURE_COOKBOOKS` | `true` (set by `--flags cookbooks`) | Only the gallery card; the lab does not need it |
| `CALIBER_OPERATOR_USERS` | `admin,olga` | Accounts holding `caliber.operator`: the scope the queue steps and the review route need. `vera` is deliberately absent |
| `MLFLOW_TRACKING_URI` | from `env.sh` | Where the script writes the 30 traces and CALIBER reads them |
| `CALIBER_BASE_URL` | `$CALIBER_URL`, the server root without `/ajax-api/...` | What the script's SDK client calls |
| `CALIBER_TOKEN` | the admin token above | The script acts as `admin` (the plan's owner and the reviewer) and signs in as `olga` and `vera` itself |
| `COOKBOOK_ARIA_PASSWORD` | optional | Password for the `olga` and `vera` accounts the Aria labs create (default `cookbook-aria-lab-passphrase`) |
| `COOKBOOK_13_ASSETS` | optional | Another assets directory |
| `OPENAI_API_KEY` | only with `--real-llm`; export it yourself | The provider key (never written anywhere by the lab) |
| `COOKBOOK_ARIA_HTTP_TIMEOUT`, `COOKBOOK_13_MIN_AGREEMENT`, `COOKBOOK_13_MIN_KAPPA` | optional (900, 0.75, 0.4) | One request's timeout in seconds, and the bars for the real judge's agreement and kappa with the reviewers |

## Run it through the API

One command runs the whole lab and verifies every outcome:

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_13_aria_review_queue
```

Real output from a run (ids and the time stamp vary):

```text
ok   the 30 flagged replies are in MLflow as traces, one per row  [30 traces]
ok   every reply says why it was flagged  [['escalation_ignored', 'jailbreak_pattern', 'keyword:lawsuit', 'keyword:medication', 'keyword:refund', 'latency_over_8s', 'low_judge_score', 'pii_detected', 'thumbs_down']]
ok   the planner drew the documented plan for every goal  [26/26 {'as_asked': 15, 'missed': 1, 'nothing': 5, 'unasked': 5}]
ok   a goal that drew no steps cannot be approved (409) and says why  [5 goals]
ok   every draft was cancelled; nothing ran  [26 drafts]
ok   the intent draws review_queue.create then review_queue.add_items  [PLAN-1b692ea7]
ok   the enqueue step's queue_id is wired from the create step, nothing else is
ok   the enqueue form asks for trace ids only: the queue's id is already known  [['trace_ids']]
ok   the plan completed with both steps done  [completed]
ok   the enqueue step reports 30 traces added to that queue  [{'queue_id': 'RVQ-9254dc48', 'added': 30}]
ok   the queue is active with the typed name, five questions and its reviewers  [RVQ-9254dc48 cookbook-13-agent-reply-review-20261018-145344]
ok   it holds the 30 traces, all pending and assigned  [30 items, 30 pending]
ok   twelve reviews are in: 12 completed, 18 still pending  [12 completed, 18 pending]
ok   every answer is on its trace as an assessment, with the reviewer's value  [12 traces]
ok   the safety answers are 12 labelled examples a judge can be checked against (lab 10)  [4 unsafe of 12]
ok   a review that does not fit the questions is refused (400) and names why  [{'a missing required answer': "missing required answer 'citation_ok'", 'an answer of the wrong type': "answer 'safety' must be a boolean", 'an option the question does not have': "answer 'severity' must be one of ['low', 'medium', 'high']", 'an answer to a question that does not exist': "unknown answer keys: ['mood']"}]
ok   after four refusals the item is still pending
ok   a reviewed item cannot be reviewed again (409)  [review item 'RVI-15e93aac' is already completed]
ok   an account without the operator scope cannot review (403)
ok   an operator the queue lists as a reviewer still cannot open or review it (404): a personal queue belongs to its owner  [reviewers: olga]
ok   a trace id that does not exist is queued without complaint, and fails when reviewed (502); the item stays pending  [failed writing review answers to trace: RESOURCE_DOES_NOT_EXIST: Trace]
ok   skipping the queue skips the enqueue step with it, and the plan completes  [skipped because step 1 (review_queue.create) was skipped]
ok   denying the enqueue leaves the queue created and empty; the plan completes
ok   the form refuses no questions, an unknown field, an untitled question and a made-up question type (409) and stays open  [{'no questions': 'questions must contain at least 1 item(s)', 'an unknown field': 'review_queue.create received unknown input field(s): colour', 'a question without a title': 'questions[0] is missing required field(s): title', 'a question type that does not exist': "questions[0].type must be one of ['pass_fail', 'categorical', 'numeric', 'text']"}]
ok   a categorical question with no options passes the form and fails the step, naming it  [invalid input: questions.0: categorical questions require a non-empty 'options' list]
ok   a queue name that is already taken fails its step  [ValueError: review-queue name 'cookbook-13-agent-reply-review-20261018-145344' is already in use]
ok   a failed plan leaves the steps after it pending, and cannot be run again (409)
ok   a goal with no steps is created with a warning and refused at approval (409)
ok   a plan owned by an account without the operator scope fails at execute and creates nothing  [plan owner lacks required scope(s): operator]
ok   an operator who does not own the plan cannot answer its question (403)  [only the plan owner may answer this interaction]

All documented outcomes verified.
```

The script then prints a JSON summary (`plan_id`, `queue_id`, `review: {reviewed: 12, pending: 18, unsafe_in_reviewed: 4}`, the suffix). Every artifact it creates is named `cookbook-13-...-<suffix>`.

### What each step does, by hand

The script is plain SDK calls; the same steps with `curl` are the routes below (all under `$CALIBER_API`, signed in as above):

| Step | Call |
| --- | --- |
| Draw the plan | `POST /aria/plans {"goal", "autonomy": "ask_each"}` answers `201` with `plan` and `steps`. The enqueue step's `inputs` is `{"queue_id": {"$from_step": "<the create step's id>", "path": "queue_id"}}` and it `depends_on` the create step |
| Approve, execute | `POST /aria/plans/{id}/approve`, then `POST /aria/plans/{id}/execute`: the plan is `paused` on the queue's `input` question |
| The queue's form | `POST /aria/interactions/{id}/answer {"inputs": {"name", "description", "reviewers", "questions": [...]}}`, then `{"approved": true}` for the approval |
| The enqueue form | `GET /aria/plans/{id}/interactions`: `evidence.missing` is `["trace_ids"]` and `evidence.current_inputs.queue_id` is the wired reference. Answer `{"inputs": {"trace_ids": [...], "assigned_to": "admin"}}`, then `{"approved": true}` |
| See the queue | `GET /review-queues/{queue_id}` answers `{queue, items}`: `queue.item_count`, `queue.pending_count`, one item per trace |
| Review an item | `POST /review-queues/{queue_id}/items/{item_id}/submit {"answers": {"safety": false, "severity": "high", "citation_ok": true, "tone_ok": false, "reviewer_notes": "..."}}` answers the completed item with its `assessment_ids` |
| Read it on the trace | `GET /observability/traces/{trace_id}`: `assessments` lists one entry per answer, `source` the reviewer |
| Labelled examples | `GET /review-queues/{queue_id}/alignment-examples?question_key=safety`: one `{label, outputs, expectations, provenance}` per completed item |

### Failure drills

The script provokes these on purpose; the same ones by hand:

| Do this | You see |
| --- | --- |
| Submit a review with a required answer missing, `safety` as `"yes"`, `severity` as `"catastrophic"` or an answer for a key the queue does not have | `400 missing required answer 'citation_ok'` / `answer 'safety' must be a boolean` / `answer 'severity' must be one of ['low', 'medium', 'high']` / `unknown answer keys: ['mood']`; the item stays `pending` |
| Submit a review for an item that is already completed | `409 review item '...' is already completed` |
| Submit as an account without the operator scope (`vera`) | `403 missing required scope; expected one of ['caliber.operator'], have ['caliber.viewer']` |
| Open or review the queue as `olga`, who is in the queue's `reviewers` | `404 review queue '...' not found`: a queue created outside a workspace belongs to its owner; the `reviewers` list is a label, not access. To let a team review it, create it inside a workspace (lab 18) |
| Enqueue a trace id that does not exist (`tr-this-trace-does-not-exist`) | `201`, the item is `pending`: the enqueue does not check the trace. Reviewing it answers `502 failed writing review answers to trace: RESOURCE_DOES_NOT_EXIST: Trace with ID '...' not found` and the item stays `pending` |
| Skip the queue's form | the queue step reads `skipped` and so does the enqueue step: `skipped because step 1 (review_queue.create) was skipped`; the plan is `completed` and nothing is created |
| Approve the queue, **Deny** the enqueue | the queue exists with 0 items; the enqueue step reads `skipped`; the plan is `completed` |
| Answer the queue's form with `"questions": []` / an unknown field / a question with no `title` / `"type": "likert"` | `409 questions must contain at least 1 item(s)` / `review_queue.create received unknown input field(s): colour` / `questions[0] is missing required field(s): title` / `questions[0].type must be one of ['pass_fail', 'categorical', 'numeric', 'text']`; the question stays open |
| A categorical question with no `options` | the form accepts it, the step fails: plan `failed`, step error `invalid input: questions.0: categorical questions require a non-empty 'options' list`; the enqueue step stays `pending` and a failed plan cannot be run again (`409`) |
| A queue name that already exists | step error `ValueError: review-queue name '...' is already in use`, plan `failed` |
| A goal with no domain word | `201` with `step_count` 0 and a `warnings` entry; approving it is `409` |
| Run a plan as an account without the operator scope (`vera`) | `execute` returns the plan `failed` at once with the step error `plan owner lacks required scope(s): operator`; nothing is created |
| Answer another account's question (`olga` answering the admin's) | `403 only the plan owner may answer this interaction` |
| Aria switched off | every `/aria/*` call: `404 {"detail": "Aria assistant and plans is disabled in this deployment. Set CALIBER_FEATURE_ARIA=true to enable it.", "reason_code": "feature_disabled"}`, and **Plans** is not in the sidebar |

## Recipe: run it through the UI

The Playwright spec `caliber/caliber-ui/e2e/cookbooks/labs/13-aria-review-queue.spec.ts` performs exactly these steps with real clicks (sign in as `admin` / `admin`). It first runs `assets/seed_traces.py` to write the 30 traces (set `CALIBER_LAB_PYTHON` to another interpreter that has `mlflow`). Run it against the stack above:

```bash
cd caliber/caliber-ui
CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/13-aria-review-queue.spec.ts --output=/tmp/lab13-pw
```

For your own run, write the traces first and keep the ids: `MLFLOW_TRACKING_URI=$MLFLOW_TRACKING_URI caliber/.venv/bin/python docs-site/cookbooks/13-aria-review-governance-queue/assets/seed_traces.py > /tmp/traces.json` (they also appear in **Operate > Observability**).

1. **State the intent.** In the sidebar open **Integrations** and click **Plans**. Paste the sentence from [`assets/intent.md`](assets/intent.md) into **Goal**, set **Autonomy** to **Stop before each change**, and press **Decompose goal**. Expected: the plan opens with the status `draft`.
2. **Review the plan.** Under **Plan steps**: `Create review queue` (`review_queue.create`) then `Enqueue traces for review` (`review_queue.add_items`), the second reading `depends on 1 prior step(s)`. Nothing has run.
3. **Approve the plan.** Press **Approve plan**. Expected: the status becomes `approved` and an **Execute** button appears.
4. **Execute and answer the queue's questions.** Press **Execute**. Expected: **Aria needs information**, "Provide the required inputs for step 1: Create review queue.". Fill **name** (`agent-reply-review`), **description**, **questions** (the JSON array from `assets/review-queues/agent-reply-review.queue.json`) and **reviewers** (`["olga"]`), press **Continue plan**. Expected: **Aria needs your approval**, "Approve step 1: Create review queue (review_queue.create)?". Press **Approve**.
5. **Answer the enqueue step.** Expected: "Provide the required inputs for step 2: Enqueue traces for review." with **no** queue id field (it is already known). Fill **trace ids** with a JSON array of trace ids (from `traces.json`, or the first five), **assigned to** with `admin`, press **Continue plan**, then **Approve** the prompt "Approve step 2: Enqueue traces for review (review_queue.add_items)?". Expected: the plan reads `completed` and both steps `done`.
6. **Find the queue.** Open **Operate > Review Queues**. Expected: a row `agent-reply-review` with 5 questions, the reviewers `olga`, and `0/5 reviewed`.
7. **Review one trace.** Open the queue. Expected: "Items (5)" listing the trace ids, the first one selected. Answer the questions (**Fail** for safety, `high` for severity, **Pass** for citation, **Fail** for tone, a note), press **Submit review**. Expected: that trace now reads `completed` in the list and the form moves to the next one; on the queue list the row reads `1/5 reviewed`.
8. **A review that does not fit.** On another item answer only the safety question and press **Submit review**. Expected: the red message `missing required answer 'citation_ok'`; the item stays `pending`.
9. **Skip the queue.** On **Plans**, state the sentence again, **Decompose goal**, **Approve plan**, **Execute**, and press **Skip step** on the queue's form. Expected: the plan reads `completed` with both steps `skipped`, the second saying `skipped because step 1 (review_queue.create) was skipped`.
10. **A question schema the queue refuses.** State the sentence again, approve and execute, fill **name** with a new name and **questions** with `[{"key": "tone", "title": "Tone?", "type": "categorical"}]`, press **Continue plan**. Expected: the plan reads `failed` and the step shows `invalid input: questions.0: categorical questions require a non-empty 'options' list`.

## Verify it worked

API checks (the script does all of them; these are the ones to read by eye):

- `GET /aria/plans/{plan_id}` has `plan.status: "completed"` and both steps `done`; `steps[0].result` is `{queue_id, name, questions: 5}` and `steps[1].result` is `{queue_id, added: 30}`.
- `GET /review-queues/{queue_id}` has `queue.status: "active"`, `queue.item_count: 30`, `queue.pending_count: 18` after the 12 reviews, and each reviewed item has `status: "completed"`, its `answers` and one `assessment_ids` entry per answer.
- `GET /observability/traces/{trace_id}` for a reviewed trace has the reviewer's answers in `assessments`.
- `GET /review-queues/{queue_id}/alignment-examples?question_key=safety` lists 12 examples; 4 have `label: false`.
- `GET /aria/plans/{failed plan}` has `status: "failed"` and the failed step's `error` is the text in the drills above.

UI checks: steps 1 to 10 above, each with the text or status it names.

## Run it with a real provider

A real provider adds two checks to this lab: a **judge** can be run against the human labels the lab collects (the human-versus-judge comparison), and the **Ask Aria** chat can act. Planning, the queue, the enqueue step and the 12 reviews are exactly as above. The numbers below are from live runs and **will vary**.

### Start a real stack

```bash
export OPENAI_API_KEY=...            # your own key. Never commit it; the lab never prints or stores it
export CALIBER_OPERATOR_USERS=admin,olga
scripts/cookbook-env.sh up lab13h --flags cookbooks,aria --real-llm
source .tmp/cookbook-env/lab13h/env.sh
```

`--real-llm` sets `CALIBER_LLM_PROVIDER=openai`, `CALIBER_EVAL_PROVIDER=mlflow` and `CALIBER_ASSISTANT_ENGINE=openai` (default model `gpt-5.6-luna`) and reads only `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` from your environment or the repository-root `.env`. `GET /readiness` then reports `"simulated": []`. Make the access token as in [Prerequisites and setup](#prerequisites-and-setup) and run the same command as above; the script reads `GET /readiness` and adds these checks on a real stack. It takes about a minute more and about 14 model calls (12 verdicts and two chat turns).

### Judge versus the reviewers

After the 12 reviews the script creates a judge from `assets/judges/reply-safety.judge.json` (`ReplySafety`, a boolean judge that returns true only for a reply that is safe to send), imports the 12 `safety` labels with `alignment-examples` and sends them to `POST /judges/{id}/alignment`. The judge sees each trace's request and the reply, and is compared with the reviewer's label.

```text
ok   the real judge scored all 12 reviewed replies (no row errored)  [n=12 scored=12]
ok   the server's numbers are arithmetic over the labels: agreement and the confusion counts add up  [{'true_pos': 7, 'false_pos': 0, 'true_neg': 4, 'false_neg': 1}]
ok   the judge agrees with the reviewers: agreement >= 75%, kappa >= 0.4 (a judge that says one thing about everything has kappa 0)  [agreement 0.9167, kappa 0.8235]
ok   the judge flags at least half of the replies the reviewers called unsafe  [4 of 4]
```

Asserted: all 12 rows scored, agreement and the confusion counts consistent with the labels, agreement at least 0.75 (`COOKBOOK_13_MIN_AGREEMENT`), kappa at least 0.4 (`COOKBOOK_13_MIN_KAPPA`), and at least half of the replies the humans called unsafe flagged. Never asserted: an exact agreement or which reply is wrong. In both recorded runs the judge caught all four unsafe replies and passed one reply the reviewer failed (a false negative); expect a row or two of movement. Read kappa, not agreement: with 8 safe and 4 unsafe replies a judge that always said "safe" would agree 67% of the time and have kappa 0. Lab 10 is the full certification (a stricter and a lenient judge, thresholds, a rationale-leak trap).

### What the real engine does, and does not, change in Aria

- **Plans are unchanged.** `POST /aria/plans` uses the rules-based planner whatever the assistant engine is; the 26 goals draw the same plans on the real stack, and **Plans** never calls the engine.
- **The chat acts only inside its permissions.** In a `build` session with approval `auto_all` (UI label **Legacy auto**) or `full_autonomy`, Ask Aria's tools include Aria's mutating capabilities, and asked to create a queue the model called `review_queue_create` and the queue exists (active, with the one question asked for). In the default session (approval **Ask first**) the model is offered read tools only: it called `review_queue_list`, `openapi_tool_list` and `list_tools`, could not create the queue, and nothing was created (run live). Approving and publishing are never offered to the chat.

```text
ok   the assistant engine is a real one, not the fake heuristic  [engine=openai model=gpt-5.6-luna]
ok   Ask Aria in a build / auto_all session called review_queue_create itself and it worked  [tools called: ['review_queue_create']]
ok   in the default session (manual approval) review_queue_create is not offered: nothing was created  [tools called: ['review_queue_list', 'openapi_tool_list', 'list_tools']]
ok   the chat's queue is active with the question that was asked for  [RVQ-584d1a75]
```

### Through the UI (real stack)

`13-aria-review-queue.spec.ts` checks `GET /readiness`; on a real stack a fifth test opens **Ask Aria**, sends "Create a review queue named ... with one pass_fail question ..." in a default chat (no queue appears in **Review Queues**), then again after choosing **Legacy auto** in the approval selector (the queue appears). The five tests passed against a real stack in about a minute. The judge comparison is an API check here; in the UI you can repeat it after the queue holds reviews: **Agent resources > Judges**, open the judge, **Human alignment**, *Import from Review Queue*, pick the queue and the `safety` question, **Import labels**, **Check alignment**.

### Real-provider troubleshooting

| You see | Cause and fix |
| --- | --- |
| The alignment report has `scored` below 12 or rows with an error | The provider rejected or rate-limited some calls. A rejected key shows up as `AuthenticationError` / `401 Incorrect API key provided` in the row error (provoked with `OPENAI_API_KEY=sk-invalid`; `/readiness` still says `ready: true`, because it does not call the model). Fix the key and restart the stack |
| Ask Aria answers "I encountered an error: AuthenticationError" | The same rejected key, in the chat |
| `VERIFICATION FAILED: the judge agrees with the reviewers` | The judge disagreed with the people more than the bars allow: read `per_example` in the report. Loosen the bars for a noisy judge with `COOKBOOK_13_MIN_AGREEMENT` / `COOKBOOK_13_MIN_KAPPA`, or sharpen the judge's instructions |
| `ReadTimeout` | One request outlived `COOKBOOK_ARIA_HTTP_TIMEOUT` (default 900 s); raise it. A provider 429 and an unknown model were not provoked in this lab |

## Common errors and troubleshooting

| You see | Cause and fix |
| --- | --- |
| `404 {"reason_code": "feature_disabled"}` on `/aria/...`; no **Plans** in the sidebar | Start the server with `--flags aria` (`CALIBER_FEATURE_ARIA=true`) and restart |
| `SetupError: Aria is off: start the server with CALIBER_FEATURE_ARIA=true` | The same, reported by the script |
| `SetupError: start CALIBER with CALIBER_OPERATOR_USERS=admin,olga ...` | The server was started without the export in Prerequisites; restart it with it. `vera` must not be listed |
| `could not sign in as 'olga': 401` | An account from an earlier run has another password: reset it under Administration, or set `COOKBOOK_ARIA_PASSWORD` |
| `ModuleNotFoundError: mlflow` or the traces cannot be written | Run the script with the dev virtualenv (`caliber/.venv/bin/python`) and make sure `MLFLOW_TRACKING_URI` is set (`source .tmp/cookbook-env/lab13/env.sh`) |
| **Decompose goal** shows an amber note and opens no plan | The sentence has no domain word (`review queue`, `judge`, `eval dataset`, `test set`, `workflow`). Reword it; see the goals table |
| The plan has a step you did not ask for | The planner matched a word inside another, ignored a negation, or read a question as a request; and a review queue always brings its enqueue step. **Cancel** the draft or **Deny** / **Skip step** that step |
| `409 ... must contain at least 1 item(s)` / `... is missing required field(s)` / `... must be one of` | The form's answer does not fit the capability's schema; the question stays open, answer it again |
| The plan reads `failed` with `invalid input: ...` or `ValueError: ... is already in use` | The step ran and the queue refused it. A failed plan cannot be run again: state the goal again with a new name or a fixed schema |
| `400 missing required answer ...` / `must be a boolean` / `must be one of` when reviewing | The review does not fit the queue's questions; fix the answer and submit again, the item is still pending |
| `502 failed writing review answers to trace: RESOURCE_DOES_NOT_EXIST` | The trace id queued does not exist in MLflow (the enqueue does not check). The item stays pending; it cannot be completed, so queue the right ids |
| `404 review queue ... not found` for another account | A queue created outside a workspace is visible to its owner only, whatever its `reviewers` say |
| `plan owner lacks required scope(s): operator` | The account that owns the plan is not in `CALIBER_OPERATOR_USERS`. Plans run with their owner's scopes |
| The queue's name is taken | Names are unique across the deployment, archived ones included; the script adds a time stamp |

## Not covered by this lab

A full judge certification (thresholds, a lenient judge, a rationale-leak check) is lab 10; here the real judge is compared with 12 reviews once. Reviews by several people need a workspace, which lab 18 sets up; here the plan's owner reviews. The enqueue step does not discover traces and does not check that the ids exist. The 12 hand reviews are the dataset's labels submitted by the script, not a measurement of anything. Aria does not edit or delete a queue (only create it and add traces): a changed question schema is `PATCH /review-queues/{id}` or **Review Queues**.

## Cleanup and reset

```bash
# archive the review queues and judges the script created (names cookbook-13-*)
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_13_aria_review_queue --cleanup
# reset everything: stop the stack and delete its state
scripts/cookbook-env.sh down lab13 && rm -rf .tmp/cookbook-env/lab13
```

Plans cannot be deleted: the script cancels every draft it makes, and plans that ran stay in the **Plans** list. The traces stay in MLflow (30 per run). `olga` and `vera` stay as accounts; disable or reset them under Administration. The UI spec leaves its `c13*` queues in the disposable stack.
