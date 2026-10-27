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
    PARTITION_KEYS,
    MemoryDependencyError,
    MemoryDisabledError,
    MemoryLifecycleUnavailableError,
    MemoryService,
    user_namespace,
)
from caliber.routes._deps import envelope_response_dict, parse_json_object

BASE_PATH = "/ajax-api/2.0/mlflow/caliber/memory"
SEARCH_PATH = BASE_PATH + "/search"

_SCOPE_KEYS = ("agent_id", "user_id", "run_id")

#: The one answer to every refused partition in the reference profile: a partition that does not exist, one that
#: belongs to someone else and one that is only a free-form label are indistinguishable, so no id is confirmed.
_PARTITION_NOT_FOUND = "memory partition not found"


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


def _strict(request: Request) -> bool:
    """True in the reference profile: partitions are owned and derived, never free-form (see ``_authorize_strict_scope``)."""
    return bool(request.app.state.config.profile == "reference")


def _strict_scope(scope: dict[str, Any]) -> dict[str, str]:
    """Strict mode: a scope key that is present must be a non-empty string; a malformed one is a 400, not ignored."""
    clean: dict[str, str] = {}
    for key in _SCOPE_KEYS:
        if key not in scope:
            continue
        value = scope[key]
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(status_code=400, detail=f"{key} must be a non-empty string")
        clean[key] = value
    return clean


def _resolve_scope(request: Request, raw: dict[str, Any]) -> dict[str, str]:
    """The partition this request acts on, authorized.

    Development: the caller's labels, checked as before. Reference: every key the request names must be a governed
    resource or the caller's own identity, and the partition is then pinned to the caller. With an agent or
    workflow, the memory is that caller's within it (``user_id`` is added); with none, it is the caller's own
    namespace, qualified by the active project. A client therefore never chooses whose memory it reads or writes.
    """
    if _strict(request):
        from caliber.auth import resolve_identity  # noqa: PLC0415

        scope = _strict_scope(raw)
        _authorize_strict_scope(request, scope)
        identity = resolve_identity(request)
        if "agent_id" in scope:
            scope["user_id"] = identity.user_id
        else:
            scope["user_id"] = user_namespace(identity.user_id, identity.active_project_id)
        return scope
    scope = _scope(raw)
    _require_scope(scope)
    _authorize_scope(request, scope)
    return scope


def _authorize_strict_scope(request: Request, scope: dict[str, str]) -> None:
    """Reference profile: no free-form partition, no admin exemption, one generic refusal.

    ``user_id`` is the caller's own identity (admins included: an admin reading another user's memory is a
    decision for the owned lifecycle, not a query parameter). ``agent_id`` names an agent or a workflow, and
    ``run_id`` a workflow run, that the caller can see; a label that names nothing is refused as well, because
    nothing owns it. Every refusal is the same 404, so which of those it was is not disclosed.
    """
    from caliber.auth import resolve_identity  # noqa: PLC0415
    from caliber.db.models import CaliberWorkflow  # noqa: PLC0415
    from caliber.db.scoping import get_visible  # noqa: PLC0415
    from caliber.routes._deps import get_session_factory  # noqa: PLC0415

    identity = resolve_identity(request)
    refusal = HTTPException(status_code=404, detail=_PARTITION_NOT_FOUND)
    if scope.get("user_id") not in (None, identity.user_id):
        raise refusal
    if "agent_id" not in scope and "run_id" in scope:
        raise HTTPException(
            status_code=403,
            detail="memory is partitioned by an agent/workflow or a user in the reference profile, "
            "not by a run alone",
        )
    factory = get_session_factory(request)
    with factory() as session:
        if "agent_id" in scope:
            visible = any(
                get_visible(session, model, column, scope["agent_id"], identity) is not None
                for model, column in (
                    (CaliberAgentConfig, CaliberAgentConfig.agent_id),
                    (CaliberWorkflow, CaliberWorkflow.workflow_id),
                )
            )
            if not visible:
                raise refusal
        if "run_id" in scope and not _run_visible(session, scope["run_id"], identity):
            raise refusal


