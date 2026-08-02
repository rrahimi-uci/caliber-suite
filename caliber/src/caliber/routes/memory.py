"""HTTP management API for agent long-term memory (mem0).

Direct add/search/list/delete over the configured memory store, for humans and
external callers — complementary to the in-run ``memory_add`` / ``memory_search``
agent tools (:mod:`caliber.workflows.memory_tools`). Every operation is scoped by
``agent_id`` / ``user_id`` / ``run_id`` (at least one required) so the shared
team store stays partitioned; reads need any authenticated user, writes need
admin scope. Returns 503 when memory is disabled or the ``[memory]`` extra is
absent.
"""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.auth import SCOPE_ADMIN, require_scopes, require_user
from caliber.db.models import CaliberAgentConfig
from caliber.memory import (
    MemoryDependencyError,
    MemoryDisabledError,
    MemoryService,
)
from caliber.routes._deps import envelope_response_dict, parse_json_object

BASE_PATH = "/ajax-api/2.0/mlflow/caliber/memory"
SEARCH_PATH = BASE_PATH + "/search"

_SCOPE_KEYS = ("agent_id", "user_id", "run_id")


def _memory_service(request: Request) -> MemoryService:
    """Build the run-time memory service or translate unavailability to 503."""
    try:
        return MemoryService.from_config(request.app.state.config)
    except (MemoryDisabledError, MemoryDependencyError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _scope(source: dict[str, Any]) -> dict[str, str]:
    return {
        key: source[key] for key in _SCOPE_KEYS if isinstance(source.get(key), str) and source[key]
    }


def _require_scope(scope: dict[str, str]) -> None:
    if not scope:
        raise HTTPException(
            status_code=400,
            detail="at least one of agent_id / user_id / run_id is required",
        )


def _authorize_scope(request: Request, scope: dict[str, str]) -> None:
    """Bind a caller-supplied memory scope to what the caller may actually reach.

    The scope keys partition a *shared* store, and they arrived straight from the
    request body or query string behind nothing but ``require_user``. Any
    authenticated user could therefore read another user's memories by sending
    their ``user_id``, or another project's agent memories by sending its
    ``agent_id`` — the partition was a filing convention, not a boundary.

    Each key is bound to a different thing, because each means something
    different:

    ``user_id``
        Must be the caller's own identity. An admin may read another user's
        memories; nobody else may. This is the key that carried the plainest
        cross-user read.
    ``agent_id`` / ``run_id``
        Checked **only when the value names a governed resource**. These are
        free-form partition labels in the memory store — the in-run tools scope
        by workflow id, and callers legitimately use labels that were never rows
        in ``caliber_agent_config`` or ``caliber_workflow_runs``. So the rule is:
        if a row with that id exists, the caller must be able to see it; if no
        such row exists, the label is an ordinary partition name and passes.

    A 404 rather than a 403 for the resource keys, matching how an invisible
    resource behaves across the rest of the surface: confirming existence to
    someone who cannot see it is itself a disclosure.

    **Known limitation, stated rather than implied.** A free-form partition has
    no owner, so nothing here can protect it: a caller who guesses an arbitrary
    label still reads that partition. Closing that needs memory partitions to
    become governed resources with an owner, which is a data-model change and a
    product decision, not a filter. What this closes is the case where the label
    *is* a governed resource the caller may not see — and the ``user_id`` case,
    which was an unambiguous cross-user read.
    """
    from caliber.auth import resolve_identity  # noqa: PLC0415
    from caliber.db.models import CaliberWorkflowRun  # noqa: PLC0415
    from caliber.db.scoping import get_visible  # noqa: PLC0415
    from caliber.routes._deps import get_session_factory  # noqa: PLC0415

    identity = resolve_identity(request)

    requested_user = scope.get("user_id")
    if (
        requested_user
        and requested_user != identity.user_id
        and SCOPE_ADMIN not in identity.scopes
    ):
        raise HTTPException(
            status_code=403,
            detail="user_id memory scope must be your own identity",
        )

    resource_keys = (
        ("agent_id", CaliberAgentConfig, CaliberAgentConfig.agent_id),
        ("run_id", CaliberWorkflowRun, CaliberWorkflowRun.workflow_run_id),
    )
    pending = [(key, model, column) for key, model, column in resource_keys if scope.get(key)]
    if not pending:
        return

    from sqlalchemy import select  # noqa: PLC0415

    factory = get_session_factory(request)
    with factory() as session:
        for key, model, column in pending:
            if get_visible(session, model, column, scope[key], identity) is not None:
                continue  # a governed resource the caller can see
            exists = session.execute(select(column).where(column == scope[key])).first()
            if exists is not None:
                # The row is real and the caller cannot see it. This is the case
                # worth refusing; an absent row is a free-form partition label.
                raise HTTPException(
                    status_code=404,
                    detail=f"{key} {scope[key]!r} not found",
                )


def _top_k(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if value >= 1 else default


def _run(operation: str, fn: Any) -> Any:
    """Invoke a memory call, mapping backend failures to a clean 502."""
    try:
        return fn()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # backend (pgvector/LLM) failure
        raise HTTPException(status_code=502, detail=f"memory {operation} failed: {exc}") from exc


async def add_memory(request: Request) -> JSONResponse:
    require_scopes(request, [SCOPE_ADMIN])
    body = await parse_json_object(request)
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=400, detail="'text' is required")
    scope = _scope(body)
    _require_scope(scope)
    _authorize_scope(request, scope)
    service = _memory_service(request)
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else None
    infer = body.get("infer") if isinstance(body.get("infer"), bool) else None
    result = _run("add", lambda: service.add(text, metadata=metadata, infer=infer, **scope))
    return envelope_response_dict({"result": result}, status_code=201)


async def search_memory(request: Request) -> JSONResponse:
    require_user(request)
    body = await parse_json_object(request)
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail="'query' is required")
    scope = _scope(body)
    _require_scope(scope)
    _authorize_scope(request, scope)
    service = _memory_service(request)
    top_k = _top_k(body.get("top_k"), 10)
    result = _run("search", lambda: service.search(query, top_k=top_k, **scope))
    return envelope_response_dict({"result": result})


async def list_memories(request: Request) -> JSONResponse:
    require_user(request)
    scope = _scope(dict(request.query_params))
    _require_scope(scope)
    _authorize_scope(request, scope)
    service = _memory_service(request)
    top_k = _top_k(_maybe_int(request.query_params.get("top_k")), 50)
    result = _run("list", lambda: service.get_all(top_k=top_k, **scope))
    return envelope_response_dict({"result": result})


async def delete_memories(request: Request) -> JSONResponse:
    require_scopes(request, [SCOPE_ADMIN])
    body = await parse_json_object(request, allow_empty=True)
    scope = _scope({**dict(request.query_params), **body})
    _require_scope(scope)
    _authorize_scope(request, scope)
    service = _memory_service(request)
    _run("delete", lambda: service.delete_all(**scope))
    return envelope_response_dict({"deleted": True, "scope": scope})


def _maybe_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def register(app: Starlette) -> None:
    app.routes.append(Route(BASE_PATH, add_memory, methods=["POST"]))
    app.routes.append(Route(BASE_PATH, list_memories, methods=["GET"]))
    app.routes.append(Route(BASE_PATH, delete_memories, methods=["DELETE"]))
    app.routes.append(Route(SEARCH_PATH, search_memory, methods=["POST"]))
