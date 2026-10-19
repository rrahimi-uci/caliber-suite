# Self-Healing Workflows

> **Prerequisite flags:** the lab itself needs none: workflows, the Run Monitor, versions and aliases are part of the supported alpha. `CALIBER_FEATURE_COOKBOOKS=true` only opens the Cookbooks gallery (a preview area, off by default), where the **Self-Healing Workflows** card installs a starter *Human Review* workflow. This lab does not use that starter: it imports its own workflow from [`assets/workflow/`](assets/workflow/). Aria is not used; the lab never asks it to narrate or fix anything.

## What it demonstrates and the expected outcome

A workflow that is fine on the tickets you tested and fails on the ones production sends. `refund-intake` validates a refund ticket, checks its order reference, asks an account service for the customer's tier and decides `approve` or `manual_review`. You run 35 real-looking tickets through version 1 and 24 of them fail, in three different nodes and for three different reasons: a schema violation in `validate`, a poison record that makes `check_reference` time out, and an account-service error that does not fail the `lookup` node that received it but the `decide` node two steps later. You read the evidence the run debugger and the trace keep, retry (the same code fails the same way: a retry is not a fix), ask `propose-patch` for help and see what it can and cannot do, and then write the patch yourself: three node bodies saved as version 2 of the same workflow. You rerun all 35 tickets on version 2: none fails, the 11 that already worked produce byte-identical output, and the failures have become either a normal decision, a clean rejection with reasons, or a "degraded" manual review that says the account lookup failed. Finally you move an alias to version 2, run through it, and roll it back.

**You are done when:**

- version 1 completes 11 of 35 tickets and fails 24: 17 at `validate`, 3 at `check_reference`, 4 at `decide`, each with the error text in the dataset;
- each failing run leaves an `ERROR` trace whose error span names the failing node, no checkpoint, and a retry (attempt 2 of 2) that fails at the same node;
- `propose-patch` returns a generic *prompt* suggestion with no graph change for the timed-out node, and refuses a guardrail patch aimed at a code node with a 400;
- version 2 completes all 35 (10 `approve`, 13 `manual_review`, 12 `rejected_input`) and the 11 tickets version 1 handled give identical output;
- the `staging` alias serves version 2, runs through it succeed, and `rollback` restores version 1 (the same ticket fails again);
- `prod` refuses a version that has no deploy gate (400), and the UI's **Promote** button, which can only target it, shows that refusal;
- `cookbook_09_self_healing_workflows.py` exits 0 and the Playwright spec passes.

## Prerequisites and setup

You need a running CALIBER ([`docs/start/alpha-install.md`](../../../docs/start/alpha-install.md)), an administrator login, a worker (the workflow run queue is on in the lab stack) and Python with `caliber-sdk` for the script. Two things are specific to this lab:

1. **The stand-in account service.** The workflow's `lookup` node calls an HTTP service. In production that is your own account API. Here it is [`assets/upstream/account_service.py`](assets/upstream/account_service.py), a 60-line server that is **not a real system**: it answers 200 for most account ids and reproduces a 503, a 500 and a 404 for three fixed ids so that the failures are the same every time.
2. **Egress.** CALIBER refuses outbound calls to loopback addresses unless the host is allowed, so the server must start with `CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1`. Against a real account API you would allow its hostname instead.

**Simulated or real provider: nothing here needs a model.** The workflow has no agent node, so `CALIBER_LLM_PROVIDER=fake` (the shipped default) does not matter, and every number below is a measurement, not a canned score. What *is* limited is Preview: the editor's **Preview** button and a deploy gate's replay both refuse a workflow with Python-code or API-request nodes (`preview blocked before execution because ordinary Preview cannot isolate these capability nodes: ...`). That is why the lab proves the fix with real runs of the dataset on the new version, and why the last step ends in a refusal. See "What is not covered".

From the repository root, with the disposable stack helper (sign in as `admin` / `admin`):

```bash
python3 docs-site/cookbooks/09-workflow-debugger-self-healing-lab/assets/upstream/account_service.py 8780 &
CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1 scripts/cookbook-env.sh up lab09 --flags cookbooks
source .tmp/cookbook-env/lab09/env.sh
```

Then create a token for the SDK script (or use **Settings > API tokens**):

```bash
curl -s -c /tmp/lab09.jar -X POST $CALIBER_API/auth/login -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"admin"}' >/dev/null
export CALIBER_TOKEN=$(curl -s -b /tmp/lab09.jar -X POST $CALIBER_API/auth/tokens \
  -H 'content-type: application/json' -d '{"name":"lab09"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["token"])')
# CALIBER_BASE_URL (the server root, which is what the SDK wants) is already exported by env.sh
```

