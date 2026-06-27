# Gateways Architecture

## At a glance

| Dimension | Gateways integration layer |
| --- | --- |
| **What it is** | An integration and visibility layer over an external MLflow AI Gateway, not a proxy or gateway service of CALIBER's own. |
| **Discovery** | Synchronous, per-request probe of `CaliberConfig.gateway_uri` → `/api/2.0/endpoints/`; no metadata cache or local endpoint registry. |
| **Routing behavior** | CALIBER's own LLM calls go through the gateway only when `CaliberConfig.llm_base_url` (opt-in via `CALIBER_LLM_BASE_URL`) points at it; `routing_through_gateway` is a URL-prefix comparison against `gateway_uri`. |
| **Key surfaces** | `GET /gateway` (read-only discovery), guardrail-governance, `/gateway/usage`, `/llm-pricing`, plus `/settings/llm`; UI is the tabbed `Gateway.tsx` page. |
| **State** | Stateless relative to the DB — no dedicated gateway tables; status is computed on demand and shaped by `GatewayStatusSchema` / `GatewayEndpointSchema`. |
| **Trust / safety** | Discovery is authenticated read-only and never forwards prompts; mutating guardrail routes are `SCOPE_OPERATOR`-audited and `PATCH /settings/llm` is `SCOPE_ADMIN`. |

The sections below start from this picture and drill down into each dimension in
detail, keeping the discovery, routing, and external-control-plane distinctions
strictly apart.

## Reference

## 1. Scope and responsibilities

The gateways module documents CALIBER's integration with an external MLflow AI
Gateway. It is important to state the boundary up front: in the current
checkout, CALIBER does not implement a general-purpose gateway service of its
own. The module is therefore an integration and visibility layer, not a proxy,
and it is responsible for the following:

- discovering whether an external AI Gateway is configured;
- probing that gateway for reachability and endpoint inventory;
- showing whether CALIBER routes its own LLM traffic through the gateway;
- presenting gateway status inside the UI without requiring operators to leave
  CALIBER; and
- exposing adjacent settings and service-health surfaces that affect routing.

These responsibilities are implemented across the following primary code paths:

- `caliber/src/caliber/routes/gateway.py`
- `caliber/src/caliber/routes/settings.py`
- `caliber/src/caliber/routes/system_services.py`
- `caliber/src/caliber/routes/health.py`
- `caliber/src/caliber/schemas.py`
- `caliber/src/caliber/config.py`
- `caliber/caliber-ui/src/pages/Gateway.tsx`
- `caliber/caliber-ui/src/api/caliberApi.ts`

## 2. Module boundaries

The module's work divides cleanly between reporting gateway truth and adjusting
CALIBER's own routing target. The table below assigns each responsibility to its
owner.

