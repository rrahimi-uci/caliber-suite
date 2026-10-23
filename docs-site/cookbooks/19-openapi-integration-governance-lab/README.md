# Governed OpenAPI Tool: curate first, then publish

> **Prerequisite flags:** `CALIBER_FEATURE_INTEGRATIONS=true` (the **OpenAPI Integrations** page and the
> `/openapi-integrations` API, shared with cookbook 05's MCP governance). A preview area, off by default.
> No model is involved anywhere in this lab, so nothing in it is simulated. Every step was run against a
> real CALIBER server (a fresh stack from `scripts/cookbook-env.sh up lab19 --flags integrations`) and a
> real upstream HTTP server, through the API.

## What this lab demonstrates

An OpenAPI spec describes somebody else's API. This lab imports one, finds out what it offers, and
decides what CALIBER may call **before** anything runs -- the same shape as cookbook 05's MCP
governance, for a REST API instead of an MCP server.

The upstream is the lab's own: [`assets/server/ticket_api.py`](assets/server/ticket_api.py), a small
Ticket API that serves its own OpenAPI document and three operations (`listTickets`, `getTicket`,
`createTicket`) behind a bearer token. CALIBER calls it over real loopback HTTP -- import, preview and
reimport are genuine outbound calls, guarded by the same egress policy a workflow `api_request` node
uses.

You create an integration, import the spec, and see CALIBER normalize three operations and auto-wire
one dependency from an OpenAPI `link` the spec declares. You curate a read-only **tool pack** (two
operations bound into one callable) and a write draft, meet the preview gate that refuses to fire an
unapproved write, and watch an explicit, deliberate opt-in run it once -- the OpenAPI analogue of
cookbook 05's break-glass admin Playground. You publish both drafts into CALIBER's ordinary tool
registry, prove the role floors (a reviewer can preview but not publish), and bind the write tool in a
workflow where a second person's approval holds it. Finally, you change the upstream's spec and
reimport it: the diff catches one breaking change and stays silent about another, real one.

**You are done when:**

- importing the spec classifies three operations (two read, one write) and auto-wires exactly one
  dependency, from `createTicket` to `getTicket`, that cannot be reviewed (`409`);
- a same-tag advisory dependency can be confirmed;
- a tool pack binds `listTickets` and `getTicket` into one draft, selected at call time by an
  `operation` key;
- the write draft defaults to `requires_approval` and is refused at preview (`409`) until
  `allow_in_preview` is explicitly set; the upstream's own request log proves the refusal never
  reached it;
- publishing needs a project role of owner or editor plus the platform operator scope; a reviewer can
  preview a draft they can never publish (`403`);
- the published write tool, bound in a workflow, pauses at `waiting_approval`; the initiator cannot
  approve their own run, a viewer cannot approve, an editor cannot approve (only the reviewer role can);
  after a second person approves and the run is resumed, it completes and the upstream's log shows
  exactly one more write;
- a host CALIBER was never told to trust (a cloud-metadata-range address) is refused by the egress
  policy even though it was never registered as an integration;
- reimporting the upstream's changed spec reports one breaking change (an operation marked
  `deprecated`) and **zero** changes for an operation whose request body quietly gained a new required
  field -- CALIBER's diff does not track which fields inside a body are required, only whether a body
  is required at all;
- the audit log carries every curation action, scoped correctly between the integration and its
  drafts.

## Prerequisites and setup

1. A running CALIBER built per [`docs/start/alpha-install.md`](../../../docs/start/alpha-install.md)
   with the **integrations** flag on.
2. The lab's upstream running, and its host allowed through CALIBER's egress policy:

   ```bash
   python3 docs-site/cookbooks/19-openapi-integration-governance-lab/assets/server/ticket_api.py 8781 &
   ```

   `OPENAPI_TICKET_TOKEN` must be set (and equal) in **both** the upstream's environment and CALIBER's:
   it is the bearer token the upstream expects and the literal value CALIBER's `env://OPENAPI_TICKET_TOKEN`
   secret reference resolves to. `TICKET_API_LEDGER` says where the upstream appends its own request log
   (an absolute path, no spaces) -- the lab's proof that a refused call never reached it.