## Dataset and sample data

All files are in [`assets/`](assets/README.md).

| File | Contents | Why |
| --- | --- | --- |
| [`dataset/failing-inputs.jsonl`](assets/dataset/failing-inputs.jsonl) | 35 tickets, `F01` to `F35`. Each row has `input.payload` (the exact string submitted as the run input), `tags`, and `expected`: what version 1 does (`v1_status`, `v1_failing_node`, `v1_error_contains`) and the decision version 2 reaches (`decision`, `reason_code`, and for some rows `upstream_status` or `v2_errors_contain`). | the failing and borderline inputs, and the answer key the script checks |
| [`workflow/ticket-intake.v1.workflow.yaml`](assets/workflow/ticket-intake.v1.workflow.yaml) | the workflow as shipped: `start`, `validate`, `check_reference`, `lookup`, `decide`, `final` | version 1, the one with the bugs |
| [`workflow/ticket-intake.v2.workflow.yaml`](assets/workflow/ticket-intake.v2.workflow.yaml) | the same graph with three node bodies replaced | version 2, the patch |
| [`workflow/patch/`](assets/workflow/patch/) | `validate.py`, `check_reference.py`, `decide.py`: exactly the new node bodies | what you paste into the editor |
| [`upstream/account_service.py`](assets/upstream/account_service.py) | the stand-in account service | the upstream that fails on purpose |

The 35 tickets: 7 **healthy** (approve, over the limit, a suspended account, a euro refund with a non-ASCII reason), 4 **borderline but fine** (a lowercase order reference, a trailing hyphen, an 1,100-character reason, a 36-character lowercase reference that looks like poison and is not), 17 **schema violations** (5 that version 2 can safely normalize: an amount sent as `"48.00"`, `"$1,048.00"`, a lowercase `usd`, a `$` currency symbol, an account id with spaces and lowercase; 1 that is ambiguous and must be rejected: `"€ 34,50"`, where the comma could be a decimal separator; 11 that must stay rejected: a missing account id, a negative, zero, `null` and boolean amount, an unsupported currency, a malformed account id, an empty reason, a JSON array, truncated JSON, several fields missing), 3 **poison records** (an order reference that sends a nested-quantifier regex into catastrophic backtracking: a 32-character id with a trailing `/`, a long hyphenated free-text reference ending in `#`, 40 letters and a `!`) and 4 **upstream failures** (account ids the stand-in answers with 503, 500 and 404 twice).

Why these: a schema failure, a timeout and an upstream error look the same on the Run Monitor's status badge (**failed**) and are fixed in completely different ways, and the borderline rows are the ones a careless patch breaks. The expectations were verified three ways: against the real runtime offline (`caliber/tests/test_cookbook_09_self_healing.py`, all 35 tickets x 2 versions), against a live stack by the script below, and for four of them by hand in the browser by the spec.

## Configuration and environment

| Variable | Value used | Meaning |
| --- | --- | --- |
| `CALIBER_EGRESS_ALLOWED_HOSTS` | `127.0.0.1` | lets the `lookup` node call the stand-in; without it `lookup` fails with `outbound request to '127.0.0.1' is blocked` |
| `CALIBER_WORKFLOW_RUN_QUEUE_ENABLED`, `CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED` | `true` (set by the helper) | runs are queued and picked up by a worker; checkpointing is on, and still no node failure writes a checkpoint (see below) |
| `CALIBER_FEATURE_COOKBOOKS` | `true` | only for the gallery card; the lab does not need it |
| `CALIBER_RELEASE_REQUIRE_GRADED_EXECUTOR_FOR_ENVIRONMENT_CLASSES` | unset | would change the `prod` refusal from "requires a deploy gate" to "requires a deploy gate graded by a real configured model" once a gate exists |
| `CALIBER_BASE_URL` | from `env.sh` (the server root) | what the SDK script connects to |
| `CALIBER_TOKEN` | a personal access token | what the script signs in with |
| `COOKBOOK_09_ASSETS` | the repo's `assets/` | optional: another copy of the assets |
| `CALIBER_LAB_BASE_URL` | `$CALIBER_URL/caliber` | the UI spec's server |

## Run it through the API

The whole lab is one script. From the repository root:

```bash
PYTHONPATH=sdk/caliber-sdk python sdk/caliber-sdk/examples/cookbooks/cookbook_09_self_healing_workflows.py
```

