# Governed Tool Connectivity (MCP): classify first, then call

> **Prerequisite flags:** `CALIBER_FEATURE_INTEGRATIONS=true` (the **MCP Servers** page and the `/mcp-servers` API) and, for the
> gallery step, `CALIBER_FEATURE_COOKBOOKS=true`. Both are preview areas and are off by default. No model is involved anywhere in
> this lab, so nothing in it is simulated. Every step was run against a real CALIBER server (a fresh stack from
> `scripts/cookbook-env.sh up lab05 --flags integrations,cookbooks`) and a real MCP server, through the API and through the browser.

## What this lab demonstrates

An MCP server is somebody else's code that CALIBER calls on your behalf. This lab connects one, finds out what it offers, and decides
what CALIBER may do with each tool **before** anything calls it.

The MCP server is the lab's own: [`assets/server/repo_tracker_mcp.py`](assets/server/repo_tracker_mcp.py), an engineering repo tracker
written with the official `mcp` Python SDK. It has six tools: four **read** tools (`search_repositories`, `list_issues`, `get_issue`,
`scan_dependencies`), a **write** tool (`issue_write`) and an **external action** (`create_release`). CALIBER starts it as a child
process over `stdio` (the lab runs offline; see [why not HTTP](#why-stdio-and-not-http)).

You register it, test the connection, discover the tools, and find that none of them can be called yet. You classify every tool
(allowed or not, read or write, approval or not, a rate limit), call an allowed read tool, and then meet each way a call can be refused
or fail. You save 28 test cases and calibrate three read tools. You bind the write tool in a workflow and watch a second person's
approval hold it. Throughout, the server writes a **ledger** of every call it actually executed, so each refusal is proved from the
server's side, not only from CALIBER's answer.

**You are done when:**

- the server is registered and `test-connection` discovers **6 tools**; every one starts **unclassified and denied**, and a call to one
  is refused with *has no explicit allow, side-effect, and approval classification*;
- after you classify the tools, `search_repositories` returns the two repositories tagged `mcp`, and the server's ledger shows exactly
  one execution;
- calibration scores **16/16**, **8/8** and **4/4** on the three read tools, and one wrong expectation turns exactly one case red
  (`15/16`, pass rate `0.9375`);
- the blocked tool (`create_release`), a tool the server never advertised (`drop_database`) and a call over the rate limit are all
  refused by CALIBER, **the ledger shows none of them ran**, and none leaves a trace; a tool's own error, a missing argument and a
  slow tool (a 10 second timeout) are failed calls that do leave a trace;
- both mutating tools accept an optional `idempotency_key`, so a client that re-issues a call whose answer was lost does not file a
  second issue or publish a second release: the retry returns the original result with `replayed: true` and shows on the server's
  ledger as `issue_write.replay` / `create_release.replay`, and a key reused for a different issue or release is refused;
- the write tool `issue_write` runs from the admin Playground (a **break-glass** surface), but inside a workflow it stops at
  `waiting_approval`, the person who started the run is refused (`403`), and it runs only after a second person approves and the run is
  resumed;
- a `/bin/sh` command, a `python -c` command and a loopback HTTP endpoint are all registered but refused, with the reason;
- the server's history, the audit log and the `mcp.<server>.<tool>` traces tell the same story.

## Why stdio and not HTTP

The lab was written to run with no network and no credentials, so the MCP server has to run on the CALIBER host. CALIBER accepts a
remote server (SSE or streamable HTTP) only if its host is on `CALIBER_MCP_REMOTE_HOST_ALLOWLIST` **and** its resolved address passes
the egress policy. The allowlist's default is `localhost,127.0.0.1,::1`, but the egress policy blocks loopback and private ranges by
default, so `http://127.0.0.1:<port>/mcp` is registered and then refused (`remote MCP host '127.0.0.1' resolves into the blocked
loopback range; allowlisting a name does not authorize the address it points at`). The lab shows that refusal (step 12 of the script)
rather than switching the policy off. Over `stdio` the server is a child of CALIBER: the command must be on
`CALIBER_MCP_STDIO_COMMAND_ALLOWLIST` (default `${PYTHON}`), and a Python script must be an allowlisted `-m` module or an absolute path
in `CALIBER_MCP_STDIO_PYTHON_SCRIPT_ALLOWLIST`. The lab sets that one variable to the server script's path.

The server script also speaks streamable HTTP (`--transport streamable-http`, with an optional bearer token); it was run directly with
`curl` to check that, but it was **not** run through CALIBER here, for the reason above.

**The real-world variant, not run here.** The same lab against GitHub's official remote MCP server (`https://api.githubcopilot.com/mcp/`)
needs network access, a personal access token in the CALIBER server's environment, `api.githubcopilot.com` on
`CALIBER_MCP_REMOTE_HOST_ALLOWLIST`, and GitHub's own tool names classified after discovery. The request body is in
[`assets/real-world/github-mcp.server.json`](assets/real-world/github-mcp.server.json). The Servers page's quick-connect catalog
(GitHub, PostgreSQL, pgvector, Apache AGE, MinIO, Hugging Face, Ollama, Playwright) pre-fills such bodies. Nothing about it was executed.

## Prerequisites and setup

1. A running CALIBER built per [`docs/start/alpha-install.md`](../../../docs/start/alpha-install.md) with the **integrations** flag on, and
   a worker running (the workflow step queues a run).
2. Three accounts that hold the right platform scopes. Scopes come from server settings, not from the account:
   `CALIBER_OPERATOR_USERS` lists `lab05-operator`, `CALIBER_APPROVER_USERS` lists `lab05-approver`; `lab05-viewer` has no scope on
   purpose. Only `admin` holds the admin scope, which registering, testing, classifying and invoking all need.
3. From a checkout, one command starts a disposable stack with all of that (this is what the lab was verified on):

   ```bash
   export CALIBER_MCP_STDIO_PYTHON_SCRIPT_ALLOWLIST="$PWD/docs-site/cookbooks/05-mcp-connectivity-governance-lab/assets/server/repo_tracker_mcp.py"
   CALIBER_OPERATOR_USERS=admin,lab05-operator CALIBER_APPROVER_USERS=admin,lab05-approver \
   scripts/cookbook-env.sh up lab05 --flags integrations,cookbooks
   source .tmp/cookbook-env/lab05/env.sh
   curl -s $CALIBER_API/readiness | jq -c '.data|{ready,simulated}'
   ```

   Expected: `{"ready":true,"simulated":["eval","llm"]}`. The simulated providers play no part in this lab.
4. `CALIBER_APPROVAL_ALLOW_SELF_APPROVAL` is **not** set, which is what makes the second-person rule bite. Leave it unset.
5. The `mcp` Python package must be importable by the interpreter CALIBER runs (`${PYTHON}`); it is a CALIBER dependency, so the
   development virtualenv already has it. For the UI path you need `npx` and Chromium
   (`cd caliber/caliber-ui && npx playwright install chromium`).

## Dataset and sample files

All under [`assets/`](assets/) (the folder's [README](assets/README.md) has the tool table):

| File | What it is | Why |
| --- | --- | --- |
| [`server/repo_tracker_mcp.py`](assets/server/repo_tracker_mcp.py) | the MCP server: 6 tools, a call ledger, optional HTTP transport | something real to connect to |
| [`dataset/repositories.json`](assets/dataset/repositories.json) | 24 repositories: language, stars, topics, archived | what `search_repositories` searches |
| [`dataset/issues.json`](assets/dataset/issues.json) | 36 issues across 6 repositories, open and closed | what `list_issues` and `get_issue` read |
| [`dataset/tool-classification.jsonl`](assets/dataset/tool-classification.jsonl) | one row per tool: allowed, side effect, approval, rate limit, and why | the classification you save |
| [`dataset/calibration-cases.jsonl`](assets/dataset/calibration-cases.jsonl) | 28 cases: 16 search, 8 list, 4 get | what you save and calibrate |
| [`dataset/invoke-checks.jsonl`](assets/dataset/invoke-checks.jsonl) | 10 refusals and failures with the error each must give | the failure scenarios |
| [`workflow/file-issue.workflow.yaml`](assets/workflow/file-issue.workflow.yaml) | a one-tool workflow with an MCP tool binding | the approval gate |
| [`real-world/github-mcp.server.json`](assets/real-world/github-mcp.server.json) | the GitHub variant's body, **not run** | for reference |

The calibration expectations were worked out from the data, not copied from the server's output, and
`caliber/tests/test_cookbook_05_mcp_governance_lab.py` checks every case against the server, so the dataset cannot drift.

## Configuration and environment

| Variable | Value used | Why |
| --- | --- | --- |
| `CALIBER_FEATURE_INTEGRATIONS` | `true` (via `--flags integrations`) | opens MCP Servers and `/mcp-servers`; without it they answer `404 feature_disabled` |
| `CALIBER_FEATURE_COOKBOOKS` | `true` (via `--flags cookbooks`) | only the last step, the gallery starter |
| `CALIBER_MCP_STDIO_PYTHON_SCRIPT_ALLOWLIST` | the absolute path of `repo_tracker_mcp.py` | lets that one script run as a stdio server |
| `CALIBER_MCP_STDIO_COMMAND_ALLOWLIST` | unset (default `${PYTHON}`) | `/bin/sh` is refused |
| `CALIBER_MCP_REMOTE_HOST_ALLOWLIST` | unset (default `localhost,127.0.0.1,::1`) | and see why it does not make loopback usable |
| `CALIBER_OPERATOR_USERS` / `CALIBER_APPROVER_USERS` | `admin,lab05-operator` / `admin,lab05-approver` | calibrate and Resume need operator; Approve needs approver |
| `CALIBER_WORKFLOW_RUN_QUEUE_ENABLED`, `..._RUNTIME_APPROVALS_ENABLED` | `true` (the environment script sets them) | a run is queued and honours the gate. Without the second it **does not pause** |
| `CALIBER_BASE_URL`, `CALIBER_TOKEN` | from `env.sh` and the next step | what the script reads |
| `CALIBER_STATE_DIR` or `LAB05_LEDGER` | `env.sh` sets the first | where the script puts the MCP server's call ledger (an absolute path, no spaces) |
| `LAB05_SUFFIX` | unset | set it (`_2`) to re-run the script on a stack that already holds an earlier run |

The people: `admin` registers, classifies, invokes and starts runs; `lab05-operator` (platform `operator`; **editor** in the workspace)
saves test cases and calibrates but cannot invoke; `lab05-approver` (platform `approver`; **reviewer**) decides approvals;
`lab05-viewer` (no scopes; **viewer**) only reads. The script creates the three accounts with lab-only passwords.

## Run it through the API

Everything below is in one script that exits `0` only when every outcome above was observed. Mint a token, then run it from the repo root:

```bash
source .tmp/cookbook-env/lab05/env.sh
curl -s -c /tmp/lab05.cookies -X POST $CALIBER_API/auth/login -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"admin"}' > /dev/null
export CALIBER_TOKEN=$(curl -s -b /tmp/lab05.cookies -X POST $CALIBER_API/auth/tokens \
  -H 'content-type: application/json' -d '{"name":"lab05"}' | jq -r .data.token)

(cd sdk/caliber-sdk && ../../caliber/.venv/bin/python -m examples.cookbooks.cookbook_05_governed_tool_connectivity)
```

It takes about a minute (the slow-tool check waits out its 10 second limit, and every call starts the server as a fresh child process,
about half a second each). Real output, trimmed (ids and timings vary):

```text
  created account lab05-operator
  created account lab05-approver
  created account lab05-viewer
== 1. workspace and people
  workspace PRJ-c98f1ca1: admin owns it, operator edits, approver reviews, viewer reads
== 2. register the MCP server
  ok  an operator without the admin scope cannot register a server -> HTTP 403
  ok  registered MCP-1f87224a (stdio)
  ok  readiness: ready, boundary local_containment (a child process, not a sandbox)
  ok  readiness warns that stdio containment does not block files or network
  ok  registering the same name twice is refused -> HTTP 409
== 3. test the connection and discover the tools
  ok  a viewer cannot test the connection -> HTTP 403
  ok  connected: 6 tools discovered
  ok  every tool carries an input schema
  ok  issue_write requires ['repo', 'title']
  ok  discover-tools agrees: 6 tools
  ok  every discovered tool starts unclassified and denied
== 4. an unclassified tool cannot be called
  ok  refused: MCP tool 'search_repositories' has no explicit allow, side-effect, and approval classification
  ok  the server's ledger is empty: nothing ran
== 5. classify every tool
  ok  an allowed write tool without requires_approval is rejected -> HTTP 400
  ok  the rejection names requires_approval
  ok  an operator without the admin scope cannot classify -> HTTP 403
  ok  all six tools are classified
  ok  the stored policies equal tool-classification.jsonl
  ok  issue_write is allowed but requires approval
  ok  create_release (external_action) is blocked
== 6. invoke an allowed read tool
  ok  search returned ['acme/mcp-gateway', 'acme/mcp-servers-internal'] in 802 ms
  ok  the server's ledger shows one execution
  ok  a viewer cannot invoke a tool -> HTTP 403
  ok  an operator without the admin scope cannot invoke a tool -> HTTP 403
== 7. save test cases and calibrate the read tools
  ok  a viewer cannot save test cases -> HTTP 403
  ok  search_repositories: 16/16 passed, pass_rate 1.0 (8s)
  ok  list_issues: 8/8 passed, pass_rate 1.0 (4s)
  ok  get_issue: 4/4 passed, pass_rate 1.0 (2s)
  ok  a wrong expectation fails exactly one case: ['S01 python repositories'], pass_rate 0.9375
  ok  a tool that requires approval cannot be calibrated (no approval checkpoint) -> HTTP 409
  ok  a tool with no saved cases cannot be calibrated -> HTTP 400
  ok  calibrating a tool the server does not have is a 404 -> HTTP 404
== 8. refusals and failures
  ok  F02 a blocked tool is refused before it reaches the server -> 'is blocked by policy'
  ok  F03 a tool the server never advertised is refused -> 'not found on this server'
  ok  F05 a malformed query is the server's error, surfaced as a failed call -> 'stars: expects a number'
  ok  F06 an empty query is the server's error -> 'query must not be empty'
  ok  F07 a missing required argument fails schema validation on the server -> 'Field required'
  ok  F08 an unknown repository is the server's error -> 'was not found'
  ok  F09 a slow upstream call times out at the Playground's 10 second limit -> 'timed out' (12s)
  ok  F04 a rate-limited tool is refused once its per-minute cap is spent -> MCP tool 'scan_dependencies' exceeded its process-local limit of 3/minute
  ok  the fourth call in the minute was the refused one
  ok  the blocked create_release never reached the server (ledger: 0 executions)
  ok  no other refusal reached the server either
== 9. an approval-class write tool, from the admin Playground
  ok  direct invoke ran: {'created': True, 'repo': 'acme/release-notes-bot', 'number': 1, 'title': 'Release notes bot skips pre-release tags'}
  ok  the ledger records the write: the Playground is a break-glass surface, not the approval gate
  ok  list_issues shows the issue that was filed
== 10. the approval gate in a workflow
  ok  import preview resolves MCP-1f87224a/issue_write
  ok  the run paused at the call_tool node
  ok  nothing was written while the run waited
  ok  the initiator cannot approve their own run -> HTTP 403
  ok  the refusal says a second person must decide
  ok  a viewer cannot approve -> HTTP 403
  ok  approving alone does not run the tool
  ok  after Resume the run completed
  ok  the workflow's write executed exactly once
  ok  the issue the workflow filed is on the server
== 11. a blocked tool inside a workflow
  ok  a run is refused before it starts -> HTTP 400
  ok  the preflight names the tool: blocked by policy
  ok  create_release still never reached the server
== 12. servers CALIBER will not run
  ok  a /bin/sh command is registered but not ready: not on the command allowlist
  ok  test-connection refuses it: stdio command '/bin/sh' is not in CALIBER_MCP_STDIO_COMMAND_ALLOWLIST
  ok  python -c is refused: only an allowlisted -m module or script may run
  ok  a loopback HTTP endpoint is refused by the egress policy, even though the host allowlist names it
  ok  a refused test leaves only the registration in that server's history
== 13. history and audit
  ok  history has create_mcp_server
  ok  history has update_mcp_tool_policy
  ok  history has update_mcp_tool_test_cases
  ok  history has calibrate_mcp_tool
  ok  one policy change per tool (6)
  ok  the audit log names lab05-operator for the calibrations
== 14. the traces
  ok  one standalone issue_write trace: the Playground write
  ok  the workflow's write is the same span, nested in the run's own trace: ['mcp.repo-tracker.issue_write', 'node.call_tool', 'node.done', 'tool.issue_write', 'workflow.run']
  ok  no trace for create_release: a refusal leaves none
  ok  no trace for drop_database: a refusal leaves none
  ok  scan_dependencies: three traces (one of them the timeout), none for the refused call
  ok  the trace is one TOOL span with the server, transport, input, output and latency
  ok  a tool's own error is traced as ERROR
== 15. the gallery starter
  ok  the gallery lists 10 steps for this lab
  ok  the starter is a generic single-agent draft with no tools: it does not use the MCP server
== done
```

What each stage does, by hand (the script wraps exactly these calls; `$S` is the server id, `$H` is `-H "Authorization: Bearer $CALIBER_TOKEN"`
plus `-H "X-CALIBER-Project: $PROJECT"` once you have a workspace):

```bash
# register: a stdio server CALIBER starts itself. ${PYTHON} is CALIBER's own interpreter.
curl -s $H -X POST $CALIBER_API/mcp-servers -H 'content-type: application/json' -d '{
  "name": "repo-tracker", "transport": "stdio", "command": "${PYTHON}",
  "args": ["'$PWD'/docs-site/cookbooks/05-mcp-connectivity-governance-lab/assets/server/repo_tracker_mcp.py",
           "--ledger", "'$CALIBER_STATE_DIR'/lab05-ledger.jsonl"]}'                       # 201, execution.ready: true
curl -s $H -X POST $CALIBER_API/mcp-servers/$S/test-connection                            # success: true, 6 tools
curl -s $H $CALIBER_API/mcp-servers/$S/tools                                              # every tool: classified: false
curl -s $H -X POST $CALIBER_API/mcp-servers/$S/invoke-tool -H 'content-type: application/json' \
  -d '{"tool_name":"search_repositories","arguments":{"query":"topic:mcp"}}'              # success: false (unclassified)
curl -s $H -X PATCH $CALIBER_API/mcp-servers/$S/tools/search_repositories/policy -H 'content-type: application/json' \
  -d '{"allowed":true,"side_effect_level":"read","requires_approval":false}'
curl -s $H -X PATCH $CALIBER_API/mcp-servers/$S/tools/issue_write/policy -H 'content-type: application/json' \
  -d '{"allowed":true,"side_effect_level":"write"}'                                       # 400: set requires_approval=true ...
curl -s $H -X PUT $CALIBER_API/mcp-servers/$S/tools/search_repositories/test-cases -H 'content-type: application/json' \
  -d '{"test_cases":[{"name":"python","input":{"query":"language:python"},
       "assertion":{"type":"output_contains","value":"\"total_count\": 8"}}]}'
curl -s $H -X POST $CALIBER_API/mcp-servers/$S/tools/search_repositories/calibrate        # pass_rate, total, passed, cases
curl -s $H "$CALIBER_API/observability/traces?limit=50" | jq '.data.traces[]|select(.name|startswith("mcp."))'
```

Four things the script taught while it was written:

- **A refusal is an answer, not an error.** `invoke-tool` answers `200` with `success: false` and an `error` for an unclassified tool, a
  blocked tool, an unknown tool and a rate-limited call (`duration_ms: 0`: CALIBER never started the server). The tool's own errors
  and a timeout are `success: false` too, but with a real `duration_ms`, because the server was started.
- **`requires_approval` is not a gate on the Playground.** `PATCH`ing `requires_approval: true` is accepted and shown as an **Approval**
  badge, and `issue_write` still ran when the admin invoked it directly (the ledger shows it). The admin-only Playground is a deliberate
  break-glass surface. The gate is in a **workflow**: step 10 of the script. Calibration refuses such a tool outright (`409`), because
  that route has no approval checkpoint.
- **A workflow run is refused before it starts when it binds a blocked tool** (`400 MCP runtime preflight failed ... blocked by policy`);
  it does not start and fail later.
- **Which calls leave a trace.** An `mcp.<server>.<tool>` trace appears once a call passes policy, whether it then succeeds or fails.
  Test-connection, discovery, policy changes and every refusal leave none. A call made inside a workflow run is the same span, nested in
  the run's own trace (`workflow.run > node.call_tool > tool.issue_write > mcp.<server>.issue_write`), not a trace of its own.

## Recipe: run it through the UI

Sign in at `$CALIBER_URL/caliber/` as `admin` / `admin`. Before step 1, set the stage once (this is what the second person and the
viewer need):

- **Administration** (sidebar group **Admin**): under **Accounts** create `lab05-operator`, `lab05-approver` and `lab05-viewer` (any
  12+ character passwords; the script and spec use `<name>-password`).
- In the top bar click the **+** next to the workspace picker, name it `mcp-ops`, **Create and select**. Back on **Administration**,
  under **Project access** pick `mcp-ops (owner)` and add `lab05-operator` as **Editor**, `lab05-approver` as **Reviewer** and
  `lab05-viewer` as **Viewer**.

Then, with `mcp-ops` selected:

1. **Register the server.** **Integrations → MCP Servers**, **Add Server**. Name `repo-tracker`, transport `stdio (local process)`,
   command `${PYTHON}`, arguments `<repo>/docs-site/cookbooks/05-mcp-connectivity-governance-lab/assets/server/repo_tracker_mcp.py --ledger <state dir>/lab05-ui-ledger.jsonl`
   (the Arguments field splits on spaces, so neither path may contain one), **Register Server**. The row shows **Runtime ready · local
   containment** (a child process, not a sandbox).
2. **Test the connection.** On the row click **Test**: **Connected · 6 tools**.
3. **Open the Playground and find nothing callable.** The **Playground** tab, pick `repo-tracker` under **Server**, **Test Connection**:
   **Connection Successful**, **6 tools discovered**. Every tool carries an amber **Unclassified** badge, and for the selected tool the
   button reads **Classify Tool Before Invoking** and is disabled.
4. **Classify every tool.** Click a tool, then in **Tool Policy** set **Allow tool**, **Requires approval**, **Side effect level** and
   **Rate limit / minute** from `tool-classification.jsonl` and **Save Policy** (**Policy saved.**). For `issue_write`, set **write** and
   leave approval off first: **Save Policy** shows the server's refusal (*set requires_approval=true for side_effect_level 'write'...*);
   tick **Requires approval** and save. `create_release` stays unallowed (`external_action`, approval on). Afterwards no tool is
   **Unclassified**; `create_release` wears **Blocked** and `issue_write` **Approval**.
5. **Invoke the read tool.** Select `search_repositories`, type `language:python topic:mcp` into **query**, **Invoke Tool**:
   **Success** and a result listing `acme/mcp-gateway` and `acme/mcp-servers-internal`, with the duration. A row appears under **History**
   (in browser memory only: gone on reload or **Clear**). Then type `stars:>>not-a-number` and invoke again: **Failed**, with the
   server's own message (*stars: expects a number such as stars:>100*).
6. **Meet the refusals.** Select `create_release`: its button reads **Tool Blocked by Policy** and is disabled, and a notice says
   *This tool is blocked by its saved policy.* The page cannot send a blocked call, so the refusal payload (`success: false`, `error:
   "MCP tool 'create_release' is blocked by policy"`, `duration_ms: 0`) comes from the API path above. Select `issue_write`, fill
   **repo** `acme/release-notes-bot` and **title**, **Invoke Tool**: **Success**. It ran, although it requires approval: this is the
   break-glass Playground.
7. **Calibrate the read tool.** Select `search_repositories`. In the **Calibration** panel use **Add case** for each of six cases from
   `calibration-cases.jsonl` (S01 to S06): name, **Test case input JSON**, **Assertion type** `output_contains`, **Assertion value**.
   **Save cases** (**Saved**), then **Run calibration**: **Pass rate 100% — 6/6 passed**, every row **pass**. Change case 1's value to
   `"total_count": 9` and run again: **Pass rate 83% — 5/6 passed**, case 1 shows **fail**. Put it back: 100%. (The API path saves all 28
   cases at once; the page types six.)
8. **Gate the write behind a second person.** Copy the server id from the row on the Servers tab. Make a copy of
   `assets/workflow/file-issue.workflow.yaml` with `__SERVER_ID__` set to it, `__TOOL__` to `issue_write`, `__LEVEL__` to `write` and
   `__APPROVAL__` to `true`. **Agent resources → Workflows**, **Import**: name it `mcp-file-issue`, **Choose file**, **Validate
   dependencies**: **Preflight passed** and the dependency `<server id>/issue_write` resolved. **Import as new** opens the editor.
   **Run Monitor**, paste a JSON object (`{"repo": "acme/release-notes-bot", "title": "...", "labels": ["bug"]}`) as the run input,
   **Run**: the badge reads **Awaiting approval**, current node `call_tool`. **Approve** yourself: *the account that triggered this
   run cannot approve it; a second person must decide*. In a second browser session sign in as `lab05-approver`, pick `mcp-ops`, open
   the workflow, **Run Monitor**, choose the run under **Recent runs**, **Approve**. In your own session **Resume** becomes enabled
   (Approve recorded the decision; it did not advance the run). **Resume**: **completed**, and the issue is on the server.
9. **Version the workflow and meet the policy gate.** In the editor press **Publish**, then **Publish** in the drawer: the header reads
   `v1 · published` and the editor is read-only. Press **Restore as draft**: `v2 · draft`, and the *Published.* and *Run ... completed.*
   chips of v1 are gone. Close the Run Monitor with its **✕** (it covers the canvas), click the `call_tool` node in the **Outline**: the
   node says that a binding's approval, retries and timeout are set under **Tool bindings** in the workflow settings. Click an empty spot
   on the canvas to open the settings, find **Tool bindings** and untick **Approval** on `issue_write`, then **Save**. Open **Run Monitor**,
   paste `{"repo": "acme/release-notes-bot", "title": "This must never be filed"}` and **Run**. Expected: the run is refused with
   *tool policy requires approval but the workflow binding does not*, and the server's ledger still shows two `issue_write` calls: loosening
   a binding in a new version does not get around the tool's policy.
10. **Servers CALIBER will not run.** **Add Server** again with command `/bin/sh` and arguments `-c echo pwned`: it registers, the row
   shows **Runtime blocked**, and **Test** answers *stdio command '/bin/sh' is not in CALIBER_MCP_STDIO_COMMAND_ALLOWLIST*.
11. **Read the audit log and the traces.** **Operate → Audit Log**: filter the action to `update_mcp_tool_policy`, then
    `calibrate_mcp_tool`. **Operate → Observability**: the `mcp.repo-tracker.search_repositories` and `...issue_write` traces; there is
    none for `create_release`.

As `lab05-viewer`, open **MCP Servers → Playground**, pick the server and **Test Connection**: **Connection Failed** with the missing
scope in the message.

The whole path is `caliber/caliber-ui/e2e/cookbooks/labs/05-mcp-governance.spec.ts`; run it against a running stack:

```bash
cd caliber/caliber-ui
CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/05-mcp-governance.spec.ts
```

## Verify it worked

| Check | Where | Expected |
| --- | --- | --- |
| Discovery | `POST /mcp-servers/{id}/test-connection` | `success: true`, 6 tools, each with an `input_schema`; `issue_write` requires `repo` and `title` |
| Deny by default | `GET /mcp-servers/{id}/tools` before classifying | every tool `classified: false`, `policy.allowed: false` |
| Classification | the same, after | the policies equal `tool-classification.jsonl`; `issue_write` allowed with `requires_approval: true`; `create_release` `allowed: false` |
| Write without approval | `PATCH .../policy` `{allowed: true, side_effect_level: "write"}` | `400` ("set requires_approval=true for side_effect_level 'write'...") |
| Read call | `POST .../invoke-tool` `search_repositories` | `success: true`, `result.total_count: 2`; the ledger has one `search_repositories` line |
| Refusals | `invoke-tool` for `create_release`, `drop_database`, and a 4th `scan_dependencies` in a minute | `success: false`, `duration_ms: 0`: *is blocked by policy*, *not found on this server*, *exceeded its process-local limit of 3/minute*; no ledger line, no trace |
| Tool failures | `invoke-tool` with `stars:>>not-a-number`, an empty query, a missing `number`, an unknown repo | `success: false` with the server's text (*stars: expects a number*, *query must not be empty*, *Field required*, *was not found*); a trace with status `ERROR` |
| Timeout | `scan_dependencies` with `delay_seconds: 14` | `success: false`, *timed out*, after about 10 seconds; the call still spent one of the three calls a minute |
| Calibration | `POST .../calibrate` | `search_repositories` 16/16, `list_issues` 8/8, `get_issue` 4/4, `pass_rate: 1.0`; a wrong expectation: `0.9375` |
| Approval-class tool | `calibrate` on `issue_write`, and no cases on `scan_dependencies` | `409` (no approval checkpoint) and `400` (no saved cases) |
| Workflow gate | `GET /workflow-runs/{id}` | `status: waiting_approval`, `current_node_id: call_tool`; the ledger's `issue_write` count does not move until **Resume** |
| Blocked tool in a workflow | `POST /workflow-runs` | `400 MCP runtime preflight failed: tool binding 'create_release': ... blocked by policy` |
| History | `GET /mcp-servers/{id}/history` | `create_mcp_server`, six `update_mcp_tool_policy`, `update_mcp_tool_test_cases`, `calibrate_mcp_tool`; no invocations |
| Audit | `GET /audit-log` | the same actions, with `lab05-operator` named for the calibrations |
| Traces | `GET /observability/traces` | `mcp.<server>.<tool>` for every call that passed policy; none for refusals |

**The ledger is the server's own record.** CALIBER's refusal tells you CALIBER did not call the tool; the ledger
(`<state dir>/lab05-sdk-ledger.jsonl`, one JSON line per call the server executed) tells you the server never ran it. That is the
difference between "the gateway said no" and "nothing happened".

### The trust boundary, honestly

- **A stdio server is a child process, not a sandbox.** CALIBER runs it with a command allowlist, a sanitized environment, a fresh
  working directory and timeouts, and no more: a script you allowlist can still read files and open connections. Readiness says so
  (*local stdio containment does not block filesystem or network access*). For production, the architecture notes say a managed sidecar or a
  non-loopback HTTPS endpoint is the boundary CALIBER accepts and that a production-class alias refuses local stdio (not exercised here).
- **Classification is yours.** CALIBER cannot tell that `issue_write` writes; you say so, and it then insists a write carry approval.
  A tool classified `read` that actually writes is invisible to every control here.
- **Policy lives on the server record, not on the remote tool.** Blocking `create_release` in CALIBER does not stop anyone calling the
  MCP server some other way.
- **The rate limit is per tool, per minute, in one process.** It is not a quota across replicas, and a restart clears it.
- **Roles apply twice.** Registering, testing, classifying and invoking need the platform **admin** scope; saving cases and calibrating
  need **operator**; inside a workspace the role floors are editor to register and classify, reviewer to test, invoke and calibrate. A
  weak scope gets `403`, and a person outside the workspace gets `404`.

## Common errors and troubleshooting

| You see | Why | Fix |
| --- | --- | --- |
| `404 feature_disabled` on `/mcp-servers`, no **MCP Servers** page | the integrations flag is off (the default) | start with `--flags integrations` (`CALIBER_FEATURE_INTEGRATIONS=true`) and restart |
| `403 missing required scope; expected one of ['caliber.admin']` on register, test, classify or invoke | the account is not an admin | use `admin`; `CALIBER_ADMIN_USERS` names the admins |
| `403 ... ['caliber.operator']` on save cases or calibrate | the account has no operator scope | add it to `CALIBER_OPERATOR_USERS` and restart |
| `409 MCP server 'repo-tracker' already registered` | the name is taken (names are global) | re-run with `LAB05_SUFFIX=_2`, or delete the server |
| `409 a project named 'mcp-ops' already exists` | an earlier run's workspace (archived workspaces keep their name) | `LAB05_SUFFIX=_2` |
| `success: false`, *has no explicit allow, side-effect, and approval classification* | discovery is not authorization | classify the tool (**Save Policy**, or `PATCH .../policy` with all three fields) |
| `400 set requires_approval=true for side_effect_level 'write', or lower the side-effect level to 'read'` | an allowed write or external-action tool needs approval | send `requires_approval: true` (or block it with `allowed: false`) |
| *is blocked by policy* | `allowed: false` | expected; flip **Allow tool** and save to open it |
| *exceeded its process-local limit of 3/minute* | the per-tool cap is spent for this minute | wait a minute, or raise `rate_limit_per_minute` |
| `Runtime blocked`, *stdio command '/bin/sh' is not in CALIBER_MCP_STDIO_COMMAND_ALLOWLIST* | the command is not allowlisted | use `${PYTHON}` with an allowlisted `-m` module or script, or add the command deliberately |
| *CALIBER Python MCP commands must use an allowlisted '-m' module or an absolute path in CALIBER_MCP_STDIO_PYTHON_SCRIPT_ALLOWLIST* | the script (or `python -c`) is not allowlisted | set `CALIBER_MCP_STDIO_PYTHON_SCRIPT_ALLOWLIST` to its absolute path and restart; the path must match after symlinks are resolved |
| *remote MCP host '127.0.0.1' resolves into the blocked loopback range* | the egress policy blocks loopback even though the MCP host allowlist names it | run the server over stdio, or put it behind a non-loopback HTTPS host. Loosening `CALIBER_EGRESS_*` also loosens SSRF protection for workflow HTTP nodes, and was not tried here |
| *MCP tools/call failed: ... timed out* | the tool outran the Playground's 10 second limit | a real server should answer sooner; the limit is fixed |
| *MCP tools/list failed: MCP session failed: Connection closed* (the server's `status` becomes `error`) | the child started and exited, for example on a bad argument | run the command by hand to see why; a successful **Test** returns the status to `active`, and while it is `error` every invoke is refused (*MCP server status is 'error', not 'active'*) |
| `409 tool 'issue_write' requires approval, but MCP calibration has no approval checkpoint` | calibration cannot ask a person | calibrate the read tools; use a governed workflow run for the write |
| `400 ... has no saved test cases to calibrate against` | nothing saved for that tool | **Save cases** first |
| the workflow run completes without pausing | runtime approvals are off | `CALIBER_WORKFLOW_RUN_RUNTIME_APPROVALS_ENABLED=true` (with the queue) and restart |
| `403 the account that triggered this run cannot approve it; a second person must decide` | you started the run | approve as another account |
| `400 MCP runtime preflight failed: ... blocked by policy` on **Run** | the workflow binds a tool that is blocked, unclassified or unknown | classify and allow the tool, then run |
| `404 workflow run not found` for the approver | the workflow is in a workspace they are not a member of | add them as a member and pick that workspace |

## Cleanup and reset

Remove what the script created (the servers are deleted, the workflows deleted, the workspace archived; accounts, traces and audit entries
stay):

```bash
(cd sdk/caliber-sdk && ../../caliber/.venv/bin/python -m examples.cookbooks.cookbook_05_governed_tool_connectivity --cleanup)
```

An archived workspace keeps its name, so a re-run on the same stack needs `LAB05_SUFFIX` (above). What the UI path created (`repo-tracker_<id>`,
`rogue-shell_<id>`, `mcp-file-issue_<id>` and the workspace) is removed from the UI (the trash icon on a server row, the workflow card's delete)
or goes with the stack. For a clean slate, discard the whole disposable stack:

```bash
scripts/cookbook-env.sh down lab05 && rm -rf .tmp/cookbook-env/lab05
```
