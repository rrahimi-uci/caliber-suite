# Incident Response Copilot

> **Prerequisite flag:** `CALIBER_FEATURE_COOKBOOKS=true` (the Cookbooks gallery is a preview area and is off by default). The optional MCP evidence nodes of the original recipe would also need `CALIBER_FEATURE_INTEGRATIONS=true`; they are not part of this lab.
> The main path runs on the alpha's default **simulated** providers. It was also run, end to end, against **real providers** (`--real-llm`), with an optional model-based variant: see [Run it with a real provider](#run-it-with-a-real-provider).

## What this lab demonstrates

A workflow that turns an incident record (alert, recent deployments, health numbers, a noisy log excerpt, paging and customer-report counts) into a severity and the lowest-risk next action, and keeps a rollback behind a human approval. The whole decision is declarative (Data Transform decision tables, a Join, a Template, a Router), so unlike lab 07 **Preview can run it**, and CALIBER's **deploy gate can replay the 41-incident test set before `prod` moves**. You import it, preview it, promote it through a gated `prod` alias with a second person's approval, watch the gate refuse a bad version, run all 41 incidents through the queue, answer the rollback gate, and exercise the failure drills (Error Boundary, missing tool, blocked API Request, Wait For Event).

The only agent node (`draft_update`, the stakeholder update) runs on the simulated provider by default. The decision does not depend on it, and the lab never asserts what it wrote. On a real-provider stack it writes a real update (judged, not asserted), the gate replays through a real model, workflow Evaluations works, and an optional variant lets a model *propose* a triage while the tables still decide.

You are done when:

- the script prints `All documented outcomes verified.` and exits 0;
- Preview ran the workflow, the `prod` gate replayed all 41 incidents with a pass rate of 1.0 and named its executor as the simulated one, and a second person approved the promotion;
- a version with a bad rollback rule was refused by the gate with a pass rate under 1.0 and `prod` stayed on version 1;
- all 41 incidents got the expected severity and action through the queue, and exactly the 5 rollback incidents paused at the approval gate;
- self-approval was refused (403), an approval released `ROLLBACK_APPROVED`, a rejection released nothing;
- each failure drill behaved as written in the table below;
- the workflow calibration finished `rejected` with no candidate, and its evidence says what measured the baseline (`provider`, `simulated`, `scorer`);
- on a real-provider stack: the gate named a real executor, Evaluations scored the workflow version, and the model variant's verdicts equalled the tables' on every incident it was run over.

### What is simulated, and what a real provider or connector would change