It takes about three minutes (about 75 runs, each picked up by the worker, plus the 2-second timeouts) and prints the following on success (ids vary; everything else is deterministic) and exits 0. It exits non-zero, naming the ticket and the value, as soon as a documented outcome does not hold.

```json
{
  "alias": {"prod_refused": 400, "through_alias": ["completed", "failed"]},
  "debugger": {
    "check_reference": {"checkpoints": 0, "completed_steps": ["start", "validate"],
                        "error_spans": ["workflow.run", "node.check_reference"], "run_id": "WR-..."},
    "decide":          {"checkpoints": 0, "completed_steps": ["start", "validate", "check_reference", "lookup"],
                        "error_spans": ["workflow.run", "node.decide"], "run_id": "WR-..."},
    "validate":        {"checkpoints": 0, "completed_steps": ["start"],
                        "error_spans": ["workflow.run", "node.validate"], "run_id": "WR-..."}
  },
  "propose_patch": {"patch_id": "WP-...", "patch_kind": "prompt", "refused": 400},
  "regression_slice": 11,
  "retry": {"attempts": 2, "retry_run_id": "WR-...", "status": "failed"},
  "tickets": 35,
  "v1": {"completed": 11, "failed": 24},
  "v1_failed_at": {"check_reference": 3, "decide": 4, "validate": 17},
  "v2": {"completed": 35, "decisions": {"approve": 10, "manual_review": 13, "rejected_input": 12}, "failed": 0},
  "version_1": "WFV-...", "version_2": "WFV-...", "workflow_id": "WF-..."
}
```

What each part did, with the raw calls if you would rather use `curl` (`$H` is `-H "authorization: Bearer $CALIBER_TOKEN"`, `$B` is `$CALIBER_API`):

1. **Import and publish version 1.** `POST $B/workflows/import {"manifest_yaml": "...", "name": "refund-intake-<n>"}`, then `POST $B/workflow-versions/{version}/publish`.
2. **Run the dataset.** `POST $B/workflow-runs {"workflow_version_id": "WFV-...", "input": "<payload>"}` for each row, then poll `GET $B/workflow-runs/{id}`. A failed run has `status: failed`, `error_code: runtime_error` and an `error_summary` such as `ToolExecutionError: python_code node 'check_reference' timed out after 2.0s`. The script compares status, node and error text with the dataset.
3. **Read the debugger's evidence.** `GET $B/workflow-runs/{id}/trace` (spans: the root `workflow.run` and the failing `node.<id>` are `ERROR`), `.../events` (a `workflow.run.failed` event), `.../checkpoints` (`[]`: a node failure writes no checkpoint, only an approval or wait does), `POST .../retry` and `GET .../lineage` (`total_attempts: 2`; the retry fails at the same node because it runs the same code on the same input).
4. **`propose-patch`.** `POST $B/workflow-versions/{id}/propose-patch {"evidence": {"node_id": "check_reference", "root_cause": "..."}}` stores a candidate (listed by `GET $B/workflows/{id}/patches`) and returns `patch_kind: prompt`, an empty `semantic_ops` and the sentence *Tighten the agent instructions: require tool-grounded answers and forbid unsupported policy claims.* The engine only knows four agent-shaped fixes (a grounding guardrail, a handoff, a guardrail relaxation, a prompt rewrite); it has nothing to say about a Python node's regex. With `"category": "guardrail"` it tries to relax `check_reference.on_failure`, which a code node does not have, and answers **400** `cannot generate patch: patch produced an invalid manifest: ... Extra inputs are not permitted`. (It used to answer 500; that is fixed.)
5. **Write version 2.** `POST $B/workflows/{id}/versions {"manifest": ...}` with version 1's manifest and the three `code` fields replaced by the files in `assets/workflow/patch/`, `POST $B/workflow-versions/{id}/validate` (`valid: true`), then publish.
6. **Prove it.** All 35 tickets on version 2, and the 11 outputs compared byte for byte with version 1's.
7. **The alias.** `POST $B/workflows/{id}/deployments/staging/promote {"version_id": ...}` for version 1 then 2, a run through `{"workflow_id", "alias": "staging"}`, `POST .../deployments/staging/rollback`, the same run again (it fails), and `POST .../deployments/prod/promote` (400 `promoting to 'prod' (production) requires a deploy gate; attach one whose required_for_aliases includes this alias`).

Run `--cleanup` as the script's last argument to delete the workflow it created.

## Recipe: run it through the UI

These are the steps `e2e/cookbooks/labs/09-workflow-debugger.spec.ts` performs with real clicks. Sign in as `admin` / `admin` at `$CALIBER_URL/caliber/`. Ticket numbers are rows of `failing-inputs.jsonl`; paste the row's `input.payload` as the run input.

