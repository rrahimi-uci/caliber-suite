# Support Triage Copilot

> **Prerequisite flag:** `CALIBER_FEATURE_COOKBOOKS=true` (the Cookbooks gallery is a preview area and is off by default). Nothing else in this lab is behind a flag.
> The main path runs offline and deterministically on the alpha's default **simulated** providers. It was also run, end to end, against **real providers**
> (`--real-llm`), and an optional model-based variant was added there: see [Run it with a real provider](#run-it-with-a-real-provider).

## What this lab demonstrates

A workflow that reads a support ticket, decides what to do with it, and acts only as far as a person has allowed. It returns one of four outcomes (`reply`, `clarify`, `escalate_support`, `escalate_bug`), gives each ticket a severity and an SLA, and pauses every refund and bug filing at a human approval gate. You run it through the queue over 44 realistic tickets, answer the gate as a second person, break it on purpose with a bad version, and roll it back.

The main path runs offline and deterministically. **The classifier is rules, not a model**: one Python Code node holds the policy, so the routing can be checked exactly against a dataset. The one agent node (`draft_reply`) is simulated under the default fake provider; the lab checks that it ran on the right branch, never what it wrote. With a real model you would put it in front of (or in place of) the rules; the router, the approval gate, the dataset check and the release controls would not change. That is what the optional real-provider variant shows: a model classifies, a deterministic guard decides, every escalation still waits for a person.

You are done when:

- the script prints `All documented outcomes verified.` and exits 0;
- all 44 tickets got the expected decision, severity, SLA minutes, approval flag and flags (`44/44`), and exactly the 15 approval-required tickets are paused at the gate;
- the account that started a run was refused when it tried to approve it (403), an approver without the `caliber.approver` scope was refused (403), and a second person with it released the action;
- a rejected run ended `failed` and never reached the node that releases the action;
- a deliberately bad version was caught by the dataset (`3 of 11 bug tickets wrong`) and rolling the alias back made the same tickets pass again;
- on a real-provider stack, the same holds, the reply agent wrote real text, Evaluations is shown refusing the Python-based workflow row by row, and the model-based variant
  is evaluated, run through the queue and calibrated, with no escalation released without a person.

### What is simulated, and what a real provider or connector would change

| Piece | In this lab (simulated stack) | On a real-provider stack (run and verified, see below) | Still not here |
| --- | --- | --- | --- |
| Ticket classification | Deterministic rules in `assets/workflows/classify-ticket.py` | Unchanged: the rules do not use a model. The optional **variant** replaces them with an agent plus a deterministic guard | Tool, Knowledge Query and `api_request` evidence for the reply (each needs a connector) |
| `draft_reply` agent | Fake provider, canned text `[support-reply] processed: ...` | Real text, written by the model (asserted: non-empty, not the simulated echo; the wording is not asserted) | Citing a knowledge base (none is connected) |
| Evidence (account state, incidents, knowledge base) | Not in the verified path | Not in the verified path | Needs connectors |
| External write (`issue_write`, refund) | The `action_request` template node stands in; it releases only after approval | Same stand-in | An MCP or API Request node after the same gate (needs `CALIBER_FEATURE_INTEGRATIONS=true` for MCP) |
| Evaluations / judges | Refused: scoring needs a real model | **Works on the variant** (a deterministic scorer and a model-graded judge). On the Python-based workflow every row errors, because Preview cannot run a Python Code node | A grounding judge over retrieved evidence |
| Workflow calibration | Not run (the manifest has no test set for it) | Run on the variant: it reports a measured baseline and **no candidate**, marked `Structural score` and not simulated | A candidate: calibration moves are guardrails, tool constraints and handoffs, and there is no tool to constrain |

## Prerequisites and setup

You need a CALIBER server from `docs/start/alpha-install.md`. For a disposable one, `scripts/cookbook-env.sh` starts exactly that stack (migrate, MLflow, CALIBER, simulated providers, workers and the workflow run queue on). From the repo root:

```bash
# two extra people: lena approves, sam is an operator who must NOT be able to approve
export CALIBER_OPERATOR_USERS=admin,lena,sam
export CALIBER_APPROVER_USERS=admin,lena
# promotions to a "staging" alias wait for a second person (the lab shows it)
export CALIBER_RELEASE_REQUIRE_HUMAN_APPROVAL_FOR_ENVIRONMENT_CLASSES=staging

scripts/cookbook-env.sh up lab07 --flags cookbooks
source .tmp/cookbook-env/lab07/env.sh      # CALIBER_URL, CALIBER_BASE_URL, CALIBER_API, ...
```

Sign in at `$CALIBER_URL/caliber/` as `admin` / `admin`, then make an access token for the script (Settings > API tokens, or):

```bash
curl -s -c /tmp/lab07.jar -X POST "$CALIBER_API/auth/login" -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"admin"}' >/dev/null
export CALIBER_TOKEN=$(curl -s -b /tmp/lab07.jar -X POST "$CALIBER_API/auth/tokens" \
  -H 'content-type: application/json' -d '{"name":"lab07"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["token"])')
```

`CALIBER_FEATURE_COOKBOOKS=true` is set by `--flags cookbooks`. The default simulated providers (`CALIBER_LLM_PROVIDER=fake`, `CALIBER_EVAL_PROVIDER=fake`) are what the main path is written for; `--real-llm` swaps them for real ones (see [Run it with a real provider](#run-it-with-a-real-provider)).

## Dataset and sample files

| File | What it is | Why |
| --- | --- | --- |
| `assets/dataset/ticket-cases.jsonl` | 44 tickets (the script posts them to a test set; in the UI you choose the file under **Test Sets > Import from file**, step 2), one JSON object per line: `{id, tags, input: {ticket_text, channel, customer_tier, [account_id], [order_id], [duplicate_of]}, expected: {decision, severity, sla_minutes, requires_approval, flags}}` | The routing is verified against it. It has how-to questions (including a hard negative that mentions a refund, `TK07`), vague, empty and French tickets, prompt injection that asks for a refund and a bug filing (`TK16`, `TK17`), abusive messages, a pasted card number, SSN, password and API key, GDPR and legal threats, three duplicates of an open ticket, eleven bugs of every severity (data loss, a noisy mail thread, a cosmetic one), and refund or chargeback requests with and without an account id |
| `assets/workflows/support-triage.workflow.json` | The workflow manifest (9 nodes) | What the lab imports |
| `assets/workflows/classify-ticket.py` | The body of the `classify` node, readable on its own | The script refuses to run if the manifest's code and this file drift apart |
| `assets/prompts`, `assets/skills`, `assets/tools`, `assets/judges` | The model-based layer from the original recipe (reply prompt, tone and escalation skills, evidence tools, a grounding judge) | Reference only. The lab does not run them: there is no model, connector or knowledge base on the lab machine |

The routing policy, in the order the rules run (the first match wins):

| Rule | Decision | Severity | Flag |
| --- | --- | --- | --- |
| empty ticket | clarify | low | `empty_ticket` |
| asks the assistant to ignore its instructions | clarify | low | `prompt_injection` |
| abusive wording | escalate_support | medium | `abusive` |
| card number, SSN or pasted secret | escalate_support | high | `pii_exposed` |
| GDPR, legal threat, data breach | escalate_support | high | `legal_or_privacy` |
| `duplicate_of` present | reply | low | `duplicate` |
| bug markers (500, stuck, crash, regression, outage...) | escalate_bug, **approval** | low to urgent | |
| charged twice, chargeback, refund request, with an `account_id` | escalate_support, **approval** | medium (high if enterprise or unauthorized) | `refund_request` |
| the same without an `account_id` | clarify | low | `missing_account_id` |
| how-to wording | reply | low | |
| anything else | clarify | low | |

SLA minutes come from the customer tier and the severity: enterprise 15 / 60 / 240 / 720, pro 30 / 120 / 480 / 1440, free 120 / 480 / 1440 / 4320 (urgent / high / medium / low).

## Configuration and environment

| Variable | Value used | What it does |
| --- | --- | --- |
| `CALIBER_FEATURE_COOKBOOKS` | `true` | Opens the Cookbooks gallery and install route (404 `feature_disabled` without it) |
| `CALIBER_OPERATOR_USERS` | `admin,lena,sam` | Accounts holding `caliber.operator` (run workflows, promote) |
| `CALIBER_APPROVER_USERS` | `admin,lena` | Accounts holding `caliber.approver` (decide an approval). `sam` is deliberately absent |
| `CALIBER_RELEASE_REQUIRE_HUMAN_APPROVAL_FOR_ENVIRONMENT_CLASSES` | `staging` | A promotion to a `staging`-class alias waits for approval instead of rotating |
| `CALIBER_WORKFLOW_RUN_QUEUE_ENABLED`, `..._RUN_CHECKPOINTING_ENABLED`, `..._RUN_RUNTIME_APPROVALS_ENABLED` | `true` (set by `scripts/cookbook-env.sh`) | Needed for approvals, waits and resume |
| `CALIBER_APPROVAL_ALLOW_SELF_APPROVAL` | unset | If `true`, the account that started a run may approve it (single-operator installs only) |
| `CALIBER_BASE_URL` | `$CALIBER_URL`, the server root without `/ajax-api/...` | What the script's SDK client calls |
| `CALIBER_TOKEN` | the admin token above | The script acts as `admin` and signs in as `lena` and `sam` itself |
| `COOKBOOK_07_PASSWORD` | optional | Password for the `lena` and `sam` accounts the script creates (default `cookbook07-lab-passphrase`) |
| `COOKBOOK_07_ASSETS` | optional | Another assets directory |
| `OPENAI_API_KEY` | unset on the simulated tier; **your own key** with `--real-llm` | Read by `scripts/cookbook-env.sh` from your environment (or the gitignored repo-root `.env`); never printed or written to the stack's files, never put in a file you commit |
| `--model-variant` (script flag) | off | Also runs the optional model-based variant (real provider only; refused on a simulated stack) |

## Run it through the API

One command runs the whole lab and verifies every outcome:

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_07_support_triage_copilot
```

It creates a project, adds `lena` and `sam` as reviewers, installs the Cookbooks starter, imports the workflow and the 44-ticket test set, runs every ticket through the queue, answers the gate, breaks and rolls back. Real output from a run (ids and the timing vary):

```text
ok   lena holds caliber.approver; sam does not  [project PRJ-3f678178]
ok   the installed starter arrives paused (activation needs review)
ok   the starter pauses at its approval node  [WR-44af162c]
ok   the person who started the run cannot approve it (403)
ok   an operator without caliber.approver cannot approve (403)
ok   a second person with the scope approves and the run completes
ok   a rejection ends the run as failed with approval_rejected  [approval_rejected]
ok   the imported manifest validates
ok   Preview refuses a workflow with a python_code node  [preview blocked before execution because ordinary Preview cannot isolate these capability ]
ok   a synchronous run of a workflow with an approval gate is refused (400): use the queue
ok   the staging promotion waits for approval  [WPR-c7e0d92e]
ok   staging now serves version 1
ok   the test set holds every ticket  [44 rows]
ok   the stored rows are the file's rows
ok   every ticket routed as expected (decision, severity, SLA, approval, flags, path)  [44/44 in 93.3s]
ok   exactly the approval-required tickets are paused at the gate  [15 paused]
ok   TK29: approved, resumed, and the action was released
ok   TK40: rejected, and no action was released
ok   the remaining paused runs cancel cleanly  [13 runs]
ok   the staging promotion waits for approval  [WPR-2a8fcd09]
ok   staging now serves version 2 (the bad one)
ok   the dataset catches the regression (the bug tickets that lost their severity)  [3 of 11 bug tickets wrong: ['TK29', 'TK30', 'TK35']]
ok   rollback puts staging back on version 1
ok   after the rollback the same tickets pass again  [11 bug tickets]
ok   the audit log names who enqueued, who approved and who resumed the approved run  [5 entries]

All documented outcomes verified.
```

The script then prints a JSON summary: `batch.decisions` is `{clarify: 10, escalate_bug: 11, escalate_support: 12, reply: 11}` and `batch.waiting_approval` lists the 15 paused ticket ids.

### What each step does, by hand

The script is plain SDK calls; the same steps with `caliberctl` or `curl` are the routes below (all under `$CALIBER_API`):

| Step | Call |
| --- | --- |
| Install the starter | `POST /cookbooks/07/install {"name", "acknowledge_prerequisites": true}`; the workflow arrives `paused`, so `PATCH /workflows/{id} {"status":"active"}` before the first run |
| Import the lab workflow | `POST /workflows/import {"manifest": <support-triage.workflow.json>, "name": "..."}` |
| Validate / Preview | `POST /workflow-versions/{id}/validate`; `POST /workflow-versions/{id}/preview-run` answers `status: "error"` (it does not run a Python Code node) |
| Publish, promote | `POST /workflow-versions/{id}/publish`; `POST /workflows/{id}/deployments/staging/promote {"version_id"}` answers `202` with a pending promotion; `POST /workflow-promotions/{promotion_id}/approve` as a second person |
| Load the dataset | `POST /eval-datasets {"name","owner"}`, then `POST /eval-datasets/{id}/examples {"input","expected","tags"}` per line |
| Run a ticket | `POST /workflow-runs {"workflow_id","alias":"staging","input":"<ticket JSON as text>","idempotency_key"}` answers `202`; poll `GET /workflow-runs/{id}` |
| Read the verdict | `summary.steps[]` of the run: the `classify` step's `output_by_port.result.result` is the triage record; `summary.node_path` is the route taken; `summary.output` is the final text |
| Answer the gate | `POST /workflow-runs/{id}/approval/approve` (or `/reject`), then `POST /workflow-runs/{id}/resume` |
| Cancel | `POST /workflow-runs/{id}/cancel` |
| Roll back | `POST /workflows/{id}/deployments/staging/rollback` |
| Audit | `GET /audit-log?entity_id=<run id>` and `GET /audit-log?action=approve_workflow_run_runtime_approval` |

A ticket is submitted as JSON text, for example `{"ticket_text": "...", "channel": "web", "customer_tier": "pro", "account_id": "acct_2101"}`. A plain string is read as a ticket with no tier (treated as `free`).

### Failure drill: the run queue is off

The run queue is not an option the script can switch, so do it by hand. Restart the same stack with the queue disabled, ask for an asynchronous run, then restart it again:

```bash
CALIBER_WORKFLOW_RUN_QUEUE_ENABLED=false scripts/cookbook-env.sh restart lab07
# POST /workflow-runs ...  ->  409
#   workflow run queue is disabled (set CALIBER_WORKFLOW_RUN_QUEUE_ENABLED=true)
scripts/cookbook-env.sh restart lab07      # queue on again (re-export the variables from Prerequisites first)
```

Observed: `POST /workflow-runs` answers `409 {"detail": "workflow run queue is disabled (set CALIBER_WORKFLOW_RUN_QUEUE_ENABLED=true)"}`. A synchronous run of this workflow (`POST /workflow-versions/{id}/run`) is refused in every configuration: `400 cannot run workflow: synchronous real runs cannot checkpoint runtime approvals required by human approval node 'review'; submit through the queued /workflow-runs endpoint`.

## Recipe: run it through the UI

The Playwright spec `caliber/caliber-ui/e2e/cookbooks/labs/07-support-triage.spec.ts` performs exactly these steps with real clicks and uploads, from a stack you have just started (sign in as `admin` / `admin`; it creates `lena` and `sam` when they do not exist). It makes no API call: it even reads whether the stack is simulated from the page's own banner. The alpha web app is in single-environment mode: there is no Deployments or Promotions tab, so the **staging alias, the gated promotion and the alias rollback are API only** (above). Everything else, including versioning (restore, edit, publish, compare), is in the UI.

1. **Administration.** `Admin > Administration > Accounts`: create `lena` and `sam` (password `cookbook07-lab-passphrase`). In the top bar press `+` (Create workspace), name it, `Create and select`. Back in Administration, `Project access`: pick the workspace and add `lena` and `sam` with the role `Reviewer`. Expected: `Created lena.`, `Added lena to <workspace>.`
2. **Load the tickets as a test set.** `Prompt lifecycle > Test Sets > + New Test Set`: name it, owner `admin`, `Create`, open it, `Import from file`, choose `assets/dataset/ticket-cases.jsonl`. Expected: `ticket-cases.jsonl: 44 rows ready to import.`; press `Import 44 rows`; the header reads `44 active examples` and each row shows the ticket, its expected decision, severity, SLA and tags (the `id` and any other key in the file is left behind; a bad line is named, for example `Line 7 is not valid JSON.`, and nothing is imported).
3. **Import the workflow.** `Agent resources > Workflows > Import`: type a name, `Choose file` `assets/workflows/support-triage.workflow.json`, `Validate dependencies`. Expected: `Preflight passed`; then `Import as new` opens the editor with the nodes `classify`, `triage_router`, `draft_reply`, `review`, `action_request` in the outline.
4. **Validate; Preview is refused.** In the editor press `Validate`: the toolbar says `Valid.`. Press `Preview`, type any text, `Run Preview`. Expected: the result shows `error` and the step `classify`; open that step and it says `preview cannot isolate this capability; no workflow node ran`.
5. **Publish.** `Publish`, `Run Validation`, `Next`, `Next`, `Publish`. Expected: the toolbar says `Published.` and the editor turns read-only (version 1 is immutable). In this build publishing creates the version and does not move an alias.
6. **Run two tickets.** Open `Run Monitor`, paste a ticket from `ticket-cases.jsonl` (the `input` object) and press `Run`. The how-to ticket `TK01` reaches `completed`; the output text contains `processed` (simulated) or real text. The bug ticket `TK29` (`The export button throws a 500 ... in production`) stops at `waiting_approval` and shows `Approve` and `Reject`.
7. **Try to approve your own run.** Press `Approve` as `admin`. Expected: `Approve failed: the account that triggered this run cannot approve it; a second person must decide (set CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=true for a single-operator install)`.
8. **A second person approves.** Sign in as `lena` (own browser profile), pick the workspace in the top bar, open `Workflows`, the workflow, tab `Runs`; select the run that shows `Awaiting approval`. Press `Approve`, then `Resume`. Expected: the run shows `Completed` and its output starts with `APPROVED_ACTION`.
9. **A rejection releases nothing; an operator cannot decide.** As `admin` run the refund ticket `TK40` (`I was charged twice for October...`); it pauses at the gate. As `sam` (an operator without `caliber.approver`) open it on the `Runs` tab and press `Approve`: the page shows the missing `caliber.approver` scope. As `lena` press `Reject`. Expected: `Failed`, and the run never reaches `action_request`.
10. **Who did what.** `Operate > Audit Log`: type `approve_workflow_run_runtime_approval` in **Action**: the newest row is by `lena`; `enqueue_workflow_run`: rows by `admin`.
11. **Modify, new version, re-run, compare.** `Workflows` > the workflow > `Versions`: `Restore as draft` on v1 opens a new editable draft (v2). Select `classify` (double-click in the outline), and in the inspector's **Python code** change `urgent_markers = has([` to `urgent_markers = False and has([` (a "tidy-up" that stops flagging production impact). `Save`, `Validate` (`Valid.`), open `Run Monitor`, run `TK29` again: it still waits for approval, but on the workflow's `Runs` tab, with that run selected, the `classify` step's output now says `"severity": "medium"` where v1 said `"high"`. Back in the editor press `Publish` (v2). On `Versions`, pick v1 as **Base (older)** and v2 as **Compare (newer)**: one modified node, `classify`.
12. **Roll back.** `Versions`: `Restore as draft` on v1 again (v3), run `TK29`: the `classify` step says `"severity": "high"` again; `Publish`. `Versions` lists three versions. (This is how a version is rolled back in this UI; moving an alias back is the API's `.../deployments/staging/rollback`.)


## Run it with a real provider

The same lab on a stack with a real model and the real evaluator. Run on 2026-10-19 with the default model (`gpt-5.6-luna`); generated text and model-dependent results **will vary**, so the script
and the spec assert invariants (ranges, counts, "nothing is released without a person"), never wording or an exact score.

**1. Start the stack** (your own key, never committed; `--real-llm` reads it from your shell):

```bash
export OPENAI_API_KEY=sk-...                       # your key
export CALIBER_OPERATOR_USERS=admin,lena,sam CALIBER_APPROVER_USERS=admin,lena
export CALIBER_RELEASE_REQUIRE_HUMAN_APPROVAL_FOR_ENVIRONMENT_CLASSES=staging
scripts/cookbook-env.sh up lab07h --flags cookbooks --real-llm
source .tmp/cookbook-env/lab07h/env.sh
curl -s $CALIBER_API/readiness | jq -c '.data | {simulated, all_real}'      # {"simulated":[],"all_real":true}
```

Make the token as in "Prerequisites and setup" (`/tmp/lab07h.jar`, name `lab07h`).

**2. Run the script, with the optional variant:**

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_07_support_triage_copilot --model-variant
```

About 6 to 7 minutes. Without `--model-variant` the script runs the main path only (about 4 minutes of which 144 s are the 44 tickets). `GET /readiness` decides the mode; on a simulated stack the flag is refused
(`SetupError: --model-variant needs a real provider`). Real output, trimmed to what differs from the simulated run (the first 24 lines are the ones shown in "Run it through the API"):

```text
ok   every ticket routed as expected (decision, severity, SLA, approval, flags, path)  [44/44 in 144.8s]
ok   exactly the approval-required tickets are paused at the gate  [15 paused]
ok   the reply agent wrote real text, not the simulated echo  [11 replies]
...
ok   Evaluations cannot score the deterministic workflow, and each row says why  [preview blocked before execution because ordinary Preview cannot isola...]
ok   the model variant validates
ok   Evaluations scores the model variant over every labelled ticket  [label agreement 1.0, judge 1.0, 0 rows errored]
ok   the model-graded judge ran next to the deterministic scorer  [1.0]
ok   through the queue, no escalation is released without a person  [6 of 12 runs paused at an approval gate]
ok   a second person's approval releases the escalation  [ESCALATE_SUPPORT. A human appr]
ok   the calibration evidence says what measured its baseline  [{'provider': 'openai', 'simulated': False, 'scorer': 'structural', 'n_examples': 13}]

All documented outcomes verified.
```

and in the JSON summary (`"mode": "real"`):

```json
"variant": {"tickets": 12, "label_agreement": 1.0, "judge": 1.0, "disagreements": [], "paused_at_gate": 6,
            "calibration": {"status": "rejected", "n_examples": 13, "baseline_quality": 0.923, "candidates": 0,
                            "provider": "openai", "simulated": false, "scorer": "structural"},
            "evaluation_run": "EVR-8b8bb9c7"}
```

**3. What the variant is.** [`assets/workflows/support-triage-model.workflow.json`](assets/workflows/support-triage-model.workflow.json), the model-based layer of the original recipe made runnable:

```text
start -> fan_out -> classify_model (agent: ticket-intake prompt) -> guard (decision table) -> join -> triage_router
   escalate_bug     -> review_bug     (human approval) -> release_bug     -> final
   escalate_support -> review_support (human approval) -> release_support -> final
   reply            -> draft_reply (agent: reply prompt + tone skill) -> reply_out ("REPLY. ...") -> final
   anything else    -> clarify_question ("CLARIFY. ...") -> final
```

The model proposes `{"decision", "confidence", "reason"}` as one line of JSON. **The guard, a Data Transform decision table, is what decides**: a confidence under `0.6`, an unreadable reply (code fences, prose) or an unknown label
falls through to `clarify` (fail closed); `escalate_bug` and `escalate_support` always go to a human approval; nothing the model writes can skip it. There is no Python Code node, so Preview and Evaluations can run it. (Unlike the
rule-based workflow, every escalation here waits for approval, including the abusive and PII tickets that the rules send to a note without one: the model cannot know whether an account id is present, so the variant is conservative.)

**4. What Evaluations does with it, and with the deterministic workflow.** Evaluations runs a workflow version in Preview with the example's input. The rule-based version has a Python Code node, which Preview cannot run, so every row **errors** with the run's own reason and the run ends `failed` (before this lab
found it, those rows were silently scored `0` with an empty prediction and the run read `completed`; they now carry the error and stay out of the scorer averages):

```text
POST /evaluations {"dataset_id": "...", "predict_target": "workflow", "subject_ref": "<v1>", "scorers": ["contains_expected"], "max_examples": 2}
-> status "failed", passed_count 0, error_message and results[*].error = "preview blocked before execution because ordinary Preview cannot isolate these capability nodes: classify (python_code)"
```

The variant scores. Create the twelve labelled tickets as a test set (`TK01 TK07 TK26 TK10 TK16 TK44 TK18 TK20 TK40 TK29 TK33 TK37`: three per decision, with `expected.answer` set to the printed label `REPLY`, `CLARIFY`, `ESCALATE_SUPPORT`, `ESCALATE_BUG`), create the
judge from [`judges/support-triage-routing-and-reply.judge.json`](assets/judges/support-triage-routing-and-reply.judge.json), and run (the script does all of this):

```text
POST /evaluations {"dataset_id": "<12 tickets>", "predict_target": "workflow", "subject_ref": "<variant version>",
                   "scorers": ["contains_expected", "Judge.<judge id>"], "max_examples": 12}
```

Observed once: `contains_expected` 1.0 (12 of 12 tickets routed as the rules route them) and the judge 1.0, 0 rows errored. **What is model-dependent:** the label agreement and the judge verdict (a weaker model, or another run, may
misroute the hard negative `TK07` (a refund *question* that is a reply), the injection `TK16`, or `TK44` (no account id)); the reply text; the confidence. **What is not:** that an escalation is never released without a person (asserted below), and that an unreadable answer becomes
`clarify`. The script asserts `0.5 <= label agreement <= 1.0`; the product's own gate for the variant would use `min_pass_rate`.

**5. Through the queue, nothing is released without a person.** The twelve tickets are run through the queue against the published variant. The six that the model sent to `escalate_bug` or `escalate_support` stopped at `review_bug` or `review_support`
(`waiting_approval`), none reached a `release_*` node, and a second person's approve and resume released one (`ESCALATE_SUPPORT. A human approved this; the refund or account action is released now.`).

**6. Workflow calibration, on the variant.** The variant names a test set (`ticket-eval`) in a deploy gate that applies only to a `prod` alias, which is what gives calibration a dataset (the main workflow has none, so there is nothing to calibrate against). Observed:

```json
{"status": "rejected", "n_examples": 13, "baseline_quality": 0.923, "candidates": 0, "provider": "openai", "simulated": false, "scorer": "structural"}
```

The baseline was **measured** on a real model (the twelve tickets plus the flagged scenario the job adds, `n_examples` 13). No candidate was produced: calibration moves are guardrails, tool constraints and handoffs, and this workflow has no tool to constrain, so the run ends `rejected` with
`no candidate met calibration gates`. It is marked `provider: openai`, `simulated: false`, `scorer: structural` (the UI shows a **Structural score** badge and no **Simulated evaluation** one): a structural score checks that the run completed and used its tools, not that the label was right.
Turning on the LLM judge for calibration (`CALIBER_WORKFLOW_LLM_JUDGE_ENABLED=true` before `up`, then **Use LLM judge** in the Calibrate panel) changes the scorer to `llm_judge`; it was not run here.
(Before this lab ran it, a calibration that ended without a candidate stored no `provider`, `simulated` or `scorer` at all, so neither badge could show; it now stores them in every outcome.)

**7. Through the UI.** On a real-provider stack the spec `07-support-triage.spec.ts` runs three more steps after the twelve in "Recipe: run it through the UI" (same command, against `lab07h`; about 40 seconds):

13. **Test Sets.** `+ New Test Set`, name it, owner `admin`, then `+ Add example` four times (`TK01`, `TK10`, `TK29`, `TK40`): the input is the ticket's `input` object, expected is `{"decision": "...", "answer": "REPLY"}` (and `CLARIFY`, `ESCALATE_BUG`, `ESCALATE_SUPPORT`).
14. **Workflows > Import** the variant (`support-triage-model.workflow.json`, with its `ticket-eval` dataset name pointed at your test set): `Preflight passed`, the outline shows `classify_model`, `guard`, `review_bug`; **Validate** says `Valid.`; **Preview** `TK01` shows `completed` and a `REPLY.` text, `TK29` shows `ESCALATE_BUG` (Preview passes the approval node as if approved).
15. **Evaluations.** `Run evaluation`, pick the test set, **What to score** `Workflow version`, paste the version id (`WFV-...` from the editor URL), untick **Exact match** and **Token F1**, `Run`. The rule-based workflow's row reads `failed` and `0/4` (every row errors; open it to read the reason); the variant's reads `completed` and at least `2/4` (observed `4/4`).

On the main steps the only difference is step 6: the how-to reply is real text, no longer `processed`.

**Cost and time.** The script makes a few hundred small calls: the 11 reply tickets of the main run, plus for the variant 12 workflow runs (two agents each) and 12 judge calls for Evaluations, 12 more variant runs through the queue, and the calibration's replays of 13 examples. About 6 to 7 minutes and a few cents (I did not
measure tokens). The spec adds roughly the same for four tickets. The default model is `gpt-5.6-luna`.

**Troubleshooting the real tier.**

| What you see | Cause and fix |
| --- | --- |
| A run `failed` with `APIConnectionError: Connection error.`, or one run that hangs for minutes and then the next ones fail within a second | The network between the server and the provider dropped (seen once for about 9 minutes while running lab 08: the in-flight request waits for the client's 10-minute timeout, then the following ones fail at once). The script resubmits runs that failed for a transient reason (connection error, 429, rate limit, timeout), twice; `resubmitted_after_provider_errors` in the JSON lists them. If it keeps failing, check your network and rerun. |
| `401 ... Incorrect API key provided` in a run's error, or in a judge or evaluation | The key is wrong or revoked. Provoked on purpose with `OPENAI_API_KEY=sk-invalid`. OpenAI echoes a masked copy of the key in this message: redact it before pasting. Fix it and `scripts/cookbook-env.sh restart lab07h`. |
| `429` / rate limit | Too many calls at once. The script retries; otherwise wait and rerun, or use a key with a higher limit. |
| The model-based variant routes a ticket differently from the rules | Expected and the point: it is reported in `variant.disagreements`. It is only an error if the model's answer is released without a person (never observed; asserted) or the script's `0.5` floor is missed. |
| `SetupError: --model-variant needs a real provider` | The stack is simulated: `curl $CALIBER_API/readiness` must show `"simulated": []`; start it with `--real-llm`. |
| `Evaluations ... rows errored` for the variant | A node failed in Preview (a provider error, or a manifest edit). Read a row's `error`. |
| `404 model ... does not exist` from the provider | The default model is not available to your key: set `CALIBER_LLM_DIAGNOSIS_MODEL` before `up`. |

## Verify it worked

API checks (the script does all of them; these are the ones to read by eye):

- `GET /workflows/{id}/deployments` lists `staging` on version 1 (version 2 only between the break and the rollback).
- `GET /workflow-runs/{approved run}` has `status: "completed"`, `summary.node_path` ending `review`, `action_request` and `summary.output` starting `APPROVED_ACTION`.
- `GET /workflow-runs/{rejected run}` has `status: "failed"`, `error_code: "approval_rejected"`, and `action_request` is not in its `node_path`.
- `GET /audit-log?entity_id={approved run}` shows `enqueue_workflow_run` by `admin`, `workflow_run_waiting_approval` by the worker, `resume_workflow_run` by `lena`, `complete_workflow_run`; `GET /audit-log?action=approve_workflow_run_runtime_approval` shows the decision by `lena` with the run id in `details`.
- Counts: `clarify 10`, `escalate_bug 11`, `escalate_support 12`, `reply 11`; 15 runs `waiting_approval`.

UI checks: steps 6 to 12 above, each with the status text it names.

## Common errors and troubleshooting

| You see | Cause and fix |
| --- | --- |
| `404 {"reason_code": "feature_disabled"}` on `/cookbooks` ("Cookbooks is disabled in this deployment. Set CALIBER_FEATURE_COOKBOOKS=true to enable it.") | Start the server with `--flags cookbooks` |
| `SetupError: start CALIBER with CALIBER_OPERATOR_USERS=...` | The server was started without the two exports in Prerequisites; restart it with them |
| `could not sign in as 'lena': 401` | An account from an earlier run has another password: reset it under Administration, or set `COOKBOOK_07_PASSWORD` |
| `409 workflow is paused; resume it before running` | A Cookbooks install arrives `paused`; set `status` to `active` first (the script does) |
| `403 the account that triggered this run cannot approve it` | By design: approve as a second person, or set `CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=true` on a single-operator install |
| `403 missing required scope ... caliber.approver` | The approver is not in `CALIBER_APPROVER_USERS` |
| `403 project role 'viewer' cannot perform resource.approve` | The approver needs the project role `reviewer` or `owner` |
| `404 workflow run not found` for the approver | The workflow lives in a workspace the approver is not a member of, or the approver has not selected it |
| `400 promoting to 'prod' (production) requires a deploy gate` | A `prod` alias needs a deploy gate and a Python Code workflow cannot be gated (the gate replays in Preview); this lab uses `staging`. Lab 08 shows a gated `prod` |
| `409 workflow run queue is disabled` | `CALIBER_WORKFLOW_RUN_QUEUE_ENABLED=false`; turn it on |
| Preview shows `error` and the node `classify` | Expected: Preview cannot isolate a Python Code node. Run it through the queue |
| Runs stay `queued` for a long time | The single worker handles one run at a time (about 5 seconds each here: 44 tickets take about 4 minutes) |
| `Line 7 is not valid JSON.` or `Line 3 needs an "input" object.` under **Import from file** | The file is not the lab's JSON Lines file (or was edited): nothing was imported. Use `assets/dataset/ticket-cases.jsonl` or fix the named line |
| `Stopped after 12 of 44 rows: ...` under **Import from file** | The server refused row 13 (a network or permission error). The first 12 rows are in the test set; create a new test set and import again rather than importing twice into the same one |
| `Restore as draft` seems to do nothing | It opens the new draft's editor (v2, v3...) after a moment; a published version is read-only, so edit only the draft |
| The `draft_reply` text is `[support-reply] processed: ...` | The simulated provider; the lab does not assert agent text. On a real stack it is the model's text |

## Not covered by this lab

The original recipe's knowledge-base retrieval, evidence tools, GitHub MCP write and `GroundedSupportReply` judge scoring need a knowledge base and connectors that are not available on the lab machine, so they are not exercised and are not claimed. The intake prompt, the reply prompt and the tone skill went into the optional variant's two agents; the tools in `assets/tools` and `assets/judges/grounded-support-reply.judge.json`
(which grades a reply against retrieved evidence) stay reference-only. Alias deployment, gated promotion and alias rollback are API only in this alpha UI (single-environment mode); version restore, publish and compare are in the UI (steps 11 and 12). A workflow calibration **candidate** is not produced for this workflow (the run measures and rejects: see the real-provider section). Wait-for-event, error boundaries and the deploy gate are exercised in lab 08.

## Cleanup and reset

```bash
# archive what the script created (projects, workflows, test sets named cookbook-07-*)
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_07_support_triage_copilot --cleanup
# reset everything: stop the stack and delete its state
scripts/cookbook-env.sh down lab07 && rm -rf .tmp/cookbook-env/lab07
```

`lena` and `sam` stay as accounts; disable or reset them under Administration. The UI spec leaves its `lab07-ui-*` workspaces, workflows and test sets in the disposable stack (discard the stack to clean up).
