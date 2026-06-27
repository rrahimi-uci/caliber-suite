"""``/caliber/gateway`` — MLflow AI Gateway surface (discovery + guardrails + usage).

Three read/operate surfaces, each guarded so an unavailable backing service
degrades (``reachable=false`` + ``error``) rather than failing the page:

* **Endpoints** — the gateway's configured LLM endpoints (provider / model / type
  / URL), read over HTTP from the standalone gateway (``CALIBER_GATEWAY_URI``).
* **Guardrails** — the scorer-based gateway guardrails + per-endpoint coverage,
  read from the **MLflow tracking server** (the gateway-guardrail API lives on
  the tracking store, not the standalone gateway — see
  ``deploy/mlflow/configure_guardrails.py``). Operators can **define** new
  guardrails (PII / toxicity / custom guidelines / regex, backed by native
  no-extra-deps MLflow scorers, or any already-registered scorer), **delete**
  them, and attach / detach / reorder them on an endpoint (operator-scoped +
  audited). CALIBER and MLflow's own gateway UI share one tracking store, so a
  guardrail defined here is immediately visible in MLflow and vice versa — there
  is no sync. The ``guardrails-ai`` validator guardrails (Presidio PII, BERT
  jailbreak, …) can't be built from CALIBER's venv (the package is intentionally
  absent) — they are created server-side in the MLflow image but still listed +
  selectable here.
* **Usage** — trace-derived token / cost / latency / error metrics over time +
  a by-model rollup (the gateway API does not expose usage stats in this MLflow
  version; CALIBER's own MLflow traces do). Reuses the observability aggregation.

Routing CALIBER's *own* LLM calls through the gateway is a separate opt-in
(``llm_base_url``) which this module reports but never changes.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_OPERATOR, require_scopes, require_user
from caliber.routes._deps import (
    envelope_response,
    envelope_response_dict,
    get_session_factory,
    parse_json_object,
)
from caliber.routes.observability import gateway_usage_payload
from caliber.schemas import (
    GatewayEndpointSchema,
    GatewayGuardrailAttachRequest,
    GatewayGuardrailCatalogSchema,
    GatewayGuardrailConfigUpdateRequest,
    GatewayGuardrailCreateRequest,
    GatewayGuardrailsStatusSchema,
    GatewayStatusSchema,
)

logger = logging.getLogger("caliber.routes.gateway")

STATUS_PATH = "/ajax-api/2.0/mlflow/caliber/gateway"
GUARDRAILS_PATH = "/ajax-api/2.0/mlflow/caliber/gateway/guardrails"
GUARDRAILS_CATALOG_PATH = "/ajax-api/2.0/mlflow/caliber/gateway/guardrails/catalog"
GUARDRAIL_PATH = "/ajax-api/2.0/mlflow/caliber/gateway/guardrails/{guardrail_id}"
ENDPOINT_GUARDRAILS_PATH = (
    "/ajax-api/2.0/mlflow/caliber/gateway/endpoints/{endpoint_id}/guardrails"
)
ENDPOINT_GUARDRAIL_PATH = (
    "/ajax-api/2.0/mlflow/caliber/gateway/endpoints/{endpoint_id}/guardrails/{guardrail_id}"
)
USAGE_PATH = "/ajax-api/2.0/mlflow/caliber/gateway/usage"

# MLflow AI Gateway lists its endpoints here (mlflow.deployments.server.constants).
_ENDPOINTS_PATH = "/api/2.0/endpoints/"
_HTTP_TIMEOUT_SECONDS = 4.0


def _err(exc: Exception) -> str:
    """Prefix the exception type so a bare message still names the failure mode."""
    detail = str(exc)
    return f"{exc.__class__.__name__}: {detail}" if detail else exc.__class__.__name__


def _routes_through(gateway_uri: str, llm_base_url: str) -> bool:
    """True when CALIBER's ``llm_base_url`` points at the gateway host."""
    if not gateway_uri or not llm_base_url:
        return False
    return llm_base_url.rstrip("/").startswith(gateway_uri.rstrip("/"))


def _map_endpoint(raw: dict[str, Any]) -> GatewayEndpointSchema:
    model = raw.get("model") or {}
    return GatewayEndpointSchema(
        name=str(raw.get("name", "")),
        endpoint_type=str(raw.get("endpoint_type", "") or ""),
        provider=str(model.get("provider", "") or ""),
        model=str(model.get("name", "") or ""),
        endpoint_url=str(raw.get("endpoint_url", "") or ""),
        limit=raw.get("limit") if isinstance(raw.get("limit"), dict) else None,
    )


