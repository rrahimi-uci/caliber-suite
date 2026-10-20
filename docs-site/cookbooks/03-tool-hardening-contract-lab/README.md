# Policy-Safe Decision Tool: a refund only a second person can release

> **Prerequisite flag:** `CALIBER_FEATURE_COOKBOOKS=true` (a preview area, off by default) for the gallery starter in the last
> step; everything else in this lab works with every preview flag off. No model is involved anywhere in this lab, so nothing in it is
> simulated. Every step was run against a real server (a fresh stack from `scripts/cookbook-env.sh up lab03 --flags cookbooks`)
> through the API and through the browser.

## What this lab demonstrates

A deterministic refund policy as a **registered tool**, hardened before anything depends on it, and then used in a workflow whose
write action waits for a human.

You register a **read** tool (`decide_refund`: approve, deny, or send to manual review, with a stable reason code) and a **write**
tool (`initiate_refund`). You run them in the **sandbox** (the read tool for real in a subprocess, the write tool mocked), save a
**49-case fixture suite**, run it, record the run and **pin it as the baseline**, then break one fixture on purpose and see exactly one
**regression**. Then you import a workflow that decides, routes, and issues the refund only after a **different person** approves, and
you try the ways that can go wrong.

**You are done when:**

- `decide_refund` and `initiate_refund` are registered; the write tool carries `requires_approval`, and registering a write tool
  without it is refused with `400`;
- in the sandbox `decide_refund` runs live (`isolation: subprocess`) and `initiate_refund` is mocked (`mocked: true`);
- the 49 fixtures score `pass_rate 1.0` (49/49), the run is recorded as a `suite` run and pinned as the baseline, and one wrong
  fixture scores 48/49 with exactly one regression against that baseline;
- a run of the imported workflow stops at `waiting_approval` on the `refund` node with no refund issued; the person who started it
  is refused with `403` (a second person must decide); `approver1` approves; the run still waits until **Resume**; then the refund
  fires once, for the right amount;
- a rejected run fails with `approval_rejected` and never issues the refund; a denied decision finishes by itself with no approval;
- a viewer cannot run, calibrate or approve (`403`), and the audit log names the approver, the initiator and the tool registrations;
- you have seen where the sandbox stops being a boundary: a stalled tool is killed, a large file write is refused, the environment
  is empty, and **the network is still reachable**.

## Prerequisites and setup

1. A running CALIBER built per [`docs/start/alpha-install.md`](../../../docs/start/alpha-install.md), with **runtime approvals**,
   the **workflow run queue** and **checkpointing** on (the lab environment script turns all three on), and a worker running.
2. Two accounts that hold the right platform scopes. Scopes come from server settings, not from the account:
   `CALIBER_APPROVER_USERS` must list `approver1` next to `admin`. Nobody else needs a scope; `viewer1` has none on purpose.
3. From a checkout, one command starts a disposable stack with all of that (this is what the lab was verified on):

   ```bash
   CALIBER_APPROVER_USERS=admin,approver1 \
   CALIBER_REGISTERED_TOOL_SANDBOX_TIMEOUT_SECONDS=5 \
   scripts/cookbook-env.sh up lab03 --flags cookbooks
   source .tmp/cookbook-env/lab03/env.sh
   curl -s $CALIBER_API/readiness | jq -c '.data|{ready,simulated}'
   ```

   Expected: `{"ready":true,"simulated":["eval","llm"]}`. The `simulated` providers play no part in this lab.
   The 5-second tool deadline only makes the "stalled tool" check quick; the default is 30 seconds and the check still passes.
4. `CALIBER_APPROVAL_ALLOW_SELF_APPROVAL` is **not** set, which is what makes the second-person rule bite. Leave it unset.
5. For the API path you need a token for `admin` (below) and the dev virtualenv, which already has `caliber_sdk` installed editable.
   For the UI path you need `npx` and Chromium (`cd caliber/caliber-ui && npx playwright install chromium`).

## Dataset and sample files

All under [`assets/`](assets/) (the folder's [README](assets/README.md) has the policy table and the dataset's tags):