3. Three accounts that hold the right platform scopes. Scopes come from server settings, not from the
   account: `CALIBER_OPERATOR_USERS` lists both `lab19-operator` and `lab19-reviewer` (publishing and
   previewing both need the platform operator-or-admin scope); `CALIBER_APPROVER_USERS` lists
   `lab19-reviewer` alone (approving a workflow run needs the platform approver scope *and* the
   project's reviewer role -- one person holding both, here). `lab19-viewer` has no scope on purpose.
4. From a checkout, one command starts a disposable stack with all of that (this is what the lab was
   verified on):

   ```bash
   export CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1
   export OPENAPI_TICKET_TOKEN=lab19-ticket-token
   export TICKET_API_LEDGER="$PWD/.tmp/cookbook-env/lab19/lab19-ticket-ledger.jsonl"
   python3 docs-site/cookbooks/19-openapi-integration-governance-lab/assets/server/ticket_api.py 8781 &
   CALIBER_OPERATOR_USERS=admin,lab19-operator,lab19-reviewer CALIBER_APPROVER_USERS=admin,lab19-reviewer \
   scripts/cookbook-env.sh up lab19 --flags integrations
   source .tmp/cookbook-env/lab19/env.sh
   curl -s $CALIBER_API/readiness | jq -c '.data|{ready,simulated}'
   ```

   Expected: `{"ready":true,"simulated":["eval","llm"]}`. The simulated providers play no part in this
   lab.
5. `CALIBER_APPROVAL_ALLOW_SELF_APPROVAL` is **not** set, which is what makes the second-person rule
   bite. Leave it unset.

## Dataset and sample files

All under [`assets/`](assets/) (the folder's [README](assets/README.md) has the operation table):

| File | What it is | Why |
| --- | --- | --- |
| [`server/ticket_api.py`](assets/server/ticket_api.py) | the upstream: serves its own spec and three ticket operations, a bearer token, a request log | something real to import and call |
| [`dataset/ticket-api-v1.yaml`](assets/dataset/ticket-api-v1.yaml) | the spec version the upstream serves at first | what you import |
| [`dataset/ticket-api-v2.yaml`](assets/dataset/ticket-api-v2.yaml) | the spec version after `POST /admin/bump-spec` | what you reimport and diff against v1 |
| [`workflow/file-ticket.workflow.yaml`](assets/workflow/file-ticket.workflow.yaml) | a one-tool workflow binding the published write tool | the approval gate |

The two spec files are exact copies of the strings `ticket_api.py` embeds and serves; a backend test
(`caliber/tests/test_cookbook_19_openapi_integration_governance_lab.py`) checks they stay identical and
feeds both through CALIBER's real classification, dependency-detection and diff code, so the dataset
cannot drift from what the lab's script actually observes.

## Configuration and environment

| Variable | Value used | Why |
| --- | --- | --- |
| `CALIBER_FEATURE_INTEGRATIONS` | `true` (via `--flags integrations`) | opens OpenAPI Integrations and `/openapi-integrations`; without it they answer `404 feature_disabled` |
| `CALIBER_EGRESS_ALLOWED_HOSTS` | `127.0.0.1` | lets CALIBER call the upstream; without it import-by-URL, preview and reimport all fail with an egress refusal |
| `OPENAPI_TICKET_TOKEN` | the same literal value in both the upstream's environment and CALIBER's | the upstream's expected bearer token; CALIBER resolves it via the `env://OPENAPI_TICKET_TOKEN` secret reference set on the auth binding |
| `TICKET_API_LEDGER` | an absolute path, set before starting the upstream | where the upstream appends its own request log |
| `CALIBER_OPERATOR_USERS` | `admin,lab19-operator,lab19-reviewer` | publishing and previewing both need this scope (or admin) |
| `CALIBER_APPROVER_USERS` | `admin,lab19-reviewer` | approving a workflow run needs this scope, in addition to the reviewer project role |
| `CALIBER_WORKFLOW_RUN_QUEUE_ENABLED`, `..._RUNTIME_APPROVALS_ENABLED` | `true` (the environment script sets them) | a run is queued and honours the gate |
| `CALIBER_BASE_URL`, `CALIBER_TOKEN` | from `env.sh` and the next step | what the script reads |
| `OPENAPI_TICKET_PORT` | `8781` (the script's default) | where the script finds the upstream |
| `LAB19_SUFFIX` | unset | set it (`_2`) to re-run the script on a stack that already holds an earlier run |

The people: `admin` owns the workspace and creates the integration; `lab19-operator` (platform
operator; **editor**) curates and publishes but cannot approve; `lab19-reviewer` (platform operator
**and** approver; **reviewer**) can preview and approve but cannot publish; `lab19-viewer` (no
platform scope; **viewer**) only reads. The script creates the three accounts with lab-only passwords.

## Run it through the API

Everything below is in one script that exits `0` only when every outcome above was observed. Mint a
token, then run it from the repo root:

```bash
source .tmp/cookbook-env/lab19/env.sh
curl -s -c /tmp/lab19.cookies -X POST $CALIBER_API/auth/login -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"admin"}' > /dev/null
export CALIBER_TOKEN=$(curl -s -b /tmp/lab19.cookies -X POST $CALIBER_API/auth/tokens \
  -H 'content-type: application/json' -d '{"name":"lab19"}' | jq -r .data.token)

(cd sdk/caliber-sdk && ../../caliber/.venv/bin/python -m examples.cookbooks.cookbook_19_openapi_integration_governance)
```

Real output, trimmed (ids vary):

```text
  account lab19-operator already existed; password reset to the lab value
  account lab19-reviewer already existed; password reset to the lab value
  account lab19-viewer already existed; password reset to the lab value
== 1. workspace and people
  workspace PRJ-f709f50d: admin owns it, operator edits, reviewer reviews, viewer reads
== 2. create the integration
  ok  a viewer cannot create an integration -> HTTP 403
  ok  created OAI-1d419b7d (draft)
  ok  creating the same name twice is refused -> HTTP 409
== 3. import the spec from the upstream's own URL
  ok  the upstream's spec URL is reachable and allowed
  ok  imported OAIV-358a7e95 from the URL
  ok  three operations classified: two read, one write
== 4. the egress guard refuses a host CALIBER was not told to trust
  ok  a cloud-metadata-range host is refused: outbound request to '169.254.169.254' is blocked: it points at the cloud instance-metadata endpoint. Add the host to CALIBER_EGRESS_ALLOWED_HOSTS if this is intentional.
== 5. dependencies: one auto-wired, one advisory
  ok  1 auto-wired dependency(ies) from the create->get path
  ok  an auto-wired dependency cannot be reviewed -> HTTP 409
  ok  the advisory (same-tag) dependency can be confirmed
== 6. curate: a read-only tool pack and a governed write draft
  ok  a viewer cannot generate tool drafts -> HTTP 403
  ok  OATD-a6fe6e0a: a tool pack binding both read operations
  ok  OATD-c91c1030: the write draft defaults to requires_approval
== 7. preview: the read pack runs for real; the write draft is refused
  ok  a reviewer can preview the read pack's listTickets operation: zero tickets exist yet
  ok  the upstream's own log shows one real GET
  ok  the same pack also serves getTicket, selected by operation key: 404 for an unknown id
  ok  the write draft cannot be previewed until allow_in_preview is set -> HTTP 409
  ok  the upstream never saw the refused preview
== 8. break-glass: opt the write draft into preview, then re-gate it
  ok  direct preview ran: TCK-1 (a deliberate, explicit opt-in -- the OpenAPI analogue of cookbook 05's break-glass Playground)
  ok  the upstream's log shows the one real write
  ok  turning allow_in_preview back off re-gates it -> HTTP 409
== 9. publish both drafts into the tool registry
  ok  a reviewer (resource.execute, not resource.publish) cannot publish -> HTTP 403
  ok  both drafts are now ordinary registry tools; the write one still requires approval
  ok  GET /tools/{tool_id} shows them in the standard registry
== 10. the approval gate in a workflow
  ok  the run paused at the call_tool node
  ok  nothing written while the run waited
  ok  the initiator cannot approve their own run -> HTTP 403
  ok  the refusal says a second person must decide
  ok  a viewer cannot approve -> HTTP 403
  ok  an editor (resource.write.runtime, not resource.approve) cannot approve either -> HTTP 403
  ok  approving alone does not run the tool
  ok  after Resume the run completed
  ok  the workflow's write executed exactly once
== 11. reimport and diff against a changed spec
  ok  reimported OAIV-f1d6a0ee: changed 2 (1 breaking), 1 unchanged
  ok  the one breaking change the diff catches: GET /tickets/{ticket_id} operation was deprecated
  ok  what the diff does NOT catch: createTicket's body gaining a required 'priority' field is not a tracked field, so the diff reports no change for it at all
== 12. credential binding validation
  ok  the bearer secret_ref resolves (OPENAPI_TICKET_TOKEN is set)
  ok  a typo'd secret reference does not resolve
== 13. audit trail
  ok  audit log has create_openapi_integration on the integration
  ok  audit log has import_openapi_integration_version on the integration
  ok  audit log has reimport_openapi_integration_version on the integration
  ok  audit log has generate_openapi_tool_drafts on the integration
  ok  audit log has update_openapi_tool_draft on a draft
  ok  audit log has preview_openapi_tool_draft on a draft
  ok  audit log has publish_openapi_tool_draft on a draft
== 14. the gallery starter
  ok  the gallery lists 10 step(s) for this lab
  ok  the starter is a generic single-agent draft with no tools: it does not use the OpenAPI-derived tools
== done
```

What each stage does, by hand (`$H` is `-H "Authorization: Bearer $CALIBER_TOKEN"` plus
`-H "X-CALIBER-Project: $PROJECT"` once you have a workspace; `$I` is the integration id):

```bash
# create, then import by URL: CALIBER fetches the spec itself, through its guarded egress path.
curl -s $H -X POST $CALIBER_API/openapi-integrations -H 'content-type: application/json' \
  -d '{"name":"Ticketing"}'                                                              # 201, status draft
curl -s $H -X POST $CALIBER_API/openapi-integrations/$I/import -H 'content-type: application/json' \
  -d '{"source_kind":"url","spec_url":"http://127.0.0.1:8781/openapi.yaml"}'              # 201, a new version
curl -s $H $CALIBER_API/openapi-integrations/$I/operations                                # 3 operations, classified
curl -s $H $CALIBER_API/openapi-integrations/$I/dependencies                              # 1 auto_wired, N advisory

# curate a read-only pack, then preview one of its bound operations by selector.
curl -s $H -X POST $CALIBER_API/openapi-integrations/$I/tool-drafts/generate -H 'content-type: application/json' \
  -d '{"operation_ids":["<get-tickets>","<get-ticket>"],"group_as_pack":true,
       "server_url":"http://127.0.0.1:8781","auth_binding":{"kind":"bearer","secret_ref":"env://OPENAPI_TICKET_TOKEN"},
       "allow_in_preview":true}'
curl -s $H -X POST $CALIBER_API/openapi-integrations/$I/tool-drafts/$D/preview -H 'content-type: application/json' \
  -d '{"input":{"operation":"GET /tickets"}}'                                             # result.status_code, result.json
```

Three things the script taught while it was written:

- **`preview`'s response is `{"draft_id", "result"}`, not a `success` flag.** `result` is
  `{"status_code", "text", "json", "headers"}` -- a non-2xx upstream *response* comes back as data
  (`result.status_code: 404`, say), not an error. Only something that prevents the call entirely
  (schema validation, an egress refusal, no server URL) raises an HTTP error from the route itself.
- **A tool pack needs an `operation` selector in its input.** `{"operation": "GET /tickets"}` picks
  which of the pack's bound operations to call; omitting it is a `400` naming the operations the pack
  actually has.
- **Four different checks gate four different actions**, and they are easy to conflate: `resource.execute`
  (owner, editor or reviewer) plus the platform admin-or-operator scope gates preview; `resource.publish`
  (owner or editor only) plus the same platform scope gates publish; `resource.approve` (reviewer, on this
  lab's role matrix) plus the platform **approver** scope gates approving a workflow run. A reviewer who
  can preview a draft may still be refused on publish, and an editor who can publish may still be refused
  on approve.

## Recipe: run it through the UI

Sign in at `$CALIBER_URL/caliber/` as `admin` / `admin`. Before step 1, set the stage once:

- **Administration** (sidebar group **Admin**): under **Accounts** create `lab19-operator`,
  `lab19-reviewer` and `lab19-viewer` (any 12+ character passwords; the script uses
  `<name>-password`).
- In the top bar click the **+** next to the workspace picker, name it `openapi-ops`, **Create and
  select**. Back on **Administration**, under **Project access** pick `openapi-ops (owner)` and add
  `lab19-operator` as **Editor**, `lab19-reviewer` as **Reviewer** and `lab19-viewer` as **Viewer**.

Then, with `openapi-ops` selected:

1. **Create the integration.** **Integrations → OpenAPI Integrations**, **+ New Integration**, name it
   `Ticketing`, **Create**.
2. **Import the spec.** Click **URL**, paste `http://127.0.0.1:8781/openapi.yaml`, **Check**
   (**Reachable (200)**), then submit the import. The integration moves to **review**.
3. **Curate a read-only tool pack.** **Operations** tab: select both `GET /tickets` and
   `GET /tickets/{ticket_id}`, check **bind as one tool pack**, **Generate tool draft(s)**:
   **Generated 1 tool draft**.
4. **Review dependencies.** **Dependencies** tab: the `createTicket` → `getTicket` row shows
   **Auto-wired** with no action. Filter to **Awaiting Review**, **Confirm** the same-tag advisory row.
5. **Publish the pack.** **Tool Drafts** tab, open the pack draft, set **Auth kind** to
   **Bearer token**, type `env://OPENAPI_TICKET_TOKEN` into the secret-reference field, **Save**, then
   **Publish**: **Tool published successfully**, with an **Open in Tool Registry** link.
6. **Curate the write.** **Operations** tab: select `POST /tickets` alone, **Generate tool draft(s)**
   (no pack). Open it: side-effect level **write**, **Requires approval** already on.
7. **Meet the preview gate.** **Preview** on the write draft: refused, *set 'allow_in_preview' before
   running a live upstream call*. Toggle **allow in preview** on, **Preview** again (fill a title):
   **Success**, a real ticket on the upstream. Toggle it back off.
8. **Publish the write.** With **allow in preview** off again, **Publish**: **Tool published
   successfully**; the tool still carries **Requires approval**.
9. **Gate it behind a second person.** Copy the published tool's name into
   `assets/workflow/file-ticket.workflow.yaml` as `__TOOL_NAME__`. **Agent resources → Workflows**,
   **Import**, name it `file-ticket`, **Run** with `{"body": {"title": "..."}}` as input: **Awaiting
   approval** at `call_tool`. Approve yourself (refused: *a second person must decide*). In a second
   browser session sign in as `lab19-reviewer`, pick `openapi-ops`, approve the run. In your own
   session, **Resume**: **completed**.
10. **Reimport and diff.** On the upstream, `curl -X POST http://127.0.0.1:8781/admin/bump-spec`. Back
    in CALIBER, **Reimport & diff**: `Re-imported OAIV-2: +0 -0 changed 2 (1 breaking)`.

As `lab19-viewer`, open **OpenAPI Integrations**: the **+ New Integration** action is not available,
and the API refuses a create with the missing scope in the message.

## Verify it worked

| Check | Where | Expected |
| --- | --- | --- |
| Import | `POST /openapi-integrations/{id}/import` | `201`, a new version; `GET .../operations` lists 3, two `read` one `write` |
| Auto-wired dependency | `GET /openapi-integrations/{id}/dependencies` | one `status: auto_wired` row from `createTicket` to `getTicket`; `PATCH` on it is `409` |
| Tool pack | `POST .../tool-drafts/generate` with `group_as_pack: true` | one draft with `operation_id` + `additional_operation_ids` covering both selected operations |
| Preview gate | `POST .../tool-drafts/{id}/preview` on the write draft | `409` until `allow_in_preview` is set; the upstream's request log shows no new line |
| Preview result shape | the same, on the read pack | `{"draft_id", "result": {"status_code", "text", "json", "headers"}}`, not a `success` flag |
| Publish role floor | `publish_tool_draft` as a reviewer vs. an editor | `403` for the reviewer (no `resource.publish`); `201` for the editor |
| Approval role floor | `POST /workflow-runs/{id}/approval/approve` as the initiator, a viewer, an editor, the reviewer | `403`, `403`, `403`, then success |
| Workflow gate | `GET /workflow-runs/{id}` | `status: waiting_approval`, `current_node_id: call_tool`; the upstream's log does not grow until **Resume** |
| Egress guard | `POST .../validate-spec-source` with a cloud-metadata-range URL | `allowed: false` |
| Reimport + diff | `POST .../reimport` after `POST /admin/bump-spec` | `diff.summary == {"added_count":0,"removed_count":0,"changed_count":2,"unchanged_count":1,"breaking_count":1}`; the one breaking entry names `getTicket`'s deprecation; `createTicket` does not appear in `changed` at all |
| Credential binding | `POST .../validate-credential-binding` | `valid: true` for the real `env://OPENAPI_TICKET_TOKEN`; `valid: false` for a typo'd reference |
| Audit | `GET /audit-log` | `create_openapi_integration` / `import_openapi_integration_version` / `reimport_openapi_integration_version` / `generate_openapi_tool_drafts` on the integration id; `update_openapi_tool_draft` / `preview_openapi_tool_draft` / `publish_openapi_tool_draft` on each draft id |

**The upstream's own request log is the proof.** CALIBER's refusal tells you CALIBER did not make the
call; `TICKET_API_LEDGER` tells you the upstream never ran it. That is the difference between "the
gateway said no" and "nothing happened" -- the same distinction cookbook 05 draws from its MCP
server's call ledger.

### The trust boundary, honestly

- **The diff is a real signal, not a complete one.** It compares a fixed set of normalized fields; a
  request body's own required-field list is not one of them. Re-run a published tool's calibration
  after every reimport, not just the diff -- a breaking change can reimport clean.
- **`allow_in_preview` is a break-glass switch, not the approval gate.** Setting it and previewing a
  write draft fires a real call immediately, the same way cookbook 05's admin Playground runs an
  approval-class MCP tool directly. The gate that actually holds a write for a second person is a
  **workflow**, where the published tool's `requires_approval` pauses the run.
- **Classification is automatic, but still worth checking.** `side_effect_level` is derived from the
  HTTP method and tokens in the path/operationId, not hand-declared the way a Python tool's decorator
  requires. A spec whose verbs lie about their effects (a `GET` that writes) is invisible to this
  control.
- **Roles apply on three different actions, not one.** Preview needs `resource.execute`; publish needs
  `resource.publish`, a strictly narrower project-role set; approving a workflow run needs
  `resource.approve` **and** the platform approver scope, independent of either. A person capable of
  one is not necessarily capable of the others.
- **Dependency detection has no LLM in it.** Every tier -- explicit links, path-hierarchy and
  identifier-field matching, same-tag grouping -- is a deterministic rule over the normalized spec. An
  "agent-suggested" tier is named in the taxonomy but not implemented.

## Common errors and troubleshooting

| You see | Why | Fix |
| --- | --- | --- |
| `404 feature_disabled` on `/openapi-integrations`, no **OpenAPI Integrations** page | the integrations flag is off (the default) | start with `--flags integrations` (`CALIBER_FEATURE_INTEGRATIONS=true`) and restart |
| `outbound request to '127.0.0.1' is blocked` on import, preview or reimport | the upstream's host is not egress-allowed | `CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1` and restart |
| the upstream answers `401` to every call CALIBER makes | `OPENAPI_TICKET_TOKEN` differs between the upstream's environment and CALIBER's, or the auth binding's `secret_ref` is wrong | set the same value in both; the binding must read `env://OPENAPI_TICKET_TOKEN` |
| `409 OpenAPI tool draft '...' is not previewable: set 'allow_in_preview' ...` | a write draft defaults to not-previewable | expected; opt in deliberately (see "break-glass"), or publish and gate it in a workflow instead |
| `400 openapi tool pack requires an 'operation' selector` | a pack draft's preview/invoke input has no `operation` key | pass `{"operation": "<operation_key>"}` naming one of the pack's bound operations |
| `409 high-confidence dependencies are already auto-wired and do not need review` | trying to `PATCH` an `auto_wired` row | expected; only `suggested`/`advisory` rows are reviewable |
| `403 ... resource.publish` on publish, for an account that can preview | a reviewer's project role is below the publish floor | publish as an editor or owner instead |
| `403 this approval requires the 'caliber.approver' scope` | the approving account lacks the platform scope, even with the right project role | add it to `CALIBER_APPROVER_USERS` and restart |
| `403 the account that triggered this run cannot approve it; a second person must decide` | you started the run | approve as another account |
| reimporting reports no changes you expected | the diff only tracks a fixed field set (see "The trust boundary, honestly") | check the spec by eye, or re-run the published tool's calibration |
| `409 ... reimport ... only meaningful for a url source` | the last imported version was pasted or uploaded, not fetched by URL | import by URL first if you want to reimport later |
| a second run's preview fails with `'priority' is a required property` | a previous run's `bump-spec` left the upstream at v2, which is permanent for the process | `curl -X POST http://127.0.0.1:8781/admin/reset` (the script and the UI spec do this themselves) |

## Cleanup and reset

Remove what the script created (the integration is archived, tools archived, workflows deleted, the
workspace archived; accounts, traces and audit entries stay):

```bash
(cd sdk/caliber-sdk && ../../caliber/.venv/bin/python -m examples.cookbooks.cookbook_19_openapi_integration_governance --cleanup)
```

An archived workspace keeps its name, so a re-run on the same stack needs `LAB19_SUFFIX` (above). What
the UI path created is removed from the UI (the integration's **Archive** action, the workflow card's
delete) or goes with the stack. For a clean slate, discard the whole disposable stack and stop the
upstream:

```bash
scripts/cookbook-env.sh down lab19 && rm -rf .tmp/cookbook-env/lab19
# then Ctrl-C the ticket_api.py process
```