| Responsibility | Owner | Notes |
| --- | --- | --- |
| Gateway discovery/status API | `routes/gateway.py` | Read-only route that reports configured state, reachability, endpoint inventory, routing mode, and errors. |
| Gateway guardrails API | `routes/gateway.py` (`/gateway/guardrails`, `…/endpoints/{id}/guardrails`) | Reads the scorer-based gateway guardrails + per-endpoint coverage from the **MLflow tracking store** (`_gateway_store` → `list_gateway_guardrails` / `list_endpoint_guardrail_configs`), and attaches / detaches / reorders existing guardrails (operator-scoped + audited, run off-thread, degrade-gracefully). Scorer/guardrail *creation* stays in the MLflow image (`deploy/mlflow/configure_guardrails.py`). |
| Gateway usage API | `routes/gateway.py` (`/gateway/usage`) → `observability.gateway_usage_payload` | Trace-derived token / cost / latency / error metrics over time + a by-model rollup (the gateway API exposes no usage stats; CALIBER's MLflow traces do). Reuses the observability bucketize; reads the `caliber.model` / `caliber.tokens` / `caliber.cost_usd` span attributes. |
| Per-model cost config | `routes/llm_pricing.py` + `CaliberLlmModelPricing` (migration 0061) | Operator-editable USD-per-1K-token rates per provider/model. `observability/mlflow_tracing.resolve_model_pricing` merges active rows over the built-in `DEFAULT_MODEL_PRICING` (cached, invalidated on edit) so cost attribution everywhere reflects them. |
| Gateway schema contract | `GatewayEndpointSchema`, `GatewayStatusSchema`, `GatewayGuardrailsStatusSchema`, `LlmPricingSchema` | Shapes the endpoint rows, guardrail coverage, pricing rows, and the status envelopes returned to the UI. |
| Runtime routing configuration | `routes/settings.py` + `CaliberConfig.llm_base_url` | Determines whether CALIBER's own LLM calls go through the gateway or directly to providers. |
| Discovery configuration | `CaliberConfig.gateway_uri` | Tells CALIBER where to probe for the external AI Gateway, but does not route traffic by itself. |
| Operator service probes | `routes/system_services.py` | Shows AI Gateway health alongside MLflow, DB, object store, NATS, and related backing services. |
| Runtime honesty surface | `routes/health.py` | Two endpoints sit adjacent to gateway operations: `GET /health` probes the database (`SELECT 1`) and returns HTTP 503 with `db: "down"` when it is unreachable, while `GET /readiness` reports real-vs-simulated provider state (and does not probe the database). |
| Frontend gateway UX | `Gateway.tsx` + `components/gateway/*` | Tabbed LLM Gateway page: **Endpoints** (status cards + endpoint table + routing), **Guardrails** (table + per-endpoint coverage + attach/detach), **Pricing** (editable per-model rate table), **Usage** (recharts time-series + by-model table). |

Two MLflow servers are involved and must not be conflated: **endpoint discovery**
reads the standalone gateway over HTTP (`CaliberConfig.gateway_uri` →
`/api/2.0/endpoints/`); **guardrail governance** is a set of tracking-server RPCs
reached via the MLflow client store (`mlflow.get_tracking_uri()`), the same
reach-through `deploy/mlflow/configure_guardrails.py` uses. Guardrails in this
MLflow version are **scorer-based** (Stage `BEFORE`/`AFTER` × Action
`VALIDATION`/`SANITIZATION`), not natural-language instructions.

The single most important architectural distinction in the module is the
separation between discovery, routing, and the external control plane, and it is
worth fixing precisely:

- `gateway_uri` is where CALIBER probes to discover an external gateway.
- `llm_base_url` is where CALIBER actually sends LLM requests.
- The external gateway control plane is the service that owns endpoint
  definitions, provider bindings, limits, and guardrails.

Conflating the first two is the most common source of confusion, which is why
the rest of this document keeps them apart.

## 3. Runtime architecture

The topology below shows the two independent paths the module reasons about:
discovery toward the external gateway, and routing toward whatever target
CALIBER's LLM clients are configured to use.

```mermaid
flowchart LR
    UI[Gateway.tsx]:::ui
    GAPI[routes/gateway.py]:::ctrl
    CFG[CaliberConfig]:::ctrl
    EXT[(External MLflow AI Gateway)]:::ext
    SET[routes/settings.py]:::ctrl
    LLM[CALIBER LLM clients]:::ctrl
    PROV[(Providers or Gateway endpoints)]:::ext
    SVC[routes/system_services.py]:::ctrl

    UI --> GAPI --> CFG
    GAPI --> EXT
    UI --> SET --> CFG
    CFG --> LLM --> PROV
    SVC --> EXT
```

```legend
```

Several structural properties define how this integration behaves:

- Gateway discovery is synchronous and per-request; CALIBER does not maintain a
  gateway metadata cache or a local endpoint registry.
- The Gateway page is informational, and is neither a proxy nor a control plane.
- Settings mutate CALIBER's runtime routing target, but they do not create,
  delete, or edit gateway endpoints.
- The same external gateway can be visible to CALIBER even when CALIBER itself
  is still routing directly to providers.

## 4. Data model and state

This module is intentionally stateless relative to the database. There are no
dedicated gateway tables in the inspected checkout; the primary state is
computed on demand from configuration plus a live HTTP probe, and it is shaped
by two schemas.

The status envelope, `GatewayStatusSchema`, carries the following fields:

- `configured`
- `reachable`
- `gateway_uri`
- `routing_through_gateway`
- `llm_base_url`
- `endpoints`
- `error`

Each endpoint row within that envelope, `GatewayEndpointSchema`, carries:

- `name`
- `endpoint_type`
- `provider`
- `model`
- `endpoint_url`
- `limit`

A few runtime semantics determine how those fields are populated, and they
encode the discovery-versus-routing distinction directly:

- `configured=false` means `CALIBER_GATEWAY_URI` is empty, so CALIBER has
  nothing to probe.
- `reachable=false` means the gateway URI was configured but the live probe
  failed or returned an error.
- `routing_through_gateway` is derived by comparing whether `llm_base_url`
  starts with the same scheme/host/port prefix as `gateway_uri`.
- Endpoint inventory is read from the external gateway's `/api/2.0/endpoints/`
  API and is never stored durably inside CALIBER.

## 5. API and interaction surfaces

All HTTP routes in CALIBER are mounted under `/ajax-api/2.0/mlflow/caliber` and
are shown relative to that prefix below. The gateway module exposes the
discovery route plus guardrail-governance, usage, and pricing routes, and the
adjacent settings and operational surfaces that govern routing.

The gateway surface is:

- `GET /gateway` — endpoint discovery + reachability (read-only)
- `GET /gateway/guardrails` — guardrail inventory + per-endpoint coverage (read-only; degrades gracefully when the tracking server is unreachable)
- `POST /gateway/endpoints/{endpoint_id}/guardrails` — attach a guardrail (operator-scoped, audited)
- `DELETE /gateway/endpoints/{endpoint_id}/guardrails/{guardrail_id}` — detach a guardrail (operator-scoped, audited)
- `PATCH /gateway/endpoints/{endpoint_id}/guardrails/{guardrail_id}` — update execution order / enable state (operator-scoped, audited)
- `GET /gateway/usage` — trace-derived usage time series + by-model rollup (auth user)

Per-model pricing is a sibling CRUD resource (`routes/llm_pricing.py`):

- `GET /llm-pricing`, `GET /llm-pricing/{pricing_id}` (auth user, visibility-scoped)
- `POST /llm-pricing` (operator-scoped, audited; duplicate provider/model → 409)
- `PATCH /llm-pricing/{pricing_id}` (admin-scoped, audited)

The adjacent settings and operational surfaces are:

- `GET /settings/llm`
- `PATCH /settings/llm`
- `GET /system/services`
- `GET /health`
- `GET /readiness`

On the frontend, these routes compose into a deliberately simple interaction
model:

- The Gateway page calls `GET /gateway` and renders three high-signal status
  cards: the configured gateway URI, reachability, and whether CALIBER routes
  through the gateway.
- If the gateway is reachable, the UI renders the endpoint inventory table with
  endpoint type, provider, model, and endpoint URL.
- If the gateway is unreachable, the UI shows the probe error, yet the page
  still loads successfully.
- The settings route separately reports and updates `gateway_url`, which maps to
  `llm_base_url` and controls CALIBER's own outbound routing behavior.

## 6. Execution lifecycle

The sequence below shows discovery resolving to one of three outcomes, followed
by the separate, optional act of changing CALIBER's routing target.

```mermaid
sequenceDiagram
    participant U as Operator
    participant UI as Gateway UI
    participant G as routes/gateway.py
    participant GW as External AI Gateway
    participant S as routes/settings.py
    participant C as Runtime config
    participant L as CALIBER LLM client

    U->>UI: open LLM Gateway page
    UI->>G: GET /gateway
    G->>C: read gateway_uri and llm_base_url

    alt gateway_uri unset
        G-->>UI: configured=false, endpoints=[]
    else gateway configured
        G->>GW: GET /api/2.0/endpoints/
        alt gateway reachable
            GW-->>G: endpoint inventory
            G-->>UI: configured=true, reachable=true, routing flag, endpoints
        else gateway unreachable
            G-->>UI: configured=true, reachable=false, error
        end
    end

    opt Change CALIBER routing
        U->>S: PATCH /settings/llm with gateway_url
        S->>C: update llm_base_url at runtime
        L->>C: read llm_base_url for future calls
    end
```

The diagram makes the module's central separation explicit by keeping two flows
apart:

- The discovery flow answers whether CALIBER can see an external gateway and
  what endpoints that gateway reports.
- The routing flow answers whether CALIBER itself chooses to send its LLM calls
  through that gateway.

## 7. Security and trust boundaries

Because discovery is read-only while routing changes where real traffic goes,
the module's authorization model grants progressively higher privilege as an
action's blast radius widens.

The authorization model is as follows:

- `GET /gateway`, `GET /gateway/guardrails`, and `GET /gateway/usage` require an authenticated user.
- The mutating guardrail routes (`POST`/`DELETE`/`PATCH` on `/gateway/endpoints/{id}/guardrails`) require `SCOPE_OPERATOR` and are audited.
- `GET /settings/llm` requires `SCOPE_OPERATOR`.
- `PATCH /settings/llm` requires `SCOPE_ADMIN`.
- Pricing reads (`GET /llm-pricing`) require an authenticated user; create requires `SCOPE_OPERATOR` and update requires `SCOPE_ADMIN` (both audited).

A small set of data-handling protections keeps discovery safe and predictable:

- Gateway probes use a short HTTP timeout and degrade into an error field rather
  than blocking or crashing the page.
- The gateway status route is read-only and never forwards prompts or model
  payloads through the discovery path.
- CALIBER treats the external gateway as the source of truth for endpoint
  inventory but still normalizes the response into a stable schema before
  exposing it to the UI.

These protections sit on top of explicit trust boundaries:

- `gateway_uri` merely declares where CALIBER looks for gateway metadata; it
  does not imply that runtime traffic is actually routed there.
- `llm_base_url` changes CALIBER's outbound call target, so it is kept on an
  admin-scoped settings surface rather than the read-only gateway page.
- Guardrail *creation* (and the scorers they wrap) lives in the MLflow image,
  not in CALIBER's route layer; guardrail *governance* — attach, detach, and
  reorder against existing guardrails — is exposed as operator-scoped, audited
  CALIBER routes that proxy the tracking-server RPCs.

## 8. Observability and operations

Operationally, the gateway module is an operator truth surface rather than a
traffic-processing subsystem, and its value lies in disambiguating connectivity
problems from configuration problems.

The behaviors that matter most in operation are these:

- `GET /gateway` distinguishes "not configured" from "configured but
  unreachable", which matters when triaging connectivity versus missing setup.
- `GET /system/services` probes the AI Gateway alongside the rest of the
  platform dependencies, so operators can verify the service separately from the
  dedicated page.
- The Gateway UI explicitly explains that routing through the gateway is opt-in
  via `CALIBER_LLM_BASE_URL`.
- The settings surface exposes the live `gateway_url` value, so operators can
  align what the runtime is doing with what the status page reports.

Two configuration knobs govern the module's behavior end to end:

- `CALIBER_GATEWAY_URI`
- `CALIBER_LLM_BASE_URL`

From those knobs and the probe result, the module resolves into a small number
of operational states:

- With no gateway configured, the discovery page shows setup guidance.
- With a gateway configured but unreachable, the route returns `reachable=false`
  plus the probe error.
- With a gateway reachable but holding no endpoints, the route is healthy, yet
  the external gateway has no configured endpoint inventory.
- With a gateway reachable while CALIBER routes directly to providers, discovery
  works, but runtime traffic is not yet opted into the gateway.

## 9. Extension points and current constraints

The module is deliberately narrow, which makes both its growth path and its
present limitations easy to state.

It can be extended along these seams:

- Additional gateway metadata fields can be added to `GatewayEndpointSchema`
  without changing the fundamental discovery architecture.
- The settings layer can evolve to support richer routing strategies while still
  keeping the read-only discovery route separate.
- Service-health and readiness surfaces can continue to cross-link gateway state
  with other platform dependencies.

Its current constraints follow directly from that narrow scope:

- The current checkout contains no dedicated gateway service implementation
  inside CALIBER; this module integrates with an external MLflow AI Gateway.
- The gateway page is read-only and cannot configure endpoints, policies, or
  limits.
- Endpoint inventory is fetched live on every request and is neither cached nor
  historized inside CALIBER.
- `routing_through_gateway` is a prefix comparison on URLs, which is sufficient
  for the current single-target setup but is not a full policy engine for
  per-model or per-tenant routing.
- The module is intentionally narrow: it reports gateway truth and routing
  state, but general API gateway behavior for the rest of CALIBER is outside its
  scope.
