# Governed Tool Connectivity (MCP)

> **Prerequisite flags:** `CALIBER_FEATURE_COOKBOOKS=true` opens the Cookbooks gallery and `CALIBER_FEATURE_INTEGRATIONS=true` for the **MCP Servers** page and the `/mcp-servers` API (both off by default).

## Demo objective

A governed MCP integration with one trusted read-only tool and explicit policy
boundaries that are enforced on invoke.

## Feasibility & substitutions

Read [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ Quick-connect catalog (8 templates), test-connection + discovery, Playground
  invoke, per-tool policy, and the tool-calibration panel are all real.
- ✅ **Policy field mapping**: the scenario's `blocked` = `allowed:false`,
  `approval_required` = `requires_approval:true`, and `side_effect_level` =
  `read`/`write`.
- ⚠️ **What's enforced where**: on the **direct invoke / Playground** path, only
  `allowed:false` (block) is enforced — the blocked tool refuses with a
  structured error. `requires_approval:true` is a **workflow-time** gate (it
  routes the tool through a `human_approval` node when used inside a workflow),
  **not** a direct-invoke refusal. So this scenario proves enforcement with a
  **block**; the approval path is demoed in SCN-03/SCN-07.
- ⚠️ **Classification comes first.** A discovered tool must carry an explicit
  policy (`allowed`, `side_effect_level`, `requires_approval`) before it can be
  invoked, tested, or calibrated; an unclassified tool is refused with "has no
  explicit allow, side-effect, and approval classification" (the UI shows an
  **Unclassified** badge and **Classify Tool Before Invoking**). The catalog
  templates seed these (GitHub: `search_repositories` read, `issue_write` write
  with `requires_approval:true`); any `PATCH …/policy` saves the full merged
  policy. A `write`/`external_action` tool that stays allowed must keep
  `requires_approval:true` (otherwise `400`).
- ⚠️ **The Playground cannot show the blocked refusal.** Once a tool is blocked,
  its **Invoke Tool** button is disabled ("Tool Blocked by Policy") and the
  calibration/test panels are replaced by a notice, so the refusal payload comes
  from the API (`POST /mcp-servers/{id}/invoke-tool`). Invoke and policy
  changes need the admin scope.
- 🟢 Easiest catalog choice for a safe demo: **GitHub** (has both read tools like
  `search_repositories` and write tools like `issue_write`) or **PostgreSQL**
  (read-only queries).

## Prerequisites & seed

- The GitHub remote host (`api.githubcopilot.com`) added to
  `CALIBER_MCP_REMOTE_HOST_ALLOWLIST` on the CALIBER server.
- A GitHub personal-access token supplied through the catalog template's
  `GITHUB_PERSONAL_ACCESS_TOKEN` environment reference. At least one read-only
  tool in discovery.

## Recipe (UI-first, with API fallbacks)

1. **Quick-connect.** With `CALIBER_FEATURE_INTEGRATIONS=true` set, `MCP Servers`, click the **GitHub** catalog tile (test id
   `catalog-github`). In the dialog, name the server and supply
   the token env var; **Register**. The GitHub tile uses the official remote
   Streamable HTTP endpoint (`https://api.githubcopilot.com/mcp/`), not the
   retired npm-based community preset.
   - API: `POST /mcp-servers {name, transport:"streamable-http", uri, auth_type:"token", auth_config:{token_env_var}, env:{GITHUB_PERSONAL_ACCESS_TOKEN:"${GITHUB_PERSONAL_ACCESS_TOKEN}"}}`
     (optional `discovered_tools` / `tool_policies` seed the tool list and the
     classifications; the body is `extra="forbid"`).
2. **Test connection + discovery.** On the server row, click **Test** → expect
   "Connected · N tools". Inspect discovered tools and their input/output schemas.
   - API: `POST /mcp-servers/{id}/test-connection`, `GET /mcp-servers/{id}/tools`.
3. **Invoke a read-only tool.** `MCP Servers → Playground` tab → select the server →
   pick a read tool (e.g. `search_repositories`) → invoke with a golden payload →
   confirm a successful result + `duration_ms`.
   - API: `POST /mcp-servers/{id}/invoke-tool {tool_name, arguments}`.
4. **Apply a policy overlay.** `Playground` → select a write tool (e.g.
   `issue_write`) → in its **Tool Policy** card clear **Allow tool** and click
   **Save Policy** (`allowed:false`, block). (You can also set
   `requires_approval:true`, but that only takes effect at workflow time — see
   the feasibility note.)
   - API: `PATCH /mcp-servers/{id}/tools/{tool}/policy {allowed:false}`.
5. **Re-invoke the governed tool.** In the Playground the blocked tool's **Invoke
   Tool** button is now disabled ("Tool Blocked by Policy") with a red **Blocked**
   badge, which is the UI evidence. Invoke it through the API to capture the
   structured **refusal** (`success:false`, `error:"MCP tool 'issue_write' is
   blocked by policy"`, `duration_ms:0`), not an execution. This is your
   enforcement record.
6. **Calibrate the read tool.** In the tool's calibration panel save a couple of
   cases + assertions and run them.
   - API: `PUT /mcp-servers/{id}/tools/{tool}/test-cases`, then
     `POST /mcp-servers/{id}/tools/{tool}/calibrate`.
7. **Collect evidence.** `MCP Servers → Playground → History` → confirm the
   History panel shows the successful read (`success:true`), the red **Blocked**
   badge and disabled Invoke button on the governed tool, and the saved
   calibration verdicts. History is browser state only (in memory, gone on reload
   or **Clear**), so copy what you need. Each *executed* invoke also emits a
   `TOOL` span named `mcp.<server>.<tool>` (input, output, latency) that appears
   in `Observability`; test-connection, discovery and a blocked refusal emit none,
   so the blocked row exists only in your API response.

## Demo evidence to capture

- Connection status + discovered tool count.
- One successful read-only invocation record.
- One blocked-tool enforcement record (the `allowed:false` refusal payload from the API).
- The read tool's calibration pass rate.

## Done when / gate

- `connection_success_rate = 1.0`; discovery schemas visible.
- `blocked_tool_enforcement = 1.0` — the blocked tool cannot execute.
- At least one read tool has a saved calibration run.
- Evidence captured: the `success:true` read row in the **MCP Servers →
  Playground → History** panel (in-memory, this session) and its `mcp.<server>.<tool>`
  trace in `Observability`, the `success:false` blocked-by-policy API response, the
  red **Blocked** badge with the disabled Invoke button, and the saved
  calibration verdicts.
