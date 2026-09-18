"""Project-scoped Workspace release-operation routes (``P5-C``).

The routes expose the intent and reconciliation state machine without making a
provider SDK part of the HTTP layer.  Every lookup and mutation authorizes the
project in the same SQLAlchemy session that reads the operation coordinates.
When no explicit adapter registry is installed, execution fails closed with a
conflict instead of inventing a provider implementation.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.auth import (
    SCOPE_OPERATOR,
    CaliberIdentity,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.db.models import (
    CaliberWorkspaceRelease,
    CaliberWorkspaceReleaseOperation,
    CaliberWorkspaceReleaseOperationItem,
)
from caliber.resource_access import require_project_access
from caliber.routes._deps import get_session_factory, list_limit, parse_json_object
from caliber.schemas import (
    WorkspaceReleaseOperationCreateRequest,
    WorkspaceReleaseOperationItemSchema,
    WorkspaceReleaseOperationSchema,
)
from caliber.workspace_release_adapters import (
    WorkspaceReleaseAdapterError,
    WorkspaceResourceAdapterRegistry,
)
from caliber.workspace_release_operation_service import WorkspaceReleaseOperationConflictError
from caliber.workspace_release_operations import (
    OperationExecutionResult,
    apply_workspace_release_operation,
    cancel_expired_workspace_release_operation,
    observe_workspace_release_operation,
    prepare_workspace_release_operation,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"
OPERATIONS_PATH = PREFIX + "/projects/{project_id}/releases/{release_id}/operations"
OPERATION_DETAIL_PATH = OPERATIONS_PATH + "/{operation_id}"
APPLY_PATH = OPERATION_DETAIL_PATH + ":apply"
OBSERVE_PATH = OPERATION_DETAIL_PATH + ":observe"
CANCEL_PATH = OPERATION_DETAIL_PATH + ":cancel-expired"

_Factory = sessionmaker[Session]


def _require_workspace_header(request: Request, project_id: str) -> None:
    supplied = request.headers.get("X-CALIBER-Project")
    if supplied is not None and supplied != project_id:
        raise HTTPException(status_code=400, detail="workspace_context_mismatch")


def _registry(request: Request) -> WorkspaceResourceAdapterRegistry:
    registry = getattr(request.app.state, "workspace_resource_adapter_registry", None)
    if registry is None:
        return WorkspaceResourceAdapterRegistry()
    if not isinstance(registry, WorkspaceResourceAdapterRegistry):
        raise RuntimeError("app.state.workspace_resource_adapter_registry has an invalid type")
    return registry


def _operation_payload(result: OperationExecutionResult) -> dict[str, object]:
    operation = WorkspaceReleaseOperationSchema.model_validate(result.operation).model_dump(
        mode="json"
    )
    items = [
        WorkspaceReleaseOperationItemSchema.model_validate(item).model_dump(mode="json")
        for item in result.items
    ]
    return {"operation": operation, "items": items}


def _operation_list_payload(
    rows: Sequence[CaliberWorkspaceReleaseOperation],
) -> list[dict[str, object]]:
    return [
        WorkspaceReleaseOperationSchema.model_validate(row).model_dump(mode="json") for row in rows
    ]


def _release_for_project(
    session: Session, project_id: str, release_id: str
) -> CaliberWorkspaceRelease:
    release = session.get(CaliberWorkspaceRelease, release_id)
    if release is None or release.project_id != project_id:
        raise HTTPException(status_code=404, detail="workspace release not found")
    return release


def _operation_for_project(
    session: Session,
    identity: CaliberIdentity,
    *,
    project_id: str,
    release_id: str,
    operation_id: str,
    action: str,
) -> CaliberWorkspaceReleaseOperation:
    require_project_access(session, identity, project_id, action)
    operation = session.execute(
        select(CaliberWorkspaceReleaseOperation).where(
            CaliberWorkspaceReleaseOperation.operation_id == operation_id,
            CaliberWorkspaceReleaseOperation.project_id == project_id,
            CaliberWorkspaceReleaseOperation.workspace_release_id == release_id,
        )
    ).scalar_one_or_none()
    if operation is None:
        raise HTTPException(status_code=404, detail="workspace release operation not found")
    return operation


def _conflict(
    exc: WorkspaceReleaseOperationConflictError | WorkspaceReleaseAdapterError,
) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


def _create_sync(
    factory: _Factory,
    *,
    identity: CaliberIdentity,
    actor: str,
    project_id: str,
    release_id: str,
    payload: WorkspaceReleaseOperationCreateRequest,
    adapters: WorkspaceResourceAdapterRegistry,
) -> dict[str, object]:
    with factory() as session:
        require_project_access(
            session,
            identity,
            project_id,
            "release.rollback" if payload.kind == "rollback" else "release.apply",
        )
        release = _release_for_project(session, project_id, release_id)
        result = prepare_workspace_release_operation(
            session,
            project_id=project_id,
            workspace_release_id=release.release_id,
            environment_id=release.environment_id,
            kind=payload.kind,
            idempotency_key=payload.idempotency_key,
            expected_environment_lock_version=payload.expected_environment_lock_version,
            requested_by=actor,
            adapters=adapters,
            expected_current_release_id=payload.expected_current_release_id,
            target_release_id=payload.target_release_id,
        )
        data = _operation_payload(result)
        session.commit()
        return data


def _list_sync(
    factory: _Factory,
    *,
    identity: CaliberIdentity,
    project_id: str,
    release_id: str,
    limit: int,
    offset: int,
) -> list[dict[str, object]]:
    with factory() as session:
        require_project_access(session, identity, project_id, "read")
        _release_for_project(session, project_id, release_id)
        rows = (
            session.execute(
                select(CaliberWorkspaceReleaseOperation)
                .where(
                    CaliberWorkspaceReleaseOperation.project_id == project_id,
                    CaliberWorkspaceReleaseOperation.workspace_release_id == release_id,
                )
                .order_by(CaliberWorkspaceReleaseOperation.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return _operation_list_payload(rows)


def _get_sync(
    factory: _Factory,
    *,
    identity: CaliberIdentity,
    project_id: str,
    release_id: str,
    operation_id: str,
) -> dict[str, object]:
    with factory() as session:
        operation = _operation_for_project(
            session,
            identity,
            project_id=project_id,
            release_id=release_id,
            operation_id=operation_id,
            action="read",
        )
        items = tuple(
            session.execute(
                select(CaliberWorkspaceReleaseOperationItem).where(
                    CaliberWorkspaceReleaseOperationItem.workspace_release_operation_id
                    == operation.operation_id
                )
            ).scalars()
        )
        return _operation_payload(OperationExecutionResult(operation, items))


def _mutate_sync(
    factory: _Factory,
    *,
    identity: CaliberIdentity,
    actor: str,
    project_id: str,
    release_id: str,
    operation_id: str,
    action: str,
    adapters: WorkspaceResourceAdapterRegistry,
) -> dict[str, object]:
    with factory() as session:
        operation = _operation_for_project(
            session,
            identity,
            project_id=project_id,
            release_id=release_id,
            operation_id=operation_id,
            action="read",
        )
        required_action = action
        if action == "release.apply" and operation.kind == "rollback":
            required_action = "release.rollback"
        elif action == "cancel_expired":
            required_action = "release.reconcile"
        require_project_access(session, identity, project_id, required_action)
        if action == "release.apply":
            result = apply_workspace_release_operation(
                session, operation.operation_id, adapters=adapters, actor=actor
            )
        elif action == "release.reconcile":
            result = observe_workspace_release_operation(
                session, operation.operation_id, adapters=adapters, actor=actor
            )
        else:
            result = cancel_expired_workspace_release_operation(
                session, operation.operation_id, actor=actor
            )
        data = _operation_payload(result)
        session.commit()
        return data


async def create_operation(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    release_id = request.path_params["release_id"]
    _require_workspace_header(request, project_id)
    payload = WorkspaceReleaseOperationCreateRequest.model_validate(
        await parse_json_object(request)
    )
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    try:
        data = await run_in_threadpool(
            _create_sync,
            get_session_factory(request),
            identity=identity,
            actor=actor,
            project_id=project_id,
            release_id=release_id,
            payload=payload,
            adapters=_registry(request),
        )
    except (WorkspaceReleaseOperationConflictError, WorkspaceReleaseAdapterError) as exc:
        raise _conflict(exc) from exc
    return JSONResponse({"data": data}, status_code=201)


async def list_operations(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    limit, offset = list_limit(request, default=50, cap=100)
    data = await run_in_threadpool(
        _list_sync,
        get_session_factory(request),
        identity=identity,
        project_id=project_id,
        release_id=request.path_params["release_id"],
        limit=limit,
        offset=offset,
    )
    return JSONResponse({"data": data})


async def get_operation(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    data = await run_in_threadpool(
        _get_sync,
        get_session_factory(request),
        identity=identity,
        project_id=project_id,
        release_id=request.path_params["release_id"],
        operation_id=request.path_params["operation_id"],
    )
    return JSONResponse({"data": data})


async def _mutate_route(request: Request, action: str, *, actor: str | None = None) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    operation_id = request.path_params["operation_id"]
    resolved_actor = actor if actor is not None else require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    try:
        data = await run_in_threadpool(
            _mutate_sync,
            get_session_factory(request),
            identity=identity,
            actor=resolved_actor,
            project_id=project_id,
            release_id=request.path_params["release_id"],
            operation_id=operation_id,
            action=action,
            adapters=_registry(request),
        )
    except (WorkspaceReleaseOperationConflictError, WorkspaceReleaseAdapterError) as exc:
        raise _conflict(exc) from exc
    return JSONResponse({"data": data})


async def apply_operation(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    return await _mutate_route(request, "release.apply", actor=actor)


async def observe_operation(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    return await _mutate_route(request, "release.reconcile", actor=actor)


async def cancel_operation(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    return await _mutate_route(request, "cancel_expired", actor=actor)


def register(app: Starlette) -> None:
    app.routes.append(Route(OPERATIONS_PATH, list_operations, methods=["GET"]))
    app.routes.append(Route(OPERATIONS_PATH, create_operation, methods=["POST"]))
    app.routes.append(Route(OPERATION_DETAIL_PATH, get_operation, methods=["GET"]))
    app.routes.append(Route(APPLY_PATH, apply_operation, methods=["POST"]))
    app.routes.append(Route(OBSERVE_PATH, observe_operation, methods=["POST"]))
    app.routes.append(Route(CANCEL_PATH, cancel_operation, methods=["POST"]))
