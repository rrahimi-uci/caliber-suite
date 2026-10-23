# Aria: Governance Starter Kit from Intent

> **Prerequisite flag:** `CALIBER_FEATURE_ARIA=true` (Aria and its **Plans** page are a preview area that ships **on** by default, so a stock server already has it; the `/aria/*` routes answer 404 `feature_disabled` only on a deployment that set `CALIBER_FEATURE_ARIA=false`). The commands below pass `--flags cookbooks,aria` so the dependency is explicit. `CALIBER_FEATURE_COOKBOOKS=true` is only needed for the Cookbooks gallery card. This lab is outside the supported alpha journey (`docs/start/alpha-scope.md`).

## What this lab demonstrates and the expected outcome

You give Aria one sentence, *"Stand up our governance starter kit: a judge for answer faithfulness, an eval dataset to score against, and a review queue for human checks."*, and it draws a plan of four platform steps: `judge.create`, `review_queue.create`, `eval_dataset.create` and `review_queue.add_items`. Nothing runs until you approve the plan. When you execute it, each step stops on an **Aria needs information** form for the fields the planner cannot know, and, because you chose **Stop before each change**, on an **Approve** / **Deny** prompt before it creates anything; you skip the fourth step because there are no traces to enqueue yet. At the end a real judge, a real review queue and a real, empty test set exist under three different names, you load 30 policy questions into the test set, and you see what happens when you ask Aria for less (no approval prompts), when you deny a step (its dependent goes with it) and when a step fails half way (what was already made stays made).

