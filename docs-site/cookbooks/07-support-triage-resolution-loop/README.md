# Support Triage Copilot

> **Prerequisite flag:** `CALIBER_FEATURE_COOKBOOKS=true` (the Cookbooks gallery is a preview area and is off by default). Nothing else in this lab is behind a flag.

## What this lab demonstrates

A workflow that reads a support ticket, decides what to do with it, and acts only as far as a person has allowed. It returns one of four outcomes (`reply`, `clarify`, `escalate_support`, `escalate_bug`), gives each ticket a severity and an SLA, and pauses every refund and bug filing at a human approval gate. You run it through the queue over 44 realistic tickets, answer the gate as a second person, break it on purpose with a bad version, and roll it back.

Everything runs offline and deterministically. **The classifier is rules, not a model**: one Python Code node holds the policy, so the routing can be checked exactly against a dataset. The one agent node (`draft_reply`) is simulated under the default fake provider; the lab checks that it ran on the right branch, never what it wrote. With a real model you would put it in front of (or in place of) the rules; the router, the approval gate, the dataset check and the release controls would not change.

You are done when:

- the script prints `All documented outcomes verified.` and exits 0;
- all 44 tickets got the expected decision, severity, SLA minutes, approval flag and flags (`44/44`), and exactly the 15 approval-required tickets are paused at the gate;
- the account that started a run was refused when it tried to approve it (403), an approver without the `caliber.approver` scope was refused (403), and a second person with it released the action;
- a rejected run ended `failed` and never reached the node that releases the action;
- a deliberately bad version was caught by the dataset (`3 of 11 bug tickets wrong`) and rolling the alias back made the same tickets pass again.

### What is simulated, and what a real provider or connector would change

| Piece | In this lab | With a real setup |
| --- | --- | --- |
| Ticket classification | Deterministic rules in `assets/workflows/classify-ticket.py` | A model (agent node) proposes, the rules or a judge check it; scores would then be model-dependent |
| `draft_reply` agent | Fake provider, canned text `[support-reply] processed: ...` | A real draft; the lab would still assert only routing and approval |
| Evidence (account state, incidents, knowledge base) | Not in the verified path | Tool, Knowledge Query and `api_request` nodes feeding the reply; each needs its connector |
| External write (`issue_write`, refund) | The `action_request` template node stands in; it releases only after approval | An MCP or API Request node after the same gate (needs `CALIBER_FEATURE_INTEGRATIONS=true` for MCP) |
| Evaluations / judges | Not used: the dataset check is exact | `Evaluations` with a `Judge.<id>` scorer needs a real model; with the simulated provider it is refused (see lab 01) |

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

`CALIBER_FEATURE_COOKBOOKS=true` is set by `--flags cookbooks`. The default simulated providers (`CALIBER_LLM_PROVIDER=fake`, `CALIBER_EVAL_PROVIDER=fake`) are what this lab is written for.

## Dataset and sample files

| File | What it is | Why |
| --- | --- | --- |
| `assets/dataset/ticket-cases.jsonl` | 44 tickets, one JSON object per line: `{id, tags, input: {ticket_text, channel, customer_tier, [account_id], [order_id], [duplicate_of]}, expected: {decision, severity, sla_minutes, requires_approval, flags}}` | The routing is verified against it. It has how-to questions (including a hard negative that mentions a refund, `TK07`), vague, empty and French tickets, prompt injection that asks for a refund and a bug filing (`TK16`, `TK17`), abusive messages, a pasted card number, SSN, password and API key, GDPR and legal threats, three duplicates of an open ticket, eleven bugs of every severity (data loss, a noisy mail thread, a cosmetic one), and refund or chargeback requests with and without an account id |
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

The Playwright spec `caliber/caliber-ui/e2e/cookbooks/labs/07-support-triage.spec.ts` performs exactly these steps with real clicks (sign in as `admin` / `admin`; it creates `lena` and `sam` when they do not exist). The alpha web app is in single-environment mode: there is no Deployments or Promotions tab, so the staging alias, gated promotion and rollback are API only (above).

