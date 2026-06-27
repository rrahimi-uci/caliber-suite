# SCN-05 assets — create these

Concrete, copy-pasteable artifacts for [the recipe](../README.md). There are
**no prompts, skills, or tools to author** for this scenario — you govern the
tools that the MCP server **discovers**. The files here are policy PATCH bodies
and calibration/enforcement datasets you paste in once the server is connected.

Build order (all on the **GitHub** quick-connect template, `catalog-github`):

| # | Step | File | Do this |
| --- | --- | --- | --- |
| 1 | Register `catalog-github` | — (catalog tile) | `Library → MCP Servers`, click `data-testid="catalog-github"`. It seeds command `npx`, args `-y @modelcontextprotocol/server-github`, transport `stdio`, token env var `GITHUB_TOKEN`. Name it, supply the token, **Register**. API: `POST /mcp-servers {name, transport, command, args, env}` |
| 2 | Test connection + discovery | — | On the server row click **Test** → expect "Connected · N tools". Inspect schemas. API: `POST /mcp-servers/{id}/test-connection`, then `GET /mcp-servers/{id}/tools` (tools + effective policy) |
| 3 | Invoke the read tool | [`dataset/mcp-tool-test-cases.jsonl`](dataset/mcp-tool-test-cases.jsonl) (row `golden`) | Playground → server → `search_repositories` → invoke the golden `input` → expect `success:true` + `duration_ms`. API: `POST /mcp-servers/{id}/invoke-tool {tool_name, arguments}` |
| 4 | Set policy on the write tool | [`policy/github-create-issue.block.policy.json`](policy/github-create-issue.block.policy.json) (block — use this for the read-only explorer) **or** [`policy/github-create-issue.approval.policy.json`](policy/github-create-issue.approval.policy.json) (approval — workflow-time gate only) | Server detail → row `create_issue` → set the policy. API: `PATCH /mcp-servers/{id}/tools/create_issue/policy` with the JSON body (drop the `_comment` key — it is documentation only) |
| 5 | Re-invoke the governed tool | [`dataset/policy-enforcement-checks.jsonl`](dataset/policy-enforcement-checks.jsonl) | Playground → `create_issue` → invoke → **expect a refusal**, not an execution: `success:false`, `result:null`, `duration_ms:0`, `error:"Tool 'create_issue' is blocked by policy"`. This is your enforcement record, and it only holds for the **block** (`allowed:false`). API: same `invoke-tool` endpoint |
| 6 | Calibrate the read tool | [`dataset/mcp-tool-test-cases.jsonl`](dataset/mcp-tool-test-cases.jsonl) | Save the cases, then run. API: `PUT /mcp-servers/{id}/tools/search_repositories/test-cases {test_cases:[{name,input,assertion}]}` → `POST /mcp-servers/{id}/tools/search_repositories/calibrate` → reports `{pass_rate,total,passed,cases}` inline |

**`blocked_tool_enforcement = 1.0`** is proved by step 5: a tool with
`allowed:false` returns the refusal payload above instead of invoking.
**`PolicyCorrectness` is a deterministic enforcement assertion, not an LLM
judge** — it is the byte-level fact that the blocked invoke returned
`success:false` / `error:"…blocked by policy"`. No model is called to grade it.
Tool calibration here is likewise **inline and deterministic** (it scores each
saved case against its assertion and returns a `pass_rate` synchronously).

> Enforcement boundary: on the **direct invoke** path the gateway only honors
> `allowed:false`. `requires_approval:true` is recorded but enforced at
> **workflow time** (the human-approval node), not in the Playground — so for a
> read-only explorer the hard block is the control that proves
> `blocked_tool_enforcement = 1.0`. See `dataset/policy-enforcement-checks.jsonl`
> row `PE02`.

## Conventions used in these files

- **Policy files** (`policy/*.policy.json`): the literal body of
  `PATCH /mcp-servers/{id}/tools/{tool}/policy`. Only `allowed`,
  `side_effect_level` (`read`|`write`), and `requires_approval` are real fields
  (all optional; a PATCH merges onto the effective policy). Any `_comment` key
  is documentation — strip it before sending.
- **Calibration cases** (`dataset/mcp-tool-test-cases.jsonl`): one case per line
  in the `CalibrationCase` shape `{name, input, assertion}`. `input` is the tool
  `arguments`. `assertion.type` is one of the **three real** kinds —
  `no_error` (default; passes when the invoke returned no error),
  `output_contains` (`{type, value}`; substring of the stringified output), or
  `equals` (`{type, value}`). The `id`/`tags`/`tool_name` keys are authoring
  metadata; when posting, send only `{name, input, assertion}` inside
  `{test_cases:[...]}`.
- **Enforcement checks** (`dataset/policy-enforcement-checks.jsonl`): one
  deterministic expectation per line — the `policy` to apply, the `invoke_input`
  to send, and the `expect`ed outcome (`invoked` + `error_kind`). These describe
  what `invoke-tool` must return after the policy is set; they are the
  human-readable form of the `PolicyCorrectness` check, not a request body.
