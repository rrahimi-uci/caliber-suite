"""mem0-backed agent long-term memory, wired to CALIBER's pgvector + gateway.

Design
------
* **Vector store** = the platform Postgres (``CALIBER_DATABASE_URL``) via mem0's
  ``pgvector`` provider — the same instance that backs the MCP DB server's
  pgvector tools, so memory lives next to the rest of the platform's data.
* **Extraction LLM** routes through the configured gateway
  (``CALIBER_LLM_BASE_URL``) — mem0's OpenAI LLM honors ``openai_base_url``.
* **Embedder** points at ``CALIBER_MEMORY_EMBEDDER_BASE_URL`` (blank → OpenAI
  direct, since not every gateway proxies ``/embeddings``).
* **Scope** = mem0's ``agent_id`` / ``user_id`` / ``run_id`` so a team shares
  one store with per-agent / per-user / per-run partitioning.

``mem0`` is the optional ``[memory]`` extra and is imported lazily inside
:meth:`MemoryService.from_config`; the module top level never imports it, so the
core / FakeLLMProvider path stays mem0-free. Default-off via
``CALIBER_MEMORY_ENABLED``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy.engine import make_url

from caliber.config import CaliberConfig
from caliber.secrets import resolve_secret

logger = logging.getLogger("caliber.memory")


class MemoryDisabledError(RuntimeError):
    """Raised when memory is used while ``CALIBER_MEMORY_ENABLED`` is false."""


class MemoryDependencyError(RuntimeError):
    """Raised when memory is enabled but the ``[memory]`` extra is not installed."""


def build_mem0_config(config: CaliberConfig) -> dict[str, Any]:
    """Build the ``mem0.Memory.from_config`` dict from :class:`CaliberConfig`.

    Pure function (does not import mem0) so it is unit-testable without the
    extra. The pgvector connection is parsed from ``database_url``; the LLM and
    embedder base URLs come from the gateway / embedder settings.
    """
    url = make_url(config.database_url)
    api_key = resolve_secret(config.llm_api_key_env) or ""

    llm_config: dict[str, Any] = {"model": config.memory_llm_model, "api_key": api_key}
    if config.llm_base_url:
        # mem0's OpenAI LLM reads ``openai_base_url`` → route extraction through
        # the gateway.
        llm_config["openai_base_url"] = config.llm_base_url

    embedder_config: dict[str, Any] = {
        "model": config.memory_embedder_model,
        "embedding_dims": config.memory_embedding_dims,
        "api_key": api_key,
    }
    if config.memory_embedder_base_url:
        embedder_config["openai_base_url"] = config.memory_embedder_base_url

    return {
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "dbname": url.database,
                "user": url.username,
                "password": url.password,
                "host": url.host,
                "port": url.port or 5432,
                "collection_name": config.memory_collection,
                "embedding_model_dims": config.memory_embedding_dims,
            },
        },
        "llm": {"provider": "openai", "config": llm_config},
        "embedder": {"provider": "openai", "config": embedder_config},
    }


def _scope(agent_id: str | None, user_id: str | None, run_id: str | None) -> dict[str, str]:
    """Collect the non-empty mem0 scope identifiers."""
    scope: dict[str, str] = {}
    if agent_id:
        scope["agent_id"] = agent_id
    if user_id:
        scope["user_id"] = user_id
    if run_id:
        scope["run_id"] = run_id
    return scope


def _normalize_entity_rows(rows: Any) -> list[dict[str, Any]]:
    """Convert mem0 entity-store rows into stable plain dicts.

    mem0's public OSS API does not currently expose a first-class
    ``list_entities`` helper, but it does maintain an entity vector store with
    predictable payload keys. This normalizes those low-level rows so CALIBER's
    tool/runtime surface does not leak backend-specific objects.
    """
    if isinstance(rows, tuple):
        rows = rows[0] if rows else []
    if rows is None:
        rows = []

    normalized: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            row_id = row.get("id")
            payload = row.get("payload")
        else:
            row_id = getattr(row, "id", None)
            payload = getattr(row, "payload", None)
        payload = payload if isinstance(payload, dict) else {}

        linked_memory_ids = payload.get("linked_memory_ids")
        if isinstance(linked_memory_ids, list):
            linked = [str(item) for item in linked_memory_ids if item is not None]
        else:
            linked = []

        entity_value = payload.get("data")
        entity = entity_value if isinstance(entity_value, str) else (
            str(entity_value) if entity_value is not None else ""
        )
        item: dict[str, Any] = {
            "id": str(row_id) if row_id is not None else "",
            "entity": entity,
            "entity_type": payload.get("entity_type"),
            "linked_memory_ids": linked,
            "memory_count": len(linked),
        }
        for key in ("agent_id", "user_id", "run_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                item[key] = value
        normalized.append(item)
    return normalized


class MemoryService:
    """Thin, scope-aware wrapper over a mem0 ``Memory`` instance."""

    def __init__(self, memory: Any, *, default_infer: bool = True) -> None:
        self._memory = memory
        self._default_infer = default_infer

    @classmethod
    def from_config(cls, config: CaliberConfig) -> MemoryService:
        """Construct a live service from config (pgvector + gateway).

        Raises
        ------
        MemoryDisabledError
            If ``memory_enabled`` is false.
        MemoryDependencyError
            If the ``[memory]`` extra (``mem0ai``) is not installed.
        """
        if not config.memory_enabled:
            raise MemoryDisabledError("agent memory is disabled; set CALIBER_MEMORY_ENABLED=true")
        # Disable mem0's anonymous telemetry for a private deployment. Must be
        # set before importing mem0 (read at import time).
        os.environ.setdefault("MEM0_TELEMETRY", "False")
        try:
            from mem0 import Memory  # noqa: PLC0415 -- optional [memory] extra
        except ImportError as exc:  # pragma: no cover - exercised via dep-absent envs
            raise MemoryDependencyError(
                "agent memory requires the [memory] extra: pip install 'caliber[memory]'"
            ) from exc
        memory = Memory.from_config(build_mem0_config(config))
        return cls(memory, default_infer=config.memory_infer)

    def add(
        self,
        text: str,
        *,
        agent_id: str | None = None,
        user_id: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        infer: bool | None = None,
    ) -> Any:
        """Store ``text`` as memory under the given scope.

        At least one of ``agent_id`` / ``user_id`` / ``run_id`` is required —
        mem0 partitions by these.
        """
        scope = _scope(agent_id, user_id, run_id)
        if not scope:
            raise ValueError("at least one of agent_id / user_id / run_id is required")
        return self._memory.add(
            text,
            metadata=metadata,
            infer=self._default_infer if infer is None else infer,
            **scope,
        )

    def search(
        self,
        query: str,
        *,
        agent_id: str | None = None,
        user_id: str | None = None,
        run_id: str | None = None,
        top_k: int = 10,
    ) -> Any:
        """Semantic-search memories in the given scope."""
        return self._memory.search(query, top_k=top_k, **_scope(agent_id, user_id, run_id))

    def get_all(
        self,
        *,
        agent_id: str | None = None,
        user_id: str | None = None,
        run_id: str | None = None,
        top_k: int = 50,
    ) -> Any:
        """List memories in the given scope."""
        return self._memory.get_all(top_k=top_k, **_scope(agent_id, user_id, run_id))

    def list_entities(
        self,
        *,
        agent_id: str | None = None,
        user_id: str | None = None,
        run_id: str | None = None,
        top_k: int = 50,
    ) -> dict[str, Any]:
        """List extracted entities in the given scope.

        mem0's OSS ``Memory`` object stores entities in an auxiliary vector
        store keyed by the same scope identifiers. CALIBER exposes that through
        a small normalized wrapper so workflows and operators can inspect the
        current memory graph surface.
        """
        scope = _scope(agent_id, user_id, run_id)
        if not scope:
            raise ValueError("at least one of agent_id / user_id / run_id is required")
        raw = self._memory.entity_store.list(filters=scope, top_k=top_k)
        return {"results": _normalize_entity_rows(raw)}

    def delete_all(
        self,
        *,
        agent_id: str | None = None,
        user_id: str | None = None,
        run_id: str | None = None,
    ) -> Any:
        """Delete all memories in the given scope."""
        scope = _scope(agent_id, user_id, run_id)
        if not scope:
            raise ValueError("at least one of agent_id / user_id / run_id is required")
        return self._memory.delete_all(**scope)