1. **Administration.** `Admin > Administration > Accounts`: create `lena` and `sam` (password `cookbook07-lab-passphrase`). In the top bar press `+` (Create workspace), name it, `Create and select`. Back in Administration, `Project access`: pick the workspace and add `lena` and `sam` with the role `Reviewer`. Expected: `Created lena.`, `Added lena to <workspace>.`
2. **Import the workflow.** `Agent resources > Workflows > Import`: type a name, `Choose file` `assets/workflows/support-triage.workflow.json`, `Validate dependencies`. Expected: `Preflight passed`; then `Import as new` opens the editor with the nodes `classify`, `triage_router`, `draft_reply`, `review`, `action_request` in the outline.
3. **Validate; Preview is refused.** In the editor press `Validate`: the toolbar says `Valid.`. Press `Preview`, type any text, `Run Preview`. Expected: the result shows `error` and the step `classify`; open that step and it says `preview cannot isolate this capability; no workflow node ran`.
4. **Publish.** `Publish`, `Run Validation`, `Next`, `Next`, `Publish`. Expected: the toolbar says `Published.` (the drawer text says "then deploys it live"; in this build publishing creates the version and does not move an alias).
5. **Run two tickets.** Open `Run Monitor`, paste a ticket from `ticket-cases.jsonl` (the `input` object) and press `Run`. The how-to ticket `TK01` reaches `completed`; the output text contains `processed`. The bug ticket `TK29` (`The export button throws a 500 ... in production`) stops at `waiting_approval` and shows `Approve` and `Reject`.
6. **Try to approve your own run.** Press `Approve` as `admin`. Expected: `Approve failed: the account that triggered this run cannot approve it; a second person must decide (set CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=true for a single-operator install)`.
7. **A second person approves.** Sign in as `lena` (own browser profile), pick the workspace in the top bar, open `Workflows`, the workflow, tab `Runs`; select the run that shows `Awaiting approval`. Press `Approve`, then `Resume`. Expected: the run shows `Completed` and its output starts with `APPROVED_ACTION`.
8. **A rejection releases nothing.** As `admin` run the refund ticket `TK40` (`I was charged twice for October...`); it pauses at the gate. As `lena` open it on the `Runs` tab and press `Reject`. Expected: `Failed`, and the run never reaches `action_request`.

## Verify it worked

API checks (the script does all of them; these are the ones to read by eye):

- `GET /workflows/{id}/deployments` lists `staging` on version 1 (version 2 only between the break and the rollback).
- `GET /workflow-runs/{approved run}` has `status: "completed"`, `summary.node_path` ending `review`, `action_request` and `summary.output` starting `APPROVED_ACTION`.
- `GET /workflow-runs/{rejected run}` has `status: "failed"`, `error_code: "approval_rejected"`, and `action_request` is not in its `node_path`.
- `GET /audit-log?entity_id={approved run}` shows `enqueue_workflow_run` by `admin`, `workflow_run_waiting_approval` by the worker, `resume_workflow_run` by `lena`, `complete_workflow_run`; `GET /audit-log?action=approve_workflow_run_runtime_approval` shows the decision by `lena` with the run id in `details`.
- Counts: `clarify 10`, `escalate_bug 11`, `escalate_support 12`, `reply 11`; 15 runs `waiting_approval`.

UI checks: steps 5 to 8 above, each with the status text it names.

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
| The `draft_reply` text is `[support-reply] processed: ...` | The simulated provider; the lab does not assert agent text |

## Not covered by this lab

The original recipe's knowledge-base retrieval, evidence tools, GitHub MCP write and `GroundedSupportReply` judge scoring need a model, a knowledge base and connectors that are not available on the lab machine, so they are not exercised and are not claimed. They are the next layer: the files in `assets/prompts`, `assets/skills`, `assets/tools` and `assets/judges` are kept as the starting point, unverified. Alias deployment, gated promotion and rollback are API only in this alpha UI. Wait-for-event, error boundaries, the deploy gate and workflow calibration are exercised in lab 08.

## Cleanup and reset

```bash
# archive what the script created (projects, workflows, test sets named cookbook-07-*)
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_07_support_triage_copilot --cleanup
# reset everything: stop the stack and delete its state
scripts/cookbook-env.sh down lab07 && rm -rf .tmp/cookbook-env/lab07
```

`lena` and `sam` stay as accounts; disable or reset them under Administration. The UI spec leaves its `lab07-ui-*` workspaces and workflows in the disposable stack.
