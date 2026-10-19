# Incident Response Commander

> **Prerequisite flag:** `CALIBER_FEATURE_COOKBOOKS=true` (the Cookbooks gallery is a preview area and is off by default). The optional MCP evidence nodes of the original recipe would also need `CALIBER_FEATURE_INTEGRATIONS=true`; they are not part of this lab.

## What this lab demonstrates

A workflow that turns an incident record (alert, recent deployments, health numbers, a noisy log excerpt, paging and customer-report counts) into a severity and the lowest-risk next action, and keeps a rollback behind a human approval. The whole decision is declarative (Data Transform decision tables, a Join, a Template, a Router), so unlike lab 07 **Preview can run it**, and CALIBER's **deploy gate can replay the 41-incident test set before `prod` moves**. You import it, preview it, promote it through a gated `prod` alias with a second person's approval, watch the gate refuse a bad version, run all 41 incidents through the queue, answer the rollback gate, and exercise the failure drills (Error Boundary, missing tool, blocked API Request, Wait For Event).

The only agent node (`draft_update`, the stakeholder update) runs on the simulated provider. The decision does not depend on it, and the lab never asserts what it wrote.

You are done when:

- the script prints `All documented outcomes verified.` and exits 0;
- Preview ran the workflow, the `prod` gate replayed all 41 incidents with a pass rate of 1.0 and named its executor as the simulated one, and a second person approved the promotion;
- a version with a bad rollback rule was refused by the gate with a pass rate under 1.0 and `prod` stayed on version 1;
- all 41 incidents got the expected severity and action through the queue, and exactly the 5 rollback incidents paused at the approval gate;
- self-approval was refused (403), an approval released `ROLLBACK_APPROVED`, a rejection released nothing;
- each failure drill behaved as written in the table below.

### What is simulated, and what a real provider or connector would change

| Piece | In this lab | With a real setup |
| --- | --- | --- |
| Severity and action | Deterministic decision tables (no model) | A model could propose; the tables, gate and approval still check it |
| `draft_update` agent | Fake provider, canned echo text | A real stakeholder update |
| Deploy gate | Replays 41 incidents with the **fake executor** (reported as `provider: fake`, `deterministic: true`). It proves the graph and the data, not a model | `CALIBER_RELEASE_REQUIRE_GRADED_EXECUTOR_FOR_ENVIRONMENT_CLASSES` defaults to `production`: a production gate then needs a real configured model. The lab sets it empty |
| Evidence (deployments, health, logs) | Supplied inside each incident record | `api_request` or MCP nodes (or the Python lookups in `assets/tools`) fetch it; each needs egress or a connector |
| Workflow calibration | Runs, finds no candidate (see below) | With agent guardrails or tool constraints to tune, candidates are produced and marked simulated under the fake provider |
| Evaluations on a workflow version | Refused (400): needs a real model | `Evaluations` with `predict_target: workflow` (at most 20 examples per run) |

## Prerequisites and setup

From the repo root:

```bash
export CALIBER_OPERATOR_USERS=admin,lena,sam
export CALIBER_APPROVER_USERS=admin,lena
# production promotions wait for a second person; the fake executor may grade the production gate
export CALIBER_RELEASE_REQUIRE_HUMAN_APPROVAL_FOR_ENVIRONMENT_CLASSES=production
export CALIBER_RELEASE_REQUIRE_GRADED_EXECUTOR_FOR_ENVIRONMENT_CLASSES=

scripts/cookbook-env.sh up lab08 --flags cookbooks
source .tmp/cookbook-env/lab08/env.sh
curl -s -c /tmp/lab08.jar -X POST "$CALIBER_API/auth/login" -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"admin"}' >/dev/null
export CALIBER_TOKEN=$(curl -s -b /tmp/lab08.jar -X POST "$CALIBER_API/auth/tokens" \
  -H 'content-type: application/json' -d '{"name":"lab08"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["token"])')
```

Sign in at `$CALIBER_URL/caliber/` as `admin` / `admin`. The default simulated providers are what this lab is written for.

## Dataset and sample files