async def get_gateway(request: Request) -> JSONResponse:
    require_user(request)
    config = getattr(request.app.state, "config", None)
    gateway_uri = str(getattr(config, "gateway_uri", "") or "").strip().rstrip("/")
    llm_base_url = str(getattr(config, "llm_base_url", "") or "").strip()

    if not gateway_uri:
        return envelope_response(
            GatewayStatusSchema(
                configured=False,
                reachable=False,
                gateway_uri="",
                routing_through_gateway=False,
                llm_base_url=llm_base_url,
                endpoints=[],
                error=None,
            )
        )

    endpoints: list[GatewayEndpointSchema] = []
    reachable = False
    error: str | None = None
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(f"{gateway_uri}{_ENDPOINTS_PATH}")
            resp.raise_for_status()
            payload = resp.json()
        raw_endpoints = payload.get("endpoints", []) if isinstance(payload, dict) else []
        endpoints = [_map_endpoint(e) for e in raw_endpoints if isinstance(e, dict)]
        reachable = True
    except Exception as exc:  # external service — degrade, never fail the page
        error = _err(exc)
        logger.warning("MLflow gateway unreachable at %s: %s", gateway_uri, error)

    return envelope_response(
        GatewayStatusSchema(
            configured=True,
            reachable=reachable,
            gateway_uri=gateway_uri,
            routing_through_gateway=_routes_through(gateway_uri, llm_base_url),
            llm_base_url=llm_base_url,
            endpoints=endpoints,
            error=error,
        )
    )


# --- Guardrails (MLflow tracking-store gateway-guardrail API) -----------------

# Buildable guardrail kinds. Each is backed by a *native* MLflow built-in scorer
# (mlflow.genai.scorers.builtin_scorers) that constructs + serializes with no
# extra dependencies — unlike the guardrails-ai validator scorers, which need a
# package CALIBER deliberately does not ship. Kept server-side so the create
# handler and the UI form agree on one source of truth.
_GUARDRAIL_TEMPLATES: list[dict[str, Any]] = [
    {
        "type": "pii",
        "label": "PII detection",
        "summary": (
            "Flag personally identifiable information (email, phone, SSN, credit "
            "card, IP) with a deterministic rule-based scorer. Blocks (VALIDATION) "
            "or redacts (SANITIZATION) text that contains PII."
        ),
        "scorer_class": "PIIDetection",
        "deterministic": True,
        "default_stage": "AFTER",
        "default_action": "SANITIZATION",
        "fields": [
            {
                "name": "pii_types",
                "label": "PII types",
                "type": "multiselect",
                "required": False,
                "options": ["email", "phone", "ssn", "credit_card", "ip_address"],
                "help": "Leave empty to check every supported type.",
            },
        ],
    },
    {
        "type": "toxicity",
        "label": "Toxicity / safety",
        "summary": (
            "LLM-judge guardrail that blocks harmful, offensive, or toxic content. "
            "Needs a judge model available to the gateway."
        ),
        "scorer_class": "Safety",
        "deterministic": False,
        "default_stage": "BEFORE",
        "default_action": "VALIDATION",
        "fields": [
            {
                "name": "model",
                "label": "Judge model",
                "type": "text",
                "required": False,
                "placeholder": "gateway:/<endpoint>",
                "help": "Optional model URI for the judge. Defaults to the server's judge model.",
            },
        ],
    },
    {
        "type": "guidelines",
        "label": "Custom (natural-language guidelines)",
        "summary": (
            "LLM-judge guardrail that passes only when the text follows your "
            "plain-English guidelines. One guideline per line."
        ),
        "scorer_class": "Guidelines",
        "deterministic": False,
        "default_stage": "AFTER",
        "default_action": "VALIDATION",
        "fields": [
            {
                "name": "guidelines",
                "label": "Guidelines",
                "type": "textarea",
                "required": True,
                "placeholder": "The response must not promise refunds.\nThe response must be in English.",
                "help": "One guideline per line.",
            },
            {
                "name": "model",
                "label": "Judge model",
                "type": "text",
                "required": False,
                "placeholder": "gateway:/<endpoint>",
                "help": "Optional model URI for the judge.",
            },
        ],
    },
    {
        "type": "regex",
        "label": "Regex match",
        "summary": (
            "Deterministic format check. Passes when the text matches the pattern "
            "— use it for 'must look like …' constraints (no LLM call)."
        ),
        "scorer_class": "RegexMatch",
        "deterministic": True,
        "default_stage": "AFTER",
        "default_action": "VALIDATION",
        "fields": [
            {
                "name": "pattern",
                "label": "Pattern",
                "type": "text",
                "required": True,
                "placeholder": r"^Answer:",
                "help": "Python regular expression.",
            },
            {
                "name": "match_type",
                "label": "Match type",
                "type": "select",
                "required": False,
                "options": ["search", "fullmatch"],
            },
            {
                "name": "case_insensitive",
                "label": "Case insensitive",
                "type": "boolean",
                "required": False,
            },
        ],
    },
]