1. **Import version 1.** Sidebar group **Agent resources**, **Workflows**, **Import**. Name it `refund-intake`, **Choose file** `ticket-intake.v1.workflow.yaml`, **Validate dependencies**: **Preflight passed**. **Import as new** opens the editor.
2. **Publish it.** Editor toolbar **Publish**: **Validate**, **Next**, **Next**, **Publish**. The editor shows **Published.**
3. **Run a healthy ticket.** **Run Monitor**, paste `F01`, **Run**. The run is **completed** and the output is `{"amount": 48.0, "currency": "USD", "decision": "approve", "reason_code": "ELIGIBLE_AUTO_APPROVE", "ticket_id": "T-3101"}`.
4. **Reproduce a schema failure.** Paste `F12` (amount `"48.00"`), **Run**. The badge reads **failed**. **Recovery Diagnostics** says *This run ended in failed.*, **Current node** `validate`, and the timeline shows `ToolExecutionError: python_code node 'validate' failed: ValueError: amount must be a number, got '48.00'`. **Resume Checkpoints** says *No persisted checkpoints exist for this run yet* (only approvals and waits create one). **Execution Debugger** shows *1 recorded step* (`start`): the failing node is not a recorded step, **Current node** names it.
5. **Reproduce the poison record.** Paste `F29`, **Run**. After about two seconds: **failed**, `python_code node 'check_reference' timed out after 2.0s`, *2 recorded steps*.
6. **Reproduce the upstream failure.** Paste `F32`, **Run**. **failed**, `python_code node 'decide' failed: KeyError: 'tier'`, *4 recorded steps*. The failing node is `decide` but the cause is in the step before it: in **Execution Debugger** select the `lookup` step and read the response: status 503, `{"error": "account service unavailable"}`.
7. **Retry.** Click **Retry**. **Retry Lineage** reads **Attempt 2 of 2** and the new run fails at the same node. A retry runs the same code on the same input; it cannot fix a code fault.
8. **Look for a patch.** Workflow page (**Workflows**, `refund-intake`), tab **CALIBER Patches**: **No CALIBER patches yet**. Nothing in the UI calls `propose-patch` (step 4 of the API path is the only way to generate a candidate), and a candidate would not change this workflow's code anyway.
9. **Patch it.** Tab **Versions**, **Restore as draft** on v1. In the editor click the `validate` node and replace the **Code** with the content of `assets/workflow/patch/validate.py`; do the same for `check_reference` and `decide`. **Save** (the status reads **Saved**), then **Publish** as in step 2. This is version 2.
10. **Prove it.** **Run Monitor**, run `F12` (completed, `ELIGIBLE_AUTO_APPROVE`), `F29` (completed, `ORDER_REF_UNVERIFIED`), `F32` (completed, `ACCOUNT_LOOKUP_FAILED`, `"upstream_status": 503`) and `F18`, a ticket with no account id (completed, `"decision": "rejected_input"`).
11. **Compare, then promote.** Workflow page, tab **Versions**, **Compare versions**: base v1, newer v2: the panel lists the three changed nodes. Click **Promote** on v2 and confirm **Promote v2 to LIVE**: the page shows *promoting to 'prod' (production) requires a deploy gate; attach one whose required_for_aliases includes this alias*. The single live alias is `prod`; the UI hides the Deployments and Promotions tabs, so the `staging` alias in the API path has no UI.

Run the spec (the server must already be up, with the stand-in service running):

```bash
cd caliber/caliber-ui
CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" \
  npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/09-workflow-debugger.spec.ts
```

## Verify it worked

| Check | How | Expected |
| --- | --- | --- |
| Version 1 against the dataset | the script, or `GET /workflow-runs/{id}` per ticket | 11 `completed`, 24 `failed`: 17 `validate`, 3 `check_reference`, 4 `decide` |
| The failing node is findable | `GET /workflow-runs/{id}/trace`; **Run Monitor > Recovery Diagnostics** | an `ERROR` span `node.<id>` under an `ERROR` `workflow.run`; **Current node** is the same id |
| The trace is an error trace | **Operate > Observability**, **Status** `Error` ([lab 16](../16-observability-triage/README.md)) | the run's trace is listed as Error with the ticket as its request |
| A retry does not fix it | `GET /workflow-runs/{retry}/lineage` | `total_attempts: 2`, same `error_summary` node |
| Version 2 against the dataset | the script | 35 `completed`: 10 approve, 13 manual_review, 12 rejected_input |
| No regression | the script (byte comparison) | the 11 version-1 successes give identical output |
| The alias and its rollback | `GET /workflows/{id}/deployments` | `staging` serves v2, then v1 after `rollback` |
| `prod` is guarded | `POST .../deployments/prod/promote` | 400, `requires a deploy gate` |
| The code is the code in the repo | `caliber/tests/test_cookbook_09_self_healing.py` | `assets/workflow/patch/*.py` equal the v2 node bodies; v1 and v2 differ only in those nodes |

