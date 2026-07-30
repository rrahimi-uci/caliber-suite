"""Deploy-workflow-as-a-service routes.

Two surfaces:

* **Internal** (operator/admin, normal ``X-CALIBER-User`` auth) — publish/read/
  unpublish a workflow's service config and mint/list/revoke its Bearer tokens.
* **External** (``Authorization: Bearer <service-token>`` auth) — invoke the
  service (async run-and-poll) and poll a run's status.

Invocation enqueues a queued :class:`CaliberWorkflowRun` via the shared
``enqueue_workflow_run`` path (the same insert the trigger endpoint and scheduler
use); the existing worker picks it up. This module never runs the workflow itself.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, cast

import jsonschema
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_OPERATOR, require_scopes, require_user, resolve_identity
from caliber.db.models import (
    CaliberServiceToken,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowRun,
    CaliberWorkflowService,
    CaliberWorkflowVersion,
)
from caliber.db.scoping import get_visible
from caliber.ids import new_service_id, new_service_token_id
from caliber.routes._deps import (
    envelope_response,
    envelope_response_dict,
    get_session_factory,
    parse_json_object,
)
from caliber.schemas import (
    ServiceInvokeRequest,
    ServiceInvokeResponse,
    ServiceRunStatusSchema,
    ServiceTokenCreatedSchema,
    ServiceTokenCreateRequest,
    ServiceTokenSchema,
    WorkflowServicePublishRequest,
    WorkflowServiceSchema,
)
from caliber.workflows.run_launch import enqueue_workflow_run
from caliber.workflows.run_state import (
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_QUEUED,
)
from caliber.workflows.service_schema import derive_service_schemas

PREFIX = "/ajax-api/2.0/mlflow/caliber"
SERVICE_PATH = PREFIX + "/workflows/{workflow_id}/service"
SERVICE_TOKENS_PATH = SERVICE_PATH + "/tokens"
SERVICE_TOKEN_DETAIL_PATH = SERVICE_TOKENS_PATH + "/{token_id}"
INTERNAL_OPENAPI_PATH = SERVICE_PATH + "/openapi.json"
INVOKE_PATH = PREFIX + "/services/{workflow_id}/invoke"
RUN_STATUS_PATH = PREFIX + "/services/{workflow_id}/runs/{run_id}"
OPENAPI_PATH = PREFIX + "/services/{workflow_id}/openapi.json"

DEFAULT_ALIAS = "prod"
INVOKE_SCOPE = "invoke"
_TOKEN_PLAINTEXT_PREFIX = "cal_svc_"  # noqa: S105 — token namespace prefix, not a credential

logger = logging.getLogger("caliber.routes.services")


def _hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _emit_queue_event(request: Request, payload: dict[str, object]) -> None:
    publish = getattr(getattr(request.app.state, "event_bus", None), "publish", None)
    if callable(publish):
        try:
            publish(payload)
        except Exception:  # best-effort fanout; never fail the request
            logger.warning("failed to publish service queue event", exc_info=True)


def _active_deployment(
    session: Session, workflow_id: str, alias: str
) -> CaliberWorkflowDeployment | None:
    return (
        session.execute(
            select(CaliberWorkflowDeployment)
            .where(
                CaliberWorkflowDeployment.workflow_id == workflow_id,
                CaliberWorkflowDeployment.alias == alias,
                CaliberWorkflowDeployment.status == "active",
            )
            .order_by(CaliberWorkflowDeployment.deployed_at.desc())
        )
        .scalars()
        .first()
    )


def _token_count(session: Session, workflow_id: str) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(CaliberServiceToken)
            .where(CaliberServiceToken.workflow_id == workflow_id)
        ).scalar_one()
    )


def _service_schema(service: CaliberWorkflowService, token_count: int) -> WorkflowServiceSchema:
    return WorkflowServiceSchema(
        service_id=service.service_id,
        workflow_id=service.workflow_id,
        alias=service.alias,
        input_schema=dict(service.input_schema or {}),
        output_schema=dict(service.output_schema or {}),
        enabled=service.enabled,
        auth_required=service.auth_required,
        rate_limit_per_minute=service.rate_limit_per_minute,
        cors_allowed_origins=service.cors_allowed_origins,
        endpoint=INVOKE_PATH.format(workflow_id=service.workflow_id),
        created_by=service.created_by,
        created_at=service.created_at,
        updated_at=service.updated_at,
        token_count=token_count,
    )


def _get_authorized_workflow(
    session: Session, request: Request, workflow_id: str
) -> CaliberWorkflow:
    """Resolve the parent workflow through the same visibility contract as Studio.

    Service configuration and token routes are nested below a workflow, so scope
    checks on the caller are not enough: an operator must also be able to see the
    workflow named by the URL. Returning 404 for an out-of-scope parent keeps the
    list/detail/mutation boundary consistent without disclosing that it exists.
    """
    identity = resolve_identity(request)
    workflow = get_visible(
        session,
        CaliberWorkflow,
        CaliberWorkflow.workflow_id,
        workflow_id,
        identity,
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")
    return cast(CaliberWorkflow, workflow)


# ---------------------------------------------------------------------------
# Internal routes (operator/admin, user-header auth).
# ---------------------------------------------------------------------------


async def publish_service(request: Request) -> JSONResponse:
    workflow_id = request.path_params["workflow_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    body = await parse_json_object(request, allow_empty=True)
    payload = WorkflowServicePublishRequest.model_validate(body)
    alias = payload.alias or DEFAULT_ALIAS
    factory = get_session_factory(request)
    with factory() as session:
        _get_authorized_workflow(session, request, workflow_id)
        deployment = _active_deployment(session, workflow_id, alias)
        if deployment is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"publishing a service requires a live deployment for workflow "
                    f"{workflow_id!r} alias {alias!r}"
                ),
            )
        version = session.get(CaliberWorkflowVersion, deployment.version_id)
        if version is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"deployment {deployment.deployment_id!r} points to missing version "
                    f"{deployment.version_id!r}"
                ),
            )
        derived_in, derived_out = derive_service_schemas(version.manifest)
        input_schema = payload.input_schema if payload.input_schema is not None else derived_in
        output_schema = payload.output_schema if payload.output_schema is not None else derived_out

        service = (
            session.execute(
                select(CaliberWorkflowService).where(
                    CaliberWorkflowService.workflow_id == workflow_id
                )
            )
            .scalars()
            .first()
        )
        if service is None:
            service = CaliberWorkflowService(
                service_id=new_service_id(),
                workflow_id=workflow_id,
                alias=alias,
                input_schema=input_schema,
                output_schema=output_schema,
                enabled=payload.enabled if payload.enabled is not None else True,
                auth_required=(
                    payload.auth_required if payload.auth_required is not None else True
                ),
                rate_limit_per_minute=payload.rate_limit_per_minute or 0,
                cors_allowed_origins=payload.cors_allowed_origins or "",
                created_by=actor,
            )
            session.add(service)
            action = "publish_workflow_service"
        else:
            service.alias = alias
            service.input_schema = input_schema
            service.output_schema = output_schema
            if payload.enabled is not None:
                service.enabled = payload.enabled
            if payload.auth_required is not None:
                service.auth_required = payload.auth_required
            if payload.rate_limit_per_minute is not None:
                service.rate_limit_per_minute = payload.rate_limit_per_minute
            if payload.cors_allowed_origins is not None:
                service.cors_allowed_origins = payload.cors_allowed_origins
            action = "update_workflow_service"
        session.flush()
        audit_record(
            session,
            actor=actor,
            action=action,
            entity_type="workflow_service",
            entity_id=service.service_id,
            details={"workflow_id": workflow_id, "alias": alias},
        )
        token_count = _token_count(session, workflow_id)
        session.commit()
        data = _service_schema(service, token_count)
    return envelope_response(data)


async def get_service(request: Request) -> JSONResponse:
    workflow_id = request.path_params["workflow_id"]
    require_user(request)
    factory = get_session_factory(request)
    with factory() as session:
        _get_authorized_workflow(session, request, workflow_id)
        service = (
            session.execute(
                select(CaliberWorkflowService).where(
                    CaliberWorkflowService.workflow_id == workflow_id
                )
            )
            .scalars()
            .first()
        )
        if service is None:
            raise HTTPException(
                status_code=404, detail=f"no service published for workflow {workflow_id!r}"
            )
        token_count = _token_count(session, workflow_id)
        data = _service_schema(service, token_count)
    return envelope_response(data)


async def get_service_openapi(request: Request) -> JSONResponse:
    """Return a workflow service's raw OpenAPI spec through CALIBER auth.

    The external ``/services/{workflow_id}/openapi.json`` route intentionally
    follows the service's Bearer-token contract. Studio cannot attach that
    token to a plain browser navigation, so this parent-scoped read route uses
    the same user/project visibility contract as ``get_service`` and renders
    the exact same spec without weakening the external endpoint.
    """
    workflow_id = request.path_params["workflow_id"]
    require_user(request)
    factory = get_session_factory(request)
    with factory() as session:
        workflow = _get_authorized_workflow(session, request, workflow_id)
        service = (
            session.execute(
                select(CaliberWorkflowService).where(
                    CaliberWorkflowService.workflow_id == workflow_id
                )
            )
            .scalars()
            .first()
        )
        if service is None:
            raise HTTPException(
                status_code=404, detail=f"no service published for workflow {workflow_id!r}"
            )
        spec = _build_service_openapi_spec(
            workflow_id=workflow_id,
            title=workflow.name,
            service=service,
        )
    return JSONResponse(spec)


async def delete_service(request: Request) -> JSONResponse:
    workflow_id = request.path_params["workflow_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    factory = get_session_factory(request)
    with factory() as session:
        _get_authorized_workflow(session, request, workflow_id)
        service = (
            session.execute(
                select(CaliberWorkflowService).where(
                    CaliberWorkflowService.workflow_id == workflow_id
                )
            )
            .scalars()
            .first()
        )
        if service is None:
            raise HTTPException(
                status_code=404, detail=f"no service published for workflow {workflow_id!r}"
            )
        service_id = service.service_id
        # Drop the service and all its tokens.
        for token in (
            session.execute(
                select(CaliberServiceToken).where(CaliberServiceToken.workflow_id == workflow_id)
            )
            .scalars()
            .all()
        ):
            session.delete(token)
        session.delete(service)
        audit_record(
            session,
            actor=actor,
            action="unpublish_workflow_service",
            entity_type="workflow_service",
            entity_id=service_id,
            details={"workflow_id": workflow_id},
        )
        session.commit()
    return envelope_response_dict({"status": "unpublished"})


async def create_service_token(request: Request) -> JSONResponse:
    workflow_id = request.path_params["workflow_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    body = await parse_json_object(request)
    payload = ServiceTokenCreateRequest.model_validate(body)
    factory = get_session_factory(request)
    with factory() as session:
        _get_authorized_workflow(session, request, workflow_id)
        service = (
            session.execute(
                select(CaliberWorkflowService).where(
                    CaliberWorkflowService.workflow_id == workflow_id
                )
            )
            .scalars()
            .first()
        )
        if service is None:
            raise HTTPException(
                status_code=409,
                detail=f"no service published for workflow {workflow_id!r}; publish it first",
            )
        plaintext = _TOKEN_PLAINTEXT_PREFIX + secrets.token_urlsafe(32)
        token = CaliberServiceToken(
            token_id=new_service_token_id(),
            workflow_id=workflow_id,
            name=payload.name,
            token_hash=_hash_token(plaintext),
            prefix=plaintext[:16],
            scopes=list(payload.scopes),
            created_by=actor,
            expires_at=payload.expires_at,
        )
        session.add(token)
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="create_service_token",
            entity_type="service_token",
            entity_id=token.token_id,
            details={"workflow_id": workflow_id, "scopes": list(payload.scopes)},
        )
        session.commit()
        data = ServiceTokenCreatedSchema(
            token_id=token.token_id,
            name=token.name,
            prefix=token.prefix,
            scopes=list(token.scopes or []),
            created_by=token.created_by,
            created_at=token.created_at,
            expires_at=token.expires_at,
            revoked_at=token.revoked_at,
            token=plaintext,
        )
    return envelope_response(data, status_code=201)


async def list_service_tokens(request: Request) -> JSONResponse:
    workflow_id = request.path_params["workflow_id"]
    require_scopes(request, [SCOPE_OPERATOR])
    factory = get_session_factory(request)
    with factory() as session:
        _get_authorized_workflow(session, request, workflow_id)
        rows = (
            session.execute(
                select(CaliberServiceToken)
                .where(CaliberServiceToken.workflow_id == workflow_id)
                .order_by(CaliberServiceToken.created_at.desc())
            )
            .scalars()
            .all()
        )
        data = [
            ServiceTokenSchema(
                token_id=row.token_id,
                name=row.name,
                prefix=row.prefix,
                scopes=list(row.scopes or []),
                created_by=row.created_by,
                created_at=row.created_at,
                expires_at=row.expires_at,
                revoked_at=row.revoked_at,
            )
            for row in rows
        ]
    return envelope_response(data)


async def revoke_service_token(request: Request) -> JSONResponse:
    workflow_id = request.path_params["workflow_id"]
    token_id = request.path_params["token_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    factory = get_session_factory(request)
    with factory() as session:
        _get_authorized_workflow(session, request, workflow_id)
        token = session.get(CaliberServiceToken, token_id)
        if token is None or token.workflow_id != workflow_id:
            raise HTTPException(
                status_code=404,
                detail=f"service token {token_id!r} not found for workflow {workflow_id!r}",
            )
        if token.revoked_at is None:
            token.revoked_at = datetime.now(timezone.utc)
            audit_record(
                session,
                actor=actor,
                action="revoke_service_token",
                entity_type="service_token",
                entity_id=token.token_id,
                details={"workflow_id": workflow_id},
            )
        session.commit()
    return envelope_response_dict({"status": "revoked"})


# ---------------------------------------------------------------------------
# External routes (Bearer service-token auth).
# ---------------------------------------------------------------------------


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise HTTPException(status_code=401, detail="missing or malformed Bearer token")
    return value.strip()


def _load_enabled_service(request: Request, workflow_id: str) -> CaliberWorkflowService:
    """Load the published service for ``workflow_id`` (404 if none, 403 if disabled).

    Auth is not enforced here. Callers gate the Bearer check on
    :attr:`CaliberWorkflowService.auth_required` via
    :func:`_validate_service_token`; new services set that bit by default while
    legacy or explicitly public services may leave it disabled.
    """
    factory = get_session_factory(request)
    with factory() as session:
        service = (
            session.execute(
                select(CaliberWorkflowService).where(
                    CaliberWorkflowService.workflow_id == workflow_id
                )
            )
            .scalars()
            .first()
        )
        if service is None:
            raise HTTPException(
                status_code=404, detail=f"no service published for workflow {workflow_id!r}"
            )
        if not service.enabled:
            raise HTTPException(status_code=403, detail="service is disabled")
        session.expunge(service)
    return service


def _validate_service_token(request: Request, workflow_id: str, scope: str = INVOKE_SCOPE) -> str:
    """Enforce a Bearer service token for ``workflow_id``/``scope`` (raises 401).

    Returns a stable audit actor derived from the matched token id. The
    plaintext token and its hash never enter the audit trail.
    """
    plaintext = _bearer_token(request)
    factory = get_session_factory(request)
    token_hash = _hash_token(plaintext)
    now = datetime.now(timezone.utc)
    with factory() as session:
        token = (
            session.execute(
                select(CaliberServiceToken).where(
                    CaliberServiceToken.workflow_id == workflow_id,
                    CaliberServiceToken.token_hash == token_hash,
                )
            )
            .scalars()
            .first()
        )
        if token is None or token.revoked_at is not None:
            raise HTTPException(status_code=401, detail="invalid service token")
        if token.expires_at is not None and _as_aware(token.expires_at) <= now:
            raise HTTPException(status_code=401, detail="service token expired")
        if scope not in (token.scopes or []):
            raise HTTPException(status_code=401, detail=f"service token missing scope {scope!r}")
        return f"service_token:{token.token_id}"


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


#: Per-service invocation timestamps, newest last. Process-local by design: the
#: shipped topology is a single app process, and a distributed counter would need
#: shared state this product does not yet have. Stated rather than implied — behind
#: several replicas the effective ceiling is per-replica, which an operator must know.
#: One minute, named so the window is a stated policy rather than a literal.
_RATE_WINDOW_SECONDS = 60.0
_SERVICE_CALLS: dict[str, list[float]] = {}
_SERVICE_CALLS_LOCK = threading.Lock()


def _enforce_service_rate_limit(service: Any) -> None:
    """Refuse an invocation that exceeds the service's per-minute budget.

    A published service was authenticated but otherwise unbounded, so any token holder
    could drive unlimited traffic through a workflow that calls paid model APIs. ``0``
    means unlimited and remains the default, so this only ever refuses traffic an
    operator has explicitly chosen to cap.

    429 with ``Retry-After``, not 400: this is a temporary condition the caller should
    retry, and saying so is the difference between a client backing off and a client
    hammering.
    """
    limit = int(getattr(service, "rate_limit_per_minute", 0) or 0)
    if limit <= 0:
        return
    now = time.time()
    # One bucket, and only authenticated invocations reach it — see the call site. An
    # earlier revision carried an ``authenticated`` flag and a second bucket key for a
    # meter that no caller ever set to ``False``. That is the exact shape of defect this
    # codebase has been audited for: an unexercised branch that reads as a control.
    key = str(service.service_id)
    with _SERVICE_CALLS_LOCK:
        window = [t for t in _SERVICE_CALLS.get(key, []) if now - t < _RATE_WINDOW_SECONDS]
        if len(window) >= limit:
            oldest = min(window)
            _SERVICE_CALLS[key] = window
            raise HTTPException(
                status_code=429,
                detail=(
                    f"service rate limit of {limit}/minute exceeded; "
                    f"retry in {max(1, int(_RATE_WINDOW_SECONDS - (now - oldest)))}s"
                ),
                headers={"Retry-After": str(max(1, int(_RATE_WINDOW_SECONDS - (now - oldest))))},
            )
        window.append(now)
        _SERVICE_CALLS[key] = window


def _cors_headers(service: Any, request: Request) -> dict[str, str]:
    """CORS headers for a browser caller, or ``{}``.

    Empty configuration emits **nothing**, which is the restrictive answer: a wildcard
    would let any site read a token-authorized response. Only an origin the operator
    listed is echoed back, and the echo is exact rather than reflected blindly.
    """
    allowed = {
        o.strip()
        for o in str(getattr(service, "cors_allowed_origins", "") or "").split(",")
        if o.strip()
    }
    origin = request.headers.get("Origin", "").strip()
    if not allowed or not origin or origin not in allowed:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Credentials": "true",
        "Vary": "Origin",
    }


async def invoke_service(request: Request) -> JSONResponse:
    workflow_id = request.path_params["workflow_id"]
    service = _load_enabled_service(request, workflow_id)
    # Charged **after** authentication, deliberately, and this is a correction.
    #
    # It was originally charged before, reasoning that a limit applying only to valid
    # tokens does not stop a flood of invalid ones. A review pass pointed out the cost:
    # that flood then exhausts the budget and denies legitimate callers. Splitting the
    # buckets does not help either, because before authenticating you cannot tell a valid
    # caller from an invalid one — the pre-auth bucket is shared, so the denial just moves.
    #
    # The resolution is that these are two different jobs. Flood protection is
    # ``RateLimitMiddleware``'s, applied per-caller across the whole app; this limit is
    # the per-service budget an operator configured, which should mean "how much work may
    # this service actually do", so only real invocations should charge it. Token
    # verification is a hash comparison, not the expensive part of the path.
    actor = (
        _validate_service_token(request, workflow_id, INVOKE_SCOPE)
        if service.auth_required
        else f"anonymous_service:{workflow_id}"
    )
    _enforce_service_rate_limit(service)
    body = await parse_json_object(request, allow_empty=True)
    payload = ServiceInvokeRequest.model_validate(body)
    try:
        jsonschema.validate(instance=payload.input, schema=service.input_schema or {})
    except jsonschema.ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    alias = service.alias
    factory = get_session_factory(request)
    with factory() as session:
        workflow = session.get(CaliberWorkflow, workflow_id)
        if workflow is None:
            raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")
        deployment = _active_deployment(session, workflow_id, alias)
        if deployment is None:
            raise HTTPException(
                status_code=409,
                detail=f"no active deployment for workflow {workflow_id!r} alias {alias!r}",
            )
        version = session.get(CaliberWorkflowVersion, deployment.version_id)
        if version is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"deployment {deployment.deployment_id!r} points to missing version "
                    f"{deployment.version_id!r}"
                ),
            )
        run, _created = enqueue_workflow_run(
            session,
            workflow=workflow,
            version=version,
            alias=alias,
            source="service",
            actor=actor,
            input_text=_input_to_text(payload.input),
            idempotency_key=payload.idempotency_key,
            publish=lambda evt: _emit_queue_event(request, evt),
        )
        run_id = run.workflow_run_id
        # A service invocation starts a real workflow run — record it like the
        # other service state changes (publish/update/token) so there's a trail.
        audit_record(
            session,
            actor=actor,
            action="invoke_workflow_service",
            entity_type="workflow",
            entity_id=workflow_id,
            details={"run_id": run_id, "alias": alias, "auth_required": service.auth_required},
        )
        session.commit()
    data = ServiceInvokeResponse(run_id=run_id, status=RUN_STATUS_QUEUED)
    response = envelope_response(data, status_code=202)
    # Emitted only for an origin the operator listed; absent otherwise, so a browser
    # cannot read a token-authorized response from an unapproved site.
    for header, value in _cors_headers(service, request).items():
        response.headers[header] = value
    return response


def _input_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    import json  # noqa: PLC0415

    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


async def get_service_run_status(request: Request) -> JSONResponse:
    workflow_id = request.path_params["workflow_id"]
    run_id = request.path_params["run_id"]
    service = _load_enabled_service(request, workflow_id)
    if service.auth_required:
        _validate_service_token(request, workflow_id, INVOKE_SCOPE)
    factory = get_session_factory(request)
    with factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        if run is None or run.workflow_id != workflow_id:
            raise HTTPException(
                status_code=404,
                detail=f"run {run_id!r} not found for workflow {workflow_id!r}",
            )
        output: dict[str, object] | None = None
        error: str | None = None
        if run.status == RUN_STATUS_COMPLETED:
            output = _run_output(run)
        elif run.status == RUN_STATUS_FAILED:
            error = run.error_summary or _summary_str(run, "error")
        data = ServiceRunStatusSchema(
            run_id=run.workflow_run_id,
            status=run.status,
            output=output,
            error=error,
            trace_id=run.trace_id,
        )
    response = envelope_response(data)
    # The poll response needs the allow-origin header for the same reason the invoke
    # response does: without it the browser blocks the read, so a browser client could
    # start a run and never learn the outcome.
    for header, value in _cors_headers(service, request).items():
        response.headers[header] = value
    return response


def _summary_str(run: CaliberWorkflowRun, key: str) -> str | None:
    summary = run.summary if isinstance(run.summary, dict) else None
    if summary is None:
        return None
    value = summary.get(key)
    return value if isinstance(value, str) else None


def _run_output(run: CaliberWorkflowRun) -> dict[str, object]:
    """Project a completed run's final output into a dict.

    The worker stores the run's final output under ``summary["output"]`` (a
    string). We wrap that scalar in ``{"output": ...}`` so the response is always
    a JSON object, matching the service's object-shaped output_schema.
    """
    summary = run.summary if isinstance(run.summary, dict) else {}
    value = summary.get("output")
    if isinstance(value, dict):
        return value
    return {"output": value}


def _build_service_openapi_spec(
    *,
    workflow_id: str,
    title: str,
    service: CaliberWorkflowService,
) -> dict[str, object]:
    """Build the canonical OpenAPI document used by both read surfaces."""
    invoke_path = INVOKE_PATH.format(workflow_id=workflow_id)
    status_path = PREFIX + "/services/" + workflow_id + "/runs/{run_id}"
    input_schema = dict(service.input_schema or {"type": "object"})
    output_schema = dict(service.output_schema or {"type": "object"})
    security: list[dict[str, list[str]]] = [{"serviceToken": []}] if service.auth_required else []
    spec: dict[str, object] = {
        "openapi": "3.0.3",
        "info": {"title": f"{title} — CALIBER service", "version": "1.0.0"},
        "paths": {
            invoke_path: {
                "post": {
                    "operationId": "invokeService",
                    "summary": "Invoke the workflow (async); returns a run id to poll.",
                    "security": security,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"input": input_schema},
                                    "required": ["input"],
                                }
                            }
                        },
                    },
                    "responses": {
                        "202": {
                            "description": "Run queued.",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "data": {
                                                "type": "object",
                                                "properties": {
                                                    "run_id": {"type": "string"},
                                                    "status": {"type": "string"},
                                                },
                                            }
                                        },
                                    }
                                }
                            },
                        },
                        "400": {"description": "Input failed schema validation."},
                    },
                }
            },
            status_path: {
                "get": {
                    "operationId": "getServiceRun",
                    "summary": "Poll a run's status and (when complete) its output.",
                    "security": security,
                    "parameters": [
                        {
                            "name": "run_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Run status.",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "data": {
                                                "type": "object",
                                                "properties": {
                                                    "run_id": {"type": "string"},
                                                    "status": {"type": "string"},
                                                    "output": output_schema,
                                                    "error": {"type": "string"},
                                                    "trace_id": {"type": "string"},
                                                },
                                            }
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
        },
    }
    if service.auth_required:
        spec["components"] = {
            "securitySchemes": {"serviceToken": {"type": "http", "scheme": "bearer"}}
        }
    return spec


async def service_openapi(request: Request) -> JSONResponse:
    """``GET /services/{workflow_id}/openapi.json`` — OpenAPI 3.0 spec for the service.

    Auto-generated from the service's stored input/output JSON Schema so the
    endpoint is self-documenting and importable into Swagger UI / Postman.
    Token-protected unless the service was explicitly published as public.
    Returns the raw spec (not enveloped).
    """
    workflow_id = request.path_params["workflow_id"]
    service = _load_enabled_service(request, workflow_id)
    if service.auth_required:
        _validate_service_token(request, workflow_id, INVOKE_SCOPE)
    factory = get_session_factory(request)
    with factory() as session:
        workflow = session.get(CaliberWorkflow, workflow_id)
        title = workflow.name if workflow is not None else workflow_id
    response = JSONResponse(
        _build_service_openapi_spec(
            workflow_id=workflow_id,
            title=title,
            service=service,
        )
    )
    # Same policy as invoke/poll, so a browser-based API explorer on an approved origin
    # can actually fetch the spec it is meant to import.
    for header, value in _cors_headers(service, request).items():
        response.headers[header] = value
    return response


async def preflight_service(request: Request) -> Response:
    """The CORS preflight for the three browser-reachable service endpoints.

    Without this the CORS support was decorative. A browser sending
    ``Authorization: Bearer …`` with ``Content-Type: application/json`` must preflight
    first, and the route only accepted POST, so the preflight got **405** and the real
    request was never sent. Allow-origin headers on the POST response cannot help when
    the browser never reaches it — an independent probe caught exactly this.

    Registered on invoke **and** on the run-status/OpenAPI reads. That is not padding:
    ``Authorization`` is not a CORS-safelisted request header, so a bearer-token ``GET``
    is preflighted exactly like the ``POST`` is. Fixing only invoke would leave a browser
    able to start a run and then unable to poll for its result, which is not a usable
    service.

    Answered without authentication, deliberately and per the CORS spec: a preflight
    carries no credentials, so requiring a token would make it impossible to satisfy.
    It discloses nothing beyond whether an origin is permitted, and an unlisted origin
    still gets no allow-origin header and is blocked by the browser.
    """
    workflow_id = request.path_params["workflow_id"]
    factory = get_session_factory(request)
    with factory() as session:
        service = (
            session.execute(
                select(CaliberWorkflowService).where(
                    CaliberWorkflowService.workflow_id == workflow_id
                )
            )
            .scalars()
            .first()
        )
    headers = _cors_headers(service, request) if service is not None else {}
    if headers:
        # Advertise the method this specific path actually accepts. A blanket
        # "GET, POST, OPTIONS" would tell a browser it may POST to the read-only poll
        # endpoint, and the browser would believe it right up to the 405.
        headers["Access-Control-Allow-Methods"] = (
            "POST, OPTIONS" if request.url.path.endswith("/invoke") else "GET, OPTIONS"
        )
        # Echo what the browser asked for rather than guessing: a fixed list would
        # break the moment a client adds a header the service legitimately accepts.
        requested = request.headers.get("Access-Control-Request-Headers", "")
        headers["Access-Control-Allow-Headers"] = requested or "Authorization, Content-Type"
        headers["Access-Control-Max-Age"] = "600"
    # 204 with no body either way. An unlisted origin simply receives no allow-origin
    # header, which is what the browser enforces on.
    return Response(status_code=204, headers=headers)


def _with_cors_on_errors(handler: Any) -> Any:
    """Attach the service's CORS headers to error responses, not just successful ones.

    Every failure on these routes — 401, 400 schema, 404 missing run, 409 disabled, 429
    quota — is raised as an ``HTTPException`` and rendered by the global handler, which
    knows nothing about a per-service origin allowlist. So the route-local headers were
    only ever on the happy path, and a browser client on a listed origin could invoke a
    service but **not read why a call failed**: an independent probe saw a 429 with a
    correct ``Retry-After`` and no ``Access-Control-Allow-Origin``, so the response was
    unreadable to JavaScript.

    Wrapping once here rather than at each ``raise`` keeps the two from drifting: a new
    error path added later is covered without anyone remembering to.

    The lookup is unauthenticated, like the preflight's, and discloses nothing — headers are
    emitted only for an origin the operator listed, and an unknown service yields none.
    """

    async def wrapped(request: Request) -> Any:
        try:
            return await handler(request)
        except HTTPException as exc:
            workflow_id = request.path_params.get("workflow_id", "")
            try:
                factory = get_session_factory(request)
                with factory() as session:
                    service = (
                        session.execute(
                            select(CaliberWorkflowService).where(
                                CaliberWorkflowService.workflow_id == workflow_id
                            )
                        )
                        .scalars()
                        .first()
                    )
                headers = _cors_headers(service, request) if service is not None else {}
            except Exception:
                # Never turn one error into a worse one: if the lookup fails, the caller
                # should still get its original status, just without CORS headers.
                headers = {}
            if headers:
                exc.headers = {**(exc.headers or {}), **headers}
            raise

    # Starlette derives the endpoint name for diagnostics; keep the real one.
    wrapped.__name__ = getattr(handler, "__name__", "wrapped")
    return wrapped


def register(app: Starlette) -> None:
    app.routes.append(Route(SERVICE_PATH, publish_service, methods=["POST"]))
    app.routes.append(Route(SERVICE_PATH, get_service, methods=["GET"]))
    app.routes.append(Route(SERVICE_PATH, delete_service, methods=["DELETE"]))
    app.routes.append(Route(SERVICE_TOKENS_PATH, create_service_token, methods=["POST"]))
    app.routes.append(Route(SERVICE_TOKENS_PATH, list_service_tokens, methods=["GET"]))
    app.routes.append(Route(SERVICE_TOKEN_DETAIL_PATH, revoke_service_token, methods=["DELETE"]))
    app.routes.append(Route(INTERNAL_OPENAPI_PATH, get_service_openapi, methods=["GET"]))
    # The three browser-reachable routes carry CORS on failures too, so a listed origin can
    # read the error contract rather than only the happy path.
    app.routes.append(Route(OPENAPI_PATH, _with_cors_on_errors(service_openapi), methods=["GET"]))
    app.routes.append(Route(INVOKE_PATH, _with_cors_on_errors(invoke_service), methods=["POST"]))
    app.routes.append(
        Route(RUN_STATUS_PATH, _with_cors_on_errors(get_service_run_status), methods=["GET"])
    )
    # Registered separately so a browser preflight is answered rather than 405ing, which
    # made the CORS allowlist unusable from a browser at all. All three of the endpoints a
    # browser client touches need one, because a bearer-token GET is preflighted too.
    for _cors_path in (INVOKE_PATH, RUN_STATUS_PATH, OPENAPI_PATH):
        app.routes.append(Route(_cors_path, preflight_service, methods=["OPTIONS"]))