| Piece | Simulated stack | Real-provider stack (run and verified, see below) | Still not here |
| --- | --- | --- | --- |
| Severity and action | Deterministic decision tables (no model) | Unchanged. The optional **variant** adds a model that *proposes*; the tables still decide | |
| `draft_update` agent | Fake provider, canned echo text | A real stakeholder update, judged by a model-graded judge (not asserted) | |
| Deploy gate | Replays 41 incidents with the **fake executor** (`provider: fake`, `deterministic: true`). It proves the graph and the data, not a model | Replays through a **real model** (`provider: openai`, `executor: OpenAIChatWorkflowExecutor`, `deterministic: false`) and passes at 1.0; the default `CALIBER_RELEASE_REQUIRE_GRADED_EXECUTOR_FOR_ENVIRONMENT_CLASSES=production` is now satisfied, so the lab does not empty it | |
| Evidence (deployments, health, logs) | Supplied inside each incident record | Same | `api_request` or MCP nodes (or the Python lookups in `assets/tools`) fetch it; each needs egress or a connector |
| Workflow calibration | Runs, finds no candidate; evidence `provider: fake`, `simulated: true`, `scorer: structural` | Runs, measures a real baseline (`0.95` over 20 incidents), finds no candidate; evidence `provider: openai`, `simulated: false`, `scorer: structural` | A candidate: the moves need a guardrail, a tool constraint or a handoff to change |
| Evaluations on a workflow version | Refused (400): needs a real model | Scores the version (a deterministic scorer and a model-graded judge, up to 20 examples per run) | |

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
| `assets/dataset/incident-cases.jsonl` | 41 incidents (the script posts them to a test set; in the UI you choose the file under **Test Sets > Import from file**, step 3): `{id, tags, input: {incident: {incident_id, service, environment, customer_impact, alert_text, evidence: {deployments[], health{}, log_excerpt, signals{}}}}, expected: {answer, severity, action, requires_approval}}`. `answer` is `"<severity> <action>"`, the string the deploy gate looks for in the output |
| `assets/workflows/incident-commander.workflow.json` | The workflow (14 nodes) with a deploy gate on `prod` that names the test set `incident-cases` |
| `assets/workflows/evidence-lookup-drill.workflow.json` | An Error Boundary around a Python lookup (`assets/tools/lookup_recent_deployments.py`) |
| `assets/workflows/missing-tool-drill.workflow.json` | Names a tool nobody registered |
| `assets/workflows/status-page-drill.workflow.json` | An API Request to an unreachable host |
| `assets/workflows/mitigation-wait-drill.workflow.json` | A Wait For Event node |
| `assets/tools/*.py` | The two lookup bodies (deployments, service health) as Python Code node sources; they run in the sandbox (no `import` lines) |
| `assets/workflows/incident-commander-model.workflow.json`, `assets/judges/stakeholder-update-honesty.judge.json` | The optional model-based variant and the judge for the stakeholder update (real provider only) |
| `assets/prompts`, `assets/skills`, `assets/judges/incident-action-correctness.judge.json` | The original model-based layer, condensed into the variant's agent; the files themselves are reference only, not run |

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
| `OPENAI_API_KEY` | unset on the simulated tier; **your own key** with `--real-llm` | Read by `scripts/cookbook-env.sh` from your environment (or the gitignored repo-root `.env`); never printed or written to the stack's files, never put in a file you commit |
| `--model-variant` (script flag) | off | Also runs the optional model-based variant (real provider only; refused on a simulated stack) |
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

`POST /workflows/{id}/calibration/runs` queues a job. It ran against the gate's test set (a sample of 20 incidents) and finished `rejected` with `"no candidate met calibration gates"`: the calibration moves (grounding guardrail, tool constraint, reroute a handoff, relax a guardrail) have nothing to change in a rule-table workflow with no tools or guardrails, so no candidate was produced; the baseline scored `0.95` overall on 20 examples. The job's evidence carries `provider`, `simulated` and `scorer` in this outcome too (it did not before this lab: a run that ended without a candidate stored none, so the UI could not show them), so a baseline replayed through the fake executor is never mistaken for a measurement:

| Stack | `eval_results` | The run row in the UI (Workflows > the workflow > Calibrate) |
| --- | --- | --- |
| Simulated | `provider: fake`, `simulated: true`, `scorer: structural` | **Simulated evaluation** and **Structural score** badges |
| Real provider | `provider: openai`, `simulated: false`, `scorer: structural` | **Structural score** badge only |

A structural score checks that the run completed and used its tools; it does not grade the output. (`CALIBER_WORKFLOW_LLM_JUDGE_ENABLED=true` with a real provider turns the scorer into `llm_judge`; not run here.) The script asserts that the job ran, covers at least 20 examples, reports a baseline, and carries those three markers.

## Recipe: run it through the UI