**The planner is rules, not a model.** It proposes a step for every capability whose domain word appears in the goal, so the whole lab is deterministic and the plan is checked exactly against a dataset of 28 goals. There is no language model in the loop for planning, and the creation steps need no model either. The one thing that does need a real model, running the judge over the test set in **Evaluations**, is refused on a simulated deployment (`400 evaluation requires a real LLM provider`), and the lab verifies that refusal instead of a score. On a stack with a real provider ([Run it with a real provider](#run-it-with-a-real-provider)) the same script scores the 30 policy cases with the judge instead, and checks the Ask Aria chat.

You are done when:

- the script prints `All documented outcomes verified.` and exits 0;
- the planner drew the documented plan for all 28 goals in `assets/dataset/goals.jsonl` (28/28), and none of those drafts ran;
- the intent drew the four steps in registry order, only the enqueue step has a wired input (the queue's id), you answered seven questions (three forms with an approval each, then the enqueue form, skipped), and the plan reads `completed` with three steps `done` and the enqueue step `skipped`;
- the judge (active, `bool`), the review queue (four questions, no items) and the test set (version 1, no rows) exist under three different names, and the test set takes the 30 cases (version 31);
- the same sentence under **Run everything it safely can** asked only the four forms (no approvals) and built the same kit; the draft's autonomy could be changed before approval and not after (409);
- the refusals happened: denying the queue skipped the enqueue step with it, a taken queue name failed the plan after the judge was made (the judge stays), a judge without an evaluation variable failed the plan before anything else was made, a goal with no steps cannot be approved (409), an account without the operator scope fails at execute and creates nothing, and an operator who does not own the plan cannot answer (403) or read it (404);
- the Playwright spec `caliber/caliber-ui/e2e/cookbooks/labs/14-aria-governance-starter-kit.spec.ts` passes.

### What is simulated, and what a real provider changes

| Piece | Simulated stack (default) | Real provider (verified, see [below](#run-it-with-a-real-provider)) |
| --- | --- | --- |
| Planning the goal into steps | The rules-based planner | **Unchanged**: no model is wired into planning; all 28 goals draw the same plans on a real stack (28/28, run live) |
| `judge.create`, `review_queue.create`, `eval_dataset.create` | Real: a judge, a queue and a test set are created in the database | Unchanged |
| The judge's verdicts | Not produced: **Evaluations** is refused (400), so the judge never runs | **Evaluations** scores each of the 30 policy cases with the judge; run live, 30 of 30 rows judged, the 5 `high-risk` cases sliced apart |
| Test set rows | Real: 30 rows added through the API (3 through the row editor in the UI) | Unchanged |
| The queue's reviewers | Nobody reviews here (lab 13 does); the enqueue step is skipped | Unchanged |
| **Ask Aria** (the chat) | Not used here; its engine is `fake` | A real engine answers and, in a `build` session with `auto_all` approval, creates a review queue itself (run live) |

## Prerequisites and setup

A CALIBER server per [`docs/start/alpha-install.md`](../../../docs/start/alpha-install.md) with the Aria flag on. For a disposable one, `scripts/cookbook-env.sh` starts exactly that stack (migrate, MLflow, CALIBER, simulated providers). From the repo root:

```bash
# olga: an operator who does NOT own the plan. vera (created by the script) must not be an operator.
export CALIBER_OPERATOR_USERS=admin,olga
scripts/cookbook-env.sh up lab14 --flags cookbooks,aria
source .tmp/cookbook-env/lab14/env.sh      # CALIBER_URL, CALIBER_BASE_URL, CALIBER_API, ...
```

Sign in at `$CALIBER_URL/caliber/` as `admin` / `admin`, then make an access token for the script (Settings > API tokens, or):

```bash
curl -s -c /tmp/lab14.jar -X POST "$CALIBER_API/auth/login" -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"admin"}' >/dev/null
export CALIBER_TOKEN=$(curl -s -b /tmp/lab14.jar -X POST "$CALIBER_API/auth/tokens" \
  -H 'content-type: application/json' -d '{"name":"lab14"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["token"])')
```

The default simulated providers (`CALIBER_LLM_PROVIDER=fake`, `CALIBER_EVAL_PROVIDER=fake`) are what this lab is written for.

## Dataset and sample files

All under `docs-site/cookbooks/14-aria-governance-starter-kit/assets/`.

| File | What it is | Why |
| --- | --- | --- |
| `dataset/goals.jsonl` | 28 goals, one JSON object per line: `{id, goal, expected_steps, outcome, note}` | The planner is checked against it. `outcome` says how the plan relates to what the sentence asked: `as_asked` (15), `unasked` (6, a step you did not ask for), `missed` (3, a step you asked for is missing), `nothing` (4, no steps) |
| `dataset/release-candidate-cases.jsonl` | 30 policy cases for a release candidate of an internal HR and IT assistant: `{id, tags, input: {question, evidence}, expected: {answer}}` | The rows loaded into the test set. Leave, expenses, IT, benefits, conduct, payroll, remote work and onboarding, with the policy text each answer must stay inside, and hard cases: a flight that is too short for business class (`C07`), a pet that is not family (`C05`), a degree that is not a course (`C20`), a partner who is not registered (`C18`), advice the assistant must not give (`C25`), a name the evidence does not contain (`C30`). Five are tagged `high-risk` (forwarding mail, a lost authenticator, customer data in an AI tool, gifts, reporting fraud) |
| `judges/answer-faithfulness.judge.json` | The `judge.create` form values: `name`, `feedback_value_type`, `instructions` (one paragraph, the form's field is a single line), `tags` | What you type into the judge form |
| `review-queues/governance-review.queue.json` | The `review_queue.create` form values: `name`, `description`, `reviewers`, `questions` (a JSON array of four) | What you type into the queue's form |
| `datasets/release-candidates.dataset.json` | The `eval_dataset.create` form values: `name`, `description`, `tags` | What you type into the test set form |
| `intent.md`, `expected-plan.json` | The canonical sentence, and the plan it must draw | Sanity checks for the draft |

The queue's four questions: `faithful` (pass/fail, required), `citation_ok` (pass/fail), `tone` (categorical `on_brand`/`neutral`/`off_brand`) and `notes` (text).

A few goals worth reading, because they show where the rules stop (all verified):

| Goal | Plan | Why |
| --- | --- | --- |
| `Stand up our governance starter kit: a judge for answer faithfulness, an eval dataset to score against, and a review queue for human checks.` | judge, queue, test set, add traces | The canonical one. A review queue always plans its enqueue step: skip it if you have no traces |
| `I need a review queue, a judge and an eval dataset for the IT helpdesk bot.` | judge, queue, test set, add traces | The plan follows registry order, not the order of the sentence |
| `Stand up our governance starter kit.` | nothing | "governance" and "starter kit" are not domain words |
| `Score the release candidate for faithfulness, keep a held-out test set, and let a human spot-check the tricky ones.` | test set only | Only "test set" is a domain word |
| `Please create a test-set and a review-queue.` | test set only | `test-set` works, `review-queue` does not |
| `Create an eval dataset and a review queue, and a workflow to use them.` | queue, test set, add traces, calibrate | `workflow` plans a calibration of an existing workflow (lab 15): Aria cannot build one |
| `Do not create a judge or a review queue, only an eval dataset.` | judge, queue, test set, add traces | Negation is not read |
| `Delete the judge, the eval dataset and the review queue from last week.` | judge, queue, test set, add traces | There is no delete capability, and the words still plan three creations |
| `We have a lot of judgement calls to make about the review queue.` | judge, queue, add traces | `judge` is matched inside `judgement` |

That is why you read the plan before you approve it: nothing runs until you do.

## Configuration and environment

| Variable | Value used | What it does |
| --- | --- | --- |
| `CALIBER_FEATURE_ARIA` | `true` (set by `--flags aria`) | Opens `/aria/*`, `/assistant/*` and the **Plans** page (404 `feature_disabled` without it) |
| `CALIBER_FEATURE_COOKBOOKS` | `true` (set by `--flags cookbooks`) | Only the gallery card; the lab does not need it |
| `CALIBER_OPERATOR_USERS` | `admin,olga` | Accounts holding `caliber.operator`: the scope the three create steps declare. `vera` is deliberately absent |
| `CALIBER_LLM_PROVIDER`, `CALIBER_EVAL_PROVIDER` | `fake` (the default); `openai` and `mlflow` with `--real-llm` | Simulated: **Evaluations** is refused. Real: the judge runs |
| `OPENAI_API_KEY` | only with `--real-llm`; export it yourself | The provider key (never written anywhere by the lab) |
| `COOKBOOK_ARIA_HTTP_TIMEOUT`, `COOKBOOK_14_MIN_RATE` | optional (900, 0.6) | One request's timeout in seconds, and the bar for the faithful share on a real stack |
| `CALIBER_BASE_URL` | `$CALIBER_URL`, the server root without `/ajax-api/...` | What the script's SDK client calls |
| `CALIBER_TOKEN` | the admin token above | The script acts as `admin` and signs in as `olga` and `vera` itself |
| `COOKBOOK_ARIA_PASSWORD` | optional | Password for the `olga` and `vera` accounts the Aria labs create (default `cookbook-aria-lab-passphrase`) |
| `COOKBOOK_14_ASSETS` | optional | Another assets directory |

## Run it through the API

One command runs the whole lab and verifies every outcome:

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_14_aria_governance_starter_kit
```

Real output from a run (ids and the time stamp vary):

```text
ok   the planner drew the documented plan for every goal  [28/28 {'as_asked': 15, 'missed': 3, 'nothing': 4, 'unasked': 6}]
ok   a goal that drew no steps cannot be approved (409) and says why  [4 goals]
ok   every draft was cancelled; nothing ran  [28 drafts]
ok   the intent draws judge, review queue, test set, then the enqueue step (registry order)  [PLAN-e1024582]
ok   only the enqueue step has a wired input (the queue's id); the three names are asked for
ok   the plan asked 7 questions: a form and an approval for each of the three, then the enqueue form
ok   the plan completed: three steps done, the enqueue step skipped (no traces yet)  [completed]
ok   the judge, the review queue and the test set exist under three different names  [JDG-1b1b78db RVQ-a88155d2 ED-63264274]
ok   the judge is active and boolean, the queue has its four questions, the test set is empty at version 1
ok   the test set holds the 30 cases, one version per row  [30 rows, version 31]
ok   every stored row is the file's row (input, expected answer, tags)
ok   Evaluations are refused without a real model (400): the judge cannot run here  [simulated stack]
ok   a plan is drawn under the default autonomy ('Review the plan, then run')
ok   a draft's autonomy can be changed before it is approved
ok   after approval it can no longer be changed (409)  [only draft plans can be edited]
ok   under 'Run everything it safely can' only the four forms are asked, no approvals; the same kit is built  [ask_each: {'input': 4, 'permission': 3}; auto_guarded: {'input': 4}]
ok   both kits are in the deployment, six artifacts under six names
ok   denying the queue skips it and the enqueue step with it (never asked); the judge and the test set are made  [skipped because step 2 (review_queue.create) was skipped]
ok   a queue name that is taken fails the plan after the judge was made: the judge stays, the steps after it never run  [ValueError: review-queue name 'cookbook-14-governance-review-kit-20261018-145903' is already in use]
ok   a judge with no evaluation variable fails the plan before anything else is made  [invalid input: instructions: instructions must reference at least one evaluation]
ok   a goal with no steps (governance is not a domain word) is refused at approval (409)
ok   a plan owned by an account without the operator scope fails at execute and creates nothing  [plan owner lacks required scope(s): operator]
ok   an operator who does not own the plan cannot answer its question (403) or read it (404)  [only the plan owner may answer this interaction]
ok   skipping the three forms skips everything: the enqueue step is never asked (its queue was skipped), and the plan completes with nothing made  [{'judge.create': 'skipped', 'review_queue.create': 'skipped', 'eval_dataset.create': 'skipped', 'review_queue.add_items': 'skipped'}]

All documented outcomes verified.
```

The script then prints a JSON summary (`plan_id`, `judge_id`, `queue_id`, `dataset_id`, `cases`, `evaluation: "refused (simulated provider)"`, the suffix). Every artifact it creates is named `cookbook-14-...-<suffix>`.

### What each step does, by hand

The script is plain SDK calls; the same steps with `curl` are the routes below (all under `$CALIBER_API`, signed in as above):

| Step | Call |
| --- | --- |
| Draw the plan | `POST /aria/plans {"goal", "autonomy": "ask_each"}` answers `201` with `plan` and `steps`: four steps; only `review_queue.add_items` has an `inputs` entry, `{"queue_id": {"$from_step": "<the queue step's id>", "path": "queue_id"}}` |
| Change the autonomy of a draft | `PATCH /aria/plans/{id} {"autonomy": "auto_guarded"}` (409 `only draft plans can be edited` once it is approved) |
| Approve, execute | `POST /aria/plans/{id}/approve`, then `POST /aria/plans/{id}/execute`: the plan is `paused` on the judge's `input` question |
| Answer a form | `POST /aria/interactions/{id}/answer {"inputs": {...}}`; a bad answer is a 409 and the question stays pending |
| Answer an approval | `POST /aria/interactions/{id}/answer {"approved": true}` (or `false` to skip the step) |
| Skip a form | `POST /aria/interactions/{id}/answer {"approved": false}` |
| Add rows | `POST /eval-datasets/{id}/examples {"input", "expected", "tags"}`, one per row (each is a new version) |
| Run it | `POST /evaluations {"dataset_id", "scorers": ["non_empty"]}`: `400` on the simulated stack |

The order of the questions under **Stop before each change** is: the judge's form, its approval, the queue's form, its approval, the test set's form, its approval, then the enqueue form (skip it). Under **Review the plan, then run** (the default) and **Run everything it safely can** the approvals are not asked: judge form, queue form, test set form, enqueue form. Both modes create the same kit.

### Failure drills

The script provokes these on purpose; the same ones by hand:

| Do this | You see |
| --- | --- |
| **Deny** the queue's approval (after its form) | the queue step reads `skipped`; the test set step still asks and runs; the enqueue step is skipped without being asked: `skipped because step 2 (review_queue.create) was skipped`; the plan is `completed`; the judge and the test set exist, the queue does not |
| Skip every form | each step reads `skipped` (the enqueue step is never asked) and the plan is `completed` with nothing made |
| Answer the queue's form with a name that is taken | step error `ValueError: review-queue name '...' is already in use`, plan `failed`; the judge made by step 1 stays, and the steps after the failed one stay `pending` and never run. Aria does not undo earlier steps |
| A judge whose instructions use no evaluation variable | the form accepts it, the step fails: plan `failed`, step error `invalid input: instructions: instructions must reference at least one evaluation variable: {{ inputs }}, {{ outputs }}, {{ expectations }}, {{ conversation }}, {{ trace }}`; nothing else is made. A failed plan cannot be run again (`409`): state the goal again |
| `PATCH /aria/plans/{id} {"autonomy": ...}` after approving | `409 only draft plans can be edited` |
| A goal with no domain word (`Create the governance kit.`) | `201` with `step_count` 0 and a `warnings` entry; approving it is `409 this plan has no steps, so it cannot be approved. ...` |
| Run a plan as an account without the operator scope (`vera`) | `execute` returns the plan `failed` at once, before any question, with the step error `plan owner lacks required scope(s): operator`; nothing is created |
| Answer another account's question (`olga` answering the admin's) | `403 only the plan owner may answer this interaction`; the plan itself is `404` for her |
| Aria switched off (`CALIBER_FEATURE_ARIA=false`) | every `/aria/*` call: `404 {"detail": "Aria assistant and plans is disabled in this deployment. Set CALIBER_FEATURE_ARIA=true to enable it.", "reason_code": "feature_disabled"}`, and **Plans** is not in the sidebar |
| Answer a form with an unknown field, a wrong type, an empty name or a missing field | `409` naming it (for the judge: `judge.create received unknown input field(s): colour`, `name must be string`, `name must contain at least 1 character(s)`, `judge.create is missing required input field(s): instructions`); the question stays open |

## Recipe: run it through the UI

The Playwright spec `caliber/caliber-ui/e2e/cookbooks/labs/14-aria-governance-starter-kit.spec.ts` performs exactly these steps with real clicks (sign in as `admin` / `admin`). Run it against the stack above:

```bash
cd caliber/caliber-ui
CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/14-aria-governance-starter-kit.spec.ts --output=/tmp/lab14-pw
```

1. **State the intent.** In the sidebar open **Operate** and click **Plans**. Paste the sentence from [`assets/intent.md`](assets/intent.md) into **Goal**, set **Autonomy** to **Stop before each change**, and press **Decompose goal**. Expected: the plan opens with the status `draft`.
2. **Review the plan.** Under **Plan steps**: `Create judge` (`judge.create`), `Create review queue` (`review_queue.create`), `Create test set` (`eval_dataset.create`) and `Enqueue traces for review` (`review_queue.add_items`, reading `depends on 1 prior step(s)`). Four steps, all `pending`, nothing run.
3. **Approve the plan.** Press **Approve plan**. Expected: the status becomes `approved` and an **Execute** button appears.
4. **Execute and answer the judge's questions.** Press **Execute**. Expected: **Aria needs information**, "Provide the required inputs for step 1: Create judge.". Fill **name** (`AnswerFaithfulness`), **instructions** (the paragraph from `assets/judges/answer-faithfulness.judge.json`), **feedback value type** (`bool`) and **tags** (`["aria-created","faithfulness","governance"]`), press **Continue plan**. Expected: **Aria needs your approval**, "Approve step 1: Create judge (judge.create)?". Press **Approve**.
5. **Answer the queue's questions.** Expected: "Provide the required inputs for step 2: Create review queue.". Fill **name** (`governance-review`), **description** and **questions** (the JSON array from `assets/review-queues/governance-review.queue.json`), press **Continue plan**, then **Approve** the prompt "Approve step 2: Create review queue (review_queue.create)?".
6. **Answer the test set's questions.** Expected: "Provide the required inputs for step 3: Create test set.". Fill **name** (`release-candidates-eval`), **description** and **tags** (from `assets/datasets/release-candidates.dataset.json`), press **Continue plan**, then **Approve** "Approve step 3: Create test set (eval_dataset.create)?".
7. **Skip the enqueue step.** Expected: "Provide the required inputs for step 4: Enqueue traces for review." with **no** queue id field (it is wired). There are no traces yet: press **Skip step**. Expected: the plan reads `completed`, steps 1 to 3 `done`, step 4 `skipped`.
8. **Verify the judge and the queue.** Open **Agent resources > Judges**: a row `AnswerFaithfulness` showing `bool`. Open **Operate > Review Queues**: a row `governance-review` reading `no items`.
9. **Verify the test set and add rows.** Open **Prompt lifecycle > Test Sets**, search for the name and open it. Expected: version `v1` and no rows. Press **+ Add example**, fill **Input (JSON object)** with a case's `input`, **Expected (JSON object)** with its `expected`, **Tags**, and press **Save**; repeat for two more cases from `assets/dataset/release-candidate-cases.jsonl`. Expected: each question appears and the version reads `v4`. (The other 27 rows are the script's job.)
10. **Ask for less.** On **Plans**, state the sentence again with the default **Autonomy**, press **Decompose goal**, then change **Autonomy** on the draft to **Run everything it safely can** (the plan header reads `Autonomy: Run everything it safely can`). **Approve plan**, **Execute**. Expected: the judge's form, and after **Continue plan** the queue's form straight away, with no **Approve** prompt anywhere; then the test set's form, then the enqueue form: **Skip step**. The plan reads `completed`.
11. **Deny a step.** State the sentence again under **Stop before each change**, approve and execute, answer the judge's form and approve it, answer the queue's form and, at "Approve step 2: Create review queue (review_queue.create)?", press **Deny**. Expected: the next question is the test set's form (step 3): the enqueue step is never asked. Answer it and approve. Expected: the plan reads `completed`; steps 2 and 4 read `skipped`, step 4 saying `skipped because step 2 (review_queue.create) was skipped`; **Review Queues** has no row for that queue.

## Verify it worked

API checks (the script does all of them; these are the ones to read by eye):

- `GET /aria/plans/{plan_id}` has `plan.status: "completed"`; the steps read `done`, `done`, `done`, `skipped`, with `results` `{judge_id, name, status}`, `{queue_id, name, questions: 4}`, `{dataset_id, name}`.
- `GET /judges/{judge_id}` has `status: "active"` and `feedback_value_type: "bool"`; `GET /review-queues/{queue_id}` has `queue.questions` of four keys and `queue.item_count: 0`; `GET /eval-datasets/{dataset_id}` has `version: 31` after the 30 rows and `GET /eval-datasets/{dataset_id}/examples` lists their 30 questions. The three names differ.
- `GET /aria/plans/{id}/interactions` for the **Stop before each change** plan lists 4 `input` and 3 `permission` questions; for the **Run everything it safely can** plan, 4 `input` and none else.
- `GET /aria/plans/{failed plan}` has `status: "failed"` and the failed step's `error` is the text in the drills above.
- On a simulated stack `POST /evaluations` answers `400 evaluation requires a real LLM provider; set CALIBER_LLM_PROVIDER (openai/anthropic) and the matching API key, then retry.` On a real stack it answers 201 and `GET /evaluations/{id}` reads `status: "completed"`, `n_examples: 30`, an `aggregate` with the judge's share of true verdicts (`Judge.<id>`), and `evidence.resolved.llm_provider` set to the real provider (see [Run it with a real provider](#run-it-with-a-real-provider)).

UI checks: steps 1 to 11 above, each with the text or status it names.

## Run it with a real provider

A real provider adds two things to this lab: the judge can be **run** over the 30 policy cases (Evaluations), and the **Ask Aria** chat can act. Everything Aria plans and creates is exactly as above. The numbers below are from live runs and **will vary**.

### Start a real stack

```bash
export OPENAI_API_KEY=...            # your own key. Never commit it; the lab never prints or stores it
export CALIBER_OPERATOR_USERS=admin,olga
scripts/cookbook-env.sh up lab14h --flags cookbooks,aria --real-llm
source .tmp/cookbook-env/lab14h/env.sh
```

`--real-llm` sets `CALIBER_LLM_PROVIDER=openai`, `CALIBER_EVAL_PROVIDER=mlflow` and `CALIBER_ASSISTANT_ENGINE=openai` (default model `gpt-5.6-luna`) and reads only `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` from your environment or the repository-root `.env`; `GET /readiness` then reports `"simulated": []`. Make the access token as in [Prerequisites and setup](#prerequisites-and-setup) and run the same command as above. The script reads `GET /readiness` and, on a real stack, skips the 400-refusal check and does the following instead. It takes about 3 to 4 minutes and about 62 model calls (30 answers and 30 verdicts, plus two chat turns); `COOKBOOK_ARIA_HTTP_TIMEOUT` (seconds, default 900) bounds one request, because an evaluation scores every row inside one request.

### Scoring the 30 policy cases

The script writes one **prompt** (`assets/prompts/grounded-answer.prompt.txt`): an answerer shown the case's policy text (`{{ evidence }}` is filled from each row) and told to stay inside it. It runs the test set in **Evaluations** with `predict_target: prompt` and the scorers `non_empty` and `Judge.<the judge Aria created>`. (With *What to score* left at the model, Evaluations send only the row's `question` field and never its `evidence`, so a faithfulness judge would grade answers the model had no sources for; scoring a prompt that holds `{{ evidence }}` is how the row's other fields reach the model.)

```text
ok   release candidate: the run completed over all 30 rows with a verdict from the judge for each  [completed, 30 rows, 30 judged]
ok   release candidate: the judge's verdicts are 0 or 1 and its aggregate is the share of 1s  [judge aggregate 0.9667]
ok   release candidate: the evidence names a real provider, the answerer prompt and the judge  [provider=openai model=gpt-5.6-luna judge model=the deployment default]
ok   the judge finds at least 60% of the grounded answers faithful  [97%]
ok   the evidence slices the run by tag, with the 5 high-risk cases apart  [{'benefits': 4, 'conduct': 3, 'edge': 3, 'expenses': 5, 'hard-negative': 3, 'high-risk': 5, 'it': 6, ...}]
```

Asserted: all 30 rows have a verdict of 0 or 1 and no error, the aggregate is the share of 1s, the evidence names a real provider and the judge, at least 60% of the grounded answers are found faithful (`COOKBOOK_14_MIN_RATE`), and the evidence's `slices` hold the five `high-risk` cases apart (`GET /evaluations/{id}` -> `evidence.slices`), so a release-candidate gate can look at them separately. Never asserted: an exact rate or which row fails. Observed in two real runs: 29 of 30 faithful (0.9667) both times (a judge compares against the reference answer, so a correct answer worded differently can be called unfaithful). The judge was created by Aria without a `model`, so it runs on the deployment's default judge model.

### What the real engine does, and does not, change in Aria

- **Plans are unchanged.** `POST /aria/plans` uses the rules-based planner whatever the assistant engine is; the 28 goals draw the same plans on the real stack, and **Plans** never calls the engine.
- **The chat acts only inside its permissions.** In a `build` session with approval `auto_all` (UI label **Legacy auto**) or `full_autonomy`, Ask Aria's tools include Aria's mutating capabilities; asked to create a queue with one `pass_fail` question the model called `review_queue_create` and the queue exists. In the default session (**Ask first**) it is offered read tools only: it called `list_tools`, `openapi_tool_list` and `review_queue_list`, could not create the queue, and nothing was created (run live). Approving and publishing are never offered to the chat.

```text
ok   the assistant engine is a real one, not the fake heuristic  [engine=openai model=gpt-5.6-luna]
ok   Ask Aria in a build / auto_all session called review_queue_create itself and it worked  [tools called: ['review_queue_create']]
ok   in the default session (manual approval) review_queue_create is not offered: nothing was created  [tools called: ['list_tools', 'openapi_tool_list', 'review_queue_list']]
ok   the chat's queue is active with the question that was asked for  [RVQ-50c7a6d7]
```

### Through the UI (real stack)

`14-aria-governance-starter-kit.spec.ts` checks `GET /readiness` and, on a real stack, adds to the first test: **Prompt lifecycle > Prompts** (create the grounded prompt from the asset), then **Evaluations > Run evaluation** with the test set (`v4`, three rows), *What to score* `prompt`, *Subject under test* `<prompt name>@1`, the three default graders unticked, **Non-empty** and the judge ticked under *Custom LLM judges*, **Run**. Expected: the run row reads `completed` and `3/3` and its scorecard lists three rows. A fourth test opens **Ask Aria** and sends the queue request in a default chat (no queue appears in **Review Queues**), then after choosing **Legacy auto** (the queue appears). On a simulated stack the evaluation step shows `evaluation requires a real LLM provider` instead.

### Real-provider troubleshooting

| You see | Cause and fix |
| --- | --- |
| `POST /evaluations` answers **201** but the run has `status: "failed"` and `error_message: "Error code: 401 ... Incorrect API key provided: sk-invalid"` | The key is rejected. Evaluations record the provider error on the run instead of failing the request, so check `status`. Provoked on purpose with `OPENAI_API_KEY=sk-invalid`; `/readiness` still says `ready: true` because it does not call the model. Fix the key and restart the stack |
| Ask Aria answers "I encountered an error: AuthenticationError" | The same rejected key, in the chat |
| `VERIFICATION FAILED: the judge finds at least 60% ...` | Fewer grounded answers were found faithful than the bar: read the rows in `GET /evaluations/{id}`, or lower `COOKBOOK_14_MIN_RATE` for a noisy judge |
| `ReadTimeout` | One request outlived `COOKBOOK_ARIA_HTTP_TIMEOUT` (default 900 s); raise it. A provider 429 and an unknown model were not provoked in this lab |

## Common errors and troubleshooting

| You see | Cause and fix |
| --- | --- |
| `404 {"reason_code": "feature_disabled"}` on `/aria/...`; no **Plans** in the sidebar | Aria ships on, so someone turned it off: remove `CALIBER_FEATURE_ARIA=false` from the server's environment (or start a new stack with `--flags cookbooks,aria`; `cookbook-env.sh restart` reuses the old flags) and restart |
| `SetupError: Aria is off: start the server with CALIBER_FEATURE_ARIA=true` | The same, reported by the script |
| `SetupError: start CALIBER with CALIBER_OPERATOR_USERS=admin,olga ...` | The server was started without the export in Prerequisites; restart it with it. `vera` must not be listed |
| `could not sign in as 'olga': 401` | An account from an earlier run has another password: reset it under Administration, or set `COOKBOOK_ARIA_PASSWORD` |
| **Decompose goal** shows an amber note and opens no plan | The sentence has no domain word (`judge`, `eval dataset`, `test set`, `review queue`, `workflow`). "Governance", "kit" and "spot-check" are not. Reword it; see the goals table |
| The plan has a step you did not ask for | The planner matched a word inside another, ignored a negation, read a question as a request, or a review queue brought its enqueue step. **Cancel** the draft or **Deny** / **Skip step** that step |
| The test set or the queue has the judge's name | It does not any more: each step asks for its own name. If you see this, the server is an older build |
| **Continue plan** says `name is required.` | A required field is empty; the form checks before it sends |
| `409 ... received unknown input field(s)` / `must be string` / `is missing required input field(s)` | The form (or your `inputs`) does not fit the capability's schema; the question stays open, answer it again |
| The plan reads `failed` with `invalid input: ...` or `ValueError: ... is already in use` | The step ran and its own check refused. A failed plan cannot be run again: state the goal again with a new name or fixed instructions. What earlier steps made stays: archive it under its page (Judges, Review Queues, Test Sets) or leave it |
| `plan owner lacks required scope(s): operator` | The account that owns the plan is not in `CALIBER_OPERATOR_USERS`. Plans run with their owner's scopes |
| `403 only the plan owner may answer this interaction` | Another account answered it. Only the owner (or an admin) can |
| `400 evaluation requires a real LLM provider` from **Evaluations** or `POST /evaluations` | The simulated provider; the judge and the deterministic scorers both need a real one: start the stack with `--real-llm` ([Run it with a real provider](#run-it-with-a-real-provider)). Not a bug in this lab |
| A judge, queue or test set name is taken | Names are unique across the deployment, archived ones included; the script adds a time stamp |

## Not covered by this lab

The judge is run once over the 30 cases and is not compared with human labels (lab 13 and lab 10 do that). Enqueuing traces into the queue and reviewing them is lab 13; this lab skips that step. Adding the 30 rows is not an Aria capability (Aria creates the test set empty). The planner does not read negation, questions or "delete", and a plan that fails half way does not undo what it already made: both are properties of the alpha, not of the lab.

## Cleanup and reset

```bash
# archive the judges, review queues and test sets the script created (names cookbook-14-*) and delete its prompt
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_14_aria_governance_starter_kit --cleanup
# reset everything: stop the stack and delete its state
scripts/cookbook-env.sh down lab14 && rm -rf .tmp/cookbook-env/lab14
```

Plans cannot be deleted: the script cancels every draft it makes, and plans that ran stay in the **Plans** list. `olga` and `vera` stay as accounts; disable or reset them under Administration. The UI spec leaves its `c14*` judges, queues and test sets in the disposable stack.