def _run_visible(session: Any, run_id: str, identity: Any) -> bool:
    """A workflow run carries no visibility columns of its own: it is visible when its parent workflow is."""
    from sqlalchemy import select  # noqa: PLC0415

    from caliber.db.models import CaliberWorkflow, CaliberWorkflowRun  # noqa: PLC0415
    from caliber.db.scoping import get_visible  # noqa: PLC0415

    workflow_id = session.execute(
        select(CaliberWorkflowRun.workflow_id).where(CaliberWorkflowRun.workflow_run_id == run_id)
    ).scalar_one_or_none()
    if workflow_id is None:
        return False
    return (
        get_visible(session, CaliberWorkflow, CaliberWorkflow.workflow_id, workflow_id, identity)
        is not None
    )


def _refuse_unsupported(request: Request, *, operation: str) -> None:
    """Reference profile: durable operations that need the owned lifecycle (M20) are refused, not degraded."""
    if _strict(request):
        raise HTTPException(
            status_code=403,
            detail=f"memory {operation} is unavailable in the reference profile until the owned memory "
            "lifecycle (retention and legal hold) exists",
        )


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
    if requested_user and requested_user != identity.user_id and SCOPE_ADMIN not in identity.scopes:
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
            if key == "run_id":
                # A run has no visibility columns of its own (``get_visible`` would raise): its parent
                # workflow's visibility decides.
                if _run_visible(session, scope[key], identity):
                    continue
            elif get_visible(session, model, column, scope[key], identity) is not None:
                continue  # a governed resource the caller can see
            exists = session.execute(select(column).where(column == scope[key])).first()
            if exists is not None:
                # The row is real and the caller cannot see it. This is the case
                # worth refusing; an absent row is a free-form partition label.
                raise HTTPException(
                    status_code=404,
                    detail=f"{key} {scope[key]!r} not found",
                )


#: Reference profile: the most memories one request may ask for.
_STRICT_TOP_K_MAX = 100


def _bounded_top_k(request: Request, top_k: int) -> int:
    return min(top_k, _STRICT_TOP_K_MAX) if _strict(request) else top_k


def _top_k(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if value >= 1 else default


def _run(operation: str, fn: Any) -> Any:
    """Invoke a memory call, mapping backend failures to a clean 502."""
    try:
        return fn()
    except MemoryLifecycleUnavailableError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
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
    scope = _resolve_scope(request, body)
    service = _memory_service(request)
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else None
    infer = body.get("infer") if isinstance(body.get("infer"), bool) else None
    if _strict(request) and metadata and PARTITION_KEYS & set(metadata):
        raise HTTPException(
            status_code=400,
            detail="metadata may not carry partition keys (user_id, agent_id, run_id, actor_id)",
        )
    if _strict(request) and infer is not False:
        # An omitted ``infer`` means the server default (LLM extraction); the strict profile writes verbatim only.
        raise HTTPException(
            status_code=403,
            detail="LLM-inferred memory writes are unavailable in the reference profile; send infer=false",
        )
    result = _run("add", lambda: service.add(text, metadata=metadata, infer=infer, **scope))
    return envelope_response_dict({"result": result}, status_code=201)


async def search_memory(request: Request) -> JSONResponse:
    require_user(request)
    body = await parse_json_object(request)
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail="'query' is required")
    scope = _resolve_scope(request, body)
    service = _memory_service(request)
    top_k = _bounded_top_k(request, _top_k(body.get("top_k"), 10))
    result = _run("search", lambda: service.search(query, top_k=top_k, **scope))
    return envelope_response_dict({"result": result})


async def list_memories(request: Request) -> JSONResponse:
    require_user(request)
    scope = _resolve_scope(request, dict(request.query_params))
    service = _memory_service(request)
    top_k = _bounded_top_k(request, _top_k(_maybe_int(request.query_params.get("top_k")), 50))
    result = _run("list", lambda: service.get_all(top_k=top_k, **scope))
    return envelope_response_dict({"result": result})


async def delete_memories(request: Request) -> JSONResponse:
    require_scopes(request, [SCOPE_ADMIN])
    _refuse_unsupported(request, operation="deletion")
    body = await parse_json_object(request, allow_empty=True)
    scope = _resolve_scope(request, {**dict(request.query_params), **body})
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
