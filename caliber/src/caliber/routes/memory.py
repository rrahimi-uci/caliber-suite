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
    service = _memory_service(request)
    top_k = _top_k(body.get("top_k"), 10)
    result = _run("search", lambda: service.search(query, top_k=top_k, **scope))
    return envelope_response_dict({"result": result})


async def list_memories(request: Request) -> JSONResponse:
    require_user(request)
    scope = _scope(dict(request.query_params))
    _require_scope(scope)
    service = _memory_service(request)
    top_k = _top_k(_maybe_int(request.query_params.get("top_k")), 50)
    result = _run("list", lambda: service.get_all(top_k=top_k, **scope))
    return envelope_response_dict({"result": result})


async def delete_memories(request: Request) -> JSONResponse:
    require_scopes(request, [SCOPE_ADMIN])
    body = await parse_json_object(request, allow_empty=True)
    scope = _scope({**dict(request.query_params), **body})
    _require_scope(scope)
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
