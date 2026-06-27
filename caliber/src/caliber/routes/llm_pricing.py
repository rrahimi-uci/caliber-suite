"""``/caliber/llm-pricing`` endpoints — per-model LLM token pricing.

A pricing row is the operator-authored cost rate for one ``(provider, model)``:
USD per 1K prompt / completion / cached-prompt tokens. CALIBER computes the
``cost_usd`` it records on trace spans + refinement jobs from a per-model price
table; these rows override / extend the built-in
:data:`caliber.observability.mlflow_tracing.DEFAULT_MODEL_PRICING`, so operators
can correct rates or add models without a code change. They power the LLM
Gateway page's Pricing tab + the trace-derived usage/cost graphs.

Surface:

* ``GET /llm-pricing`` — list (filterable by status).
* ``POST /llm-pricing`` (operator) — create.
* ``GET /llm-pricing/{pricing_id}`` — single row.
* ``PATCH /llm-pricing/{pricing_id}`` (admin) — update / archive.
"""

from __future__ import annotations

from sqlalchemy import select
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import (
    SCOPE_ADMIN,
    SCOPE_OPERATOR,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.db.models import CaliberLlmModelPricing
from caliber.db.scoping import apply_visibility_filter, get_visible
from caliber.ids import new_llm_pricing_id
from caliber.observability.mlflow_tracing import invalidate_pricing_cache
from caliber.routes._deps import (
    envelope_response,
    get_session_factory,
    list_limit,
    parse_json_object,
    visibility_param,
)
from caliber.schemas import (
    LlmPricingCreateRequest,
    LlmPricingSchema,
    LlmPricingUpdateRequest,
)

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/llm-pricing"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/llm-pricing/{pricing_id}"

_LIST_STATUS_VALUES: frozenset[str] = frozenset({"active", "archived", "all"})
_UPDATABLE_FIELDS = (
    "provider",
    "model_id",
    "prompt_price",
    "completion_price",
    "cached_prompt_price",
    "tags",
    "status",
)


async def list_pricing(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    requested_status = request.query_params.get("status", "active")
    if requested_status not in _LIST_STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid value for 'status': {requested_status!r}; "
                f"expected one of {sorted(_LIST_STATUS_VALUES)}"
            ),
        )
    factory = get_session_factory(request)
    limit, offset = list_limit(request)
    with factory() as session:
        stmt = select(CaliberLlmModelPricing).order_by(
            CaliberLlmModelPricing.provider, CaliberLlmModelPricing.model_id
        )
        if requested_status != "all":
            stmt = stmt.where(CaliberLlmModelPricing.status == requested_status)
        stmt = apply_visibility_filter(
            stmt,
            CaliberLlmModelPricing,
            identity,
            identity.active_project_id,
            only=visibility_param(request),
        )
        rows = session.execute(stmt.limit(limit).offset(offset)).scalars().all()
    items = [LlmPricingSchema.model_validate(row) for row in rows]
    return envelope_response(items)


async def get_pricing(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    pricing_id = request.path_params["pricing_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = get_visible(
            session, CaliberLlmModelPricing, CaliberLlmModelPricing.pricing_id, pricing_id, identity
        )
    if row is None:
        raise HTTPException(status_code=404, detail=f"pricing {pricing_id!r} not found")
    return envelope_response(LlmPricingSchema.model_validate(row))


async def create_pricing(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = LlmPricingCreateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    factory = get_session_factory(request)
    with factory() as session:
        existing = (
            session.execute(
                select(CaliberLlmModelPricing).where(
                    CaliberLlmModelPricing.provider == payload.provider,
                    CaliberLlmModelPricing.model_id == payload.model_id,
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"pricing for {payload.provider}/{payload.model_id} already exists "
                    f"({existing.pricing_id!r}) — edit it instead"
                ),
            )
        pricing = CaliberLlmModelPricing(
            pricing_id=new_llm_pricing_id(),
            provider=payload.provider,
            model_id=payload.model_id,
            prompt_price=payload.prompt_price,
            completion_price=payload.completion_price,
            cached_prompt_price=payload.cached_prompt_price,
            owner=actor,
            project_id=identity.active_project_id,
            visibility="project" if identity.active_project_id else "user",
            tags=list(payload.tags),
            status="active",
        )
        session.add(pricing)
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="create_llm_pricing",
            entity_type="llm_pricing",
            entity_id=pricing.pricing_id,
            details={"provider": pricing.provider, "model_id": pricing.model_id},
        )
        session.commit()
        data = LlmPricingSchema.model_validate(pricing)
    invalidate_pricing_cache()  # new rate applies to subsequent cost attribution
    return envelope_response(data, status_code=201)


async def update_pricing(request: Request) -> JSONResponse:
    pricing_id = request.path_params["pricing_id"]
    body = await parse_json_object(request)
    payload = LlmPricingUpdateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN])

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="request body must include at least one field")

    factory = get_session_factory(request)
    with factory() as session:
        pricing = session.get(CaliberLlmModelPricing, pricing_id)
        if pricing is None:
            raise HTTPException(status_code=404, detail=f"pricing {pricing_id!r} not found")

        diff: dict[str, dict[str, object]] = {}
        for field in _UPDATABLE_FIELDS:
            if field not in changes:
                continue
            new_value = changes[field]
            old_value = getattr(pricing, field)
            if new_value != old_value:
                diff[field] = {"from": old_value, "to": new_value}
                setattr(pricing, field, new_value)

        if not diff:
            return envelope_response(LlmPricingSchema.model_validate(pricing))

        audit_record(
            session,
            actor=actor,
            action="update_llm_pricing",
            entity_type="llm_pricing",
            entity_id=pricing_id,
            details={"changed": sorted(diff)},
        )
        session.commit()
        data = LlmPricingSchema.model_validate(pricing)
    invalidate_pricing_cache()  # edited rate applies to subsequent cost attribution
    return envelope_response(data)


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_pricing, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, create_pricing, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH, get_pricing, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, update_pricing, methods=["PATCH"]))
