# Cookbook 20 · Metered LLM Gateway

> **Flags:** none required for the gateway surface itself (`CALIBER_FEATURE_COOKBOOKS=true` only
> for the gallery step). What this lab *does* need is a stack started with `--real-llm` and routed
> through the lab's stand-in gateway — the exact variables are under
> [Configuration and environment](#configuration-and-environment).

Route every agent LLM call through one gateway, price the model per 1K tokens, and read exact
cost next to usage — with the gateway's own request ledger agreeing on every number.

An agent platform without a meter is a blank check. This lab wires CALIBER's own LLM traffic
through an OpenAI-compatible gateway, then walks the whole accountability chain: discovery of the
gateway's endpoints, a provider key that is wrong and then fixed at runtime through an audited
admin surface, a pricing row that turns tokens into dollars, a usage rollup whose numbers are
checked against the gateway's own log, a rate change that re-prices the future but never the
past, the encrypted secrets store a key like this belongs in, and a deterministic guardrail
definition — honestly scoped to what it does and does not enforce.

## What this lab demonstrates

- **Endpoint discovery, and honest degradation.** The **LLM Gateway** page fetches the MLflow AI
  Gateway listing from `CALIBER_GATEWAY_URI` and shows configured / reachable / routed-through.
  While the gateway is down the page degrades to *Unreachable* with the error — it never breaks.
- **Runtime key governance.** The platform boots with a deliberately wrong provider key. The
  first run fails with the gateway's own `401`, recorded in the gateway's ledger; an admin then
  rotates the key live through `PATCH /settings/llm` (admin-only, audited, fingerprint-masked,
  never readable back) and the same run succeeds. An operator gets `403`.
- **Model pricing with role floors.** An operator prices the model in USD per 1K tokens;
  a duplicate `(provider, model_id)` row is `409`; a viewer cannot create one (`403`); edits are
  admin-only — even for the operator who created the row.
- **Exact, auditable cost.** The stand-in bills a flat **1000 prompt + 1000 completion tokens**
  per call, so at `$1/$2` per 1K every call costs exactly **$3.00** — and `/gateway/usage` says
  so, with the gateway's ledger agreeing on the call count. Cost is computed when the trace span
  is written: raising the rate afterwards re-prices only the next call (`$12.00`), never history.
- **The secrets store.** Admin-only put/rotate/revoke, AES-256-GCM at rest, a version bump on
  every rotation, a `secret://` reference, and no readback — the durable home for a gateway key
  (`CALIBER_LLM_API_KEY_ENV=secret://…` resolves from it at boot; see
  [the trust boundary](#the-trust-boundary-honestly)).
- **Guardrail definitions, honestly scoped.** The catalog's deterministic kinds (`regex`, `pii`)
  need no judge model; a definition is created, listed and deleted for real in the MLflow
  tracking store. Enforcement happens in MLflow's **own** AI Gateway (the tracking server's
  gateway API), *not* in the standalone gateway the Endpoints tab lists — the lab says so instead
  of pretending otherwise.

**You are done when:**

- the Endpoints tab shows *Reachable*, *Through gateway*, and the stand-in's two endpoints — and
  shows *Unreachable* with the error while the stand-in is paused;
- a run on the wrong key ends `error` naming the gateway's `401`, and the gateway's ledger has
  exactly one refused call;
- after the admin rotates the key, one run adds exactly **2000 tokens / $3.00** to
  `lab-chat-mini`'s usage row, and the ledger agrees on the call count;
- raising the prompt rate to `$10` leaves past cost unchanged and bills the next call **$12.00**;
- a secret rotates with a version bump and its value is never shown again;
- a `regex` guardrail is defined, listed and deleted, with its enforcement boundary understood.

## Prerequisites and setup

1. **Start the stand-in gateway** from the repository root (it listens on `127.0.0.1` only, port 8791 must be free). The `&` keeps it
   running in this shell; do the next steps in the same shell, or run it in its own terminal and `export` the same three variables in the other one:

   ```bash
   export LAB20_GATEWAY_KEY=lab20-gateway-key
   export LLM_GATEWAY_LEDGER=/tmp/lab20-gateway-ledger.jsonl
   rm -f "$LLM_GATEWAY_LEDGER"           # the ledger is the lab's proof: start it empty
   python3 docs-site/cookbooks/20-llm-gateway-cost-guardrails-lab/assets/server/llm_gateway.py 8791 &
   ```

2. **Start a disposable stack routed through it.** The boot key is *deliberately wrong* — stage 3
   of the lab is watching it fail and fixing it live:

   ```bash
   export OPENAI_API_KEY=lab20-boot-key-wrong        # placeholder; also keeps your real key out of the stack
   export ANTHROPIC_API_KEY=stand-in
   export CALIBER_GATEWAY_URI=http://127.0.0.1:8791
   export CALIBER_LLM_BASE_URL=http://127.0.0.1:8791/v1
   export CALIBER_ASSISTANT_ENGINE=fake
   export CALIBER_SECRET_ENCRYPTION_KEY_SOURCE=LAB20_SECRET_KEY
   export LAB20_SECRET_KEY="$(python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())')"
   export CALIBER_OPERATOR_USERS=admin,lab20-operator
   scripts/cookbook-env.sh up lab20 --flags cookbooks --real-llm
   ```

   `--real-llm` makes the workflow executor a real OpenAI client; its `base_url` is the stand-in,
   so no real provider is ever called and no real key is ever needed. Pre-exporting
   `OPENAI_API_KEY` matters: without it, `--real-llm` would read your own key from the repo's
   `.env` into a disposable stack.

3. **Run the lab script:**

   ```bash
   source .tmp/cookbook-env/lab20/env.sh      # CALIBER_URL, CALIBER_BASE_URL, CALIBER_API, CALIBER_STATE_DIR, ...
   # mint an admin token (Settings > API tokens does the same in the browser):
   curl -s -c /tmp/lab20.jar -X POST $CALIBER_API/auth/login -H 'content-type: application/json' \
     -d '{"user_id":"admin","password":"admin"}' >/dev/null
   export CALIBER_TOKEN=$(curl -s -b /tmp/lab20.jar -X POST $CALIBER_API/auth/tokens \
     -H 'content-type: application/json' -d '{"name":"lab20"}' | jq -r .data.token)
   export LAB20_GATEWAY_PORT=8791 LLM_GATEWAY_LEDGER=/tmp/lab20-gateway-ledger.jsonl
   (cd sdk/caliber-sdk && PYTHONPATH=. ../../caliber/.venv/bin/python \
      examples/cookbooks/cookbook_20_llm_gateway_cost_guardrails.py)
   ```

## Dataset and sample files

| File | What it is | Used for |
| --- | --- | --- |
| [`server/llm_gateway.py`](assets/server/llm_gateway.py) | the stand-in OpenAI-compatible gateway: the MLflow discovery listing, `/v1/chat/completions` behind a bearer key, a flat 1000+1000-token bill, pause/resume/reset, its own request ledger | everything the lab calls |
| [`workflow/metered-reply.workflow.yaml`](assets/workflow/metered-reply.workflow.yaml) | a one-agent workflow with `model: lab-chat-mini` | the metered runs; its model string is the join key to the discovery row and the pricing row |

## Configuration and environment

| Variable | Lab value | Why |
| --- | --- | --- |
| `CALIBER_GATEWAY_URI` | `http://127.0.0.1:8791` | where the Endpoints tab fetches `GET /api/2.0/endpoints/` |
| `CALIBER_LLM_BASE_URL` | `http://127.0.0.1:8791/v1` | routes CALIBER's workflow LLM calls through the gateway; also what makes the page say *Through gateway*. The OpenAI client appends `/chat/completions`, so the value ends in `/v1` — **not** `/gateway` |
| `OPENAI_API_KEY` | `lab20-boot-key-wrong` | the deliberately wrong boot key stage 3 exposes; also prevents `--real-llm` from reading your real key out of `.env` |
| `CALIBER_SECRET_ENCRYPTION_KEY_SOURCE` | `LAB20_SECRET_KEY` | names the env var holding a 32-byte base64 key; without it the secrets stages answer 503 |
| `CALIBER_OPERATOR_USERS` | `admin,lab20-operator` | who may price models and define guardrails |
| `CALIBER_ASSISTANT_ENGINE` | `fake` | keeps Aria off the stand-in; this lab is about the workflow path |
| `LAB20_GATEWAY_KEY` | `lab20-gateway-key` | the bearer key the stand-in demands; the *right* value the admin rotates in |
| `LLM_GATEWAY_LEDGER` | a JSONL path | the gateway's own request log — the lab's independent proof |

## Run it through the API

The transcript below is a real captured run (`cookbook_20_llm_gateway_cost_guardrails.py`)
against a fresh stack:

```text
  created account lab20-operator
  created account lab20-viewer
== 1. workspace and people
  workspace PRJ-61fc7be2: admin owns it, operator edits, viewer reads
== 2. the gateway, as CALIBER sees it
  ok  a gateway URI is configured
  ok  the gateway answers discovery
  ok  discovery lists the gateway's own endpoints: ['chat-lab', 'embeddings-lab']
  ok  CALIBER's own LLM base URL points through the gateway
  ok  a paused gateway degrades to unreachable-with-error, never a failed page
  ok  and recovers the moment it answers again
== 3. the key is wrong, and the gateway's own ledger proves where it failed
  ok  the platform's LLM provider is openai (via the gateway)
  ok  a key is present -- presence only, never the value
  ok  the run errors, and the error names the gateway's 401
  ok  the gateway's own ledger shows exactly one refused call and no successful one
== 4. an admin rotates the key at runtime, through the governed surface
  ok  an operator cannot write provider credentials -> HTTP 403
  ok  the masked fingerprint names the live key without disclosing it
== 5. pricing: the operator puts a price on the gateway's model
  created pricing row LPRC-338d55be
  ok  a second row for the same provider and model is refused -> HTTP 409
  ok  a viewer cannot create pricing -> HTTP 403
  ok  updating a rate is admin-only, even for the operator who created it -> HTTP 403
== 6. the metered run: tokens become dollars, and two ledgers agree
  ok  the run completed through the gateway
  ok  the output is the gateway model's reply, not a simulation
  ok  the gateway's own ledger shows exactly one real call
  ok  one more call, 2000 more tokens -- the stand-in bills a flat 1000+1000
  ok  it cost exactly $3.0: (1000/1000)x$1.0 + (1000/1000)x$2.0
== 7. the meter is written at call time, not at read time
  ok  raising the price does not rewrite what past calls cost
  ok  the next call is billed at the new rate: $12.0
  ok  the gateway's ledger agrees: two real calls
== 8. the secrets store: the governed home for a key like this one
  ok  writing a secret is admin-only -> HTTP 403
  ok  rotating a secret bumps its version (a rotation, not an overwrite)
  ok  the response never carries the value
  ok  the reference is the secret:// form other features resolve
  ok  the listing carries metadata and a reference, never the value
  ok  the reference scheme is secret://, the form the LLM key source accepts at boot
== 9. guardrail definitions, honestly scoped
  ok  the tracking store's guardrail surface is reachable
  ok  the catalog offers deterministic kinds (regex, pii) that need no judge model
  ok  the guardrail is defined and listed
  note: definitions live in the MLflow tracking store; they are enforced by MLflow's own
        AI Gateway, not by the standalone gateway this lab's Endpoints tab lists
== 10. the audit trail
  ok  audit log has settings.update_llm
  ok  audit log has update_llm_pricing
  ok  audit log has put_secret
  ok  audit log has revoke_secret
  ok  audit log has create_gateway_guardrail
  ok  audit log has delete_gateway_guardrail
  ok  audit log has create_llm_pricing (this run or an earlier one created the row)
== 11. the gallery starter
  ok  the gallery lists 10 step(s) for this lab
== done
```

By hand, the same spine is five calls:

```bash
API=$CALIBER_URL/ajax-api/2.0/mlflow/caliber
AUTH="Authorization: Bearer $CALIBER_TOKEN"

curl -s -H "$AUTH" "$API/gateway" | python3 -m json.tool          # configured, reachable, endpoints
curl -s -H "$AUTH" -X PATCH "$API/settings/llm" \
  -H 'content-type: application/json' -d '{"openai_api_key":"lab20-gateway-key"}'   # fix the key (admin)
curl -s -H "$AUTH" -X POST "$API/llm-pricing" -H 'content-type: application/json' \
  -d '{"provider":"lab","model_id":"lab-chat-mini","prompt_price":1.0,"completion_price":2.0}'
curl -s -H "$AUTH" -X POST "$API/workflow-versions/<WFV-id>/run" \
  -H 'content-type: application/json' -d '{"input":"Hello, gateway"}'               # a metered run
curl -s -H "$AUTH" "$API/gateway/usage?since_ms=0" | python3 -m json.tool           # tokens -> dollars
```

## Recipe: run it through the UI

1. **Read the Endpoints tab.** Open **Integrations › LLM Gateway**. The status cards say *Reachable*
   and *Through gateway* (CALIBER's own LLM base URL points at the gateway), and the table lists
   the stand-in's two endpoints, `chat-lab` and `embeddings-lab`.
2. **Watch it degrade, honestly.** Pause the stand-in
   (`curl -X POST http://127.0.0.1:8791/admin/pause`) and refresh: *Unreachable*, with the
   discovery error on the card — a degraded page, never a broken one. Resume and refresh.
3. **Run on the wrong key.** Import [`metered-reply.workflow.yaml`](assets/workflow/metered-reply.workflow.yaml)
   (**Agent resources › Workflows › Import**), publish it, and run it with any input. The run
   ends **error**, naming the gateway's `401` — and the gateway's own ledger shows the refused
   call. The failure happened at the gateway, not in CALIBER; both records say so.
4. **Rotate the key as an admin.** Through Settings (or
   `PATCH /settings/llm {"openai_api_key": "lab20-gateway-key"}`). The response and
   `GET /settings/llm` show presence and a masked `••••`-fingerprint only. An operator gets 403.
5. **Price the model.** **LLM Gateway › Pricing › Add rate**: provider `lab`, model
   `lab-chat-mini`, `$1`/1K in, `$2`/1K out, **Create**. A duplicate is refused (409); edits are
   admin-only.
6. **Run the metered workflow.** The run completes with the gateway model's reply, and the
   gateway's ledger gains one `ok` line billing a flat 1000+1000 tokens.
7. **Read the Usage tab.** The by-model table shows `lab-chat-mini`: one more call, 2000 more
   tokens, exactly **$3.00** more cost.
8. **Raise the rate, run again.** Set the prompt rate to `$10` (admin). Usage for past calls is
   unchanged — the meter is written at call time — and the next run bills **$12.00**.
9. **Store the key in the secrets store.** **Admin › Administration › Secrets › Store / rotate**,
   twice: the version bumps, the listing shows a `secret://` reference and never a value.
10. **Define a deterministic guardrail.** **LLM Gateway › Guardrails › New guardrail**: kind
    `regex`, a name, a pattern, **Create guardrail** — it lists; then delete it. Definitions live
    in the MLflow tracking store; enforcement is the job of MLflow's own AI Gateway (see below).

## Verify it worked

| Claim | Where to look | What proves it |
| --- | --- | --- |
| Discovery is real | Endpoints tab / `GET /gateway` | `reachable: true`, two endpoint rows matching the stand-in's listing; `503` from a paused stand-in flips it to `reachable: false` with the error |
| The wrong key failed *at the gateway* | the run's error + `LLM_GATEWAY_LEDGER` | run status `error` naming `401`; a ledger line with `ok: false`; no line at all for a key CALIBER refuses to resolve |
| Cost is exact | Usage tab / `GET /gateway/usage` | `+1 call, +2000 tokens, +$3.00` per run at `$1/$2`; the ledger's `ok` count equals usage's call count |
| The meter is written at call time | `/gateway/usage` after a rate change | past cost unchanged; the next call bills at the new rate |
| Credentials are never readable | `GET /settings/llm`, `GET /secrets` | presence flags and masked fingerprints only; secret listings carry metadata and a `secret://` reference |
| Every governed write is audited | **Audit Log** | `settings.update_llm`, `create_llm_pricing`, `update_llm_pricing`, `put_secret`, `revoke_secret`, `create_gateway_guardrail`, `delete_gateway_guardrail` |

## The trust boundary, honestly

- **Guardrails are definitions here, not enforcement.** CALIBER's Guardrails tab manages scorer
  definitions in the MLflow tracking store, and MLflow's **own** AI Gateway (the tracking
  server's `/gateway` API) is what enforces them on *its* endpoints. The standalone gateway this
  lab runs (and the real `deploy/mlflow-gateway` deployment) does not load guardrails at all. A
  guardrail you define on this page does not filter the calls the Endpoints tab lists.
- **Pricing matches on `model_id` alone** (exact, then longest prefix; provider is ignored), the
  table is global across projects, and an unknown model costs `$0` — absence of cost is not
  evidence of absence of spend.
- **Cost is frozen at span-write time.** That is an audit property (history cannot be rewritten
  by editing a rate), and a bookkeeping caveat (correcting a wrong rate does not correct history).
- **Not every LLM call routes through `CALIBER_LLM_BASE_URL`.** Workflow agent turns, memory and
  workflow guardrail judges do; the refinement/diagnosis provider builds its own direct client.
- **A `secret://` LLM key source resolves at boot**, so the secret must exist first: boot once
  with an env-var key, store the secret, then restart with
  `CALIBER_LLM_API_KEY_ENV=secret://<name>`. The runtime rotation this lab demonstrates
  (`PATCH /settings/llm`) is the live-server path; the secret store is the durable one.

## Common errors and troubleshooting

| You see | Why | Fix |
| --- | --- | --- |
| `configured: false` on `/gateway` | `CALIBER_GATEWAY_URI` is not set | export it and restart the stack |
| `reachable: false` with a connect error | the stand-in is not running (or is paused) | start `llm_gateway.py` / `POST /admin/resume` |
| the run errors with `404` instead of completing | `CALIBER_LLM_BASE_URL` ends in `/gateway` or the bare root | it must end in `/v1`: the OpenAI client appends `/chat/completions` |
| the run errors with the gateway's `401` | the provider key does not match `LAB20_GATEWAY_KEY` | stage 4: `PATCH /settings/llm` with the right key (admin) |
| `llm_provider='openai' but secret source ... did not resolve` at boot | a `secret://` key source whose secret does not exist yet | boot with an env-var key first, or seed the secret and restart |
| usage shows tokens but `cost_usd: 0` | no pricing row matches the model | create the `lab/lab-chat-mini` row; pricing matches on `model_id` |
| the usage row lags a run by a moment | spans land in MLflow asynchronously | re-read after a beat; the script polls for exactly this reason |
| `503` from `/secrets` | `CALIBER_SECRET_ENCRYPTION_KEY_SOURCE` is unset | set it to an env var holding a 32-byte base64/hex key and restart |
| guardrail create answers `502` | the MLflow tracking server behind `MLFLOW_TRACKING_URI` predates the gateway-guardrail API | MLflow ≥ 3.13 (the repo venv ships 3.15) |

## Cleanup and reset

```bash
(cd sdk/caliber-sdk && PYTHONPATH=. ../../caliber/.venv/bin/python \
   examples/cookbooks/cookbook_20_llm_gateway_cost_guardrails.py --cleanup)
```

archives the workspace and the pricing row and deletes the lab secret. Traces, usage history and
audit entries stay, by design. For a clean slate, discard the whole disposable stack and stop the
stand-in:

```bash
scripts/cookbook-env.sh down lab20 && rm -rf .tmp/cookbook-env/lab20
# then Ctrl-C the llm_gateway.py process
```

A re-run on the same stack needs no manual reset: the script truncates the ledger, resets the
stand-in, re-stages the wrong key deliberately, and adopts the existing pricing row.
