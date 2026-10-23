# SCN-20 assets

Everything the lab runs, from the [README](../README.md). There is nothing to author beyond the
stand-in itself: the workflow is three nodes, and every other artifact (pricing rows, secrets,
guardrails) is created through the product.

| Folder | Files | Used for |
| --- | --- | --- |
| [`server/`](server/) | `llm_gateway.py` | the OpenAI-compatible gateway CALIBER calls over real loopback HTTP: serves the MLflow AI Gateway discovery listing and `/v1/chat/completions` behind a bearer key; `POST /admin/pause` / `resume` stand in for an outage, `POST /admin/reset` restores a pristine start |
| [`workflow/`](workflow/) | `metered-reply.workflow.yaml` | a one-agent workflow whose `model: lab-chat-mini` is the join key -- the same string the gateway's discovery row and the pricing row's `model_id` carry |

## The flat bill

`llm_gateway.py` answers every chat call with a canned reply and a **fixed** usage block: 1000
prompt + 1000 completion tokens. That is deliberate. Cost in CALIBER is
`(prompt/1000) x prompt_price + (completion/1000) x completion_price`, computed at span-write time
from the pricing table, so a flat bill makes every number downstream checkable by eye: at the
lab's `$1/$2` per 1K, one call is exactly `$3.00` -- the same ground truth
`caliber/tests/test_routes_llm_pricing.py` pins.

## The gateway's own request log

`llm_gateway.py` appends one JSON line (`{"path", "model", "ok", "prompt_tokens",
"completion_tokens"}`) to `LLM_GATEWAY_LEDGER` for every chat call, written **before** the response
is sent. A call CALIBER refuses to make (a key that no longer resolves) never appears here; a call
the gateway refuses (a wrong key) appears with `ok: false` and never appears in CALIBER's usage.
Two independent ledgers that must agree is this lab's proof, the same pattern the MCP and OpenAPI
labs use.