_TEMPLATE_TYPES = frozenset(t["type"] for t in _GUARDRAIL_TEMPLATES)


def _gateway_store() -> Any:
    """Return the MLflow tracking store carrying the gateway-guardrail methods.

    The gateway-guardrail API is exposed on the tracking store (RestStore for an
    http tracking URI), not on the public ``MlflowClient`` — the same
    reach-through ``deploy/mlflow/configure_guardrails.py`` uses. Tests monkeypatch
    this function to inject a fake store.
    """
    import mlflow  # noqa: PLC0415

    client = mlflow.MlflowClient()
    # Reach-through to the tracking store: the gateway-guardrail methods live there,
    # not on the public MlflowClient (same as deploy/mlflow/configure_guardrails.py).
    return client._tracking_client.store


def _enum_name(value: Any) -> str:
    return str(getattr(value, "name", None) or (value if value else "") or "")


def _map_guardrail(g: Any) -> dict[str, Any]:
    scorer = getattr(g, "scorer", None)
    scorer_name = getattr(scorer, "name", None)
    if scorer_name is None and scorer is not None:
        scorer_name = str(scorer)
    return {
        "guardrail_id": str(getattr(g, "guardrail_id", "") or ""),
        "name": str(getattr(g, "name", "") or ""),
        "stage": _enum_name(getattr(g, "stage", "")),
        "action": _enum_name(getattr(g, "action", "")),
        "scorer": scorer_name,
        "action_endpoint_name": getattr(g, "action_endpoint_name", None),
    }


def _collect_guardrails(store: Any) -> dict[str, Any]:
    """Read guardrails + per-endpoint coverage from the tracking store (blocking)."""
    guardrails = [_map_guardrail(g) for g in store.list_gateway_guardrails()]
    name_by_id = {g["guardrail_id"]: g["name"] for g in guardrails}
    coverage: list[dict[str, Any]] = []
    for endpoint in store.list_gateway_endpoints():
        endpoint_id = str(getattr(endpoint, "endpoint_id", None) or getattr(endpoint, "id", "") or "")
        endpoint_name = str(getattr(endpoint, "name", None) or endpoint_id)
        try:
            configs = list(store.list_endpoint_guardrail_configs(endpoint_id=endpoint_id))
        except Exception:  # per-endpoint read failed — show the endpoint with no coverage
            configs = []
        coverage.append(
            {
                "endpoint": endpoint_name,
                "endpoint_id": endpoint_id,
                "guardrails": [
                    {
                        "guardrail_id": str(getattr(c, "guardrail_id", "") or ""),
                        "name": name_by_id.get(str(getattr(c, "guardrail_id", "") or ""), ""),
                        "execution_order": getattr(c, "execution_order", None),
                        "enabled": bool(getattr(c, "enabled", True)),
                    }
                    for c in configs
                ],
            }
        )
    return {"guardrails": guardrails, "coverage": coverage}


class _GuardrailBuildError(Exception):
    """A bad create request (unknown type / missing required config) — maps to 400."""


def _slugify_scorer_name(name: str) -> str:
    """A scorer name MLflow accepts, derived from the human guardrail name."""
    slug = re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_")
    return slug or "guardrail_scorer"