| File | What it is |
| --- | --- |
| `assets/dataset/incident-cases.jsonl` | 41 incidents: `{id, tags, input: {incident: {incident_id, service, environment, customer_impact, alert_text, evidence: {deployments[], health{}, log_excerpt, signals{}}}}, expected: {answer, severity, action, requires_approval}}`. `answer` is `"<severity> <action>"`, the string the deploy gate looks for in the output |
| `assets/workflows/incident-commander.workflow.json` | The workflow (14 nodes) with a deploy gate on `prod` that names the test set `incident-cases` |
| `assets/workflows/evidence-lookup-drill.workflow.json` | An Error Boundary around a Python lookup (`assets/tools/lookup_recent_deployments.py`) |
| `assets/workflows/missing-tool-drill.workflow.json` | Names a tool nobody registered |
| `assets/workflows/status-page-drill.workflow.json` | An API Request to an unreachable host |
| `assets/workflows/mitigation-wait-drill.workflow.json` | A Wait For Event node |
| `assets/tools/*.py` | The two lookup bodies (deployments, service health) as Python Code node sources; they run in the sandbox (no `import` lines) |
| `assets/prompts`, `assets/skills`, `assets/judges` | The original model-based layer; reference only, not run |

The dataset has 8 `sev1`, 10 `sev2`, 12 `sev3`, 11 `sev4` incidents, and the actions `escalate_human` 14, `monitor` 9, `gather_more_evidence` 6, `rollback` 5, `disable_feature_flag` 4, `scale_out` 3. It includes noisy logs (retries, banner errors, personal data in log lines), conflicting evidence (healthy metrics with paging alerts or customer reports), missing metrics, an incident with no evidence at all (`IN38`), and two boundary cases (`IN40` at exactly the 5% and 120-minute thresholds, `IN41` just under).

Decision tables, first match wins. Severity: `sev1` for prod with all customers affected and error rate at least 10%, or a paging storm (3 or more pages) with all affected, or `DATA CORRUPTION` in the logs; `sev2` for prod with error rate at least 5%, p99 at least 2000 ms, or partial impact and degraded; `sev3` for prod degraded, health unknown or missing, 2 or more pages, or any customer report; otherwise `sev4`. Action: health unknown with 3 or more pages: `escalate_human`; health unknown: `gather_more_evidence`; healthy but paged or reported (conflicting evidence): `escalate_human`; a high-risk deploy within 120 minutes with errors at least 5% or p99 at least 2000 ms: `rollback`; a medium-risk deploy within 120 minutes and degraded: `disable_feature_flag`; a low-risk one: `monitor`; degraded and saturation at least 0.85: `scale_out`; degraded with no explanation: `escalate_human`; healthy: `monitor`; otherwise `gather_more_evidence`. Only `rollback` goes through the approval node.

## Configuration and environment

| Variable | Value used | What it does |
| --- | --- | --- |
| `CALIBER_FEATURE_COOKBOOKS` | `true` | Cookbooks gallery and install route |
| `CALIBER_OPERATOR_USERS` / `CALIBER_APPROVER_USERS` | `admin,lena,sam` / `admin,lena` | Who can run and promote; who can decide an approval (`sam` cannot) |
| `CALIBER_RELEASE_REQUIRE_HUMAN_APPROVAL_FOR_ENVIRONMENT_CLASSES` | `production` | `prod` promotions wait for a second person |
| `CALIBER_RELEASE_REQUIRE_GRADED_EXECUTOR_FOR_ENVIRONMENT_CLASSES` | empty | Lets the fake executor grade the production gate. Left at its default `production`, the promotion is refused until a real model is configured |
| `CALIBER_RELEASE_REQUIRE_QUALITY_GATE_FOR_ENVIRONMENT_CLASSES` | default `production` | `prod` needs a deploy gate (the manifest has one) |
| `CALIBER_WORKFLOW_RUN_QUEUE_ENABLED` and checkpointing and runtime approvals | `true` (set by `scripts/cookbook-env.sh`) | Runs, approvals and waits |
| `CALIBER_EGRESS_ALLOW_UNRESOLVABLE_HOSTS` | unset | The blocked API Request drill relies on it being off |
| `CALIBER_BASE_URL`, `CALIBER_TOKEN` | `$CALIBER_URL`, the admin token | The script's client |
| `COOKBOOK_08_PASSWORD`, `COOKBOOK_08_ASSETS` | optional | Password for `lena`/`sam` (default `cookbook08-lab-passphrase`); another assets directory |

