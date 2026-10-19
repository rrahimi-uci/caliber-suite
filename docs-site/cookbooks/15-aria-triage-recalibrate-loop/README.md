# Aria: Triage & Recalibrate Loop

> **Prerequisite flag:** `CALIBER_FEATURE_ARIA=true` (Aria and its **Plans** page are a preview area, off by default; the `/aria/*` routes answer 404 `feature_disabled` without it). `CALIBER_FEATURE_COOKBOOKS=true` is only needed for the Cookbooks gallery card. This lab is outside the supported alpha journey (`docs/start/alpha-scope.md`). It also needs the server's background workers (the plan worker and the refinement worker), which `scripts/cookbook-env.sh` starts.

## What this lab demonstrates and the expected outcome

You give Aria one sentence about weak runs, *"Our workflow's recent runs look weak - set up a review queue for the flagged traces and kick off a workflow calibration."*, and it draws a plan of three platform steps: `review_queue.create`, `review_queue.add_items` and `workflow.calibrate`. You answer each step's form and approve it. The last approval enqueues a real **calibration job** and the plan **parks**: its status stays `running` and the step reads `waiting job`. Nothing is left for you to click: the plan worker watches the job and, when it finishes, resumes the plan, which ends where the job ended. Meanwhile a reviewer triages the flagged traces in the queue, with a verdict, a failure mode and the answer it should have given.

**This loop remediates things that already exist.** Aria creates neither the traces nor the workflow: `add_items` needs trace ids that exist, `workflow.calibrate` needs a workflow and an agent that exist. The lab creates them first with two scripts in `assets/` (a published support workflow with a 30-case test set as its deploy gate, two tools, an agent; and 12 flagged traces). In a real deployment they are yours.

**What a simulated stack proves, and what it cannot.** The planner is rules, not a model, so the plan is deterministic and checked exactly against 28 goals. The parking, the resume by the worker and the plan mirroring its job are real: the lab runs a real calibration job. But on the simulated provider the calibration is **rejected**: the fake workflow executor gives the candidate the same outputs as the baseline, so the quality gain is `+0.000`, below the `0.02` the candidate must clear, and the plan ends `failed` with that reason. A calibration that finds a candidate (`candidate_ready`) needs a real model; the plan would then complete. The script accepts either outcome and asserts that the plan follows the job; the completing path is covered by unit tests (`caliber/tests/test_aria_async.py`) and is **not demonstrated live here**.

You are done when:

- the script prints `All documented outcomes verified.` and exits 0;
- the planner drew the documented plan for all 28 goals in `assets/dataset/goals.jsonl` (28/28), none of them ran;
- the intent drew `review_queue.create`, `review_queue.add_items`, `workflow.calibrate`; only the enqueue step's queue id is wired; you answered six questions (a form and an approval for each step);
- approving the calibration parked the plan (`running`, step `waiting_job`, a job id `RFN-...`), a parked plan could not be cancelled (409), the worker resumed it without being asked, and the plan ended where the job ended (here: job `rejected`, plan `failed`, step error `rejected: workflow calibration rejected: no candidate met calibration gates; quality delta +0.000 < epsilon 0.02`);
- the review queue exists with its four triage questions and the 12 flagged traces, pending and assigned, and three triaged runs carry the reviewer's verdict, failure mode and expected answer on their traces;
- the refusals happened: an unknown workflow, an unregistered agent and a workflow with no test set each fail the step at once without queueing a job, a form with an empty id or an unknown field is refused (409) and stays open, a denied calibration queues nothing, skipping the queue skips its enqueue with it, a goal with no steps cannot be approved (409), an account without the operator scope fails at execute, and an operator who does not own the plan cannot answer (403) or poll it (404);
- the Playwright spec `caliber/caliber-ui/e2e/cookbooks/labs/15-aria-triage-recalibrate-loop.spec.ts` passes.

### What is simulated, and what a real provider would change

| Piece | In this lab | With a real provider |
| --- | --- | --- |
| Planning the goal into steps | The rules-based planner (the same on a real deployment) | Unchanged |
| The queue, the enqueue, the job, the parking and the worker's resume | Real | Unchanged |
| The workflow's runs and traces | The traces are recorded runs written by `assets/seed_traces.py`; the workflow itself is replayed by the calibration with a fake executor | Your workflow produces them |
| The calibration's result | The job ends `rejected` (`quality delta +0.000 < epsilon 0.02`): nothing beats the baseline, so the plan fails | A real executor can find a candidate: the job reaches `candidate_ready`, the step finishes and the plan completes. The candidate is not applied by the plan |
| Reviews | Three runs triaged by the script with the dataset's labels | People answer in **Review Queues** |

