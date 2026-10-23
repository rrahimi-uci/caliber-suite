# Cookbook 16 — Production Observability & Triage

> **Prerequisite flags:** the lab itself needs none: Observability, Test Sets and Review Queues are part of the supported alpha. `CALIBER_FEATURE_COOKBOOKS=true` only opens the Cookbooks gallery (a preview area, off by default). Aria is not used.

**Track:** Operate · **Level:** Core · **Time:** 30–45 min

**Surfaces:** Observability · Test Sets · Review Queues · Workflows (to produce the traces)

## What it demonstrates and the expected outcome

A workflow that is **already running** in production, and what you do when it goes wrong. You replay 40 production-like refund tickets through a real workflow (the buggy version 1 of [cookbook 09](../09-workflow-debugger-self-healing-lab/README.md)), so CALIBER records 40 real MLflow traces: 12 are `ERROR` (a schema violation, a poison record that times out a node, an upstream error that surfaces two nodes later) and 28 are `OK`. Four of those 28 are wrong anyway: nothing errored, but the ticket went to manual review when the right answer is *approve*. You find the failures with the status filter, read a trace's node tree to see which node failed, capture the traces that matter into a **test set** with the outcome they should have had, and stand up a **review queue** where people classify them (where did it fail, how severe, what should have happened). Their answers are written back onto the traces. You also see what Observability does when there is nothing to show, when a trace does not exist, and when MLflow is down. Finally you close the loop: fix the workflow as a new version, re-run the 16 tickets that mattered, and compare an old trace with its new one.

**You are done when:**

- Observability lists 40 new traces, 12 `ERROR` and 28 `OK`, each with the ticket as its request, and the `Error` filter finds every failing one;
- a failing trace's node tree names the node that failed: `node.validate`, `node.check_reference` or `node.decide`, and for an upstream failure the `lookup` node that received the 503 is `OK` while `decide` is `ERROR`;
- an unknown trace id is a **404** (*trace 'tr-...' not found*), a filter that matches nothing is an empty list, and with MLflow down the routes answer **503** *the trace store (MLflow) is not reachable*;
- a test set holds the 16 traces that matter (12 errors + the 4 wrong `OK` ones), each with its gold outcome;
- the `prod-triage` queue holds the same 16 traces, two are answered, and the answers appear on those traces as assessments (`failure_mode`, `severity`, `root_cause_known`, `expected_output`, `reviewer_notes`);
- after the fix (workflow version 2) the 16 tickets re-run to their gold outcome and a version 1 trace can be compared with its version 2 trace;
- `cookbook_16_observability_triage.py` exits 0 and the Playwright spec passes.

## Prerequisites and setup

