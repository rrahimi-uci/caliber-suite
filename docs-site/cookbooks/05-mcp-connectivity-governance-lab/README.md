# Governed Tool Connectivity (MCP)

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
- 🟢 Easiest catalog choice for a safe demo: **GitHub** (has both read tools like
  `search_repositories` and write tools like `create_issue`) or **PostgreSQL**
  (read-only queries).

## Prerequisites & seed

- One MCP server template + credentials (e.g. a GitHub token in the env var the
  template expects). At least one read-only tool in discovery.

## Recipe (UI-first, with API fallbacks)

1. **Quick-connect.** `Library → MCP Servers`, click a catalog tile
   (`data-testid="catalog-github"`). In the dialog, name the server and supply
   the token env var; **Register**.
   - API: `POST /mcp-servers {name, transport, command, args, env}`.
2. **Test connection + discovery.** On the server row, click **Test** → expect
   "Connected · N tools". Inspect discovered tools and their input/output schemas.
   - API: `POST /mcp-servers/{id}/test-connection`, `GET /mcp-servers/{id}/tools`.
3. **Invoke a read-only tool.** `MCP Servers → Playground` → select the server →
   pick a read tool (e.g. `search_repositories`) → invoke with a golden payload →
   confirm a successful result + `duration_ms`.
   - API: `POST /mcp-servers/{id}/invoke-tool {tool_name, arguments}`.
4. **Apply a policy overlay.** Open server detail → for a write tool (e.g.
   `create_issue`) set `allowed:false` (block). (You can also set
   `requires_approval:true`, but that only takes effect at workflow time — see
   the feasibility note.)
   - API: `PATCH /mcp-servers/{id}/tools/{tool}/policy {allowed:false}`.
5. **Re-invoke the governed tool.** From the Playground, invoke the blocked tool
   → expect a structured **refusal** (`success:false`, policy error), not an
   execution. This is your enforcement record.
6. **Calibrate the read tool.** In the tool's calibration panel save a couple of
   cases + assertions and run them.
   - API: `PUT /mcp-servers/{id}/tools/{tool}/test-cases`, then
     `POST /mcp-servers/{id}/tools/{tool}/calibrate`.
7. **Collect evidence.** `MCP Servers → Playground → History` → confirm the
   invocation History panel shows the successful read (`success:true`) and the
   blocked write (`success:false`, blocked-by-policy), the red **Blocked** badge
   on the governed tool, and the saved calibration verdicts. MCP invokes emit
   **no** MLflow spans (the gateway + invoke route use `perf_counter` only), so
   nothing appears in `Observe → Observability` for this lab — the Playground
   History panel is the evidence surface.

## Demo evidence to capture

- Connection status + discovered tool count.
- One successful read-only invocation record.
- One blocked-tool enforcement record (the `allowed:false` refusal payload).
- The read tool's calibration pass rate.

## Done when / gate

- `connection_success_rate = 1.0`; discovery schemas visible.
- `blocked_tool_enforcement = 1.0` — the blocked tool cannot execute.
- At least one read tool has a saved calibration run.
- Evidence captured in the **MCP Servers → Playground → History** panel: the
  `success:true` read row, the `success:false` blocked-by-policy write row, the
  red **Blocked** badge, and the saved calibration verdicts. (MCP paths emit no
  traces, so there is nothing to verify in `Observe → Observability`.)