def _build_scorer(scorer_type: str, guardrail_name: str, config: dict[str, Any]) -> Any:
    """Construct a native built-in scorer from a template type + form config.

    Imports are local: these classes are pure-Python (no ``guardrails-ai``), so
    they always import in CALIBER's venv. Raises ``_GuardrailBuildError`` on bad
    input so the route returns 400 rather than 502.
    """
    from mlflow.genai.scorers.builtin_scorers import (  # noqa: PLC0415
        Guidelines,
        PIIDetection,
        RegexMatch,
        Safety,
    )

    raw_name = str(config.get("scorer_name") or "").strip()
    scorer_name = _slugify_scorer_name(raw_name or guardrail_name)
    try:
        if scorer_type == "pii":
            types = config.get("pii_types") or None
            if types is not None and not isinstance(types, list):
                raise _GuardrailBuildError("'pii_types' must be a list")
            return PIIDetection(name=scorer_name, pii_types=types)
        if scorer_type == "toxicity":
            return Safety(name=scorer_name, model=(config.get("model") or None))  # type: ignore[no-untyped-call]
        if scorer_type == "guidelines":
            raw = config.get("guidelines")
            if isinstance(raw, str):
                lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            elif isinstance(raw, list):
                lines = [str(ln).strip() for ln in raw if str(ln).strip()]
            else:
                lines = []
            if not lines:
                raise _GuardrailBuildError("'guidelines' is required for a custom guardrail")
            return Guidelines(
                name=scorer_name, guidelines=lines, model=(config.get("model") or None)
            )
        if scorer_type == "regex":
            pattern = str(config.get("pattern") or "").strip()
            if not pattern:
                raise _GuardrailBuildError("'pattern' is required for a regex guardrail")
            match_type = "fullmatch" if config.get("match_type") == "fullmatch" else "search"
            return RegexMatch(
                name=scorer_name,
                pattern=pattern,
                match_type=match_type,  # type: ignore[arg-type]
                case_insensitive=bool(config.get("case_insensitive", False)),
            )
    except _GuardrailBuildError:
        raise
    except Exception as exc:  # pydantic / regex validation inside the scorer
        raise _GuardrailBuildError(_err(exc)) from exc
    raise _GuardrailBuildError(f"unknown scorer_type {scorer_type!r}")


def _guardrail_experiment_id(store: Any, config: Any) -> str:
    """Resolve the experiment to register guardrail scorers under (id, not name).

    Gateway-guardrail scorers must reference a real experiment id. Use CALIBER's
    configured tracing experiment when it exists; otherwise the default ("0").
    """
    name = str(getattr(config, "tracing_experiment", "") or "").strip()
    if name:
        try:
            exp = store.get_experiment_by_name(name)
            if exp is not None:
                return str(exp.experiment_id)
        except Exception:  # noqa: S110 — store too old / lookup unsupported; default below
            pass
    return "0"


def _scorer_ref(store: Any, registered: Any, experiment_id: str, name: str) -> tuple[str, int]:
    """Pull (scorer_id, version) off a register_scorer result, looking it up if absent."""
    scorer_id = getattr(registered, "scorer_id", None)
    version = getattr(registered, "scorer_version", None)
    if scorer_id and version is not None:
        return str(scorer_id), int(version)
    got = store.get_scorer(experiment_id, name)
    return str(got.scorer_id), int(got.scorer_version)


