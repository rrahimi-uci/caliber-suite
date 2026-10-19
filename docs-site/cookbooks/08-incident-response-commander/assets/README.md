# SCN-08 assets

Files for [the lab](../README.md). The first group is what the lab runs and verifies; the second is the original model-based layer, kept as a starting point and **not** exercised.

## Used and verified

| File | What it is | How it is loaded |
| --- | --- | --- |
| [`dataset/incident-cases.jsonl`](dataset/incident-cases.jsonl) | 41 incidents: `{id, tags, input: {incident: {...}}, expected: {answer, severity, action, requires_approval}}` | API: `POST /eval-datasets`, then `POST /eval-datasets/{id}/examples` per line. UI: `Test Sets > + Add example` (one row at a time) |
| [`workflows/incident-commander.workflow.json`](workflows/incident-commander.workflow.json) | The 14-node workflow and its `prod` deploy gate (`artifacts.eval_datasets.incident-cases`, `deploy_gates.verdict-matches-dataset`) | `Workflows > Import > Choose file`, or `POST /workflows/import`. Set the test set name in `artifacts.eval_datasets.incident-cases.dataset_name` first; the test set must exist |
| [`workflows/evidence-lookup-drill.workflow.json`](workflows/evidence-lookup-drill.workflow.json) | Error Boundary around the deployment lookup | API import; run through the queue |
| [`workflows/missing-tool-drill.workflow.json`](workflows/missing-tool-drill.workflow.json) | Names an unregistered tool: the import is refused | API import (expect 400) |
| [`workflows/status-page-drill.workflow.json`](workflows/status-page-drill.workflow.json) | API Request to an unreachable host | API import; the run fails with the egress reason |
| [`workflows/mitigation-wait-drill.workflow.json`](workflows/mitigation-wait-drill.workflow.json) | Wait For Event `incident.mitigation_confirmed` | API import; resume with `POST /workflow-runs/resume-by-event` |
| [`tools/lookup_recent_deployments.py`](tools/lookup_recent_deployments.py), [`tools/query_service_health.py`](tools/query_service_health.py) | Python Code node bodies (synthetic fixtures). The first is the Error Boundary drill's target | Paste into a Python Code node. They run in the tool sandbox: no `import` lines (`json`, `re`, `math`, `datetime`, `statistics` are in scope) |

Dataset conventions: `input` is what the run receives (JSON text) and is a single key `incident` holding an object, so a deploy-gate replay (which sends the first string field of `input`, else the whole `input` as JSON) sends the whole record. `expected.answer` is `"<severity> <action>"`: the gate's `contains_expected` scorer looks for it in the output (the `summarize` node writes `"verdict": "sev1 rollback"`).

## Reference only, not run by the lab

`prompts/incident-commander.md`, `skills/*.md`, `judges/incident-action-correctness.judge.json`: the model-based layer of the original recipe (fact and hypothesis separation, rollback checklist, an action-correctness judge). They need a real model to mean anything and have not been run.
