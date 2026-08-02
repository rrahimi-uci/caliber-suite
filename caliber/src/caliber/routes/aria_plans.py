"""``/caliber/aria/plans`` endpoints — Aria goal-plans (agentic orchestration).

Implemented surface: decompose a goal into a plan, inspect and edit draft plans,
approve them, execute or resume them, poll async steps, and answer mid-run
interactions.

* ``GET /aria/plans`` — list the caller's plans.
* ``POST /aria/plans`` — decompose a goal into a draft plan.
* ``GET /aria/plans/{plan_id}`` — plan + its steps.
* ``PATCH /aria/plans/{plan_id}`` — edit autonomy / cancel (draft only).
* ``POST /aria/plans/{plan_id}/approve`` — approve the plan shape.
* ``POST /aria/plans/{plan_id}/execute`` — execute or resume the plan.
* ``POST /aria/plans/{plan_id}/poll`` — advance steps waiting on async jobs.
* ``GET /aria/plans/{plan_id}/interactions`` — list plan interactions.
* ``POST /aria/interactions/{interaction_id}/answer`` — answer and resume.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.assistant.executor import (
    MLflowJobStatusResolver,
    PlanExecutionError,
    PlanExecutor,
    PlanForbiddenError,
)
from caliber.assistant.plans import PlanService
from caliber.auth import require_user, resolve_identity
from caliber.routes._deps import (
    envelope_response,
    envelope_response_dict,
    get_session_factory,
    list_limit,
    parse_json_object,
)
from caliber.schemas import (
    AriaInteractionAnswerRequest,
    AriaInteractionSchema,
    AriaPlanCreateRequest,
    AriaPlanUpdateRequest,
)

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/aria/plans"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/aria/plans/{plan_id}"
APPROVE_PATH = "/ajax-api/2.0/mlflow/caliber/aria/plans/{plan_id}/approve"
EXECUTE_PATH = "/ajax-api/2.0/mlflow/caliber/aria/plans/{plan_id}/execute"
POLL_PATH = "/ajax-api/2.0/mlflow/caliber/aria/plans/{plan_id}/poll"
INTERACTIONS_PATH = "/ajax-api/2.0/mlflow/caliber/aria/plans/{plan_id}/interactions"
ANSWER_PATH = "/ajax-api/2.0/mlflow/caliber/aria/interactions/{interaction_id}/answer"
CAPABILITIES_PATH = "/ajax-api/2.0/mlflow/caliber/aria/capabilities"

_service = PlanService()
_executor = PlanExecutor(_service)
_job_resolver = MLflowJobStatusResolver()


def _config(request: Request) -> object | None:
    return getattr(request.app.state, "config", None)


async def list_plans(request: Request) -> JSONResponse:
    actor = require_user(request)
    factory = get_session_factory(request)
    session_id = request.query_params.get("session_id") or None
    limit, offset = list_limit(request)
    plans = _service.list_plans(
        session_factory=factory, owner=actor, session_id=session_id, limit=limit, offset=offset
    )
    return envelope_response(plans)


async def create_plan(request: Request) -> JSONResponse:
    actor = require_user(request)
    body = await parse_json_object(request)
    payload = AriaPlanCreateRequest.model_validate(body)
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    detail = _service.create_plan(
        session_factory=factory,
        goal=payload.goal,
        owner=actor,
        session_id=payload.session_id,
        project_id=identity.active_project_id,
        autonomy=payload.autonomy,
        constraints=payload.constraints,
        done_when=payload.done_when,
        context_refs=payload.context_refs,
    )
    return envelope_response_dict(detail, status_code=201)


async def get_plan(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    plan_id = request.path_params["plan_id"]
    factory = get_session_factory(request)
    detail = _service.get_plan(session_factory=factory, plan_id=plan_id, identity=identity)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"aria plan {plan_id!r} not found")
    return envelope_response_dict(detail)


async def update_plan(request: Request) -> JSONResponse:
    actor = require_user(request)
    identity = resolve_identity(request)
    plan_id = request.path_params["plan_id"]
    body = await parse_json_object(request)
    payload = AriaPlanUpdateRequest.model_validate(body)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="request body must include at least one field")
    factory = get_session_factory(request)

    # Status edits go through set_status; autonomy edits patch the row in place.
    detail = _service.get_plan(session_factory=factory, plan_id=plan_id, identity=identity)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"aria plan {plan_id!r} not found")
    if detail["plan"]["status"] != "draft":
        raise HTTPException(status_code=409, detail="only draft plans can be edited")

    if "autonomy" in changes:
        # Relaxing the autonomy dial is security-relevant — route it through the
        # service so it is owner-scoped, race-safe (404 not 500 on a delete
        # race), and audited rather than a bare in-place mutation.
        updated = _service.set_autonomy(
            session_factory=factory, plan_id=plan_id, autonomy=changes["autonomy"], actor=actor
        )
        if updated is None:
            raise HTTPException(status_code=404, detail=f"aria plan {plan_id!r} not found")
    if changes.get("status") == "cancelled":
        _service.set_status(
            session_factory=factory, plan_id=plan_id, status="cancelled", actor=actor
        )

    detail = _service.get_plan(session_factory=factory, plan_id=plan_id, identity=identity)
    assert detail is not None
    return envelope_response_dict(detail)


async def approve_plan(request: Request) -> JSONResponse:
    actor = require_user(request)
    identity = resolve_identity(request)
    plan_id = request.path_params["plan_id"]
    factory = get_session_factory(request)
    detail = _service.get_plan(session_factory=factory, plan_id=plan_id, identity=identity)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"aria plan {plan_id!r} not found")
    if detail["plan"]["status"] != "draft":
        raise HTTPException(
            status_code=409, detail=f"plan is {detail['plan']['status']!r}, not draft"
        )
    updated = _service.set_status(
        session_factory=factory, plan_id=plan_id, status="approved", actor=actor
    )
    assert updated is not None
    return envelope_response_dict(updated)


async def execute_plan(request: Request) -> JSONResponse:
    actor = require_user(request)
    plan_id = request.path_params["plan_id"]
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    detail = _service.get_plan(session_factory=factory, plan_id=plan_id, identity=identity)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"aria plan {plan_id!r} not found")
    if detail["plan"]["status"] not in ("approved", "paused", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"plan is {detail['plan']['status']!r}; approve it before executing",
        )
    result = _executor.execute(
        session_factory=factory,
        config=_config(request),
        actor=actor,
        plan_id=plan_id,
        project_id=identity.active_project_id,
    )
    return envelope_response_dict(result if result is not None else detail)


async def poll_plan(request: Request) -> JSONResponse:
    actor = require_user(request)
    plan_id = request.path_params["plan_id"]
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    if _service.get_plan(session_factory=factory, plan_id=plan_id, identity=identity) is None:
        raise HTTPException(status_code=404, detail=f"aria plan {plan_id!r} not found")
    result = _executor.poll(
        session_factory=factory,
        config=_config(request),
        plan_id=plan_id,
        resolver=_job_resolver,
        actor=actor,
        project_id=identity.active_project_id,
    )
    return envelope_response_dict(result if result is not None else {})


async def list_interactions(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    plan_id = request.path_params["plan_id"]
    factory = get_session_factory(request)
    # Gate on plan visibility so a guessed plan_id can't read another user's
    # interaction history.
    if _service.get_plan(session_factory=factory, plan_id=plan_id, identity=identity) is None:
        raise HTTPException(status_code=404, detail=f"aria plan {plan_id!r} not found")
    limit, offset = list_limit(request)
    rows = _executor.list_interactions(
        session_factory=factory, plan_id=plan_id, pending_only=False, limit=limit, offset=offset
    )
    return envelope_response([AriaInteractionSchema.model_validate(r) for r in rows])


async def answer_interaction(request: Request) -> JSONResponse:
    actor = require_user(request)
    interaction_id = request.path_params["interaction_id"]
    body = await parse_json_object(request)
    payload = AriaInteractionAnswerRequest.model_validate(body)
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    # For a permission/confirm ask, approval defaults to the explicit flag; a
    # choice/input answer is treated as approval-to-proceed.
    approved = (
        bool(payload.approved)
        if payload.approved is not None
        else (payload.choice is not None or payload.value is not None or payload.inputs is not None)
    )
    extra = {
        k: v
        for k, v in {
            "choice": payload.choice,
            "value": payload.value,
            "inputs": payload.inputs,
        }.items()
        if v is not None
    }
    try:
        result = _executor.answer(
            session_factory=factory,
            config=_config(request),
            actor=actor,
            interaction_id=interaction_id,
            approved=approved,
            response=extra,
            project_id=identity.active_project_id,
        )
    except PlanForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PlanExecutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="plan not found after answer")
    return envelope_response_dict(result)




async def list_capabilities(request: Request) -> JSONResponse:
    """``GET /caliber/aria/capabilities`` — what Aria can actually execute.

    The drafting UI is broad and the executable registry is not: seven built-ins.
    Nothing surfaced that number, so the envelope a user infers from the panel is
    wider than the one the product implements — which is the same
    claim-exceeds-behaviour pattern this codebase has been removing elsewhere,
    just pointed at the user instead of the operator.

    Read-only and unfiltered by scope on purpose. This is a description of the
    product's capability surface, not an authorization decision; a viewer asking
    "what could this do?" should get the honest answer, and the capability's own
    ``required_scopes`` travels with it so the UI can show what the caller may
    actually invoke.
    """
    require_user(request)
    from caliber.assistant.capabilities import registered_capabilities  # noqa: PLC0415

    capabilities = [
        {
            "key": capability.key,
            "title": capability.title,
            "description": capability.description,
            "tier": capability.tier,
            "required_scopes": list(capability.required_scopes),
            "input_schema": {
                "properties": dict(capability.properties),
                "required": list(capability.required),
            },
        }
        for capability in sorted(registered_capabilities(), key=lambda item: item.key)
    ]
    return envelope_response_dict({"capabilities": capabilities, "count": len(capabilities)})


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_plans, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, create_plan, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH, get_plan, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, update_plan, methods=["PATCH"]))
    app.routes.append(Route(APPROVE_PATH, approve_plan, methods=["POST"]))
    app.routes.append(Route(EXECUTE_PATH, execute_plan, methods=["POST"]))
    app.routes.append(Route(POLL_PATH, poll_plan, methods=["POST"]))
    app.routes.append(Route(INTERACTIONS_PATH, list_interactions, methods=["GET"]))
    app.routes.append(Route(ANSWER_PATH, answer_interaction, methods=["POST"]))
    app.routes.append(Route(CAPABILITIES_PATH, list_capabilities, methods=["GET"]))