## Run it through the API

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_08_incident_response_copilot
```

The script creates a project (with `lena` and `sam` as reviewers), then walks the lab. The expected output, captured from a real run (ids and timings vary):

```text
ok   the installed starter runs through the queue  [WR-decb8666]
ok   Preview refuses the starter (its API Request nodes cannot be isolated)  [oyment_connector (api_request), service_health_connector (api_request)]
ok   the workflow cannot be imported before its test set exists  [['unresolved_import_dependency']]
ok   the test set holds every incident, unchanged  [41 rows]
ok   the imported manifest validates
ok   Preview runs the workflow (no node needs isolating)
ok   staging rotates at once (it is not gated)
ok   the prod gate replayed every incident and passed  [41 replays, pass rate 1.0]
ok   the gate says its executor is the simulated one  [{'provider': 'fake', 'executor': 'FakeWorkflowExecutor', 'model': None, 'deterministic': True}]
ok   prod waits for a second person, then serves version 1  [promotion approved by lena]
ok   the gate refuses the bad version (400) and says why  [pass rate 0.9512 over 41 replays]
ok   prod still serves version 1
ok   every incident got the expected severity, action and path  [41/41 in 87.2s]
ok   exactly the rollback incidents are paused at the gate  [['IN01', 'IN11', 'IN12', 'IN15', 'IN23']]
ok   the person who started the run cannot approve the rollback (403)
ok   IN01: approved, resumed, rollback released  [ROLLBACK_APPROVED. A human approved the ]
ok   IN11: rejected, nothing released
ok   the remaining paused runs cancel cleanly  [3]
ok   Error Boundary: a good request returns the deployments
ok   Error Boundary: a failing lookup degrades to the fallback and the run completes  [NO_EVIDENCE: the deployment lo]
ok   Preview refuses a Python Code node
ok   a workflow naming an unregistered tool is refused at import (400)  [preflight failed]
ok   an API Request to a blocked host fails the run with the egress reason  [ToolExecutionError: outbound request to 'status.example.invalid' is bl]
ok   Wait For Event: the run pauses with a readable checkpoint  [waiting_event]
ok   Wait For Event: the event resumes the run to completion
ok   a good version passes the gate and prod moves to it
ok   rollback puts prod back on version 1
ok   a workflow calibration runs against the test set and reports its baseline  [rejected: ['no candidate met calibration gates']]
ok   Evaluations refuses workflow scoring without a real model (400)