You need a running CALIBER ([`docs/start/alpha-install.md`](../../../docs/start/alpha-install.md)), an administrator login, a worker, and `caliber/.venv` with `caliber_sdk` installed (see [Before you start](../README.md#before-you-start-one-time-setup)). The traces come from running cookbook 09's workflow, so the same two things apply as there:

1. the stand-in account service, [`assets/upstream/account_service.py`](../09-workflow-debugger-self-healing-lab/assets/upstream/account_service.py) in lab 09 (it is **not a real system**; it answers 503, 500 and 404 for three fixed ids), on port 8780;
2. `CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1` on the server.

**Simulated or real provider: nothing here needs a model.** The workflow has no agent node, so the simulated default (`CALIBER_LLM_PROVIDER=fake`) changes nothing: every trace is a real trace of a real run. The one thing a model would add is the optional *Run evaluation* baseline on the test set, which is **not part of this lab** (Evaluations refuse to run on a simulated stack, see [lab 10](../10-judge-certification-human-review-lab/README.md)).

From the repository root:

```bash
python3 docs-site/cookbooks/09-workflow-debugger-self-healing-lab/assets/upstream/account_service.py 8780 &
CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1 scripts/cookbook-env.sh up lab16 --flags cookbooks
source .tmp/cookbook-env/lab16/env.sh

curl -s -c /tmp/lab16.jar -X POST $CALIBER_API/auth/login -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"admin"}' >/dev/null
export CALIBER_TOKEN=$(curl -s -b /tmp/lab16.jar -X POST $CALIBER_API/auth/tokens \
  -H 'content-type: application/json' -d '{"name":"lab16"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["token"])')
# CALIBER_BASE_URL (the server root, which is what the SDK wants) is already exported by env.sh
```

Seed the traffic. The API script does this itself and the UI path sends the tickets through the Run Monitor (step 2 of the UI recipe), so this is only needed to make traces for your own exploration. It takes about a minute and a half and prints one line of JSON per ticket, the run and its trace:

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python docs-site/cookbooks/16-observability-triage/assets/seed_traces.py > .tmp/traces.json
python3 -c 'import json,collections; d=json.load(open(".tmp/traces.json")); print(collections.Counter(v["status"] for v in d.values()))'
# Counter({'completed': 28, 'failed': 12})
```

## Dataset and sample data

| File | Contents | Why |
| --- | --- | --- |
| [`assets/dataset/prod-regression.sample.jsonl`](assets/dataset/prod-regression.sample.jsonl) | 40 tickets `P01` to `P40`. Each row has `input.payload` (the ticket as a JSON string, the run input), `expected` (the gold outcome: `decision`, `reason_code`, and `upstream_status` or `v2_errors_contain` where relevant), `tags` and a `note` that says why. | the production traffic, and what it should have produced |
| [`assets/seed_traces.py`](assets/seed_traces.py) | imports cookbook 09's version 1 workflow, publishes it and submits the 40 tickets as runs | real traces: the runtime records them, the script writes none |
| [`assets/fix/`](assets/fix/) | `validate.py` and `decide.py` (identical to lab 09's patch) and `check_reference.py` (lab 09's, plus trimming and upper-casing the order reference) | the code of workflow version 2, pasted into the three nodes in step 12; a test runs all 40 tickets through it and checks the gold outcome |
| [`assets/review-queues/triage-review.review-questions.json`](assets/review-queues/triage-review.review-questions.json) | the five triage questions: `root_cause_known` (pass/fail), `failure_mode` (prompt, tool, retrieval, data, infra), `severity` (low, medium, high), `expected_output` (text, becomes a trace expectation), `reviewer_notes` | the queue's label schema |

The 40 tickets: 24 **correct in production** (approve, over the limit by a cent, a suspended account, a euro and a pound refund, exactly the premium limit and one over it), 4 **`OK` but wrong** (a lowercase order reference `ord-5521`, a lowercase one with a suffix, and two with trailing spaces: the workflow sends them to manual review, a person would approve), and 12 **errors**: 4 schema violations that a careful normalizer would accept (an amount sent as `"75.00"`, `"$120.00"`, `"35"`, a lowercase `usd`), 2 that must be rejected with a reason (a missing account id, truncated JSON), 3 **poison records** (an order reference that sends a regex into catastrophic backtracking: `check_reference` times out after 2 seconds) and 3 **upstream failures** (account service answering 503, 500 and 404, which crash `decide` rather than `lookup`).

The silent failures are the point of the triage queue: the Error filter finds 12, a person has to find the other 4.

## Configuration and environment

| Variable | Value used | Meaning |
| --- | --- | --- |
| `CALIBER_EGRESS_ALLOWED_HOSTS` | `127.0.0.1` | lets the workflow's `lookup` node call the stand-in |
| `CALIBER_TRACING_ENABLED` | `true` (default) | with `false` runs complete but record no trace (see below) |
| `CALIBER_TRACING_EXPERIMENT` | unset | when set, Observability only searches that experiment |
| `MLFLOW_TRACKING_URI` | from `env.sh` | the MLflow server CALIBER reads traces from |
| `CALIBER_BASE_URL` | from `env.sh` (the server root) | what the SDK scripts connect to |
| `CALIBER_TOKEN` | a personal access token | what the API script signs in with (the UI spec does not need it) |
| `COOKBOOK_16_ASSETS` | the repo's `assets/` | optional: another copy of the assets |
| `CALIBER_LAB_BASE_URL` | `$CALIBER_URL/caliber` | the UI spec's server |

## Run it through the API

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python sdk/caliber-sdk/examples/cookbooks/cookbook_16_observability_triage.py
```

About a minute and a half. It exits 0 and prints the following (ids vary):

```json
{
  "failure_paths": {"by_trace": "WR-...", "future_window": 0, "unknown_trace": 404},
  "node_tree": {"poison": "node.check_reference", "upstream": "node.decide", "validate": "node.validate"},
  "ok_but_wrong": ["P25", "P26", "P27", "P28"],
  "seeded": 40,
  "test_set": {"dataset_id": "ED-...", "examples": 16},
  "traces": {"ERROR": 12, "OK": 28},
  "triage": {
    "answered": 2,
    "assessments": {
      "P25": ["expected_output", "failure_mode", "reviewer_notes", "root_cause_known", "severity"],
      "P38": ["expected_output", "failure_mode", "reviewer_notes", "root_cause_known", "severity"]
    },
    "queued": 16
  }
}
```

It exits non-zero, naming the value, as soon as one of those does not hold. The calls, in order (`$H` is `-H "authorization: Bearer $CALIBER_TOKEN"`, `$B` is `$CALIBER_API`):

1. **Seed.** `seed_traces.py`: `POST $B/workflows/import`, `POST $B/workflow-versions/{id}/publish`, then `POST $B/workflow-runs` for each ticket. A run carries its trace id (`trace_id`).
2. **Observability.** `GET $B/observability/traces?limit=200&since_ms=<before seeding>` (add `&status=ERROR`), `GET $B/observability/traces/{trace_id}`. A trace has `status`, a `request` (the ticket), a `response` (the output, or the error text of a failed run) and `spans`: the root `workflow.run`, one `node.<id>` per node. A failed run's root span and the failing node's span are `ERROR`.
3. **Failure paths.** `GET $B/observability/traces/tr-000...` is **404**; a `since_ms` in the future is `{"traces": []}`; `GET $B/workflow-runs/by-trace/{trace_id}` finds a trace's run (the trace itself does not name its run).
4. **The test set.** `POST $B/eval-datasets {"name", "owner"}`, then for each of the 16 traces `POST $B/eval-datasets/{id}/examples/from-trace {"trace_id", "expected": {gold}, "tags"}`. The example's input is the trace's request, tagged `from-trace` and `trace:<id>`.
5. **Triage.** `POST $B/review-queues {"name", "questions": [...]}`, `POST $B/review-queues/{id}/items {"trace_ids": [...]}`, `POST $B/review-queues/{id}/items/{item}/submit {"answers": {...}}` for two items, then `GET $B/observability/traces/{id}`: its `assessments` hold the answers.

Add `--cleanup` as the last argument to archive the test set and the queue when the run succeeds.

### What Observability does when something is wrong

Checked on a running stack (lab16), not inferred:

| Situation | How I produced it | What happens |
| --- | --- | --- |
| a trace id that does not exist | `GET $B/observability/traces/tr-0123456789abcdef` | `404 {"detail": "trace 'tr-0123456789abcdef' not found"}`; the page shows the same message |
| nothing matches | `?status=ERROR&since_ms=<an hour from now>`; in the UI, search `no-such-ticket-zzz` | `{"data": {"traces": []}}`; the page shows its empty state |
| MLflow is down | stopped the MLflow process of the stack | `503 {"detail": "the trace store (MLflow) is not reachable: MlflowException: API request to http://127.0.0.1:.../api/2.0/mlflow/experiments/search failed with exception HTTPConnectionPool(...): Max retries exceeded ..."}` in about 5 ms, for the list and for a single trace. It used to answer an empty `200`, which looks exactly like "no traces" |
| tracing is disabled | restarted with `CALIBER_TRACING_ENABLED=false` | **not an error**: `/readiness` reports `tracing_enabled: false`, a run still `completed` but its `trace_id` is `null`, the 40 existing traces stay readable and no new one appears |
| a signed-in user with no role | created `outsider16` (`POST $B/auth/accounts`), signed in | **can read every trace and its request text** (200), sees no workflows (`[]`), and cannot run one (403 `missing required scope`). This is the documented alpha limit: traces are not scoped to a project ([`alpha-scope.md`](../../../docs/start/alpha-scope.md)); do not put sensitive data in them on a shared deployment |

## Recipe: run it through the UI

These are the steps `e2e/cookbooks/labs/16-observability-triage.spec.ts` performs with real clicks and file uploads (it calls no API). Sign in as `admin` / `admin` at `$CALIBER_URL/caliber/`. A ticket is a row of `assets/dataset/prod-regression.sample.jsonl`; paste the row's `input.payload` as the run input. Ticket `P38` is `T-5038`, and so on.

1. **Import and publish the production workflow.** Sidebar group **Agent resources**, **Workflows**, **Import**. Name it `prod-refund-intake`, **Choose file** `docs-site/cookbooks/09-workflow-debugger-self-healing-lab/assets/workflow/ticket-intake.v1.workflow.yaml` (the buggy version 1 of lab 09), **Validate dependencies**: **Preflight passed**. **Import as new** opens the editor; toolbar **Publish**: **Validate**, **Next**, **Next**, **Publish**. The editor shows **Published.** This is the "workflow that is already running in production".
2. **Send the 40 production tickets through it.** In the editor open **Run Monitor**, paste a ticket, click **Run**, wait for the badge (**completed** or **failed**), and repeat for `P01` to `P40` (about two minutes in all; the poison tickets take two seconds each to time out). Twelve end **failed**, 28 **completed**. Every run records a trace: the run's **Execution Trace** panel has an **Open in Observability** link to it. (If you would rather not paste 40 tickets, the API path's `assets/seed_traces.py` submits the same 40 in about a minute and a half.)
3. **Filter by status.** Sidebar group **Operate**, **Observability**. The list shows the newest traces, each with the ticket as its request. Open **Filter by status** and choose **Error**: every row is `ERROR` (12 for each time you sent the 40). Choose **OK**: 28 per time. Choose **All statuses**. The newest trace can read **IN_PROGRESS** for a moment after its run ended (MLflow closes traces asynchronously); the list refreshes itself every five seconds, under any filter, so a run that had not closed when you opened the page (the last failure, under **Error**) appears without a reload.
4. **Open a failing trace.** Search `T-5035` in **Search traces** and click the row (the trace id goes into the URL as `?trace=`). The **Spans** tree for a poison record reads `workflow.run` ERROR, `node.start` OK, `node.validate` OK, `node.check_reference` ERROR, 2.04 s: the node that timed out. For an upstream failure (`T-5038`) `node.lookup` is **OK** and `node.decide` is **ERROR**: the cause is one step before the failing node. For a silent failure (`T-5025`) nothing is `ERROR`: the trace looks healthy, only its response gives it away.
5. **Nothing matches; an unknown trace.** In **Search traces** type `no-such-ticket-zzz`: the empty state appears. Open `/caliber/observability?trace=tr-doesnotexist`: the **Trace** panel says *trace 'tr-doesnotexist' not found*.
6. **Create the test set.** **Prompt lifecycle**, **Test Sets**, **+ New Test Set**: name `prod-regression`, owner `@admin`, **Create**. (Observability can only add to an existing test set; it has no create option.)
7. **Capture failures.** Back in **Observability**, open the `T-5038` trace, in **Add to test set** choose `prod-regression`, **Add example**. Then open the test set, **Capture from trace** (the toggle in the page header), paste the `T-5035` trace id and **Capture**. The set has 2 examples; each input is the ticket, tagged `from-trace` and `trace:<id>`.
8. **Put the gold outcome on it.** A capture stores the trace's **response** as the expected answer, and for a failed run the response is the error text: `"ToolExecutionError: python_code node 'decide' failed: KeyError: 'tier'"`. That is evidence of the failure, not the answer you want. On the first example click **Edit**, replace **Expected (JSON object)** with `{"decision": "manual_review", "reason_code": "ACCOUNT_LOOKUP_FAILED", "upstream_status": 503}` and **Save**: the example becomes version 2 and shows the new value.
9. **Create the triage queue.** **Operate**, **Review Queues**, **New queue**, name `prod-triage`, then the five questions of `triage-review.review-questions.json` with **+ Add question** (key, title, type, target, and for `failure_mode` and `severity` the comma-separated options), **Create queue**.
10. **Enqueue and review.** Select the queue, paste two trace ids (the `T-5038` trace and a silent-wrong one, `T-5025`) into **Trace ids**, **Enqueue**: **Items (2)**. For each: **Pass** on the first question, pick **Where did it fail?** and **Operational severity**, fill **What should the run have produced?** and **Triage notes**, **Submit review**. After the second: **All items reviewed.**
11. **Read the answers on the trace.** **Observability**, open the `T-5038` trace: **Assessments** lists `failure_mode` tool and `severity` high among the five answers. The reviewer's *expected output* is a trace expectation.
12. **Fix the workflow: a new version.** **Workflows**, `prod-refund-intake`, tab **Versions**, **Restore as draft** on v1. In the editor click the `validate` node and replace its **Code** with `assets/fix/validate.py`; do the same for `check_reference` (`assets/fix/check_reference.py`: it also upper-cases and trims the order reference, which the four silent failures needed) and `decide` (`assets/fix/decide.py`). **Save** (the status reads **Saved**), then **Publish** as in step 1. This is version 2.
13. **Re-run the 16 tickets that mattered and compare.** In **Run Monitor** run `P25` to `P40`: all 16 are **completed** and the output is the gold outcome of the dataset row (`P38`: `decision` `manual_review`, `reason_code` `ACCOUNT_LOOKUP_FAILED`, `upstream_status` 503; `P25`: `approve`, `ELIGIBLE_AUTO_APPROVE`). In **Observability** search `T-5038`: two traces, the newest `OK` (version 2) and below it the `ERROR` one (version 1). Click **Compare**, select both rows: the panel shows both statuses side by side with their span trees. The **Error** filter finds no new failures.
14. **Compare the two versions.** **Workflows**, `prod-refund-intake`, tab **Versions**, **Compare versions**: base v1, newer v2: the panel lists the changed nodes, `check_reference` among them.

Run the spec (the server must be up and the stand-in service running):

```bash
cd caliber/caliber-ui
CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" \
  npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/16-observability-triage.spec.ts
```

It takes about three minutes and leaves 56 traces, a workflow with two versions, a test set and a queue (all uniquely named) on the stack.

## Verify it worked

| Check | How | Expected |
| --- | --- | --- |
| 40 real traces | `.tmp/traces.json`, **Observability** | 28 `completed` runs, 12 `failed`; 28 `OK` and 12 `ERROR` traces |
| The Error filter is complete | `GET $B/observability/traces?status=ERROR` | every failed run's trace |
| The failing node | trace `spans` | `node.validate`, `node.check_reference` or `node.decide` is `ERROR`, with an `ERROR` root |
| The request is there | trace `request` | the ticket JSON |
| The silent failures | compare the run's output to `expected` | `P25` to `P28`: `OK` traces whose decision is not the gold one |
| The test set | `GET $B/eval-datasets/{id}/examples` | 16 examples (API path) or 2 (UI path), tagged `from-trace`, `trace:<id>` |
| The queue | **Review Queues** | `Items (16)` (API path) or `(2)` (UI path), answered items read **All items reviewed.** |
| Write-back | trace `assessments` | `failure_mode`, `severity`, `root_cause_known`, `expected_output`, `reviewer_notes` |
| The fix | step 13 | the 16 re-runs give the dataset's gold `decision` and `reason_code`; `T-5038` shows an `ERROR` trace (v1) and an `OK` one (v2) in **Compare** |
| The data files | `caliber/tests/test_cookbook_16_observability.py` | 40 rows, tags, gold outcomes consistent with the workflow's real behavior |

## Common errors and troubleshooting

| You see | Cause | Fix |
| --- | --- | --- |
| the seed runs fail with `outbound request to '127.0.0.1' is blocked` (**all** 40 fail, 0 `completed`) | the server was started without the allowed host | restart with `CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1` |
| the seed runs fail with a connection error | the stand-in service is not running | start `account_service.py 8780` |
| `503 the trace store (MLflow) is not reachable` | MLflow is down or `MLFLOW_TRACKING_URI` is wrong | start MLflow; `GET /readiness` says which check fails |
| `404 trace '...' not found` | the id is wrong, or the trace was written to another MLflow | copy the id from the `?trace=` URL; traces appear after the run ends |
| a trace reads `IN_PROGRESS` right after its run ended, and the status counts are 11 `ERROR`, 1 `IN_PROGRESS` | MLflow records traces asynchronously; the last trace of a run can lag by a moment | wait a few seconds and read again; the script does this itself |
| Observability is empty after a run | `CALIBER_TRACING_ENABLED=false`, or `CALIBER_TRACING_EXPERIMENT` points elsewhere | check `/readiness` (`tracing_enabled`) and the variable |
| **Add example** answers `trace '...' not found or has no request to capture` | the trace has no request. Traces of runs recorded by an older server have none | seed again; a current server records the run input as the request |
| the captured example's expected answer is an error message | a capture stores the response, which for a failed run is the error | edit the example (step 8) or pass `expected` in the API |
| Failed runs show as `OK` in Observability | an older server closed a failed run's trace `OK` | upgrade: the root span is now marked `ERROR` (`observability/mlflow_tracing.py`) |
| `403` creating a queue or a test set | the account lacks `caliber.operator` | sign in as an operator, or see [lab 18](../18-projects-roles-tokens-audit/README.md) |
| `409` creating the test set or queue | the name exists | use another name |

## What is not covered

- **No trace-to-refinement automation.** Nothing watches MLflow for flagged traces and verifying a queue item starts no job ([`alpha-scope.md`](../../../docs/start/alpha-scope.md)).
- **No Evaluations baseline.** Scoring the test set needs a model; on the simulated stack Evaluations refuse (lab 10 shows the refusal and the stand-in model that works around it).
- **No UI for the MLflow-down case.** It was checked through the API; the page's rendering of a 503 was not exercised.
- **The trace does not name its run.** `GET /workflow-runs/by-trace/{trace_id}` does, and so does the run's own `trace_id`.
- **A request preview is the run input as MLflow stores it**: a string input is shown JSON-quoted in some list rows.

## Cleanup and reset

```bash
# the script's test set and queue
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python sdk/caliber-sdk/examples/cookbooks/cookbook_16_observability_triage.py --cleanup
# the UI path's: archive the prod-regression test set (Test Sets) and the prod-triage queue (Review Queues)

# everything, including the 40 traces
scripts/cookbook-env.sh down lab16
rm -rf .tmp/cookbook-env/lab16
pkill -f assets/upstream/account_service.py
```

Verification of this lab: `caliber/.venv/bin/python -m pytest caliber/tests/test_cookbook_16_observability.py caliber/tests/test_routes_observability.py caliber/tests/test_failed_run_trace_status.py --no-cov`, `caliber/.venv/bin/python -m pytest sdk/caliber-sdk/tests/test_cookbook_examples.py -k cookbook_16 --no-cov` (the script against an offline fake server), the script and the Playwright spec above against a live stack.