def _collect_scorers(store: Any, config: Any) -> list[dict[str, Any]]:
    """List registered scorers usable to back a guardrail (deduped by scorer_id)."""
    exp_ids = []
    resolved = _guardrail_experiment_id(store, config)
    for exp_id in (resolved, "0"):
        if exp_id not in exp_ids:
            exp_ids.append(exp_id)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for exp_id in exp_ids:
        try:
            versions = store.list_scorers(exp_id)
        except Exception:  # noqa: S112 — experiment missing / API too old; skip it
            continue
        for sv in versions:
            sid = str(getattr(sv, "scorer_id", "") or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append(
                {
                    "name": str(getattr(sv, "scorer_name", "") or ""),
                    "scorer_id": sid,
                    "version": int(getattr(sv, "scorer_version", 1) or 1),
                }
            )
    return out


def _create_guardrail(store: Any, config: Any, payload: GatewayGuardrailCreateRequest) -> dict[str, Any]:
    """Register the scorer (if a template) and create the gateway guardrail (blocking)."""
    from mlflow.entities.gateway_guardrail import (  # noqa: PLC0415
        GuardrailAction,
        GuardrailStage,
    )

    if payload.scorer_type:
        scorer = _build_scorer(payload.scorer_type, payload.name, payload.config)
        experiment_id = _guardrail_experiment_id(store, config)
        serialized = json.dumps(scorer.model_dump())
        registered = store.register_scorer(experiment_id, scorer.name, serialized)
        scorer_id, scorer_version = _scorer_ref(store, registered, experiment_id, scorer.name)
    else:
        scorer_id = str(payload.scorer_id)
        scorer_version = int(payload.scorer_version or 1)

    guardrail = store.create_gateway_guardrail(
        name=payload.name,
        scorer_id=scorer_id,
        scorer_version=scorer_version,
        stage=GuardrailStage[payload.stage],
        action=GuardrailAction[payload.action],
        action_endpoint_id=(payload.action_endpoint_id or None),
    )
    return _map_guardrail(guardrail)


async def get_gateway_guardrail_catalog(request: Request) -> JSONResponse:
    require_user(request)
    config = getattr(request.app.state, "config", None)
    try:
        store = _gateway_store()
    except Exception as exc:  # MLflow not importable / no tracking client
        return envelope_response(
            GatewayGuardrailCatalogSchema.model_validate(
                {
                    "configured": False,
                    "reachable": False,
                    "templates": _GUARDRAIL_TEMPLATES,
                    "scorers": [],
                    "error": _err(exc),
                }
            )
        )
    scorers: list[dict[str, Any]] = []
    reachable = True
    error: str | None = None
    try:
        scorers = await run_in_threadpool(_collect_scorers, store, config)
    except Exception as exc:  # scorer API unavailable — templates still usable
        reachable = False
        error = _err(exc)
    return envelope_response(
        GatewayGuardrailCatalogSchema.model_validate(
            {
                "configured": True,
                "reachable": reachable,
                "templates": _GUARDRAIL_TEMPLATES,
                "scorers": scorers,
                "error": error,
            }
        )
    )


async def get_gateway_guardrails(request: Request) -> JSONResponse:
    require_user(request)
    try:
        store = _gateway_store()
    except Exception as exc:  # MLflow not importable / no tracking client
        return envelope_response(
            GatewayGuardrailsStatusSchema(configured=False, reachable=False, error=_err(exc))
        )
    try:
        data = await run_in_threadpool(_collect_guardrails, store)
    except Exception as exc:  # API too old / gateway not enabled / unreachable
        logger.warning("gateway-guardrail API unavailable: %s", _err(exc))
        return envelope_response(
            GatewayGuardrailsStatusSchema(
                configured=True,
                reachable=False,
                error=(
                    f"{_err(exc)} — the gateway-guardrail API needs MLflow >=3.13 "
                    "with the gateway enabled on the tracking server"
                ),
            )
        )
    return envelope_response(
        GatewayGuardrailsStatusSchema.model_validate(
            {"configured": True, "reachable": True, **data}
        )
    )


async def create_gateway_guardrail(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    body = await parse_json_object(request)
    payload = GatewayGuardrailCreateRequest.model_validate(body)
    if payload.scorer_type and payload.scorer_type not in _TEMPLATE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown scorer_type {payload.scorer_type!r}; expected one of "
            f"{sorted(_TEMPLATE_TYPES)}",
        )
    config = getattr(request.app.state, "config", None)
    try:
        store = _gateway_store()
        result = await run_in_threadpool(_create_guardrail, store, config, payload)
    except _GuardrailBuildError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"gateway guardrail create failed: {_err(exc)}"
        ) from exc
    factory = get_session_factory(request)
    with factory() as session:
        audit_record(
            session,
            actor=actor,
            action="create_gateway_guardrail",
            entity_type="gateway_guardrail",
            entity_id=str(result.get("guardrail_id", "")),
            details={
                "name": payload.name,
                "stage": payload.stage,
                "action": payload.action,
                "scorer_type": payload.scorer_type,
                "scorer_id": result.get("scorer") or payload.scorer_id,
            },
        )
        session.commit()
    return envelope_response_dict(result, status_code=201)


async def delete_gateway_guardrail(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    guardrail_id = request.path_params["guardrail_id"]
    try:
        store = _gateway_store()
        await run_in_threadpool(lambda: store.delete_gateway_guardrail(guardrail_id))
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"gateway guardrail delete failed: {_err(exc)}"
        ) from exc
    factory = get_session_factory(request)
    with factory() as session:
        audit_record(
            session,
            actor=actor,
            action="delete_gateway_guardrail",
            entity_type="gateway_guardrail",
            entity_id=guardrail_id,
            details={},
        )
        session.commit()
    return envelope_response_dict({"guardrail_id": guardrail_id, "deleted": True})