All documented outcomes verified.
```

Then the script prints a JSON summary: `batch.severities` is `{sev1: 8, sev2: 10, sev3: 12, sev4: 11}`, `batch.actions` is `{disable_feature_flag: 4, escalate_human: 14, gather_more_evidence: 6, monitor: 9, rollback: 5, scale_out: 3}`, `batch.waiting_approval` is `[IN01, IN11, IN12, IN15, IN23]` and `gate` is `{bad: 0.9512, good: 1.0}`.

### The steps by hand

| Step | Call (under `$CALIBER_API`) |
| --- | --- |
| Install the starter and run it | `POST /cookbooks/08/install`, then `PATCH /workflows/{id} {"status":"active"}`, publish, `POST /workflow-runs`. `POST /workflow-versions/{id}/preview-run` answers `status: "error"` naming `deployment_connector (api_request), service_health_connector (api_request)` |
| Test set first | `POST /eval-datasets {"name","owner"}` and `POST /eval-datasets/{id}/examples` per line. Importing the workflow before this gives `unresolved_import_dependency` ("Evaluation dataset 'incident-cases' is not visible in this workspace.") |
| Import | `POST /workflows/import/preview` (reports `ready_to_import`), then `POST /workflows/import {"manifest"}`. Edit `artifacts.eval_datasets.incident-cases.dataset_name` to your test set's name first |
| Preview | `POST /workflow-versions/{id}/preview-run {"input": "<incident JSON text>"}`. Read `Verdict: {...}` in `output` or the `summarize` step |
| Publish and promote | `POST /workflow-versions/{id}/publish`; `POST /workflows/{id}/deployments/staging/promote` rotates at once; `.../prod/promote` replays the gate and answers `202` with a pending promotion and the `gate` block; `POST /workflow-promotions/{id}/approve` as a second person |
| The gate's answer | `gate.runs[0]`: `n_examples`, `pass_rate`, `thresholds[]`, `scorers`; `gate.executor`: `{"provider":"fake","executor":"FakeWorkflowExecutor","model":null,"deterministic":true}`. A failing gate answers `400` with the same block and `"passed": false` |
| Run incidents | `POST /workflow-runs {"workflow_id","alias":"prod","input":"{\"incident\": {...}}","idempotency_key"}` |
| The rollback gate | `POST /workflow-runs/{id}/approval/approve` or `/reject`, then `/resume`; `/cancel` for the rest |
| Wait For Event | `POST /workflow-runs/resume-by-event {"event_name","event_payload","workflow_id"}`; `GET /workflow-runs/{id}/checkpoints` shows the paused state |
| Roll back an alias | `POST /workflows/{id}/deployments/prod/rollback` |
| Calibration | `POST /agents` then `POST /workflows/{id}/calibration/runs {"agent_id"}`; read the job's `eval_results` |

An incident is submitted as JSON text: `{"incident": {"incident_id": "INC-1001", "service": "gateway", "environment": "prod", "customer_impact": "all", "alert_text": "...", "evidence": {...}}}`. The gate replays a test-set row by the first string field of its `input`, else the whole `input` as JSON, which is why every row has the single key `incident` (an object).

### What the failure drills show

| Drill | What happens (observed) |
| --- | --- |
| Starter, Preview | Refused: `preview blocked before execution because ordinary Preview cannot isolate these capability nodes: deployment_connector (api_request), service_health_connector (api_request)`. Run through the queue it completes (the connector nodes are not reachable from the start node) |
| Python Code, Preview | Refused for `lookup (python_code)`; the queued run works |
| Error Boundary | A good request returns the fixture deployments (`a1b9f3c`); input that is not JSON makes the lookup raise and the run **completes** with the fallback `NO_EVIDENCE: the deployment lookup failed; gather evidence by hand.` |
| Missing tool | `POST /workflows/import` answers `400 workflow import preflight failed` (the tool `lookup_service_owner` is not registered) |
| API Request to a blocked host | The run ends `failed` (`runtime_error`): `outbound request to 'status.example.invalid' is blocked: this process could not resolve it ...`. Preview refuses it too (`call (api_request)`). Allowing a host needs egress configuration (`CALIBER_EGRESS_*`) |
| Wait For Event | The run stops at `waiting_event` with a checkpoint (`expected_event_name: incident.mitigation_confirmed`); resuming by event completes it. Preview reports it `blocked: waiting_event:wait` |
| Self-approval | `403 the account that triggered this run cannot approve it; a second person must decide` |
| Bad version | `400 deploy gate failed for alias 'prod'`; gate `pass_rate` `0.9512` (39 of 41) against a threshold of 1; `prod` does not move |
| Evaluations on a workflow version | `400 evaluation requires a real LLM provider; set CALIBER_LLM_PROVIDER ...` (even though the workflow's own output is deterministic) |

### Workflow calibration: what it did and did not show

`POST /workflows/{id}/calibration/runs` queues a job. It ran against the gate's test set (a sample of 20 incidents) and finished `rejected` with `"no candidate met calibration gates"`: the calibration moves (grounding guardrail, tool constraint, reroute a handoff, relax a guardrail) have nothing to change in a rule-table workflow with no tools or guardrails, so no candidate was produced, and the baseline scored `0.95` overall on 20 examples. The `Simulated evaluation` and `Structural score` markers appear on a candidate's evidence; **this lab produced no candidate, so those markers were not observed here.** The script asserts only that the job ran, is marked as a workflow calibration, covers at least 20 examples and reports a baseline.

## Recipe: run it through the UI

The Playwright spec `caliber/caliber-ui/e2e/cookbooks/labs/08-incident-response.spec.ts` does these steps with real clicks. The alpha web app is in single-environment mode (no Deployments or Promotions tab), so the aliases, the `prod` gate and rollback are API only; the gate and calibration are therefore not in the UI path. Sign in as `admin` / `admin`.

1. **Administration.** Create the accounts `lena` and `sam` (password `cookbook08-lab-passphrase`), create a workspace with the top bar `+`, and add both as `Reviewer` under `Project access`. Expected: `Created lena.`, `Added lena to <workspace>.`
2. **Import before the test set exists.** `Agent resources > Workflows > Import`: type a name, `Choose file` the manifest with its `dataset_name` set to a test set name that does not exist yet, `Validate dependencies`. Expected: `Preflight needs attention` and `is not visible in this workspace`.
3. **Create the test set.** `Prompt lifecycle > Test Sets > + New Test Set`: name it as in the manifest, owner `@lab08`, `Create`, open it, and `+ Add example` three times (the `input`, `expected` and tags of `IN01`, `IN03`, `IN06`). Expected: three example rows.
4. **Import the workflow.** Import again and `Validate dependencies`. Expected: `Preflight passed`; `Import as new` opens the editor with `severity_table`, `action_table`, `join`, `triage_router` and `review` in the outline.
5. **Validate and Preview.** `Validate` says `Valid.`; `Preview` with the `IN01` input and `Run Preview`. Expected: `completed`; the step `summarize` shows `sev1 rollback`.
6. **Publish.** `Publish`, `Run Validation`, `Next`, `Next`, `Publish`. Expected: `Published.`
7. **Run incidents.** `Run Monitor`: `IN06` (healthy staging) completes, its output contains `RECOMMENDATION` and `sev4 monitor`. `IN01` (the gateway regression) stops at `waiting_approval`; pressing `Approve` as `admin` shows `a second person must decide`.
8. **A second person approves.** As `lena` choose the workspace, `Workflows`, the workflow, `Runs`, select the run that shows `Awaiting approval`, `Approve`, `Resume`. Expected: **Recovery Diagnostics** says *This run ended in completed.* and the **Execution Debugger** (which follows the newest step until you click one) shows `ROLLBACK_APPROVED` in the output of the last step. If you clicked an earlier step, click `rollback_request` or `final` to see it.

## Verify it worked

- `GET /workflows/{id}/deployments`: `prod` on version 1 after the approval, and again after the rollback of the good version 3.
- The promotion that waited: `GET /workflows/{id}/promotions` shows it `approved`, `decided_by: lena`, with `gate_result.runs[0].n_examples` 41 and `pass_rate` 1.0.
- A run of `IN01`: `status: "completed"` after approval, `summary.node_path` ending `review`, `rollback_request`; `summary.output` starting `ROLLBACK_APPROVED. ... Verdict: {"verdict": "sev1 rollback", ...}`.
- The rejected run: `failed`, no `ROLLBACK_APPROVED` in its output.
- Batch: 41 runs, the `summarize` step of each shows the severity and action of the dataset, and the 5 rollback incidents (`IN01`, `IN11`, `IN12`, `IN15`, `IN23`) are the paused ones.

## Common errors and troubleshooting

| You see | Cause and fix |
| --- | --- |
| `404 feature_disabled` on `/cookbooks` | Start with `--flags cookbooks` |
| `Evaluation dataset 'incident-cases' is not visible in this workspace` on import | Create the test set first, in the same workspace, and name it in the manifest |
| `400 promoting to 'prod' (production) requires a deploy gate` | The manifest has no gate for `prod` (`deploy_gates[].required_for_aliases`) |
| `400 ... graded ... real model` when promoting to `prod` | The server defaults to requiring a real model for production gates; start it with `CALIBER_RELEASE_REQUIRE_GRADED_EXECUTOR_FOR_ENVIRONMENT_CLASSES=` (empty) as above, or configure a provider |
| `400 deploy gate failed for alias 'prod'` | The replay did not score 1.0: read `gate.runs[0].detail` and fix the rule (or the dataset row) before promoting |
| Gate fails with `n_examples: 0`, `dataset not found; gate failed closed` | A version whose manifest carries the wrong `workflow_id` (copy the stored manifest of an existing version, not the file) or a test set that is archived or in another workspace |
| `409 event '...' matched multiple waiting workflow runs` | Pass `workflow_id` to `resume-by-event` (the script does) |
| `403` on approve | Same rules as lab 07: not the initiator, `caliber.approver`, project role `reviewer` or `owner` |
| Preview refuses with `(python_code)` / `(api_request)` | Expected for those nodes; run through the queue |
| The `draft_update` text is an echo | Simulated provider |

## Not covered by this lab

Live evidence (deployment and service-health APIs, a status-page snapshot, an incident issue) needs egress, MCP servers and credentials that are not available here; the GitHub, Playwright and PostgreSQL MCP steps of the original recipe were removed, not run. A real model for the stakeholder update, a graded production gate and `Evaluations` on the workflow need a provider. Workflow calibration produced no candidate (see above). Aliases, promotions and rollback are API only in this UI.

## Cleanup and reset

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_08_incident_response_copilot --cleanup
scripts/cookbook-env.sh down lab08 && rm -rf .tmp/cookbook-env/lab08
```

`--cleanup` archives the `cookbook-08-*` projects, workflows and test sets (and leaves `lena` and `sam`, and the agent the calibration step registered). The UI spec leaves `lab08-ui-*` objects in the disposable stack.