| File | What it is | Why |
| --- | --- | --- |
| [`dataset/refund-fixtures.jsonl`](assets/dataset/refund-fixtures.jsonl) | 49 refund cases, `{id, name, tags, inputs, expectations}` | window and amount boundaries, three customer tiers, abuse patterns (fraud flags, refund velocity), fail-closed cases, nine kinds of malformed input |
| [`tools/decide-refund.tool.json`](assets/tools/decide-refund.tool.json) | the read tool, as a `POST /tools` body | the policy under test |
| [`tools/initiate-refund.tool.json`](assets/tools/initiate-refund.tool.json) | the write tool, with `requires_approval: true` | the effect that must wait |
| [`workflow/refund-decision.workflow.yaml`](assets/workflow/refund-decision.workflow.yaml) | the workflow | decide, route, gate, refund |

The expectations were written from the policy, not copied from the tool's output, and a unit test
(`caliber/tests/test_cookbook03_refund_policy.py`) checks every row against the tool, so the dataset cannot drift.
The tools are the shipped `caliber.workflows.demo_tools:decide_refund` and `:initiate_refund`: **a registered tool must be importable
Python in the server's environment, and there is no inline tool editor.** To register your own, install your package into that
environment and point `module_path` at it.

## Configuration and environment

| Variable | Value used | Why |
| --- | --- | --- |
| `CALIBER_APPROVER_USERS` | `admin,approver1` | `approver1` holds `caliber.approver`, the scope the approval gate demands |
| `CALIBER_OPERATOR_USERS` | `admin` (the script's default) | Run a tool, calibrate, and **Resume** need `caliber.operator`; `approver1` deliberately lacks it |
| `CALIBER_ADMIN_USERS` | `admin` | only an admin registers a tool |
| `CALIBER_REGISTERED_TOOL_SANDBOX_TIMEOUT_SECONDS` | `5` | wall-clock deadline for one tool call (default `30`, at most `120`) |
| `CALIBER_WORKFLOW_RUN_QUEUE_ENABLED`, `..._CHECKPOINTING_ENABLED`, `..._RUNTIME_APPROVALS_ENABLED` | `true` | a run is queued, can pause, and honours the gate. Without the last one a run **does not pause** |
| `CALIBER_APPROVAL_ALLOW_SELF_APPROVAL` | unset | set it to `true` only on a single-operator install |
| `CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST` | unset (any module) | set it (`caliber.*,yourco.tools.*`) for anything beyond a trial |
| `CALIBER_BASE_URL`, `CALIBER_TOKEN` | from `env.sh` and the next step | what the script reads |
| `LAB03_SUFFIX` | unset | set it (`_2`) to re-run the script on a stack that already holds an earlier run |

The people: `admin` starts runs and holds every scope; `approver1` (an `approver` platform scope; **reviewer** in the workspace) decides;
`viewer1` (no scopes; **viewer** in the workspace) only reads. The script creates both accounts, with lab-only passwords.

## Run it through the API

Everything below is in one script that exits `0` only when every outcome above was observed. Mint a token, then run it from the repo root:

```bash
source .tmp/cookbook-env/lab03/env.sh
curl -s -c /tmp/lab03.cookies -X POST $CALIBER_API/auth/login -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"admin"}' > /dev/null
export CALIBER_TOKEN=$(curl -s -b /tmp/lab03.cookies -X POST $CALIBER_API/auth/tokens \
  -H 'content-type: application/json' -d '{"name":"lab03"}' | jq -r .data.token)

(cd sdk/caliber-sdk && ../../caliber/.venv/bin/python -m examples.cookbooks.cookbook_03_policy_safe_decision_tool)
```

It takes one to two minutes (calibration runs one subprocess per case, about 0.7 s each; the `(34s)` varies with load). Real output, trimmed (ids vary):

```text
== 1. workspace and people
  workspace PRJ-dae76ea4: admin owns it, approver1 reviews, viewer1 reads
== 2. register the tools
  ok  decide_refund is a read tool allowed in preview
  ok  initiate_refund is a write tool with requires_approval
  ok  registered tool carries no Built-in label and is available
  ok  a write tool without requires_approval is rejected -> HTTP 400
== 3. sandbox
  ok  decide_refund ran live in a subprocess
  ok  live output is {'decision': 'approve', 'reason_code': 'ELIGIBLE_AUTO_APPROVE', 'requires_approval': False}
  ok  initiate_refund was mocked, not called
  ok  an unknown field is a tool error, not a decision
  ok  viewer1 cannot run a tool -> HTTP 403
  ok  approver1 (no operator scope) cannot run a tool -> HTTP 403
== 4. fixtures, calibration, test run, baseline
  ok  viewer1 cannot calibrate -> HTTP 403
  ok  calibration 49/49 passed, pass_rate 1.0 (34s)
  ok  suite run TTR-02730981 recorded
  ok  the run is pinned as the baseline
== 5. regression drill
  ok  the drill fails exactly one case: ['R09 amount boundary: exactly 200.00']
  ok  compared with the baseline, that case is the one regression
== 6. the workflow
  ok  import preview is valid
  ok  one advisory warning only: ['write_tool_without_approval']
== 7. approve: a second person decides
  ok  run paused at the refund node
  ok  the initiator cannot approve their own run -> HTTP 403
  ok  viewer1 cannot approve -> HTTP 403
  ok  approval recorded by approver1; the run still waits for Resume
  ok  approver1 cannot Resume (needs operator) -> HTTP 403
  ok  refund issued: {'amount_usd': 49.0, 'order_id': 'ord_1007', 'refund_status': 'initiated'}
== 8. reject
  ok  run failed with approval_rejected
== 9. deny and manual review
  ok  output is the decision: {'decision': 'deny', 'reason_code': 'NOT_REFUNDABLE_STATE', 'requires_approval': False}
  ok  approved manual review refunded 250.0
== 10. audit trail and effect ledger
  ok  audit has approve_workflow_run_runtime_approval by approver1
  effect ledger rows for the approved run: 0 (the ledger covers webhook and api_request nodes, not registered tools)
== 11. where the sandbox boundary stops
  ok  a stalled tool is killed: tool sandbox timed out after 5.00s
  ok  the file-size limit stops a large write
  ok  the child starts with an empty environment
  ok  BUT the child can still reach the network: this is a process boundary, not isolation
== 12. the gallery starter
  ok  the installed draft has a decision table and a Human Approval node
== done
```

What each stage does, by hand (the script wraps exactly these calls; `$T` is a tool id, `$H` is `-H "Authorization: Bearer $CALIBER_TOKEN"`
plus `-H "X-CALIBER-Project: $PROJECT"` once you have a workspace):

```bash
curl -s $H -X POST $CALIBER_API/tools -H 'content-type: application/json' \
  -d @docs-site/cookbooks/03-tool-hardening-contract-lab/assets/tools/decide-refund.tool.json     # 201
curl -s $H -X POST $CALIBER_API/tools/$T/test-run -H 'content-type: application/json' \
  -d '{"input": {"order_state":"delivered","amount":49.0,"days_since_order":5,"risk_flags":[]}}'     # live, isolation: subprocess
curl -s $H -X PUT  $CALIBER_API/tools/$T/test-cases -d '{"test_cases": [ ... 49 cases ... ]}'        # fixtures
curl -s $H -X POST $CALIBER_API/tools/$T/calibrate                                                   # inline: pass_rate, cases
curl -s $H -X POST $CALIBER_API/tools/test-runs -d '{"tool_id":"'$T'","kind":"suite","results":[...]}'  # record the run
curl -s $H -X POST $CALIBER_API/tools/$T/baseline -d '{"test_run_id":"TTR-..."}'                    # pin it
curl -s $H -X POST $CALIBER_API/workflows/import -d '{"manifest_yaml": "...", "name":"refund-decision"}'
curl -s $H -X POST $CALIBER_API/workflow-runs -d '{"workflow_version_id":"WFV-...","input":"{...case...}"}'
curl -s $H -X POST $CALIBER_API/workflow-runs/$RUN/approval/approve -d '{"reason":"within policy"}'  # as approver1
curl -s $H -X POST $CALIBER_API/workflow-runs/$RUN/resume                                            # as admin
```

Two things the script taught while it was written:

- **Calibration is synchronous and slow for 49 cases** (about 35 s), which is longer than the SDK's default 30 s request timeout; the
  script builds its client with `timeout=180`. For a larger suite use the durable form: `POST /tools/{id}/calibration-jobs` returns `202`
  with a job id; a worker runs it and `GET /tools/{id}/calibration-jobs/{job}` returns `status: completed` and
  `result: {pass_rate: 1.0, total: 49, passed: 49}`. It does not keep per-case output, so it cannot be recorded as a test run.
- **A calibration is not a test run.** `calibrate` returns the scores; the Tools page records a `suite` run from them for you, and over
  the API you record it yourself (`POST /tools/test-runs`), which is what you pin as the baseline.

## Recipe: run it through the UI

Sign in at `$CALIBER_URL/caliber/` as `admin` / `admin`. Before step 1, set the stage once:

- **Administration** (sidebar group **Admin**): under **Accounts** create `approver1` and `viewer1` (any 12+ character passwords; the script
  and spec use `lab03-approver1-password` and `lab03-viewer1-password`).
- In the top bar click the **+** next to the workspace picker, name it `refund-ops`, **Create and select**. Back on **Administration**,
  under **Project access** pick `refund-ops (owner)` and add `approver1` as **Reviewer** and `viewer1` as **Viewer**.

Then, with `refund-ops` selected:

1. **Register the decision and refund tools.** **Agent resources → Tools**, **Register Tool**. Name `decide_refund`, version `1.0`,
   module `caliber.workflows.demo_tools`, callable `decide_refund`; on the Schema step switch each schema to **Raw JSON** and paste
   `input_schema` and `output_schema` from the asset; on **Safety & Review** leave **Read** and tick **Allow in Preview**. Register again
   for `initiate_refund` and click **Write**: **Requires Approval** switches on and is locked. On the Tools list neither row carries a
   **Built-in** or **Unavailable** label (those mark CALIBER's own tools). Registering opens the tool's detail page; its
   **Open the workspace** link leads to the workspace (Sandbox, Fixtures, Hardening, Test Runs) the next steps use. The address
   becomes `/tools?tool=TL-…`, so a reload or a bookmark returns to the same tool.
2. **Sandbox.** Open `decide_refund`, tab **Sandbox**, paste the R01 inputs, **Test Run**: **Success** with `"decision": "approve"`.
   Open `initiate_refund`, **Sandbox**, input `{"order_id": "ord_1007", "amount": 49.0}`: **Success (mocked)**.
3. **Fixtures.** Open `decide_refund`, tab **Fixtures**, click **Load cases from file** and choose
   `assets/dataset/refund-fixtures.jsonl`. See: `Loaded 49 cases from refund-fixtures.jsonl. Save cases to keep them.` and 49 case
   blocks, named `R01 small delivered order` and so on, each with the line's `inputs` as the input JSON, assertion `equals`, and the line's
   `expectations` as the value (loading only fills the editor). Click **Save cases**: **Saved**. Reload the page and open **Fixtures**
   again: still 49. (A case can also be added by hand with **Add case**; the value is the JSON the tool must return, in any key order.
   The file may be JSON Lines or one JSON array; a row is `{name, inputs, expectations}`, or the stored shape
   `{name, input, assertion: {type, value}}`; an `id` is put in front of the name.)
4. **Hardening.** Tab **Hardening**, **Run calibration**. After about half a minute: **Pass rate 100% — 49/49 passed**, and every case
   shows a green **pass**. A `suite` run is recorded for you.
5. **Baseline and a regression.** Tab **Test Runs**, **Set as baseline**: the run shows a **Baseline** marker. Back on **Hardening**,
   set case R09's expected value to `{"decision": "manual_review", "reason_code": "AMOUNT_OVER_THRESHOLD", "requires_approval": true}`
   and **Run calibration**: **Pass rate 98% — 48/49 passed**, R09 shows **fail**. On **Test Runs** the comparison says **1 regression**,
   flags R09, and shows **Net score -2%**. Put the value back and run once more: 100% again.
6. **Import the workflow.** **Workflows**, **Import**. Name it `refund-decision`, **Choose file** `refund-decision.workflow.yaml` (if
   you registered the tools under other names, replace the two tool names in the file first), **Validate dependencies**: **Preflight
   passed**, both tools **resolved**. **Import as new** opens the editor.
7. **Approve as a second person.** **Run Monitor**, paste the R01 inputs as the run input, **Run**: the status badge reads **Awaiting
   approval**, current node `refund`, with **Approve** and **Reject** under it. Click **Approve** yourself: refused, *the account that
   triggered this run cannot approve it; a second person must decide*. In a second browser session sign in as `approver1`, pick
   `refund-ops`, open the workflow, **Run Monitor**, choose the run under **Recent runs**, **Approve**. In your own session **Resume**
   becomes enabled (Approve recorded the decision; it did not advance the run). Click **Resume**: the run is **completed** and the output
   shows `"refund_status": "initiated"` and `"amount_usd": 49.0`.
8. **Reject, deny and read the audit log.** Start the R01 inputs again; as `approver1` click **Reject**: the run is **failed** and its output
   has no refund. Start the R23 inputs (a cancelled order): the run **completes** with `"decision": "deny"` and shows no approval buttons.
   Open **Audit Log** (group **Operate**): `approve_workflow_run_runtime_approval` and `reject_workflow_run_runtime_approval` by `approver1`,
   `resume_workflow_run` by `admin`.

As `viewer1`, open `decide_refund` → **Sandbox** → **Test Run**: the page shows `missing required scope; expected one of ['caliber.operator']`.
Last, **Integrations → Cookbooks**, card 03 **Install**, acknowledge the prerequisite, **Install**: the gallery's starter draft (a decision
table and a Human Approval node) opens in the editor.

The whole path is `caliber/caliber-ui/e2e/cookbooks/labs/03-tool-hardening.spec.ts`; run it against a running stack:

```bash
cd caliber/caliber-ui
CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/03-tool-hardening.spec.ts
```

## Verify it worked

| Check | Where | Expected |
| --- | --- | --- |
| Tool kinds | `GET /tools/{id}` | `decide_refund`: `side_effect_level: read`, `allow_in_preview: true`. `initiate_refund`: `write`, `requires_approval: true` |
| Write without approval | `POST /tools` with `side_effect_level: write` and no flag | `400` ("set requires_approval=true for side_effect_level 'write'") |
| Sandbox | `POST /tools/{id}/test-run` | read tool: `isolation: subprocess`, `mocked: false`; write tool: `mocked: true`, `isolation: mocked`, output `{"_preview_mock": true}` |
| Suite | `POST /tools/{id}/calibrate` | `total: 49`, `passed: 49`, `pass_rate: 1.0` |
| Baseline | `GET /tools/{id}/workspace` | `baseline_run_id` is the pinned `TTR-…` id |
| Regression | the drill's run vs the baseline | the one case `R09 amount boundary: exactly 200.00` flips pass to fail; pass rate `0.8571` on the 7-case slice the script re-scores (the UI re-runs all 49: 48/49) |
| Gate | `GET /workflow-runs/{id}` | `status: waiting_approval`, `current_node_id: refund`; `GET .../approvals` shows one `pending` approval with `policy_snapshot.allow_self_approval: false`, `required_role: caliber.approver` |
| Refund | `summary.steps[refund].tool_calls[0].result` | `{"amount_usd": 49.0, "order_id": "ord_1007", "refund_status": "initiated"}`; before Resume this list is empty |
| Rejection | the rejected run | `status: failed`, `error_code: approval_rejected`; the `refund` step is `blocked` and has no tool call |
| Audit | `GET /audit-log` (or **Audit Log**) | `approve_workflow_run_runtime_approval` and `reject_workflow_run_runtime_approval` by `approver1`; `resume_workflow_run` and `enqueue_workflow_run` by `admin`; `register_tool`, `calibrate_tool`, `create_tool_test_run`, `set_tool_baseline`, `import_workflow` |
| Events | `GET /workflow-runs/{id}/events` | `workflow.run.waiting_approval` (node `refund`), `workflow.run.approval.approved` (`decided_by: approver1`), `workflow.run.resumed` (`actor: admin`), `workflow.run.completed` |

**The effect ledger.** `GET /system/effects?status=any&workflow_run_id=<run>` returns `effects: []` for the approved run. That is correct, and it
is a limit worth knowing: the ledger (at-most-once effects across a restart) covers `webhook` and `api_request` nodes, **not registered
tools**. A registered write tool is protected by the approval gate, and its call is in the run's events, trace and audit trail, but a
restart between the call and its checkpoint is not suppressed by the ledger. Make a payments tool idempotent on its own side.

### The trust boundary, honestly

- **Tool authors are trusted.** A registered tool is code, imported from a Python module and called. Only a global admin registers one.
  CALIBER does not defend itself against an admin who registers a malicious tool.
- **The sandbox is a process boundary, not isolation.** Each call runs in a separate `python -I` interpreter with an empty environment, a
  private working directory, POSIX resource limits and a wall-clock deadline that kills the process group. The script shows each of those, and
  the last one it shows is the limit of the idea: the child **can still open a connection to the CALIBER server's own health endpoint**
  and keeps ambient filesystem access. It is not a container, VM or seccomp boundary. For authors you do not trust, point
  `CALIBER_TOOL_SANDBOX_BACKEND` at a backend you provide. Without one, promoting a workflow that uses registered tools to a
  production-class alias is refused.
- **Memory limit not exercised.** The limit is `RLIMIT_AS`; on the macOS machine this lab was verified on, a 1 GiB allocation inside a
  tool succeeded, so the limit is not enforced there. The file-size limit (`RLIMIT_FSIZE`) and the deadline are.
- **Roles apply twice.** The platform scope (`caliber.operator` to run, `caliber.approver` to decide) and, inside a workspace, the project
  role (`reviewer` or `owner` to approve; `editor` or `owner` to register and save fixtures). A weak role gets `403`, and a person outside
  the workspace gets `404` because the run is invisible to them.

## Common errors and troubleshooting

| You see | Why | Fix |
| --- | --- | --- |
| `404 feature_disabled` on `/cookbooks` | the Cookbooks flag is off | start the stack with `--flags cookbooks` (only the gallery step needs it) |
| the run **completes** without ever pausing | runtime approvals are off in this deployment | set `CALIBER_WORKFLOW_RUN_RUNTIME_APPROVALS_ENABLED=true` (with the queue and checkpointing) and restart |
| the run stays `queued` | no worker is running | set `CALIBER_BACKGROUND_TASKS_ENABLED=true` (the lab script does) |
| `403 the account that triggered this run cannot approve it; a second person must decide` | you are the initiator | approve as another account; on a single-operator install only, set `CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=true` |
| `403 missing required scope; expected one of ['caliber.approver']` on Approve | the account is not in `CALIBER_APPROVER_USERS` | add it and restart |
| `403 project role 'viewer' cannot perform resource.approve` | the account is a viewer in the workspace | make them a **Reviewer** under **Administration → Project access** |
| `403 missing required scope; expected one of ['caliber.operator']` on Test Run, Calibrate or **Resume** | the account has the approver scope but not the operator scope | use an operator account; this split is deliberate |
| `404 workflow run not found` for `approver1` | the workflow lives in a workspace they are not a member of | add them as a member and pick that workspace |
| `400 ... set requires_approval=true for side_effect_level 'write'` | a write tool was registered without approval | send `requires_approval: true` (the wizard does it for you) |
| `409 tool 'decide_refund' version '1.0' already registered` | the tool exists from an earlier run | re-run the script with `LAB03_SUFFIX=_2`, or reset the stack |
| calibration call fails with `timed out` after 30 s | 49 cases take about 35 s | use a client timeout of 180 s, or the queued `calibration-jobs` form |
| a case errors with `TypeError: decide_refund() got an unexpected keyword argument` | the fixture input has a field the tool does not take | the contract is strict by design; the policy ignores nothing it does not know |
| `tool sandbox timed out after 5.00s` | the tool outran the deadline and its process group was killed | raise `CALIBER_REGISTERED_TOOL_SANDBOX_TIMEOUT_SECONDS` (at most 120) or fix the tool |
| `Preflight passed`, but the editor's Validate reports `write_tool_without_approval` | the warning looks for a Human Approval node; this workflow gates the write with the tool's own `requires_approval` | advisory; the gate is real (see the `waiting_approval` run) |
| `module '...' is not in CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST` | an allowlist is set and your module is not on it | add its prefix |

## Cleanup and reset

Remove what the script created (tools are archived, the workflow deleted, the workspace archived; accounts and audit entries stay):

```bash
(cd sdk/caliber-sdk && ../../caliber/.venv/bin/python -m examples.cookbooks.cookbook_03_policy_safe_decision_tool --cleanup)
```

Archived tools keep their name and version, so a re-run on the same stack needs `LAB03_SUFFIX` (above). For a clean slate, discard the whole
disposable stack:

```bash
scripts/cookbook-env.sh down lab03 && rm -rf .tmp/cookbook-env/lab03
```
