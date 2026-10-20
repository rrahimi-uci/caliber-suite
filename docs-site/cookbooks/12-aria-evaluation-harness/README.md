# Aria: Evaluation Harness from Intent

> **Prerequisite flag:** `CALIBER_FEATURE_ARIA=true` (Aria and its **Plans** page are a preview area, off by default; the `/aria/*` routes answer 404 `feature_disabled` without it). `CALIBER_FEATURE_COOKBOOKS=true` is only needed for the Cookbooks gallery card. This lab is outside the supported alpha journey (`docs/start/alpha-scope.md`).

## What this lab demonstrates and the expected outcome

You give Aria one sentence, *"Create a judge for answer faithfulness and an eval dataset to run it on."*, and it draws a plan of two platform steps, `judge.create` and `eval_dataset.create`. Nothing runs until you approve the plan. When you execute it, each step stops on an **Aria needs information** form for the fields the planner cannot know (the judge's name and instructions, the test set's name), and, because you chose **Stop before each change**, on an **Approve** / **Deny** prompt before it creates anything. At the end a real judge and a real, empty test set exist, you load 30 support questions into the test set, and you see what Aria refuses: a sentence with no steps, a form answer that does not fit, a judge that cannot be created, an account that is not allowed.

**The planner is rules, not a model.** It proposes a step for every capability whose domain word appears in the goal, so the whole lab is deterministic and the plan is checked exactly against a dataset of 28 goals. There is no language model in the loop for planning, and the creation steps need no model either. The one thing that does need a real model, running the judge over the test set in **Evaluations**, is refused on a simulated deployment (`400 evaluation requires a real LLM provider`), and the lab verifies that refusal instead of a score. On a stack with a real provider ([Run it with a real provider](#run-it-with-a-real-provider)) the same script scores the 30 rows with the judge instead, and checks the Ask Aria chat.

You are done when:

- the script prints `All documented outcomes verified.` and exits 0;
- the planner drew the documented plan for all 28 goals in `assets/dataset/goals.jsonl` (28/28), and none of those drafts ran;
- the intent drew `judge.create` then `eval_dataset.create`, you answered four questions (judge form, approval, test set form, approval), the plan reads `completed`, the judge is active as `bool`, and the test set has its own name (not the judge's), version 1 and no rows;
- the test set holds the 30 cases from `assets/dataset/faithfulness-cases.jsonl`, one version per row (version 31), and each stored row is the file's row;
- the refusals happened: a goal with no steps cannot be approved (409), a form refuses an unknown field, a wrong type, an empty name and a missing field (409) and stays open, a denied step is skipped while the other still runs, a judge without an evaluation variable fails its step and the plan, a taken name fails its step, an account without the operator scope fails at execute and creates nothing, and an operator who does not own the plan is refused (403) and cannot see it (404);
- the Playwright spec `caliber/caliber-ui/e2e/cookbooks/labs/12-aria-evaluation-harness.spec.ts` passes.

### What is simulated, and what a real provider changes

| Piece | Simulated stack (default) | Real provider (verified, see [below](#run-it-with-a-real-provider)) |
| --- | --- | --- |
| Planning the goal into steps | The rules-based planner | **Unchanged.** The assistant engine does not plan: all 28 goals draw the same plans on a real stack (28/28, run live) |
| `judge.create`, `eval_dataset.create` | Real: a judge and a test set are created in the database | Unchanged |
| The judge's verdicts | Not produced: **Evaluations** is refused (400), so the judge never runs | **Evaluations** scores each of the 30 rows with the judge (`Judge.<id>`); run live, 30 of 30 rows judged |
| Test set rows | Real: 30 rows loaded by **Import from file** in the UI (or `POST /eval-datasets/{id}/examples` from the script), one edited for a new version | Unchanged |
| **Ask Aria** (the chat) | Not used here; its engine is `fake` (a deterministic heuristic, `GET /assistant/config`) | A real engine answers and, in a `build` session with `auto_all` approval, calls Aria's tools itself (it created a judge, run live) |

## Prerequisites and setup

A CALIBER server per [`docs/start/alpha-install.md`](../../../docs/start/alpha-install.md) with the Aria flag on. For a disposable one, `scripts/cookbook-env.sh` starts exactly that stack (migrate, MLflow, CALIBER, simulated providers). From the repo root:

```bash
# olga: an operator who does NOT own the plan. vera (created by the script) must not be an operator.
export CALIBER_OPERATOR_USERS=admin,olga
scripts/cookbook-env.sh up lab12 --flags cookbooks,aria
source .tmp/cookbook-env/lab12/env.sh      # CALIBER_URL, CALIBER_BASE_URL, CALIBER_API, ...
```

Sign in at `$CALIBER_URL/caliber/` as `admin` / `admin`, then make an access token for the script (Settings > API tokens, or):

```bash
curl -s -c /tmp/lab12.jar -X POST "$CALIBER_API/auth/login" -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"admin"}' >/dev/null
export CALIBER_TOKEN=$(curl -s -b /tmp/lab12.jar -X POST "$CALIBER_API/auth/tokens" \
  -H 'content-type: application/json' -d '{"name":"lab12"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["token"])')
```

The default simulated providers (`CALIBER_LLM_PROVIDER=fake`, `CALIBER_EVAL_PROVIDER=fake`) are what this lab is written for. The Playwright spec needs the Chromium that `npx playwright install chromium` provides (already installed in the dev checkout).

## Dataset and sample files

All under `docs-site/cookbooks/12-aria-evaluation-harness/assets/`.

| File | What it is | Why |
| --- | --- | --- |
| `dataset/goals.jsonl` | 28 goals, one JSON object per line: `{id, goal, expected_steps, outcome, note}` | The planner is checked against it. `outcome` says how the plan relates to what the sentence asked: `as_asked` (18), `unasked` (5, a step you did not ask for), `missed` (1, a step you asked for is missing), `nothing` (4, no steps) |
| `dataset/faithfulness-cases.jsonl` | 30 support cases: `{id, tags, input: {question, evidence}, expected: {answer}}` | The rows loaded into the test set. Refunds, shipping, account, billing, warranty and safety questions, with the policy text each answer must stay inside, and hard cases: a gift card that is not refundable, a window that has passed (`F02`), a country not served, evidence that does not cover the question (`F13`), a partly covered question (`F07`), medical advice the support team must refuse (`F30`) |
| `judges/answer-faithfulness.judge.json` | The `judge.create` form values: `name`, `feedback_value_type`, `instructions` (one paragraph; the form's field is a multi-line box) | What you type into the judge form |
| `datasets/support-faithfulness.dataset.json` | The `eval_dataset.create` form values: `name`, `description`, `tags` | What you type into the test set form |
| `intent.md`, `expected-plan.json` | The canonical sentence, and the plan it must draw | Sanity checks for the draft |

A few goals worth reading, because they show where the rules stop (all verified):

| Goal | Plan | Why |
| --- | --- | --- |
| `Create a judge for answer faithfulness and an eval dataset to run it on.` | judge, test set | The canonical one |
| `Spin up a testset for the returns assistant.` | test set | `testset`, `test set`, `test-set`, `eval dataset` and `eval_dataset` all mean the test set step |
| `Set up faithfulness scoring for our support answers, with a test set to run it on.` | test set only | "faithfulness scoring" is not the word `judge` |
| `Add an evaluation dataset for the onboarding assistant.` | nothing | "evaluation dataset" is not `eval dataset` |
| `Do not create a judge, I only want a test set for the claims assistant.` | judge, test set | Negation is not read |
| `The reviewers disagree, so I will have to use my judgement before we ship.` | judge | `judge` is matched inside `judgement` |
| `What is a judge in CALIBER?` | judge | A question that contains the word still plans a step |
| `Create a judge and an eval dataset, and also a review queue for the cases the judge fails.` | judge, queue, test set, add traces | A review queue always plans its `add_items` step too (skip it) |

That is why you read the plan before you approve it: nothing runs until you do.

## Configuration and environment

| Variable | Value used | What it does |
| --- | --- | --- |
| `CALIBER_FEATURE_ARIA` | `true` (set by `--flags aria`) | Opens `/aria/*`, `/assistant/*` and the **Plans** page (404 `feature_disabled` without it) |
| `CALIBER_FEATURE_COOKBOOKS` | `true` (set by `--flags cookbooks`) | Only the gallery card; the lab does not need it |
| `CALIBER_OPERATOR_USERS` | `admin,olga` | Accounts holding `caliber.operator`: the scope `judge.create` and `eval_dataset.create` declare. `vera` is deliberately absent |
| `CALIBER_LLM_PROVIDER`, `CALIBER_EVAL_PROVIDER` | `fake` (the default); `openai` and `mlflow` with `--real-llm` | Simulated: **Evaluations** is refused. Real: the judge runs |
| `OPENAI_API_KEY` | only with `--real-llm`; export it yourself | The provider key (never written anywhere by the lab) |
| `COOKBOOK_ARIA_HTTP_TIMEOUT`, `COOKBOOK_12_MIN_GAP` | optional (900, 0.2) | One request's timeout in seconds, and the bar for the grounded-vs-careless gap on a real stack |
| `CALIBER_BASE_URL` | `$CALIBER_URL`, the server root without `/ajax-api/...` | What the script's SDK client calls |
| `CALIBER_TOKEN` | the admin token above | The script acts as `admin` and signs in as `olga` and `vera` itself |
| `COOKBOOK_ARIA_PASSWORD` | optional | Password for the `olga` and `vera` accounts the script creates (default `cookbook-aria-lab-passphrase`) |
| `COOKBOOK_12_ASSETS` | optional | Another assets directory |

## Run it through the API

One command runs the whole lab and verifies every outcome:

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_12_aria_evaluation_harness
```

Real output from a run on a fresh stack (ids and the time stamp vary):

```text
ok   Aria lists its nine capabilities with their tiers  [9]
ok   the planner drew the documented plan for every goal  [28/28 {'as_asked': 18, 'missed': 1, 'nothing': 4, 'unasked': 5}]
ok   a goal that drew no steps cannot be approved (409) and says why  [4 goals]
ok   every draft was cancelled; nothing ran  [28 drafts]
ok   the intent draws judge.create then eval_dataset.create  [PLAN-3eb4070b]
ok   neither step has an input wired from the other (each asks for its own name)
ok   execute pauses on the judge step, waiting for input
ok   the plan asked for the judge's fields, an approval, the test set's fields and an approval
ok   the plan completed with both steps done  [completed]
ok   the judge is active with the typed name, type and instructions  [JDG-94528c02 cookbook-12-AnswerFaithfulness-20261018-144512]
ok   the test set has its own name (not the judge's), owner, tags, version 1 and no rows  [ED-60d06037 cookbook-12-support-faithfulness-eval-20261018-144512]
ok   the test set holds the 30 cases, one version per row  [30 rows, version 31]
ok   every stored row is the file's row (input, expected answer, tags)
ok   Evaluations are refused without a real model (400): the judge cannot run here  [simulated stack]
ok   a goal with no steps is created with a warning and refused at approval (409)
ok   a cancelled plan can be neither approved nor executed (409)
ok   the form refuses an unknown field, a wrong type, an empty name and a missing field (409)  [{'unknown field': 'judge.create received unknown input field(s): colour', 'wrong type': 'name must be string', 'empty name': 'name must contain at least 1 character(s)', 'missing field': 'judge.create is missing required input field(s): instructions'}]
ok   after four refusals the question is still pending
ok   Skip step marks the step skipped and the plan completes
ok   an answered question cannot be answered again (409)
ok   denying the judge skips it; the test set step still runs and the plan completes
ok   a judge with no evaluation variable fails its step and the plan, naming the field  [invalid input: instructions: instructions must reference at least one evaluation variable:]
ok   a failed plan cannot be run again (409): state the goal again
ok   a judge name that is already taken fails its step  [ValueError: judge name 'cookbook-12-AnswerFaithfulness-20261018-144512' is already in use]
ok   without the operator scope the judge route refuses the account (403)
ok   a plan owned by an account without the operator scope fails at execute, before it asks for anything, and creates nothing  [plan owner lacks required scope(s): operator]
ok   an operator who does not own the plan cannot answer its question (403)  [only the plan owner may answer this interaction]
ok   nor can they see it (404): plans are private to their owner

All documented outcomes verified.
```

The script then prints a JSON summary (`plan_id`, `judge_id`, `dataset_id`, `rows`, `evaluation: "refused (simulated provider)"`, the suffix). Every artifact it creates is named `cookbook-12-...-<suffix>`.

### What each step does, by hand

The script is plain SDK calls; the same steps with `curl` are the routes below (all under `$CALIBER_API`, signed in as above):

| Step | Call |
| --- | --- |
| Draw the plan | `POST /aria/plans {"goal", "autonomy": "ask_each"}` answers `201` with `plan` (status `draft`) and `steps`; a goal with no steps adds `warnings` |
| Approve | `POST /aria/plans/{plan_id}/approve` (409 for a plan with no steps or one that is not a draft) |
| Execute | `POST /aria/plans/{plan_id}/execute`: runs until the first question; the plan is `paused` |
| See the question | `GET /aria/plans/{plan_id}/interactions`: an `input` question carries `evidence.input_schema`, `evidence.missing` and `evidence.current_inputs` |
| Answer a form | `POST /aria/interactions/{id}/answer {"inputs": {...}}`; a bad answer is a 409 and the question stays pending |
| Answer an approval | `POST /aria/interactions/{id}/answer {"approved": true}` (or `false` to skip the step) |
| Skip a form | `POST /aria/interactions/{id}/answer {"approved": false}` |
| Cancel a draft | `PATCH /aria/plans/{plan_id} {"status": "cancelled"}` |
| Add rows | `POST /eval-datasets/{id}/examples {"input", "expected", "tags"}`, one per row (each is a new version) |
| Run it | `POST /evaluations {"dataset_id", "scorers": ["non_empty"]}`: `400` on the simulated stack |

For example, the first three calls and the first question, as they answer on a real stack (trimmed):

```text
POST /aria/plans  {"goal": "Create a judge for answer faithfulness and an eval dataset to run it on.", "autonomy": "ask_each"}
  plan  {"plan_id": "PLAN-80a75bc2", "status": "draft", "autonomy": "ask_each", "step_count": 2}
  steps [{"capability_key": "judge.create",        "status": "pending", "inputs": {}, "depends_on": []},
         {"capability_key": "eval_dataset.create", "status": "pending", "inputs": {}, "depends_on": []}]
POST /aria/plans/PLAN-80a75bc2/approve   -> status "approved"
POST /aria/plans/PLAN-80a75bc2/execute   -> status "paused": judge.create waiting_input, eval_dataset.create pending
GET  /aria/plans/PLAN-80a75bc2/interactions
  [{"interaction_id": "ASK-d85a91be", "kind": "input", "status": "pending",
    "prompt": "Provide the required inputs for step 1: Create judge.", "evidence": {"missing": ["name", "instructions"], ...}}]
```

Both steps have empty `inputs`: neither takes anything from the other. (Before this lab, the planner wired the test set's `name` to the judge step's `name`, so the test set silently took the judge's name and its form never asked. A `name` is the identity of what a step is about to create, so it is now always asked. Only a field that points at an earlier step's result, such as the review queue's `queue_id` in lab 13, is wired.)

### Failure drills

The script provokes these on purpose; the same ones by hand:

| Do this | You see |
| --- | --- |
| A goal with no domain word: `POST /aria/plans {"goal": "Write me a poem about customer support."}` | `201`, `step_count` 0 and `warnings: ["No platform steps matched this goal, so there is nothing to approve. Name what you want created, for example 'create a test set' or 'create a judge', and the plan will list its steps."]`; approving it is `409 this plan has no steps, so it cannot be approved. ...` |
| Answer the judge form with `{"name": "x", "instructions": "{{ outputs }}", "colour": "red"}` | `409 judge.create received unknown input field(s): colour`; the question stays pending |
| ... with `{"name": 7, ...}` / `{"name": "", ...}` / `{"name": "x"}` | `409 name must be string` / `409 name must contain at least 1 character(s)` / `409 judge.create is missing required input field(s): instructions` |
| Answer **Deny** to the approval of a step | that step reads `skipped`; the next step still runs; the plan can end `completed` with nothing created by the denied step |
| A judge whose instructions use no evaluation variable (`Is {{ evidence }} faithful to the policy?`) | the form accepts it, the step fails: plan `failed`, step error `invalid input: instructions: instructions must reference at least one evaluation variable: {{ inputs }}, {{ outputs }}, {{ expectations }}, {{ conversation }}, {{ trace }}`. A failed plan cannot be executed again (`409`): state the goal again |
| A judge name that already exists | step error `ValueError: judge name '...' is already in use`, plan `failed` |
| Run a plan as an account without the operator scope (`vera`) | `execute` returns the plan `failed` at once, before any question, with the step error `plan owner lacks required scope(s): operator`; nothing is created. The judge route itself answers `403` for her |
| Answer another account's question (`olga` answering the admin's) | `403 only the plan owner may answer this interaction`; the plan itself is `404` for her: plans are private to their owner |
| Aria switched off | every `/aria/*` call: `404 {"detail": "Aria assistant and plans is disabled in this deployment. Set CALIBER_FEATURE_ARIA=true to enable it.", "reason_code": "feature_disabled"}`, and **Plans** is not in the sidebar. To see it, start a stack without the flag (`scripts/cookbook-env.sh up lab12off --flags none`) |

## Recipe: run it through the UI

The Playwright spec `caliber/caliber-ui/e2e/cookbooks/labs/12-aria-evaluation-harness.spec.ts` performs exactly these steps with real clicks (sign in as `admin` / `admin`). Run it against the stack above:

```bash
cd caliber/caliber-ui
CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/12-aria-evaluation-harness.spec.ts --output=/tmp/lab12-pw
```

1. **State the intent.** In the sidebar open **Integrations** and click **Plans**. Paste the sentence from [`assets/intent.md`](assets/intent.md) into **Goal**, set **Autonomy** to **Stop before each change**, and press **Decompose goal**. Expected: the plan opens with the status `draft`, "2 steps".
2. **Review the plan.** Under **Plan steps**: `Create judge` (`judge.create`) and `Create test set` (`eval_dataset.create`), both `pending`, each listing its required `inputs` (`name, instructions` and `name`). Nothing has run.
3. **Approve the plan.** Press **Approve plan**. Expected: the status becomes `approved` and an **Execute** button appears.
4. **Execute and answer the judge's questions.** Press **Execute**. Expected: **Aria needs information**, "Provide the required inputs for step 1: Create judge.", and the step reads `waiting input`. Fill **name** (`AnswerFaithfulness`), **instructions** (the paragraph from `assets/judges/answer-faithfulness.judge.json`) and **feedback value type** (`bool`), leave the rest, press **Continue plan**. Expected: **Aria needs your approval**, "Approve step 1: Create judge (judge.create)?". Press **Approve**.
5. **Answer the test set's questions.** Expected: "Provide the required inputs for step 2: Create test set." and step 1 now `done`. Fill **name** (`support-faithfulness-eval`), **description** and **tags** (`["aria-created","faithfulness","support"]`, from `assets/datasets/support-faithfulness.dataset.json`), press **Continue plan**, then **Approve** the prompt "Approve step 2: Create test set (eval_dataset.create)?". Expected: the plan reads `completed` and both steps `done`.
6. **Verify the judge.** Open **Agent resources > Judges**. Expected: a row `AnswerFaithfulness` showing `bool`.
7. **Verify the test set and load the 30 cases.** Open **Prompt lifecycle > Test Sets**, search for the name and open it. Expected: version `v1` and no rows (the empty-state line reads "No examples yet. Add one by hand, import a file, or capture one from a trace."). Press **Import from file**, choose [`assets/dataset/faithfulness-cases.jsonl`](assets/dataset/faithfulness-cases.jsonl) under **Test set file**. Expected: `faithfulness-cases.jsonl: 30 rows ready to import.`; press **Import 30 rows**. Expected: a progress line, then 30 rows in the list and the version `v31` (one version per row; the extra `id` key in each line is ignored).
8. **Modify, which makes a new version.** On the first row (`Can I return a laptop I bought 20 days ago?`) press **Edit**, add a sentence to **Expected (JSON object)** (for example ` Day 20 of 30 is inside the window.`), press **Save**. Expected: the version reads `v32` and the row shows the new text. The old text is not lost: set **Version view** to **Active as of version** and pick `31`; the list shows the 30 rows as they were, the first one without your sentence, read-only.
9. **Run the judge in Evaluations.** Open **Prompt lifecycle > Evaluations**, **Run evaluation**, pick the test set at `v32`, give it a **Label**, untick **Exact match**, **Token F1** and **Contains expected**, tick **Non-empty** and, under *Custom LLM judges*, your judge, and press **Run**. Expected on the simulated stack: the red message `evaluation requires a real LLM provider` (nothing is stored). On a real stack the same screen scores the 30 rows: see [Through the UI (real stack)](#through-the-ui-real-stack) for the two prompt versions and the comparison.
10. **A sentence with no steps.** On **Plans**, state `Write me a poem about customer support.` and press **Decompose goal**. Expected: an amber note, "No platform steps matched this goal, so there is nothing to approve. ...", the sentence stays in the box so you can reword it, and no plan opens (nothing to approve).
11. **Skip a step.** State `Create a judge for tone.`, **Decompose goal**, **Approve plan**, **Execute**, and press **Skip step** on the form. Expected: the plan reads `completed` and its step `skipped`.
12. **A judge that cannot be created.** State `Create a judge for tone.` again, approve, execute, fill **name** with a new name and **instructions** with `Is {{ evidence }} polite?`, press **Continue plan**. Expected: the plan reads `failed` and the step shows `invalid input: instructions: instructions must reference at least one evaluation variable...`; **Judges** has no row for that name.

## Verify it worked

API checks (the script does all of them; these are the ones to read by eye):

- `GET /aria/plans/{plan_id}` has `plan.status: "completed"` and both steps `done`; `steps[0].result` is `{judge_id, name, status: "active"}` and `steps[1].result` is `{dataset_id, name}`.
- `GET /judges/{judge_id}` has `status: "active"`, `feedback_value_type: "bool"`, and the instructions you typed. The test set's name differs from the judge's.
- `GET /eval-datasets/{dataset_id}` has `version: 31` and `GET /eval-datasets/{dataset_id}/examples` lists 30 rows whose `input.question` are the 30 questions in the file.
- `GET /aria/plans/{failed plan}` has `status: "failed"` and the failed step's `error` is the text in the drills above.
- On a simulated stack `POST /evaluations` answers `400 evaluation requires a real LLM provider; set CALIBER_LLM_PROVIDER (openai/anthropic) and the matching API key, then retry.` On a real stack it answers 201 and `GET /evaluations/{id}` reads `status: "completed"`, `n_examples: 30`, an `aggregate` with the judge's share of true verdicts (`Judge.<id>`), and `evidence.resolved.llm_provider` set to the real provider (see [Run it with a real provider](#run-it-with-a-real-provider)).

UI checks: steps 1 to 12 above, each with the text or status it names.

## Run it with a real provider

A real provider changes three things in this lab and nothing else: the judge can be **run** (Evaluations), the **Ask Aria** chat can act, and you can see what Aria's planner does *not* get from a real model. Everything below was run against a live stack; the numbers are from those runs and **will vary** (a model's answers and a judge's verdicts are not deterministic).

### Start a real stack

```bash
export OPENAI_API_KEY=...            # your own key. Never commit it; the lab never prints or stores it
export CALIBER_OPERATOR_USERS=admin,olga
scripts/cookbook-env.sh up lab12h --flags cookbooks,aria --real-llm
source .tmp/cookbook-env/lab12h/env.sh
curl -s -b /tmp/lab12.jar "$CALIBER_API/readiness"   # after the login below: "simulated": [], "all_real": true
```

`--real-llm` sets `CALIBER_LLM_PROVIDER=openai`, `CALIBER_EVAL_PROVIDER=mlflow` and `CALIBER_ASSISTANT_ENGINE=openai` (default model `gpt-5.6-luna`), and reads only `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` from your environment or the repository-root `.env`. Make the access token exactly as in [Prerequisites and setup](#prerequisites-and-setup), then run the same command:

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_12_aria_evaluation_harness
```

The script reads `GET /readiness` and runs in the mode it finds. On a real stack it skips the 400-refusal check and does the following instead. It takes about 5 to 6 minutes and about 125 model calls (30 answers and 30 verdicts for each of two evaluations, plus two chat turns); `COOKBOOK_ARIA_HTTP_TIMEOUT` (seconds, default 900) bounds one request, because an evaluation scores every row inside one request.

### What the evaluation does (and a trap in the default target)

1. It writes two **prompts** (`assets/prompts/`): a *grounded* answerer that is shown the row's policy text (`{{ evidence }}` is filled from each test set row) and told to stay inside it, and a *careless* control that never sees the policy and is told to sound sure.
2. It runs the test set twice in **Evaluations** (`predict_target: prompt`), with the scorers `non_empty` and `Judge.<the judge Aria created>`.

Why a prompt and not the default target: with *What to score* left at the model, Evaluations send the model only the row's `question` field and never its `evidence`, so a faithfulness judge would grade answers the model had no sources for. In a 3-row probe through that default target the model answered "it depends on the retailer" to the laptop question and the judge's verdicts measured nothing about your policy. Scoring a prompt that holds `{{ evidence }}` is how a test set row's other fields reach the model.

Real output (trimmed; ids, the time stamp and the rates vary):

```text
ok   grounded answerer: the run completed over all 30 rows with a verdict from the judge for each  [completed, 30 rows, 30 judged]
ok   grounded answerer: the judge's verdicts are 0 or 1 and its aggregate is the share of 1s  [judge aggregate 0.9667]
ok   grounded answerer: the evidence names a real provider, the answerer prompt and the judge  [provider=openai model=gpt-5.6-luna judge model=the deployment default]
ok   careless control: the run completed over all 30 rows with a verdict from the judge for each  [completed, 30 rows, 30 judged]
ok   careless control: the judge's verdicts are 0 or 1 and its aggregate is the share of 1s  [judge aggregate 0.0]
ok   the judge tells the two answerers apart: grounded beats careless by at least 0.2  [grounded 0.9667, careless 0.0, gap +0.97]
...
ok   the assistant engine is a real one, not the fake heuristic  [engine=openai model=gpt-5.6-luna]
ok   Ask Aria in a build / auto_all session called judge_create itself and it worked  [tools called: ['judge_list', 'judge_create']]
ok   in the default session (manual approval) judge_create is not offered: nothing was created  [tools called: ['judge_list']]
ok   the chat's judge is active, boolean, and its instructions use an evaluation variable  [JDG-9712b3df]

All documented outcomes verified.
```

What is asserted is structure and ranges, never a score: every row has a verdict of 0 or 1 and no error, the aggregate is the share of 1s, the evidence names a real provider and the judge, and the grounded answerer beats the careless one by at least 0.2 (override: `COOKBOOK_12_MIN_GAP`). Observed in three real runs: grounded 0.9667 (29 of 30) each time and careless 0.0 each time; expect the grounded rate to move by a row or two. The one grounded row the judge failed was the shipping question "Do you ship to Canada?" (tags `shipping`, `hard-negative`): the model answered "Our policy says we currently ship only to the United States and its territories. It does not cover shipping to Canada." without saying "No", and the judge, which compares against the reference answer, called it unfaithful. A real judge is strict in ways you only see by reading its rows (the per-row `scores` and the model's `prediction` are in `GET /evaluations/{id}`).

The judge was created by Aria without a `model`, so it runs on the deployment's default judge model (the evidence records `model: null` for it).

### What the real engine does, and does not, change in Aria

Verified live, in this order:

- **Plans are unchanged.** `POST /aria/plans` always uses the rules-based planner, whatever `CALIBER_ASSISTANT_ENGINE` is. On the real stack `"Set up faithfulness scoring for our support answers, with a test set to run it on."` still draws only the test set step, and the script's 28 goals draw the same 28 plans as on the simulated stack. The Plans page does not call the engine.
- **The chat acts, within its permissions.** **Ask Aria** (top bar) is the only place the real engine acts. Its toolset is built per turn from Aria's capability registry: read tools in every session; mutating tools (here `judge_create`) only when the session is in `build` mode **and** its approval mode is `auto_all` (UI label **Legacy auto**) or `full_autonomy`; gated tools never. In a default session (approval **Ask first**) the model is offered `judge_list` only: asked to create a judge it called `judge_list`, reported it could not create one, and nothing was created (run live). In an `auto_all` session it called `judge_list` then `judge_create` and the judge exists (`status: active`, `feedback_value_type: bool`).
- **The model picks what you do not specify.** In the first probe the model chose `model: "openai:/gpt-4o-mini"` for the judge it created, which the form path never does (it leaves the model empty). Say the model you want in the message.
- **Gated actions stay out of reach.** Approving and publishing are never offered to the chat. `workflow.calibrate` is a mutating capability, so by the same rule it is offered under `auto_all` (read from the code, not exercised in this lab; lab 15 runs it through a plan).

### Through the UI (real stack)

The spec reads the amber **Simulated providers** banner at the top of every page (no banner on a real stack) and, on a real stack, continues step 9 above like this:

1. **Write the answerer as a prompt, and version it.** In **Prompt lifecycle > Prompts** press **New prompt**, **Write / paste**, paste [`assets/prompts/careless-answer.prompt.txt`](assets/prompts/careless-answer.prompt.txt) into **Prompt text**, name it, **Create and Open**. This is version 1: a control that never sees the policy. Replace the text in **Prompt template** with [`assets/prompts/grounded-answer.prompt.txt`](assets/prompts/grounded-answer.prompt.txt) and press **Save draft**: version 2, whose `{{ evidence }}` is filled from each test set row.
2. **Run both versions.** In **Evaluations > Run evaluation** pick the test set at `v32`, set **What to score** to `prompt`, **Subject under test** to `<prompt name>@1`, untick the three default graders, tick **Non-empty** and the judge, label it `careless v1`, **Run**; then the same with `@2` and the label `grounded v2`. Expected: both run rows read `completed` and `30/30`. Each takes about two minutes.
3. **Compare.** Open the `grounded v2` run, set **Compare to** to the `careless v1` run. Expected: the line `Dataset/scorer-compatible comparison` naming `@1` and `@2`, and a green delta on the judge's card. The scorecard lists 30 rows with each row's prediction and the judge's verdict.

The spec asserts structure and a range, never a score: 30 rows each, the subject names, and the grounded judge share exceeding the careless one by at least 0.2.

A second test opens **Ask Aria**, sends the judge request in a default chat (no judge is created), then again after choosing **Legacy auto** under the approval selector (a judge row appears in **Agent resources > Judges**).

### Real-provider troubleshooting

| You see | Cause and fix |
| --- | --- |
| `POST /evaluations` answers **201** but the run has `status: "failed"` and `error_message: "Error code: 401 ... Incorrect API key provided: sk-invalid"` | The key is rejected. Evaluations record the provider error on the run instead of failing the request, so check `status`. Provoked on purpose with `OPENAI_API_KEY=sk-invalid`; `/readiness` still says `ready: true` because it does not call the model. Fix the key and restart the stack |
| Ask Aria answers "I encountered an error: AuthenticationError" | The same rejected key, in the chat (provoked the same way) |
| The script stops with `VERIFICATION FAILED: ... the judge tells the two answerers apart` | The judge scored both answerers alike, or the grounded answerer failed too many rows: read the rows in `GET /evaluations/{id}`. A looser bar for a noisy judge: `COOKBOOK_12_MIN_GAP=0.1` |
| `ReadTimeout` from the script | One request outlived `COOKBOOK_ARIA_HTTP_TIMEOUT` (default 900 s): raise it; a rate-limited account (HTTP 429 from the provider) is retried by the provider client, so it only slows the run. A 429 and an unknown model were not provoked in this lab |
| The UI test shows "Failed to fetch dynamically imported module" | The web bundle was rebuilt while the page was open (another `cookbook-env.sh up` rebuilds it); reload and run the spec again |
| The model keeps answering "it depends" without the policy | The evaluation used the default target; use a prompt with `{{ evidence }}` as above |

## Common errors and troubleshooting

| You see | Cause and fix |
| --- | --- |
| `404 {"reason_code": "feature_disabled"}` on `/aria/...`; no **Plans** in the sidebar | Start the server with `--flags aria` (`CALIBER_FEATURE_ARIA=true`) and restart |
| `SetupError: Aria is off: start the server with CALIBER_FEATURE_ARIA=true` | The same, reported by the script |
| `SetupError: start CALIBER with CALIBER_OPERATOR_USERS=admin,olga ...` | The server was started without the export in Prerequisites; restart it with it. `vera` must not be listed |
| `SetupError: CALIBER_TOKEN must be an admin personal access token` | The script creates accounts, so it needs the admin's token |
| `could not sign in as 'olga': 401` | An account from an earlier run has another password: reset it under Administration, or set `COOKBOOK_ARIA_PASSWORD` |
| **Decompose goal** shows an amber note and opens no plan | The sentence has none of the domain words (`judge`, `eval dataset`, `test set`, `test-set`, `testset`, `eval_dataset`). Reword it; see the goals table |
| The plan has a step you did not ask for | The planner matched a word inside another (`judgement`), ignored a negation, or read a question as a request. **Cancel** the draft or **Deny** / **Skip step** that step |
| **Continue plan** says `name is required.` | A required field is empty; the form checks before it sends |
| `409 ... received unknown input field(s)` / `must be string` / `is missing required input field(s)` | The form (or your `inputs`) does not fit the capability's schema; the question stays open, answer it again |
| The plan reads `failed` with `invalid input: ...` or `ValueError: ... is already in use` | The step ran and its own check refused. A failed plan cannot be run again: state the goal again with a new name or fixed instructions |
| `plan owner lacks required scope(s): operator` | The account that owns the plan is not in `CALIBER_OPERATOR_USERS`. Plans run with their owner's scopes |
| `403 only the plan owner may answer this interaction` | Another account answered it. Only the owner (or an admin) can |
| `400 evaluation requires a real LLM provider` from **Evaluations** or `POST /evaluations` | The simulated provider; the judge and the deterministic scorers both need a real one. Start the stack with `--real-llm` ([Run it with a real provider](#run-it-with-a-real-provider)). Not a bug in this lab |
| The judge or test set name is taken | Names are unique across the deployment, archived ones included; the script adds a time stamp |

## Not covered by this lab

The judge's verdicts are checked for structure and against a deliberately careless control, not for being *right*: the lab does not measure the judge against human labels (lab 10 certifies a judge, and lab 13 checks one against 12 reviews). Aria cannot add the 30 rows (it creates the test set empty; you load them with **Import from file**), a judge has no versions to modify (you archive it and create another), and Aria's chat engine does not plan: **Plans** never calls it. The planner does not read negation, synonyms beyond the five listed, or questions: that is a property of the rules-based planner, not of the lab.

## Cleanup and reset

```bash
# archive the judges and test sets the script created (names cookbook-12-*) and delete its prompts
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_12_aria_evaluation_harness --cleanup
# reset everything: stop the stack and delete its state
scripts/cookbook-env.sh down lab12 && rm -rf .tmp/cookbook-env/lab12
```

Plans cannot be deleted: the script cancels every draft it makes, and plans that ran stay in the **Plans** list. `olga` and `vera` stay as accounts; disable or reset them under Administration. The UI spec leaves its `c12-*` judges and test sets in the disposable stack.