## Prerequisites and setup

A CALIBER server per [`docs/start/alpha-install.md`](../../../docs/start/alpha-install.md) with the Aria flag on, an MLflow server, and its workers. For a disposable one, `scripts/cookbook-env.sh` starts exactly that stack. From the repo root:

```bash
# olga: an operator who does NOT own the plan. vera (created by the script) must not be an operator.
export CALIBER_OPERATOR_USERS=admin,olga
scripts/cookbook-env.sh up lab15 --flags cookbooks,aria
source .tmp/cookbook-env/lab15/env.sh      # CALIBER_URL, CALIBER_BASE_URL, CALIBER_API, MLFLOW_TRACKING_URI, ...
```

Sign in at `$CALIBER_URL/caliber/` as `admin` / `admin`, then make an access token for the script (Settings > API tokens, or):

```bash
curl -s -c /tmp/lab15.jar -X POST "$CALIBER_API/auth/login" -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"admin"}' >/dev/null
export CALIBER_TOKEN=$(curl -s -b /tmp/lab15.jar -X POST "$CALIBER_API/auth/tokens" \
  -H 'content-type: application/json' -d '{"name":"lab15"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["token"])')
```

The default simulated providers (`CALIBER_LLM_PROVIDER=fake`, `CALIBER_EVAL_PROVIDER=fake`) are what this lab is written for. The script (and the Playwright spec) run the two `assets/` scripts themselves; to do it by hand for the UI path:

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python docs-site/cookbooks/15-aria-triage-recalibrate-loop/assets/setup_subject.py --suffix demo
caliber/.venv/bin/python docs-site/cookbooks/15-aria-triage-recalibrate-loop/assets/seed_traces.py > /tmp/traces.json
```

The first prints the `workflow_id` and `agent_id` you type into the calibration form; the second prints `{run id: trace id}` for the 12 weak runs.

## Dataset and sample files

All under `docs-site/cookbooks/15-aria-triage-recalibrate-loop/assets/`.

| File | What it is | Why |
| --- | --- | --- |
| `dataset/goals.jsonl` | 28 goals, one JSON object per line: `{id, goal, expected_steps, outcome, note}` | The planner is checked against it. `outcome`: `as_asked` (16), `unasked` (7, a step you did not ask for), `nothing` (5, no steps) |
| `dataset/support-runs.jsonl` | 30 recorded runs of a support agent: `{id, question, reply, tools_called, required_tools, reference, weak, reason}` | Three things at once. The **test set** the workflow is calibrated against (input = the question, expected = `reference` and `required_tools`). The **flagged traces**: the 12 `weak` rows are the runs that skipped a tool their question needed (`S19`..`S30`: answered from memory, the wrong tool, a claim the policy contradicts, an escalation the policy did not require). And the labels a reviewer gives. 18 rows are runs the agent got right, including honest uncertainty (`S17`) and small talk (`S10`, `S14`) |
| `workflows/support-policy-agent.workflow.json` | The workflow manifest: `start -> support_agent (tools lookup_policy, get_order) -> policy_guardrail -> final`, with a deploy gate on the test set | The existing workflow Aria calibrates. Imported by `setup_subject.py` with the test set's real name |
| `tools/lookup-policy.tool.json`, `tools/get-order.tool.json` | The two read tools the agent binds, backed by `caliber.workflows.demo_tools` | Registered by `setup_subject.py` (one already registered is kept) |
| `setup_subject.py` | Registers the tools, loads the 30-case test set, imports and publishes the workflow, registers an agent; prints their ids | The subject: Aria remediates it, it does not create it |
| `seed_traces.py` | Writes the 12 weak runs to MLflow as traces and prints `{row id: trace id}` | A fresh checkout has no workflow producing flagged runs |
| `review-queues/triage-review.queue.json` | The `review_queue.create` form values: `name`, `description`, `reviewers`, `questions` (a JSON array of four) | What you type into the queue's form |
| `enqueue.json`, `calibrate.json` | The `review_queue.add_items` and `workflow.calibrate` form values (with placeholders for the ids) | What you type into the other two forms |
| `intent.md`, `expected-plan.json` | The canonical sentence, and the plan it must draw | Sanity checks for the draft |

The queue's four questions: `resolution_correct` (pass/fail, required), `failure_mode` (categorical: `wrong_tool`, `missing_evidence`, `hallucinated_claim`, `bad_escalation`, `none`), `severity` (numeric) and `expected_answer` (text, written back onto the trace as the **expectation**, the ground truth the next calibration can use).

A few goals worth reading, because they show where the rules stop (all verified):

| Goal | Plan | Why |
| --- | --- | --- |
| `Our workflow's recent runs look weak - set up a review queue for the flagged traces and kick off a workflow calibration.` | queue, enqueue, calibrate | The canonical one |
| `Calibrate the support workflow.` | calibrate | One step |
| `Tune the pipeline and get a human to look at the bad runs.` | nothing | "pipeline", "tune" and "get a human to look" are not domain words |
| `Recalibrate the pipeline.` | nothing | "pipeline" is not "workflow" |
| `Create a new workflow for returns.` | calibrate | Aria cannot build a workflow: the word plans a calibration of an existing one |
| `Roll back the workflow to last week's version.` | calibrate | There is no rollback capability; the word still plans a calibration |
| `We are in the middle of a workflow outage.` | calibrate | The word is matched whatever the sentence is about |
| `Do not calibrate the workflow, only set up the review queue.` | queue, enqueue, calibrate | Negation is not read |
| `Put the flagged traces in a review queue.` | queue, enqueue | There is no "add to an existing queue" step on its own: it creates a queue first |
| `Calibrate every workflow we have.` | calibrate | One step, one workflow id |