async def attach_gateway_guardrail(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    endpoint_id = request.path_params["endpoint_id"]
    body = await parse_json_object(request)
    payload = GatewayGuardrailAttachRequest.model_validate(body)
    order = payload.execution_order if payload.execution_order is not None else 0
    try:
        store = _gateway_store()
        await run_in_threadpool(
            lambda: store.add_guardrail_to_endpoint(
                endpoint_id=endpoint_id,
                guardrail_id=payload.guardrail_id,
                execution_order=order,
            )
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"gateway guardrail attach failed: {_err(exc)}"
        ) from exc
    factory = get_session_factory(request)
    with factory() as session:
        audit_record(
            session,
            actor=actor,
            action="attach_gateway_guardrail",
            entity_type="gateway_endpoint",
            entity_id=endpoint_id,
            details={"guardrail_id": payload.guardrail_id, "execution_order": order},
        )
        session.commit()
    return envelope_response_dict(
        {"endpoint_id": endpoint_id, "guardrail_id": payload.guardrail_id, "attached": True},
        status_code=201,
    )


async def detach_gateway_guardrail(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    endpoint_id = request.path_params["endpoint_id"]
    guardrail_id = request.path_params["guardrail_id"]
    try:
        store = _gateway_store()
        await run_in_threadpool(
            lambda: store.remove_guardrail_from_endpoint(
                endpoint_id=endpoint_id, guardrail_id=guardrail_id
            )
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"gateway guardrail detach failed: {_err(exc)}"
        ) from exc
    factory = get_session_factory(request)
    with factory() as session:
        audit_record(
            session,
            actor=actor,
            action="detach_gateway_guardrail",
            entity_type="gateway_endpoint",
            entity_id=endpoint_id,
            details={"guardrail_id": guardrail_id},
        )
        session.commit()
    return envelope_response_dict(
        {"endpoint_id": endpoint_id, "guardrail_id": guardrail_id, "detached": True}
    )


async def update_gateway_guardrail_config(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    endpoint_id = request.path_params["endpoint_id"]
    guardrail_id = request.path_params["guardrail_id"]
    body = await parse_json_object(request)
    payload = GatewayGuardrailConfigUpdateRequest.model_validate(body)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="request body must include at least one field")
    try:
        store = _gateway_store()
        await run_in_threadpool(
            lambda: store.update_endpoint_guardrail_config(
                endpoint_id=endpoint_id, guardrail_id=guardrail_id, **changes
            )
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"gateway guardrail update failed: {_err(exc)}"
        ) from exc
    factory = get_session_factory(request)
    with factory() as session:
        audit_record(
            session,
            actor=actor,
            action="update_gateway_guardrail_config",
            entity_type="gateway_endpoint",
            entity_id=endpoint_id,
            details={"guardrail_id": guardrail_id, "changed": sorted(changes)},
        )
        session.commit()
    return envelope_response_dict(
        {"endpoint_id": endpoint_id, "guardrail_id": guardrail_id, **changes}
    )


# --- Usage (trace-derived) ----------------------------------------------------


async def get_gateway_usage(request: Request) -> JSONResponse:
    require_user(request)
    config = getattr(request.app.state, "config", None)
    configured_experiment = str(getattr(config, "tracing_experiment", "") or "") if config else ""
    experiment_filter = request.query_params.get("experiment_id", "").strip()
    raw_since = request.query_params.get("since_ms")
    since_ms: int | None = None
    if raw_since:
        try:
            since_ms = int(raw_since)
        except ValueError:
            since_ms = None
    payload = gateway_usage_payload(
        experiment_filter=experiment_filter,
        configured=configured_experiment,
        since_ms=since_ms,
    )
    return envelope_response_dict(payload)


def register(app: Starlette) -> None:
    app.routes.append(Route(STATUS_PATH, get_gateway, methods=["GET"]))
    # Catalog before the {guardrail_id} param route so "catalog" isn't captured as an id.
    app.routes.append(
        Route(GUARDRAILS_CATALOG_PATH, get_gateway_guardrail_catalog, methods=["GET"])
    )
    app.routes.append(Route(GUARDRAILS_PATH, get_gateway_guardrails, methods=["GET"]))
    app.routes.append(Route(GUARDRAILS_PATH, create_gateway_guardrail, methods=["POST"]))
    app.routes.append(Route(GUARDRAIL_PATH, delete_gateway_guardrail, methods=["DELETE"]))
    app.routes.append(Route(ENDPOINT_GUARDRAILS_PATH, attach_gateway_guardrail, methods=["POST"]))
    app.routes.append(
        Route(ENDPOINT_GUARDRAIL_PATH, detach_gateway_guardrail, methods=["DELETE"])
    )
    app.routes.append(
        Route(ENDPOINT_GUARDRAIL_PATH, update_gateway_guardrail_config, methods=["PATCH"])
    )
    app.routes.append(Route(USAGE_PATH, get_gateway_usage, methods=["GET"]))
