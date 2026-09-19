"""``/caliber/rework-tasks`` endpoints — owned work from a rejected job.

Wires :class:`caliber.db.models.CaliberReworkTask`, created automatically by
``orchestrator/eval_stage.py`` in the same transaction that terminally
rejects a refinement job (see that module for the creation side), to real
routes so a failed gate produces owned, recoverable work instead of a silent
terminal ``rejected`` row. See ``docs/workspace-plan.md`` section 3.6.

* ``GET /rework-tasks`` — list, filterable by status/assigned_to.
* ``GET /rework-tasks/{task_id}`` — one task.
* ``POST /rework-tasks/{task_id}/claim`` (operator) — ``open`` -> ``in_progress``, assigns the caller.
* ``POST /rework-tasks/{task_id}/resolve`` (operator, assignee or admin) — ``in_progress`` -> ``resolved``.
* ``POST /rework-tasks/{task_id}/reassign`` (admin) — change the assignee.

``quality_no_go`` and ``release_no_go`` are not created here either:
``routes/quality_reviews.py`` creates a ``quality_no_go`` task directly from
a human "no_go" decision on a job, the same way ``eval_stage.py`` creates one
from a machine-gate rejection, and ``workspace_release_governance.py``
creates a ``release_no_go`` task directly from a rejected Workspace release
(see that module's ``record_workspace_release_decision``).

Every handler offloads its synchronous SQLAlchemy work to
:func:`starlette.concurrency.run_in_threadpool`, matching
``routes/verification.py`` — see ``tests/test_async_offload_ratchet.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Select
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import (
    SCOPE_ADMIN,
    SCOPE_OPERATOR,
    CaliberIdentity,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.db.models import CaliberAgentConfig, CaliberRefinementJob, CaliberReworkTask
from caliber.resource_access import ROLE_OWNER, require_project_access
from caliber.routes._deps import envelope_response, get_session_factory, parse_json_object
from caliber.schemas import ReworkTaskReassignRequest, ReworkTaskResolveRequest, ReworkTaskSchema

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/rework-tasks"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/rework-tasks/{task_id}"
CLAIM_PATH = "/ajax-api/2.0/mlflow/caliber/rework-tasks/{task_id}/claim"
RESOLVE_PATH = "/ajax-api/2.0/mlflow/caliber/rework-tasks/{task_id}/resolve"
REASSIGN_PATH = "/ajax-api/2.0/mlflow/caliber/rework-tasks/{task_id}/reassign"

# Workspace-scoped counterparts. The colon action suffix follows the
# workspace-plan contract; the existing global routes above remain unchanged
# for compatibility with jobs created before project binding was available.
PROJECT_LIST_PATH = "/ajax-api/2.0/mlflow/caliber/projects/{project_id}/rework-tasks"
PROJECT_DETAIL_PATH = PROJECT_LIST_PATH + "/{task_id}"
PROJECT_CLAIM_PATH = PROJECT_DETAIL_PATH + ":claim"
PROJECT_RESOLVE_PATH = PROJECT_DETAIL_PATH + ":resolve"
PROJECT_REASSIGN_PATH = PROJECT_DETAIL_PATH + ":reassign"

_LIST_STATUS_VALUES: frozenset[str] = frozenset({"open", "in_progress", "resolved", "all"})

_Factory = sessionmaker[Session]


def _project_task_statement(project_id: str) -> Select[tuple[CaliberReworkTask]]:
    """Return the task query restricted to one project's tasks.

    Rework tasks intentionally remain globally compatible in this phase, so
    a job-sourced task's project boundary is derived from its source agent
    (unchanged from before the Workspace-release source existed): a task
    whose agent is global or belongs to another project is not a member of
    the project's task collection and is therefore indistinguishable from a
    missing task. A release-sourced task has no agent to join through --
    ``CaliberReworkTask.agent_id`` is null -- so the agent join is outer and
    a release-sourced task instead matches directly on its own
    ``project_id`` (populated at creation time from the Workspace release;
    see ``workspace_release_governance.py``).
    """
    return (
        select(CaliberReworkTask)
        .join(
            CaliberAgentConfig,
            CaliberAgentConfig.agent_id == CaliberReworkTask.agent_id,
            isouter=True,
        )
        .where(
            or_(
                CaliberAgentConfig.project_id == project_id,
                CaliberReworkTask.project_id == project_id,
            )
        )
    )


def _task_for_scope(
    session: Session, *, task_id: str, project_id: str | None
) -> CaliberReworkTask | None:
    if project_id is None:
        return session.get(CaliberReworkTask, task_id)
    return session.execute(
        _project_task_statement(project_id).where(CaliberReworkTask.task_id == task_id)
    ).scalar_one_or_none()


def _authorize_project_task(
    session: Session,
    *,
    identity: CaliberIdentity,
    project_id: str,
    action: str,
) -> str | None:
    """Authorize a project task operation in its SQLAlchemy session.

    The action remains a literal at each route call site so the required-scope
    inventory and generated OpenAPI document stay tied to the route contract.
    """
    _project, decision = require_project_access(session, identity, project_id, action)
    return decision.role


def _list_tasks_sync(
    factory: _Factory,
    *,
    status: str,
    assigned_to: str | None,
    project_id: str | None = None,
    identity: CaliberIdentity | None = None,
    project_action: str | None = None,
) -> list[ReworkTaskSchema]:
    with factory() as session:
        if project_id is not None:
            if identity is None or project_action is None:
                raise RuntimeError("project-scoped task operations require identity and action")
            _authorize_project_task(
                session, identity=identity, project_id=project_id, action=project_action
            )
        stmt = (
            _project_task_statement(project_id)
            if project_id is not None
            else select(CaliberReworkTask)
        ).order_by(CaliberReworkTask.created_at.desc())
        if status != "all":
            stmt = stmt.where(CaliberReworkTask.status == status)
        if assigned_to:
            stmt = stmt.where(CaliberReworkTask.assigned_to == assigned_to)
        rows = session.execute(stmt).scalars().all()
        return [ReworkTaskSchema.model_validate(row) for row in rows]


async def list_tasks(request: Request) -> JSONResponse:
    require_user(request)
    status = request.query_params.get("status", "open")
    if status not in _LIST_STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid value for 'status': {status!r}; expected one of {sorted(_LIST_STATUS_VALUES)}",
        )
    assigned_to = request.query_params.get("assigned_to")

    factory = get_session_factory(request)
    schemas = await run_in_threadpool(
        _list_tasks_sync, factory, status=status, assigned_to=assigned_to
    )
    return envelope_response(schemas)


def _get_task_sync(
    factory: _Factory,
    *,
    task_id: str,
    project_id: str | None = None,
    identity: CaliberIdentity | None = None,
    project_action: str | None = None,
) -> ReworkTaskSchema:
    with factory() as session:
        if project_id is not None:
            if identity is None or project_action is None:
                raise RuntimeError("project-scoped task operations require identity and action")
            _authorize_project_task(
                session, identity=identity, project_id=project_id, action=project_action
            )
        task = _task_for_scope(session, task_id=task_id, project_id=project_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"rework task {task_id!r} not found")
        return ReworkTaskSchema.model_validate(task)


async def get_task(request: Request) -> JSONResponse:
    require_user(request)
    task_id = request.path_params["task_id"]
    factory = get_session_factory(request)
    schema = await run_in_threadpool(_get_task_sync, factory, task_id=task_id)
    return envelope_response(schema)


def _claim_task_sync(
    factory: _Factory,
    *,
    task_id: str,
    actor: str,
    project_id: str | None = None,
    identity: CaliberIdentity | None = None,
    project_action: str | None = None,
) -> ReworkTaskSchema:
    with factory() as session:
        if project_id is not None:
            if identity is None or project_action is None:
                raise RuntimeError("project-scoped task operations require identity and action")
            _authorize_project_task(
                session, identity=identity, project_id=project_id, action=project_action
            )
        task = _task_for_scope(session, task_id=task_id, project_id=project_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"rework task {task_id!r} not found")
        if task.status != "open":
            raise HTTPException(
                status_code=409, detail=f"rework task {task_id!r} is already {task.status}"
            )
        task.status = "in_progress"
        task.assigned_to = actor
        audit_record(
            session,
            actor=actor,
            action="claim_rework_task",
            entity_type="rework_task",
            entity_id=task_id,
            details={},
        )
        session.commit()
        return ReworkTaskSchema.model_validate(task)


async def claim_task(request: Request) -> JSONResponse:
    task_id = request.path_params["task_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    factory = get_session_factory(request)
    schema = await run_in_threadpool(_claim_task_sync, factory, task_id=task_id, actor=actor)
    return envelope_response(schema)


def _resolve_task_sync(
    factory: _Factory,
    *,
    task_id: str,
    actor: str,
    is_admin: bool,
    payload: ReworkTaskResolveRequest,
    project_id: str | None = None,
    identity: CaliberIdentity | None = None,
    project_action: str | None = None,
) -> ReworkTaskSchema:
    with factory() as session:
        if project_id is not None:
            if identity is None or project_action is None:
                raise RuntimeError("project-scoped task operations require identity and action")
            _authorize_project_task(
                session, identity=identity, project_id=project_id, action=project_action
            )
        task = _task_for_scope(session, task_id=task_id, project_id=project_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"rework task {task_id!r} not found")
        if task.status != "in_progress":
            raise HTTPException(
                status_code=409, detail=f"rework task {task_id!r} is not in progress"
            )
        if not is_admin and task.assigned_to != actor:
            raise HTTPException(
                status_code=403,
                detail=f"rework task {task_id!r} is assigned to someone else",
            )
        if payload.resolution_job_id is not None:
            resolution_job = session.get(CaliberRefinementJob, payload.resolution_job_id)
            if resolution_job is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"job {payload.resolution_job_id!r} not found",
                )
            if resolution_job.agent_id != task.agent_id:
                raise HTTPException(
                    status_code=400,
                    detail="resolution_job_id must belong to the same agent as the rework task",
                )
            task.resolution_job_id = payload.resolution_job_id
        task.resolution_notes = payload.resolution_notes
        task.status = "resolved"
        task.resolved_by = actor
        task.resolved_at = datetime.now(timezone.utc)
        audit_record(
            session,
            actor=actor,
            action="resolve_rework_task",
            entity_type="rework_task",
            entity_id=task_id,
            details={"resolution_job_id": task.resolution_job_id},
        )
        session.commit()
        return ReworkTaskSchema.model_validate(task)


async def resolve_task(request: Request) -> JSONResponse:
    task_id = request.path_params["task_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = ReworkTaskResolveRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    factory = get_session_factory(request)
    schema = await run_in_threadpool(
        _resolve_task_sync,
        factory,
        task_id=task_id,
        actor=actor,
        is_admin=identity.has_scope(SCOPE_ADMIN),
        payload=payload,
    )
    return envelope_response(schema)


def _reassign_task_sync(
    factory: _Factory,
    *,
    task_id: str,
    actor: str,
    payload: ReworkTaskReassignRequest,
    project_id: str | None = None,
    identity: CaliberIdentity | None = None,
    project_action: str | None = None,
) -> ReworkTaskSchema:
    with factory() as session:
        if project_id is not None:
            if identity is None or project_action is None:
                raise RuntimeError("project-scoped task operations require identity and action")
            role = _authorize_project_task(
                session, identity=identity, project_id=project_id, action=project_action
            )
            if role != ROLE_OWNER:
                raise HTTPException(
                    status_code=403,
                    detail="only a project Admin may reassign a project rework task",
                )
        task = _task_for_scope(session, task_id=task_id, project_id=project_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"rework task {task_id!r} not found")
        if task.status not in ("open", "in_progress"):
            raise HTTPException(
                status_code=409, detail=f"rework task {task_id!r} is already {task.status}"
            )
        previous_assignee = task.assigned_to
        task.assigned_to = payload.assigned_to
        # An admin assigning an unowned task is equivalent to claiming it on
        # that person's behalf -- it should not stay "open" once someone is
        # named responsible for it.
        task.status = "in_progress"
        audit_record(
            session,
            actor=actor,
            action="reassign_rework_task",
            entity_type="rework_task",
            entity_id=task_id,
            details={"from": previous_assignee, "to": task.assigned_to},
        )
        session.commit()
        return ReworkTaskSchema.model_validate(task)


async def reassign_task(request: Request) -> JSONResponse:
    task_id = request.path_params["task_id"]
    body = await parse_json_object(request)
    payload = ReworkTaskReassignRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN])

    factory = get_session_factory(request)
    schema = await run_in_threadpool(
        _reassign_task_sync, factory, task_id=task_id, actor=actor, payload=payload
    )
    return envelope_response(schema)


# ---------------------------------------------------------------------------
# Workspace-scoped task collection. These routes deliberately reuse the
# lifecycle helpers above so a task cannot behave differently merely because
# it was reached through a project path. The project role is checked first,
# then the query joins the task's source agent to the same project id.
# ---------------------------------------------------------------------------


async def list_project_tasks(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    project_id = request.path_params["project_id"]
    factory = get_session_factory(request)
    status = request.query_params.get("status", "open")
    if status not in _LIST_STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid value for 'status': {status!r}; expected one of {sorted(_LIST_STATUS_VALUES)}",
        )
    schemas = await run_in_threadpool(
        _list_tasks_sync,
        factory,
        status=status,
        assigned_to=request.query_params.get("assigned_to"),
        project_id=project_id,
        identity=identity,
        project_action="read",
    )
    return envelope_response(schemas)


async def get_project_task(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    project_id = request.path_params["project_id"]
    factory = get_session_factory(request)
    schema = await run_in_threadpool(
        _get_task_sync,
        factory,
        task_id=request.path_params["task_id"],
        project_id=project_id,
        identity=identity,
        project_action="read",
    )
    return envelope_response(schema)


async def claim_project_task(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    project_id = request.path_params["project_id"]
    factory = get_session_factory(request)
    schema = await run_in_threadpool(
        _claim_task_sync,
        factory,
        task_id=request.path_params["task_id"],
        actor=actor,
        project_id=project_id,
        identity=identity,
        project_action="rework.update",
    )
    return envelope_response(schema)


async def resolve_project_task(request: Request) -> JSONResponse:
    task_id = request.path_params["task_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = ReworkTaskResolveRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    project_id = request.path_params["project_id"]
    factory = get_session_factory(request)
    schema = await run_in_threadpool(
        _resolve_task_sync,
        factory,
        task_id=task_id,
        actor=actor,
        is_admin=identity.has_scope(SCOPE_ADMIN),
        payload=payload,
        project_id=project_id,
        identity=identity,
        project_action="rework.update",
    )
    return envelope_response(schema)


async def reassign_project_task(request: Request) -> JSONResponse:
    task_id = request.path_params["task_id"]
    body = await parse_json_object(request)
    payload = ReworkTaskReassignRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    project_id = request.path_params["project_id"]
    factory = get_session_factory(request)
    schema = await run_in_threadpool(
        _reassign_task_sync,
        factory,
        task_id=task_id,
        actor=actor,
        payload=payload,
        project_id=project_id,
        identity=identity,
        project_action="rework.update",
    )
    return envelope_response(schema)


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_tasks, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, get_task, methods=["GET"]))
    app.routes.append(Route(CLAIM_PATH, claim_task, methods=["POST"]))
    app.routes.append(Route(RESOLVE_PATH, resolve_task, methods=["POST"]))
    app.routes.append(Route(REASSIGN_PATH, reassign_task, methods=["POST"]))
    app.routes.append(Route(PROJECT_LIST_PATH, list_project_tasks, methods=["GET"]))
    app.routes.append(Route(PROJECT_DETAIL_PATH, get_project_task, methods=["GET"]))
    app.routes.append(Route(PROJECT_CLAIM_PATH, claim_project_task, methods=["POST"]))
    app.routes.append(Route(PROJECT_RESOLVE_PATH, resolve_project_task, methods=["POST"]))
    app.routes.append(Route(PROJECT_REASSIGN_PATH, reassign_project_task, methods=["POST"]))