That is why you read the plan before you approve it: nothing runs until you do.

## Configuration and environment

| Variable | Value used | What it does |
| --- | --- | --- |
| `CALIBER_FEATURE_ARIA` | `true` (set by `--flags aria`) | Opens `/aria/*`, `/assistant/*` and the **Plans** page (404 `feature_disabled` without it) |
| `CALIBER_FEATURE_COOKBOOKS` | `true` (set by `--flags cookbooks`) | Only the gallery card; the lab does not need it |
| `CALIBER_OPERATOR_USERS` | `admin,olga` | Accounts holding `caliber.operator`: the scope the three steps declare. `vera` is deliberately absent |
| background workers | on (set by `scripts/cookbook-env.sh`) | The plan worker (resumes parked plans, about every 10 seconds, `aria_plan_worker_interval_seconds`) and the refinement worker (runs the calibration job). Without them the job never runs |
| `MLFLOW_TRACKING_URI` | from `env.sh` | Where `seed_traces.py` writes the traces and CALIBER reads them |
| `CALIBER_BASE_URL` | `$CALIBER_URL`, the server root without `/ajax-api/...` | What the script's SDK client calls |
| `CALIBER_TOKEN` | the admin token above | The script acts as `admin` (the plan's owner and the reviewer) and signs in as `olga` and `vera` itself |
| `COOKBOOK_ARIA_PASSWORD` | optional | Password for the `olga` and `vera` accounts the Aria labs create (default `cookbook-aria-lab-passphrase`) |
| `COOKBOOK_15_ASSETS` | optional | Another assets directory |

## Run it through the API

One command runs the whole lab and verifies every outcome. It takes about a minute and a half: the calibration job replays 20 cases at about 1.4 seconds each, then the worker's next tick resumes the plan.

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_15_aria_triage_recalibrate_loop
```

Real output from a run (ids and the time stamp vary):

```text
ok   the two tools are registered, the workflow is imported and published with the 30-case test set as its deploy gate, and an agent is registered  [WF-85c990d9, gate 'cookbook-15-support-policy-gate-20261018-154053']
ok   the recorded runs are consistent: a run is weak exactly when it skipped a tool its question needs  [12 weak of 30]
ok   the 12 weak runs are in MLflow as traces, one per run  [12 traces]
ok   the planner drew the documented plan for every goal  [28/28 {'as_asked': 16, 'nothing': 5, 'unasked': 7}]
ok   a goal that drew no steps cannot be approved (409) and says why  [5 goals]
ok   every draft was cancelled; nothing ran  [28 drafts]
ok   the intent draws review_queue.create, review_queue.add_items, workflow.calibrate  [PLAN-207a10fc]
ok   only the enqueue step's queue_id is wired (from the create step); the calibrate step depends on nothing
ok   before the last approval the plan has made the queue and queued the 12 traces, and the calibration has not started  [{'queue_id': 'RVQ-30bef725', 'added': 12}]
ok   approving the calibration enqueues a job and PARKS the plan: the plan stays 'running' and the step reads 'waiting_job' with the job's id  [running; waiting_job; RFN-a9bd80e6]
ok   a parked plan cannot be cancelled or edited (409): only drafts can  [only draft plans can be edited]
ok   the plan asked six questions in all: a form and an approval for each step
ok   the plan resumed by itself (worker) and ends where the job ended: the calibration was rejected: the plan failed with the job's reason  [job rejected; plan failed; step: rejected: workflow calibration rejected: no candidate met calibration gates; quality delta +0.000 < epsilon 0.]
ok   polling a finished plan changes nothing
ok   the job replayed 20 of the gate's 30 cases (the default budget) and reports its baseline  [20 examples; gate: ['no candidate met calibration gates', 'quality delta +0.000 < epsilon 0.02']]
ok   the review queue exists with its four triage questions and the 12 flagged traces pending, assigned to the reviewer  [RVQ-30bef725: 12 items]
ok   three triaged runs carry the reviewer's verdict, failure mode and the expected answer on their traces  [{'S19': 'missing_evidence', 'S21': 'wrong_tool', 'S24': 'hallucinated_claim'}]
ok   the queue shows 3 of 12 reviewed  [3/12 reviewed]
ok   an unknown workflow fails the step at once (404), without parking  [workflow 'WF-does-not-exist' not found (HTTP 404)]
ok   an agent that is not registered fails the step at once (400)  [agent 'agent-that-is-not-registered' is not registered (HTTP 400)]
ok   a workflow with no deploy-gate test set fails the step at once (400): calibration needs one  [Workflow calibration requires an active deploy-gate eval dataset with examples. (HTTP 400)]
ok   none of the three refusals queued a job  [1]
ok   the form refuses an empty workflow id, a missing agent id and a field it does not have (409) and stays open  [{'an empty workflow id': 'workflow_id must contain at least 1 character(s)', 'a missing agent id': 'workflow.calibrate is missing required input field(s): agent_id', 'an unknown field': 'workflow.calibrate received unknown input field(s): objective'}]
ok   denying the calibration skips the step and queues no job; the plan completes
ok   skipping the queue skips the enqueue step with it (never asked); the calibration's form is still asked, and skipping it ends the plan  [{'review_queue.create': 'skipped', 'review_queue.add_items': 'skipped', 'workflow.calibrate': 'skipped'}]
ok   a goal with no domain word is created with a warning and refused at approval (409)
ok   a plan owned by an account without the operator scope fails at execute and queues nothing  [plan owner lacks required scope(s): operator]
ok   an operator who does not own the plan cannot answer its question (403) or poll it (404)  [only the plan owner may answer this interaction]

All documented outcomes verified.
```

The script then prints a JSON summary (`plan_id`, `queue_id`, `workflow_id`, `agent_id`, `calibration: {job_id, job_status: "rejected", plan_status: "failed", advanced_by: "worker"}`, `triage: {reviewed: 3, pending: 9}`, the suffix). Every artifact it creates is named `cookbook-15-...-<suffix>`. If the worker has not resumed the plan after 75 seconds the script advances it with `POST /aria/plans/{id}/poll` and says so (`advanced_by: "poll"`).

### What each step does, by hand

The script is plain SDK calls; the same steps with `curl` are the routes below (all under `$CALIBER_API`, signed in as above):

| Step | Call |
| --- | --- |
| Make the subject | `POST /tools` (twice), `POST /eval-datasets` and `POST /eval-datasets/{id}/examples` (30 rows), `POST /workflows/import {"manifest", "name"}`, `POST /workflow-versions/{id}/publish`, `POST /agents {"agent_id", "experiment_id", "name"}` |
| Draw, approve, execute | `POST /aria/plans {"goal", "autonomy": "ask_each"}` (3 steps; only `add_items` has an `inputs` entry, its wired `queue_id`), `POST /aria/plans/{id}/approve`, `POST /aria/plans/{id}/execute` |
| The three forms | `POST /aria/interactions/{id}/answer {"inputs": {...}}`: the queue's (`name`, `description`, `reviewers`, `questions`), the enqueue's (`trace_ids`, `assigned_to`; `queue_id` is wired), the calibration's (`workflow_id`, `agent_id`); each followed by `{"approved": true}` |
| The park | After the last approval `GET /aria/plans/{id}`: `plan.status: "running"`, the `workflow.calibrate` step `status: "waiting_job"`, `job_id: "RFN-..."`, no pending question |
| The job | `GET /jobs/{job_id}`: `status` `queued`, `running`, then `rejected` (or `candidate_ready`); `eval_results.gate.reasons` and `error_message` say why |
| The resume | Nothing to call: the plan worker polls parked plans about every 10 seconds. `POST /aria/plans/{id}/poll` does the same on demand and is safe to repeat |
| The end | `GET /aria/plans/{id}`: `completed` if the job reached `candidate_ready` or `applied`; `failed` with the step error `<job status>: <the job's error_message>` if it ended `rejected`, `failed` or `error` |
| Triage | `POST /review-queues/{queue_id}/items/{item_id}/submit {"answers": {"resolution_correct": false, "failure_mode": "wrong_tool", "severity": 3, "expected_answer": "..."}}`; read it back with `GET /observability/traces/{trace_id}` (`assessments`) |

### Failure drills

The script provokes these on purpose; the same ones by hand:

| Do this | You see |
| --- | --- |
| Calibrate a workflow id that does not exist | the step fails at once, the plan is `failed`, no job: `workflow 'WF-does-not-exist' not found (HTTP 404)` |
| Calibrate with an agent id that is not registered | `agent '...' is not registered (HTTP 400)` |
| Calibrate a workflow whose manifest has no deploy gate (or whose test set is empty) | `Workflow calibration requires an active deploy-gate eval dataset with examples. (HTTP 400)` |
| Answer the calibration form with `"workflow_id": ""` / without `agent_id` / with a field such as `objective` | `409 workflow_id must contain at least 1 character(s)` / `workflow.calibrate is missing required input field(s): agent_id` / `workflow.calibrate received unknown input field(s): objective`; the question stays open. Aria passes only the two ids: the objective and the budget are the defaults |
| **Deny** the calibration's approval | the step reads `skipped`, no job is queued, the plan is `completed` |
| Skip the queue's form | the queue step and the enqueue step read `skipped` (`skipped because step 1 (review_queue.create) was skipped`, never asked); the calibration's form is still asked |
| Cancel or edit the plan while it is parked | `409 only draft plans can be edited`. A parked plan cannot be stopped from here; it ends when its job does |
| The job ends `rejected` (the simulated default) | the plan is `failed`, the step error `rejected: workflow calibration rejected: no candidate met calibration gates; quality delta +0.000 < epsilon 0.02`. A failed plan cannot be run again: calibrate again by stating the goal again (or `POST /workflows/{id}/calibration/runs {"agent_id", "objective": {...}}`, which can change the objective and the margin) |
| Run a plan as an account without the operator scope (`vera`) | `execute` returns the plan `failed` at once with the step error `plan owner lacks required scope(s): operator`; nothing is created |
| Answer or poll another account's plan (`olga`) | `403 only the plan owner may answer this interaction` / `404 aria plan '...' not found` |
| A goal with no domain word | `201` with `step_count` 0 and a `warnings` entry; approving it is `409` |
| Workers off (`--no-workers`) | Observed: the plan stays `running` with the step `waiting_job` and the job `queued`, after 40 seconds and after `POST /aria/plans/{id}/poll`. Poll only reads the job; nothing runs it |
| Aria switched off | every `/aria/*` call: `404 {"detail": "Aria assistant and plans is disabled in this deployment. Set CALIBER_FEATURE_ARIA=true to enable it.", "reason_code": "feature_disabled"}`, and **Plans** is not in the sidebar |

## Recipe: run it through the UI

The Playwright spec `caliber/caliber-ui/e2e/cookbooks/labs/15-aria-triage-recalibrate-loop.spec.ts` performs exactly these steps with real clicks (sign in as `admin` / `admin`). It first runs `assets/setup_subject.py` and `assets/seed_traces.py` (set `CALIBER_LAB_PYTHON` to another interpreter that has `mlflow` and the SDK on its path). Run it against the stack above; the first test takes about a minute and a half:

```bash
cd caliber/caliber-ui
CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/15-aria-triage-recalibrate-loop.spec.ts --output=/tmp/lab15-pw
```

1. **State the intent.** In the sidebar open **Integrations** and click **Plans**. Paste the sentence from [`assets/intent.md`](assets/intent.md) into **Goal**, set **Autonomy** to **Stop before each change**, and press **Decompose goal**. Expected: the plan opens with the status `draft`.
2. **Review the plan.** Under **Plan steps**: `Create review queue` (`review_queue.create`), `Enqueue traces for review` (`review_queue.add_items`, reading `depends on 1 prior step(s)`) and `Calibrate workflow` (`workflow.calibrate`, depending on nothing). Nothing has run.
3. **Approve the plan.** Press **Approve plan**. Expected: the status becomes `approved` and an **Execute** button appears.
4. **Execute and answer the queue's questions.** Press **Execute**. Expected: **Aria needs information**, "Provide the required inputs for step 1: Create review queue.". Fill **name** (`weak-runs-triage`), **description**, **questions** and **reviewers** (the JSON arrays from `assets/review-queues/triage-review.queue.json`), press **Continue plan**, then **Approve** "Approve step 1: Create review queue (review_queue.create)?".
5. **Answer the enqueue step.** Expected: "Provide the required inputs for step 2: Enqueue traces for review." with **no** queue id field. Fill **trace ids** with the JSON array of the 12 trace ids (from `seed_traces.py`; they also appear in **Operate > Observability**) and **assigned to** with `admin`, press **Continue plan**, then **Approve**.
6. **Answer the calibration step and watch the plan park.** Expected: "Provide the required inputs for step 3: Calibrate workflow.". Fill **workflow id** and **agent id** (from `setup_subject.py`), press **Continue plan**, then **Approve** "Approve step 3: Calibrate workflow (workflow.calibrate)?". Expected: the plan header reads `running`, the step reads `waiting job`, and there is nothing to answer.
7. **Wait: the plan settles by itself.** Do not click anything. The page re-reads the plan every five seconds; after about half a minute the status changes. On the simulated stack: `failed`, steps 1 and 2 `done`, step 3 `failed` with `rejected: workflow calibration rejected: no candidate met calibration gates; quality delta +0.000 < epsilon 0.02`. With a real model the job can reach `candidate_ready` and the plan reads `completed`.
8. **Find the queue.** Open **Operate > Review Queues**. Expected: a row `weak-runs-triage`, 4 questions, reviewer `olga`, `0/12 reviewed`.
9. **Triage one flagged run.** Open the queue. Expected: "Items (12)". Pick the trace of `S19` and answer: **Fail** for "Did the workflow reach the correct resolution?", `missing_evidence` for "Primary failure mode", `3` for "Severity (1-5)", and the reference answer for "What should the answer have been?"; press **Submit review**. Expected: that trace reads `completed` and the list row reads `1/12 reviewed`.
10. **A workflow that does not exist.** On **Plans**, state `Calibrate the support workflow.`, **Decompose goal**, **Approve plan**, **Execute**, fill **workflow id** with `WF-does-not-exist` and any agent id, press **Continue plan**. Expected: the plan reads `failed` at once, the step shows `workflow 'WF-does-not-exist' not found (HTTP 404)` and does not read `waiting job`.
11. **Deny the calibration.** State the same sentence under **Stop before each change**, approve, execute, fill the two real ids, **Continue plan**, and at "Approve step 1: Calibrate workflow (workflow.calibrate)?" press **Deny**. Expected: the plan reads `completed` and the step `skipped`; no job was queued.

## Verify it worked

API checks (the script does all of them; these are the ones to read by eye):

- Before the last approval: `GET /aria/plans/{id}` has `review_queue.create` and `review_queue.add_items` `done` (`results` `{queue_id, ...}` and `{queue_id, added: 12}`) and `workflow.calibrate` `waiting_input`.
- After it: `plan.status: "running"`, `workflow.calibrate` `waiting_job` with a `job_id`, no pending question.
- At the end: the plan mirrors the job. `GET /jobs/{job_id}` `rejected` with `error_message: "workflow calibration rejected: no candidate met calibration gates; quality delta +0.000 < epsilon 0.02"` means plan `failed` and the step error `rejected: <that message>`; `candidate_ready` or `applied` means plan `completed` and the step `done`.
- `GET /jobs/{job_id}` has `eval_results.calibration: true`, `n_examples: 20` (the default budget) and a `baseline`.
- `GET /review-queues/{queue_id}`: `queue.item_count: 12`, `queue.pending_count: 9` after three reviews; `GET /observability/traces/{trace_id}` of a triaged run lists `resolution_correct`, `failure_mode`, `severity` and `expected_answer` under `assessments`.

UI checks: steps 1 to 11 above, each with the text or status it names.

## Common errors and troubleshooting

| You see | Cause and fix |
| --- | --- |
| `404 {"reason_code": "feature_disabled"}` on `/aria/...`; no **Plans** in the sidebar | Start the server with `--flags aria` (`CALIBER_FEATURE_ARIA=true`) and restart |
| `SetupError: start CALIBER with CALIBER_OPERATOR_USERS=admin,olga ...` | The server was started without the export in Prerequisites; restart it with it. `vera` must not be listed |
| `could not sign in as 'olga': 401` | An account from an earlier run has another password: reset it under Administration, or set `COOKBOOK_ARIA_PASSWORD` |
| `ModuleNotFoundError: mlflow` or the traces cannot be written | Run with the dev virtualenv (`caliber/.venv/bin/python`) and make sure `MLFLOW_TRACKING_URI` is set (`source .tmp/cookbook-env/lab15/env.sh`) |
| The plan stays `running` with the step `waiting job` for minutes | The refinement worker or the plan worker is not running (`scripts/cookbook-env.sh up` starts both; `--no-workers` does not). Check the job: `GET /jobs/{job_id}`. A plan cannot be cancelled while parked |
| The plan ends `failed` with `rejected: workflow calibration rejected: no candidate met calibration gates; quality delta +0.000 < epsilon 0.02` | Expected on the simulated provider: the candidate cannot beat the baseline. Configure a real provider to let a candidate win, or run `POST /workflows/{id}/calibration/runs` by hand with an objective whose margin is 0 |
| `workflow '...' not found (HTTP 404)` / `agent '...' is not registered (HTTP 400)` / `Workflow calibration requires an active deploy-gate eval dataset with examples. (HTTP 400)` | The calibration needs a workflow you can see, a registered agent and a workflow whose manifest has a deploy gate pointing at a test set that has rows. `setup_subject.py` makes all three |
| `ValueError: review-queue name '...' is already in use` | Queue names are unique across the deployment; the script adds a time stamp |
| `409 ... must contain at least 1 character(s)` / `... is missing required input field(s)` / `... received unknown input field(s)` | The form's answer does not fit the capability's schema; the question stays open, answer it again |
| `plan owner lacks required scope(s): operator` | The account that owns the plan is not in `CALIBER_OPERATOR_USERS`. Plans run with their owner's scopes |
| `403 only the plan owner may answer this interaction` / `404 aria plan ... not found` | Another account answered or read it. Plans are private to their owner |
| The Plans page still says `waiting job` | The job has not finished: check `GET /jobs/{job_id}` (the page re-reads a running plan every five seconds). If the job is `queued` and stays so, the workers are off |

## Not covered by this lab

A calibration that finds a candidate, and the plan completing because of it, is not demonstrated: it needs a model behind the workflow executor. The plan does not apply the candidate: applying is a person's decision (`POST /jobs/{id}/apply`). Aria does not build the workflow or discover the traces. The enqueue step does not check that the trace ids exist. A review queue created outside a workspace is visible to its owner only (lab 13): the reviewers here are a label. The 12 flagged traces are recorded runs written by a script; the workflow's own runs do not write traces on this stack.

## Cleanup and reset

```bash
# archive the review queues, test sets and workflows the script created (names cookbook-15-*)
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_15_aria_triage_recalibrate_loop --cleanup
# reset everything: stop the stack and delete its state
scripts/cookbook-env.sh down lab15 && rm -rf .tmp/cookbook-env/lab15
```

Plans cannot be deleted: the script cancels every draft it makes, and plans that ran stay in the **Plans** list. The two tools stay registered (a second run keeps them), the agent stays, and the traces stay in MLflow. `olga` and `vera` stay as accounts; disable or reset them under Administration. The UI spec leaves its `c15*` objects in the disposable stack.
