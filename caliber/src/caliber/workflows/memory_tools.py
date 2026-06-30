"""Built-in agent memory tools — ``memory_search`` / ``memory_add`` and friends.

These let a workflow agent recall and persist long-term memory (mem0 over the
platform pgvector + gateway LLM — see :mod:`caliber.memory`). Two layers, mirroring
the run working-directory file tools:

* **Registry stubs** (:func:`memory_search`, :func:`memory_add`,
  :func:`memory_list`, :func:`list_entities`) — what the
  ``CaliberToolRegistry`` rows resolve to by ``module_path:callable_name``.
  They return a clear "unavailable" payload, because memory needs a *run
  scope* that only exists at execution time.
* **Run-scoped closures** (:func:`bind_run_memory_tools`) — bound to the run's
  config + scope and injected into ``execute(..., extra_tools=...)``, overriding
  the stubs. The runtime invokes a tool as ``fn(input_text)``: for
  ``memory_search`` that string is the query, for ``memory_add`` it is the text
  to remember. ``memory_list`` and ``list_entities`` accept either an empty
  argument or a small options dict like ``{"top_k": 20}``. Scope (which agent
  / run the memory belongs to) is bound in the closure, never supplied by the
  model.

``bind_run_memory_tools`` returns ``{}`` when memory is disabled or no scope is
available, so callers can always merge it into ``extra_tools`` unconditionally.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from caliber.config import CaliberConfig
from caliber.memory import (
    MemoryDependencyError,
    MemoryDisabledError,
    MemoryService,
)

logger = logging.getLogger("caliber.workflows.memory_tools")

_DEFAULT_TOP_K = 8
_DEFAULT_LIST_TOP_K = 20
_UNAVAILABLE = (
    "agent memory is unavailable here: it is disabled "
    "(set CALIBER_MEMORY_ENABLED=true) or this context is not run-scoped"
)


# ---------------------------------------------------------------------------
# Registry stubs (resolved from the tool registry row when not injected)
# ---------------------------------------------------------------------------


def memory_search(arg: object = "") -> dict[str, Any]:  # noqa: ARG001 - registry stub
    """Stub: real implementation is injected per-run via ``bind_run_memory_tools``."""
    return {"error": _UNAVAILABLE, "memories": [], "count": 0}


def memory_add(arg: object = "") -> dict[str, Any]:  # noqa: ARG001 - registry stub
    """Stub: real implementation is injected per-run via ``bind_run_memory_tools``."""
    return {"error": _UNAVAILABLE, "added": False}


def memory_list(arg: object = "") -> dict[str, Any]:  # noqa: ARG001 - registry stub
    """Stub: real implementation is injected per-run via ``bind_run_memory_tools``."""
    return {"error": _UNAVAILABLE, "memories": [], "count": 0}


def list_entities(arg: object = "") -> dict[str, Any]:  # noqa: ARG001 - registry stub
    """Stub: real implementation is injected per-run via ``bind_run_memory_tools``."""
    return {"error": _UNAVAILABLE, "entities": [], "count": 0}


# ---------------------------------------------------------------------------
# Run-scoped implementations
# ---------------------------------------------------------------------------


def _coerce_text(arg: object) -> str:
    """Reduce a tool argument to the operative string.

    The runtime passes a single positional argument. It is usually the model's
    free-text input, but be defensive about a dict (some callers pass
    ``{"query": ...}`` / ``{"text": ...}``).
    """
    if isinstance(arg, str):
        return arg.strip()
    if isinstance(arg, dict):
        for key in ("query", "text", "input", "memory", "content"):
            value = arg.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    return str(arg).strip() if arg is not None else ""


def _coerce_top_k(arg: object, default: int) -> int:
    """Parse an optional top-k override from a loose tool argument."""
    if isinstance(arg, bool):
        return default
    if isinstance(arg, int):
        return arg if arg >= 1 else default
    if isinstance(arg, dict):
        for key in ("top_k", "limit", "count"):
            value = arg.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                return value
        return default
    if isinstance(arg, str):
        stripped = arg.strip()
        if stripped.isdigit():
            return max(int(stripped), 1)
    return default


def _format_search(result: Any) -> dict[str, Any]:
    """Normalize a mem0 search result into a compact, model-friendly payload."""
    items = result.get("results", result) if isinstance(result, dict) else result
    memories: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            memories.append({"memory": str(item)})
            continue
        memories.append(
            {
                "memory": item.get("memory") or item.get("text") or "",
                "score": item.get("score"),
                "id": item.get("id"),
            }
        )
    return {"memories": memories, "count": len(memories)}


def _format_add(result: Any) -> dict[str, Any]:
    items = result.get("results", []) if isinstance(result, dict) else (result or [])
    return {"added": True, "count": len(items) if isinstance(items, list) else 1}


def _format_entities(result: Any) -> dict[str, Any]:
    items = result.get("results", result) if isinstance(result, dict) else result
    entities: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            entities.append({"entity": str(item)})
            continue
        payload = {
            "entity": item.get("entity") or item.get("data") or "",
            "entity_type": item.get("entity_type"),
            "id": item.get("id"),
            "linked_memory_ids": item.get("linked_memory_ids") or [],
            "memory_count": item.get("memory_count"),
        }
        for key in ("agent_id", "user_id", "run_id"):
            if key in item:
                payload[key] = item[key]
        entities.append(payload)
    return {"entities": entities, "count": len(entities)}


def bind_run_memory_tools(
    config: CaliberConfig,
    *,
    agent_id: str | None,
    run_id: str | None = None,
    top_k: int = _DEFAULT_TOP_K,
) -> dict[str, Callable[..., Any]]:
    """Return memory tool closures bound to a run scope.

    Returns ``{}`` when memory is disabled or ``agent_id`` is missing (no scope
    to partition by) — so the caller's stub rows remain in effect. The mem0
    service is built lazily on first tool use so a run that never touches memory
    never opens a pgvector connection.
    """
    if not config.memory_enabled or not agent_id:
        return {}

    holder: list[MemoryService] = []

    def _service() -> MemoryService:
        if not holder:
            holder.append(MemoryService.from_config(config))
        return holder[0]

    def _search(arg: object = "") -> dict[str, Any]:
        query = _coerce_text(arg)
        if not query:
            return {"error": "memory_search requires a query string", "memories": [], "count": 0}
        try:
            result = _service().search(query, agent_id=agent_id, run_id=run_id, top_k=top_k)
            return _format_search(result)
        except (MemoryDisabledError, MemoryDependencyError) as exc:
            return {"error": str(exc), "memories": [], "count": 0}
        except Exception as exc:  # never let a memory failure crash the run
            logger.warning("memory_search failed for agent=%s: %s", agent_id, exc)
            return {"error": f"memory_search failed: {exc}", "memories": [], "count": 0}

    def _add(arg: object = "") -> dict[str, Any]:
        text = _coerce_text(arg)
        if not text:
            return {"error": "memory_add requires text to remember", "added": False}
        try:
            result = _service().add(text, agent_id=agent_id, run_id=run_id)
            return _format_add(result)
        except (MemoryDisabledError, MemoryDependencyError) as exc:
            return {"error": str(exc), "added": False}
        except Exception as exc:  # never let a memory failure crash the run
            logger.warning("memory_add failed for agent=%s: %s", agent_id, exc)
            return {"error": f"memory_add failed: {exc}", "added": False}

    def _list(arg: object = "") -> dict[str, Any]:
        top_k = _coerce_top_k(arg, _DEFAULT_LIST_TOP_K)
        try:
            result = _service().get_all(agent_id=agent_id, run_id=run_id, top_k=top_k)
            return _format_search(result)
        except (MemoryDisabledError, MemoryDependencyError) as exc:
            return {"error": str(exc), "memories": [], "count": 0}
        except Exception as exc:  # never let a memory failure crash the run
            logger.warning("memory_list failed for agent=%s: %s", agent_id, exc)
            return {"error": f"memory_list failed: {exc}", "memories": [], "count": 0}

    def _list_entities(arg: object = "") -> dict[str, Any]:
        top_k = _coerce_top_k(arg, _DEFAULT_LIST_TOP_K)
        try:
            result = _service().list_entities(agent_id=agent_id, run_id=run_id, top_k=top_k)
            return _format_entities(result)
        except (MemoryDisabledError, MemoryDependencyError) as exc:
            return {"error": str(exc), "entities": [], "count": 0}
        except Exception as exc:  # never let a memory failure crash the run
            logger.warning("list_entities failed for agent=%s: %s", agent_id, exc)
            return {"error": f"list_entities failed: {exc}", "entities": [], "count": 0}

    return {
        "memory_search": _search,
        "memory_add": _add,
        "memory_list": _list,
        "list_entities": _list_entities,
    }


__all__ = [
    "bind_run_memory_tools",
    "list_entities",
    "memory_add",
    "memory_list",
    "memory_search",
]