The Playwright spec `caliber/caliber-ui/e2e/cookbooks/labs/08-incident-response.spec.ts` does these steps with real clicks and uploads, from a stack you have just started (it makes no API call, and reads whether the stack is simulated from the page's own banner). The alpha web app is in single-environment mode (no Deployments or Promotions tab), so **the aliases, the `prod` gate and alias rollback are API only**; the gate is therefore not in the UI path. Versions are: restore, edit, Preview, publish, compare. Sign in as `admin` / `admin`.

1. **Administration.** Create the accounts `lena` and `sam` (password `cookbook08-lab-passphrase`), create a workspace with the top bar `+`, and add both as `Reviewer` under `Project access`. Expected: `Created lena.`, `Added lena to <workspace>.`
2. **Import before the test set exists.** `Agent resources > Workflows > Import`: type a name, `Choose file` `assets/workflows/incident-commander.workflow.json`, `Validate dependencies`. Expected: `Preflight needs attention` and `Evaluation dataset 'incident-cases' ... is not visible in this workspace`: the manifest's deploy gate names a test set that does not exist yet. (The spec points the manifest at a uniquely named test set instead; on a fresh stack leave the manifest as shipped and create `incident-cases` in step 3.)
3. **Create the test set from the file.** `Prompt lifecycle > Test Sets > + New Test Set`: name it as in the manifest, owner `@lab08`, `Create`, open it, `Import from file`, choose `assets/dataset/incident-cases.jsonl`. Expected: `incident-cases.jsonl: 41 rows ready to import.`; `Import 41 rows`; the header reads `41 active examples` and the gateway incident's row shows `"answer": "sev1 rollback"`.
4. **Import the workflow.** Import again and `Validate dependencies`. Expected: `Preflight passed`; `Import as new` opens the editor with `severity_table`, `action_table`, `join`, `triage_router` and `review` in the outline.
5. **Validate and Preview.** `Validate` says `Valid.`; `Preview` with the `IN01` input and `Run Preview`. Expected: `completed`; the step `summarize` shows `sev1 rollback`.
6. **Publish.** `Publish`, `Run Validation`, `Next`, `Next`, `Publish`. Expected: `Published.`; the editor is now read-only (version 1 is immutable).
7. **Run incidents.** `Run Monitor`: `IN06` (healthy staging) completes, its output contains `RECOMMENDATION` and `sev4 monitor`. `IN01` (the gateway regression) stops at `waiting_approval`; pressing `Approve` as `admin` shows `a second person must decide`.
8. **A second person approves.** As `lena` choose the workspace, `Workflows`, the workflow, `Runs`, select the run that shows `Awaiting approval`, `Approve`, `Resume`. Expected: **Recovery Diagnostics** says *This run ended in completed.* and the **Execution Debugger** (which follows the newest step until you click one) shows `ROLLBACK_APPROVED` in the output of the last step. If you clicked an earlier step, click `rollback_request` or `final` to see it.
9. **Modify the rule, new version, re-run, compare.** `Workflows` > the workflow > `Versions`: `Restore as draft` on v1 opens an editable draft (v2). Double-click `action_table` in the outline; in the inspector's **Configuration (JSON)** change the `value` of the `error_rate` condition of the rule `rollback-recent-high-risk-deploy-errors` from `0.05` to `0.5` (a "tidy-up" that makes rollback rarer) and click outside the box. `Save`, `Validate` (`Valid.`), `Preview` the `IN23` incident (a 6% error rate after a high-risk deploy): the `summarize` step now shows `sev2 escalate_human`; v1 said `sev2 rollback`. `Publish` (v2). On `Versions`, pick v1 as **Base (older)** and v2 as **Compare (newer)**: exactly one modified node, `action_table`. This is the regression the API script's `prod` gate refuses with a pass rate of `0.9512` (`IN11` and `IN23` lose their rollback); the UI has no gate, so here you see the same change by Preview and compare.
10. **Roll back.** `Versions`: `Restore as draft` on v1 again (v3). `Preview` `IN23`: `sev2 rollback` again. `Publish`. `Versions` lists three versions. (Moving a `prod` alias back is the API's `.../deployments/prod/rollback`.)
11. **Evaluations.** `Prompt lifecycle > Evaluations > Run evaluation`: the test set from step 3, **What to score** `Workflow version`, paste the version id (`WFV-...` from the editor URL of v1), untick **Exact match** and **Token F1**, `Run`. Simulated: the red message `evaluation requires a real LLM provider ...`. Real: a row `completed` with `20/20` (a workflow run scores at most 20 examples; the run's detail says `graded 20 of 41 active examples`).
12. **Agents and Calibrate.** `Agent resources > Agents > New agent` (any id, an experiment id, a name, `Register agent`), then `Workflows` > the workflow > **Calibrate**: pick the agent, **Start Calibration**. The dataset badge reads `<test set> · 41 examples`. The run ends `rejected`; its row carries **Structural score** (and **Simulated evaluation** on a simulated stack, not on a real one).

On a real-provider stack the spec adds one more step, 13 (the model variant), described in [Run it with a real provider](#run-it-with-a-real-provider), item 6.

## Run it with a real provider

The same lab on a stack with a real model and the real evaluator. Run on 2026-10-19 with the default model (`gpt-5.6-luna`). Generated text and model scores **will vary**; the script and the spec assert invariants (markers, counts, "the model cannot change a verdict or authorise a rollback"), never wording or an exact score.

**1. Start the stack** (your own key, never committed; `--real-llm` reads it from your shell). Do **not** empty `CALIBER_RELEASE_REQUIRE_GRADED_EXECUTOR_FOR_ENVIRONMENT_CLASSES` here: with a real model the production gate is allowed to run and is graded by it.

```bash
export OPENAI_API_KEY=sk-...                       # your key
export CALIBER_OPERATOR_USERS=admin,lena,sam CALIBER_APPROVER_USERS=admin,lena
export CALIBER_RELEASE_REQUIRE_HUMAN_APPROVAL_FOR_ENVIRONMENT_CLASSES=production
scripts/cookbook-env.sh up lab08h --flags cookbooks --real-llm
source .tmp/cookbook-env/lab08h/env.sh
curl -s $CALIBER_API/readiness | jq -c '.data | {simulated, all_real}'      # {"simulated":[],"all_real":true}
```

Make the token as in "Prerequisites and setup" (`/tmp/lab08h.jar`, name `lab08h`).

**2. Run the script, with the optional variant:**

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_08_incident_response_copilot --model-variant
```

About 16 minutes (the two gate replays are about 4 minutes each on a real model; the 41 incidents through the queue took 238 s). Without `--model-variant` it runs the main path only. `GET /readiness` picks the mode; on a simulated stack the flag is refused. Real output, only the lines that differ from the simulated run:

```text
ok   the prod gate replayed every incident and passed  [41 replays, pass rate 1.0]
ok   the gate says its executor is a real model, not the simulated one  [{'provider': 'openai', 'executor': 'OpenAIChatWorkflowExecutor', 'model': 'gpt-5.6-luna', 'deterministic': False}]
ok   the gate refuses the bad version (400) and says why  [pass rate 0.9512 over 41 replays]
ok   every incident got the expected severity, action and path  [41/41 in 238.2s]
...
ok   a workflow calibration runs against the test set and reports its baseline  [rejected: ['no candidate met calibration gates']]
ok   the calibration evidence says what measured its baseline  [{'provider': 'openai', 'simulated': False, 'scorer': 'structural'}]
ok   Evaluations scores the workflow version for real over the test set  [8/8 passed, contains_expected 1.0, judge 0.625]
ok   the model-graded judge ran next to the deterministic scorer  [0.625]
ok   the model variant validates
ok   the model variant's verdicts equal the tables' (the model cannot change a decision)  [10/10]
ok   a rollback is released exactly when the tables say rollback, not when the model does  [10/10]
ok   the model produced a readable proposal for most incidents  [10/10 parsed]
ok   through the queue the rollback still pauses at the approval gate  [waiting_approval]

All documented outcomes verified.
```

and in the JSON summary (`"mode": "real"`):

```json
"calibration": {"status": "rejected", "n_examples": 20, "baseline_quality": 0.95, "candidates": 0, "provider": "openai", "simulated": false, "scorer": "structural"},
"evaluation": {"run_id": "EVR-73393527", "n_examples": 8, "available_examples": 41, "pass_rate": 1.0, "contains_expected": 1.0, "judge": 0.625, "model": "gpt-5.6-luna"},
"variant": {"incidents": 10, "verdicts_equal_tables": 10, "proposals_parsed": 10, "model_agrees_severity": 10, "model_agrees_action": 10, "model_disagreements": []}
```

**3. What changes versus the simulated tier.**

| Step | Simulated | Real |
| --- | --- | --- |
| Deploy gate | `executor: {provider: fake, deterministic: true}` | `executor: {provider: openai, executor: OpenAIChatWorkflowExecutor, model: gpt-5.6-luna, deterministic: false}`; still 41 replays, pass rate 1.0, bad version `0.9512` (the decision tables are deterministic, so the model's `draft_update` text does not move the score) |
| `draft_update` | `[incident-commander] processed: {...}` | A real three-sentence update inside the verdict's `stakeholder_update` |
| Workflow calibration | markers `fake` / `simulated: true` / `structural` | markers `openai` / `simulated: false` / `structural`; baseline measured; still no candidate |
| Evaluations on the workflow | `400 evaluation requires a real LLM provider` | A run: 8 of the 41 incidents (`max_examples`; the route caps a workflow run at 20) |
| Network | none | the run needs the provider reachable (see troubleshooting) |

**4. Evaluations on the workflow version, with a judge.** Evaluations runs the version in Preview with each example's input (Preview passes the approval node as if approved, which is why the rollback incidents print `ROLLBACK_APPROVED` there). Create the judge from [`assets/judges/stakeholder-update-honesty.judge.json`](assets/judges/stakeholder-update-honesty.judge.json) and run (the script does):

```text
POST /judges {"name": "StakeholderUpdateHonesty-<suffix>", "instructions": "<the file's>", "feedback_value_type": "bool"}
POST /evaluations {"dataset_id": "<the 41 incidents>", "predict_target": "workflow", "subject_ref": "<version id>",
                   "scorers": ["contains_expected", "Judge.<judge id>"], "max_examples": 8}
```

Observed: `contains_expected` 1.0 (the verdict string `sev1 rollback` etc. is in every output, deterministic) and the judge `0.75` in one run and `0.625` in another: **the judge passed five or six of eight stakeholder updates**. The judge asks for a claim-by-claim honesty (facts supported by the incident, anything else labelled a hypothesis, no claim of resolution, no promised time); a three-sentence model update that states an unlabelled inference fails it. I did not read the individual verdicts, so treat the rate as "most, not all" and use the scores as a trend, not a gate. `evidence.sampling` says `available_examples: 41`, `evaluated_examples: 8`, `truncated: true`: a bounded run states what it left out.

**5. The optional model variant.** [`assets/workflows/incident-commander-model.workflow.json`](assets/workflows/incident-commander-model.workflow.json) is the lab workflow plus one more agent node, `propose_triage`, in the parallel fan-out. Its instructions condense the original recipe's incident-commander prompt, severity matrix and rollback checklist, in the lab's own vocabulary (`sev1` to `sev4`; `rollback`, `disable_feature_flag`, `scale_out`, `monitor`, `escalate_human`, `gather_more_evidence`), and ask for one line of JSON. Its answer is **advisory**: it joins the verdict as `model_proposal`, and nothing routes on it.

```text
"stakeholder_update": "...", "model_proposal": "{\"severity\":\"sev1\",\"action\":\"rollback\",\"reason\":\"Prod-wide impact with 18% 5xx, p99 4200ms, pool exhaustion, and a high-risk deployment 12 minutes ago.\"}"
```

The script imports it, runs Preview over ten incidents (`IN01 IN02 IN03 IN04 IN05 IN07 IN09 IN12 IN36 IN38`: one or two per action, plus the vague and the conflicting-evidence cases where a model is most likely to disagree), and asserts: every verdict (`severity`, `action`) equals the dataset's (`10/10`); `ROLLBACK_APPROVED` appears exactly when the **tables** say rollback; and through the queue `IN01` still pauses at `review`. **Model-dependent, reported not asserted:** how often the proposal agrees with the tables. Observed once: `10/10` on severity and on action, no disagreement; a weaker model, or `IN09`/`IN36` (paging alerts over healthy metrics) on another run, may well differ, and when it does it appears in `model_disagreements` while the verdict stays the tables'. That is the design: a model can propose, and the tables, the gate and the approval decide.

**6. Through the UI.** On a real-provider stack the spec runs the twelve steps of "Recipe: run it through the UI" (step 11 now scores for real) and one more (same command against `lab08h`, about two minutes):

13. **The model variant.** `Workflows > Import` `incident-commander-model.workflow.json`: `Preflight passed`, `Import as new`; the outline shows `propose_triage`. `Preview` with the `IN01` input: `completed`; the `summarize` step shows `sev1 rollback` **and** a `model_proposal` field.

**Cost and time.** The script's real calls: the 41 gate replays twice (each replay runs the `draft_update` agent), the 41 incidents through the queue (the same agent), the calibration's 20 replays of the workflow, 8 workflow runs plus 8 judge calls for Evaluations, and 10 Preview runs (two agents each) plus one queue run for the variant: on the order of 250 short model calls, 16 minutes, a few cents at the default model's price (tokens were not measured). The spec adds about as many as twenty workflow runs for Evaluations and one calibration. Do not loop it.

**Troubleshooting the real tier.**

| What you see | Cause and fix |
| --- | --- |
| Runs `failed` with `APIConnectionError: Connection error.`, one run that hangs for minutes and the next ones fail within a second | The network between the server and the provider dropped. Seen once for about 9 minutes: the request in flight waited for the client's long timeout, then the following ones failed at once, then it recovered. The script resubmits runs that failed for a transient reason (connection error, 429, rate limit, timeout) twice; `resubmitted_after_provider_errors` lists them. Check your connection and rerun if it persists |
| The server stops answering during a `prod` promotion | Fixed in this build: the gate replay used to run on the server's event loop, so a replay through a real model (minutes) froze every other request, `/readiness` included, until it ended. It now runs on a worker thread. If you see it on an older build, wait for the promotion to end |
| `401 ... Incorrect API key provided` in a run, a judge or an evaluation | The key is wrong or revoked (OpenAI echoes a masked copy of it: redact before pasting). Fix it and `scripts/cookbook-env.sh restart lab08h` |
| `429` / rate limit | Too many calls at once (the gate replays are sequential, the queue is one worker). The script retries; otherwise wait or use a key with a higher limit |
| `the gate says its executor is a real model` fails with `fake` | The stack is simulated: `curl $CALIBER_API/readiness` must show `"simulated": []` |
| `400 ... graded ... real model` when promoting to `prod` on a simulated stack | The default requires a real model for production gates; keep it that way on a real stack, and empty the variable only for the simulated tier |
| The judge score is low or changes between runs | It is a model grading a model on eight rows: use it as a trend. Read a row's verdict in the run's detail before changing the instructions |
| `SetupError: --model-variant needs a real provider` | Simulated stack; start with `--real-llm` |

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
| `Line 7 is not valid JSON.` or `Line 3 needs an "input" object.` under **Import from file** | The file is not the lab's JSON Lines file (or was edited): nothing was imported. Use `assets/dataset/incident-cases.jsonl` or fix the named line |
| `Stopped after 12 of 41 rows: ...` under **Import from file** | The server refused row 13. The first 12 rows are in the test set; create a new test set (and import the file again) rather than importing twice into one |
| Evaluations over the 41-row test set say `20/20` | A workflow run is capped at 20 examples (the run's detail says `graded 20 of 41 active examples`); use a smaller test set or several runs to cover the rest |
| The `draft_update` text is an echo | Simulated provider; on a real stack it is the model's text |

## Not covered by this lab

Live evidence (deployment and service-health APIs, a status-page snapshot, an incident issue) needs egress, MCP servers and credentials that are not available here; the GitHub, Playwright and PostgreSQL MCP steps of the original recipe were removed, not run. The original recipe's seven-key JSON output and its `IncidentActionCorrectness` judge (for a model that returns that JSON) are not run: the variant's agent returns the lab's own vocabulary so it can be compared with the tables, and the honesty judge grades the stakeholder update. A workflow calibration **candidate** is not produced for this workflow (see above); the LLM-judge scorer for calibration (`CALIBER_WORKFLOW_LLM_JUDGE_ENABLED`) was not run. Aliases, promotions and the deploy gate are API only in this UI (single-environment mode); version restore, publish, Preview and compare are in the UI (steps 9 and 10).

## Cleanup and reset

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_08_incident_response_copilot --cleanup
scripts/cookbook-env.sh down lab08 && rm -rf .tmp/cookbook-env/lab08
```

`--cleanup` archives the `cookbook-08-*` projects, workflows and test sets and the `StakeholderUpdateHonesty-*` judges (and leaves `lena` and `sam`, and the agent the calibration step registered). The UI spec leaves `lab08-ui-*` workspaces, workflows and test sets in the disposable stack (discard the stack to clean up).