## Common errors and troubleshooting

| You see | Cause | Fix |
| --- | --- | --- |
| a run fails with `outbound request to '127.0.0.1' is blocked: it points at a loopback address ... Add the host to CALIBER_EGRESS_ALLOWED_HOSTS` (the failing node is `lookup`, which is not named in the text) | the server was started without the allowed host | restart with `CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1` |
| every ticket that reaches `lookup` fails with a connection error | the stand-in service is not running | start `account_service.py 8780`; check `curl 'http://127.0.0.1:8780/accounts?account_id=A-1007'` |
| runs stay `queued` | no worker | the helper starts one (`--no-workers` turns it off); `GET /readiness` names the queue |
| Preview (or a deploy gate) reports `preview blocked before execution because ordinary Preview cannot isolate these capability nodes: validate (python_code), ...` | Python-code and API-request nodes are not isolated in Preview | prove the change with real runs, as this lab does |
| `400 promoting to 'prod' (production) requires a deploy gate` | a production alias needs a `deploy_gates` entry whose `required_for_aliases` contains it | expected here; see "What is not covered" |
| `400 ... requires a deploy gate graded by a real configured model, but it was graded by the deterministic FakeWorkflowExecutor` | a gate exists but the stack is simulated | set `CALIBER_RELEASE_REQUIRE_GRADED_EXECUTOR_FOR_ENVIRONMENT_CLASSES=''` to accept deterministic evidence, or configure a model |
| `400 deploy gate failed for alias 'prod'` with `completion_rate 0 >= 1` | the gate replays in Preview, which cannot run these nodes | a gate cannot grade this workflow |
| `400 cannot generate patch: patch produced an invalid manifest` | `propose-patch` was asked for a guardrail patch on a node that is not a guardrail | expected; use a different `category` or write the patch by hand |
| `409` on import | a workflow with that name exists | use another name |
| Observability shows the failed runs as `OK` | an older server: failed runs used to close their trace `OK` | upgrade; this lab's fix is `observability/mlflow_tracing.py` |
| the Run Monitor's **Resume Checkpoints** is empty | a node failure writes no checkpoint | expected |

## What is not covered

- **Preview and deploy gates cannot run this workflow**, so "validated before publish" here means: version 2 ran 35 real tickets and 11 of them matched version 1 exactly. A gate (`deploy_gates`, `required_for_aliases: ["prod"]`, a test set) is the product's way to make that automatic; with a workflow made of Python and API nodes it fails closed, as shown.
- **No UI for aliases.** The alpha UI has one live alias (`prod`) and hides Deployments and Promotions; the `staging` alias and its rollback are API only.
- **`propose-patch` is API only and agent-shaped.** It cannot repair a code node, and no page calls it.
- **Retry from a checkpoint** exists for approvals and waits (see [lab 03](../03-tool-hardening-contract-lab/README.md)); it is not what happens to a node failure.
- **The regex is the real thing, the service is not.** The poison records time out because Python's `re` really backtracks; the account service is a stand-in.
- Role failures (`403` for a non-operator running a workflow) are covered by [lab 18](../18-projects-roles-tokens-audit/README.md), not repeated here.

## Cleanup and reset

```bash
# the script's workflow
PYTHONPATH=sdk/caliber-sdk python sdk/caliber-sdk/examples/cookbooks/cookbook_09_self_healing_workflows.py --cleanup
# or by hand: DELETE $CALIBER_API/workflows/WF-...   (and delete the workflow from Workflows in the UI)

# everything
scripts/cookbook-env.sh down lab09
rm -rf .tmp/cookbook-env/lab09
pkill -f assets/upstream/account_service.py
```

Verification of this lab: `caliber/.venv/bin/python -m pytest caliber/tests/test_cookbook_09_self_healing.py --no-cov` (the dataset, both workflow versions and the stand-in service against the real runtime, offline), `caliber/.venv/bin/python -m pytest sdk/caliber-sdk/tests/test_cookbook_examples.py -k cookbook_09 --no-cov` (the script against an offline fake server), the script above against a live stack, and the Playwright spec.
