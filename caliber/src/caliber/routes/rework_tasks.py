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

**Deliberately not built here:** a ``release_no_go`` failure kind — that
needs an aggregate Workspace release, which doesn't exist yet (see
``docs/workspace-plan.md`` section 3.6). ``quality_no_go`` is no longer in
this category: ``routes/quality_reviews.py`` creates a task with that
``failure_kind`` directly from a human "no_go" decision, the same way
``eval_stage.py`` creates one from a machine-gate rejection.

Every handler offloads its synchronous SQLAlchemy work to
:func:`starlette.concurrency.run_in_threadpool`, matching
``routes/verification.py`` — see ``tests/test_async_offload_ratchet.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_ADMIN, SCOPE_OPERATOR, require_scopes, require_user, resolve_identity
from caliber.db.models import CaliberRefinementJob, CaliberReworkTask
from caliber.routes._deps import envelope_response, get_session_factory, parse_json_object
from caliber.schemas import ReworkTaskReassignRequest, ReworkTaskResolveRequest, ReworkTaskSchema

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/rework-tasks"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/rework-tasks/{task_id}"
CLAIM_PATH = "/ajax-api/2.0/mlflow/caliber/rework-tasks/{task_id}/claim"
RESOLVE_PATH = "/ajax-api/2.0/mlflow/caliber/rework-tasks/{task_id}/resolve"
REASSIGN_PATH = "/ajax-api/2.0/mlflow/caliber/rework-tasks/{task_id}/reassign"

_LIST_STATUS_VALUES: frozenset[str] = frozenset({"open", "in_progress", "resolved", "all"})

_Factory = sessionmaker[Session]


def _list_tasks_sync(
    factory: _Factory, *, status: str, assigned_to: str | None
) -> list[ReworkTaskSchema]:
    with factory() as session:
        stmt = select(CaliberReworkTask).order_by(CaliberReworkTask.created_at.desc())
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


def _get_task_sync(factory: _Factory, *, task_id: str) -> ReworkTaskSchema:
    with factory() as session:
        task = session.get(CaliberReworkTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"rework task {task_id!r} not found")
        return ReworkTaskSchema.model_validate(task)


async def get_task(request: Request) -> JSONResponse:
    require_user(request)
    task_id = request.path_params["task_id"]
    factory = get_session_factory(request)
    schema = await run_in_threadpool(_get_task_sync, factory, task_id=task_id)
    return envelope_response(schema)


def _claim_task_sync(factory: _Factory, *, task_id: str, actor: str) -> ReworkTaskSchema:
    with factory() as session:
        task = session.get(CaliberReworkTask, task_id)
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
) -> ReworkTaskSchema:
    with factory() as session:
        task = session.get(CaliberReworkTask, task_id)
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
    factory: _Factory, *, task_id: str, actor: str, payload: ReworkTaskReassignRequest
) -> ReworkTaskSchema:
    with factory() as session:
        task = session.get(CaliberReworkTask, task_id)
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


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_tasks, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, get_task, methods=["GET"]))
    app.routes.append(Route(CLAIM_PATH, claim_task, methods=["POST"]))
    app.routes.append(Route(RESOLVE_PATH, resolve_task, methods=["POST"]))
    app.routes.append(Route(REASSIGN_PATH, reassign_task, methods=["POST"]))
