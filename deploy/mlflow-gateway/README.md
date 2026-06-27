# MLflow AI Gateway (LLM Gateway)

A standalone MLflow **AI Gateway** (`mlflow.gateway.app`) that fronts the
configured LLM providers behind one endpoint surface. Part of the `app` profile;
listens on **:5002**.

```
http://localhost:5002/health                 # liveness
http://localhost:5002/api/2.0/endpoints/      # list endpoints (JSON)
POST /gateway/<name>/invocations              # call an endpoint by name
```

## Endpoints

Defined in [`gateway.yaml`](gateway.yaml) — each maps a stable name to a
provider + model. `$OPENAI_API_KEY` / `$ANTHROPIC_API_KEY` are resolved from the
environment (suite-root `.env`) at startup, so the real keys never live in the
config and clients only ever reference the endpoint name.

Ships with `chat-openai`, `completions-openai`, `embeddings-openai`, and
`chat-anthropic`. Add an entry and restart to expose another model.

## How CALIBER uses it

- **Discovery (default on):** CALIBER's **Gateway** page reads this server's
  endpoints + health via `CALIBER_GATEWAY_URI` (default `http://mlflow-gateway:5002`).
  This only makes the gateway *visible* in CALIBER.
- **Routing (opt-in):** set `CALIBER_LLM_BASE_URL=http://mlflow-gateway:5002/gateway`
  on the CALIBER service to route every LLM call through the gateway. Left unset
  by default, so existing direct-provider routing is unchanged.

## Guardrails

MLflow's gateway guardrails (pre/post-LLM validation) attach at the gateway, so
they apply to all routed traffic without touching CALIBER. See
[`../mlflow/configure_guardrails.py`](../mlflow/configure_guardrails.py).

## Run

Built and started with the rest of the `app` profile:

```
./start.sh                  # or: docker compose -f deploy/compose.yaml --profile app up -d --build
```
