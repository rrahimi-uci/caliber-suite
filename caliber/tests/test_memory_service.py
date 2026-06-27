"""Tests for mem0-backed agent memory (caliber.memory).

Covers the config-dict builder (pure, no mem0), the service's
disabled/enabled construction paths (mem0 mocked — no real pgvector/LLM), the
scope handling, and the Settings exposure.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from caliber.config import CaliberConfig
from caliber.memory import (
    MemoryDisabledError,
    MemoryService,
    build_mem0_config,
)
from caliber.memory.service import _scope
from caliber.routes.settings import _runtime_groups


def _config(**overrides: Any) -> CaliberConfig:
    base: dict[str, Any] = {
        "memory_enabled": True,
        "database_url": "postgresql+psycopg://caliber:secret@db-host:5433/caliberdb",
        "llm_base_url": "http://gateway:5050/v1",
        "memory_llm_model": "gpt-4o-mini",
        "memory_embedder_model": "text-embedding-3-small",
        "memory_embedding_dims": 1536,
        "memory_collection": "caliber_memories",
    }
    base.update(overrides)
    return CaliberConfig(**base)


# ---------------------------------------------------------------------------
# build_mem0_config
# ---------------------------------------------------------------------------


def test_build_mem0_config_pgvector_from_database_url(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = build_mem0_config(_config())

    pg = cfg["vector_store"]
    assert pg["provider"] == "pgvector"
    assert pg["config"]["dbname"] == "caliberdb"
    assert pg["config"]["user"] == "caliber"
    assert pg["config"]["password"] == "secret"
    assert pg["config"]["host"] == "db-host"
    assert pg["config"]["port"] == 5433
    assert pg["config"]["collection_name"] == "caliber_memories"
    assert pg["config"]["embedding_model_dims"] == 1536


def test_build_mem0_config_llm_routes_through_gateway(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = build_mem0_config(_config())

    llm = cfg["llm"]["config"]
    assert cfg["llm"]["provider"] == "openai"
    assert llm["model"] == "gpt-4o-mini"
    assert llm["api_key"] == "sk-test"
    assert llm["openai_base_url"] == "http://gateway:5050/v1"


def test_build_mem0_config_no_gateway_omits_base_url(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = build_mem0_config(_config(llm_base_url=""))
    assert "openai_base_url" not in cfg["llm"]["config"]


def test_build_mem0_config_embedder_base_url(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    # Blank embedder base URL → OpenAI direct (no key set on dict).
    cfg = build_mem0_config(_config())
    assert "openai_base_url" not in cfg["embedder"]["config"]
    assert cfg["embedder"]["config"]["embedding_dims"] == 1536

    # Set → routed.
    cfg2 = build_mem0_config(_config(memory_embedder_base_url="http://gateway:5050/v1"))
    assert cfg2["embedder"]["config"]["openai_base_url"] == "http://gateway:5050/v1"


# ---------------------------------------------------------------------------
# MemoryService construction
# ---------------------------------------------------------------------------


def test_from_config_disabled_raises() -> None:
    with pytest.raises(MemoryDisabledError):
        MemoryService.from_config(CaliberConfig(memory_enabled=False))


def test_from_config_builds_and_threads_infer(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import mem0

    captured: dict[str, Any] = {}

    def _fake_from_config(cfg: dict[str, Any]) -> Any:
        captured["cfg"] = cfg
        return SimpleNamespace(name="fake-memory")

    monkeypatch.setattr(mem0.Memory, "from_config", _fake_from_config)

    service = MemoryService.from_config(_config(memory_infer=False))
    # The built config reached mem0, and infer default was threaded through.
    assert captured["cfg"]["vector_store"]["provider"] == "pgvector"
    assert service._default_infer is False


# ---------------------------------------------------------------------------
# scope handling on the wrapped methods
# ---------------------------------------------------------------------------


class _FakeMemory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.entity_store = _FakeEntityStore()

    def add(self, *args: Any, **kwargs: Any) -> dict:
        self.calls.append(("add", args, kwargs))
        return {"results": []}

    def search(self, *args: Any, **kwargs: Any) -> dict:
        self.calls.append(("search", args, kwargs))
        return {"results": []}

    def get_all(self, *args: Any, **kwargs: Any) -> dict:
        self.calls.append(("get_all", args, kwargs))
        return {"results": []}

    def delete_all(self, *args: Any, **kwargs: Any) -> dict:
        self.calls.append(("delete_all", args, kwargs))
        return {}


class _EntityRow:
    def __init__(self, row_id: str, payload: dict[str, Any]) -> None:
        self.id = row_id
        self.payload = payload


class _FakeEntityStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    def list(self, *, filters: dict[str, Any] | None = None, top_k: int | None = None) -> list[_EntityRow]:
        self.calls.append(("list", filters or {}, top_k or 0))
        return [
            _EntityRow(
                "e1",
                {
                    "data": "blue",
                    "entity_type": "preference",
                    "linked_memory_ids": ["m1", "m2"],
                    "agent_id": "support",
                },
            )
        ]


def test_scope_collects_non_empty_ids() -> None:
    assert _scope("a", None, None) == {"agent_id": "a"}
    assert _scope("a", "u", "r") == {"agent_id": "a", "user_id": "u", "run_id": "r"}
    assert _scope(None, None, None) == {}


def test_add_passes_scope_and_infer() -> None:
    fake = _FakeMemory()
    service = MemoryService(fake, default_infer=True)
    service.add("remember this", agent_id="support", metadata={"k": "v"})

    name, _args, kwargs = fake.calls[0]
    assert name == "add"
    assert kwargs["agent_id"] == "support"
    assert kwargs["infer"] is True
    assert kwargs["metadata"] == {"k": "v"}


def test_add_explicit_infer_override() -> None:
    fake = _FakeMemory()
    MemoryService(fake, default_infer=True).add("x", user_id="u", infer=False)
    assert fake.calls[0][2]["infer"] is False


def test_add_requires_scope() -> None:
    with pytest.raises(ValueError, match="agent_id"):
        MemoryService(_FakeMemory()).add("x")


def test_search_passes_scope_and_top_k() -> None:
    fake = _FakeMemory()
    MemoryService(fake).search("query", agent_id="support", top_k=5)
    _name, args, kwargs = fake.calls[0]
    assert args == ("query",)
    assert kwargs == {"top_k": 5, "agent_id": "support"}


def test_delete_all_requires_scope() -> None:
    with pytest.raises(ValueError):
        MemoryService(_FakeMemory()).delete_all()


def test_get_all_passes_scope_and_top_k() -> None:
    fake = _FakeMemory()
    MemoryService(fake).get_all(agent_id="support", top_k=12)
    _name, args, kwargs = fake.calls[0]
    assert args == ()
    assert kwargs == {"top_k": 12, "agent_id": "support"}


def test_list_entities_passes_scope_and_normalizes_rows() -> None:
    fake = _FakeMemory()
    result = MemoryService(fake).list_entities(agent_id="support", top_k=7)
    assert fake.entity_store.calls[0] == ("list", {"agent_id": "support"}, 7)
    assert result == {
        "results": [
            {
                "id": "e1",
                "entity": "blue",
                "entity_type": "preference",
                "linked_memory_ids": ["m1", "m2"],
                "memory_count": 2,
                "agent_id": "support",
            }
        ]
    }


def test_list_entities_requires_scope() -> None:
    with pytest.raises(ValueError, match="agent_id"):
        MemoryService(_FakeMemory()).list_entities()


# ---------------------------------------------------------------------------
# Settings exposure
# ---------------------------------------------------------------------------


def test_settings_exposes_memory_group() -> None:
    groups = _runtime_groups(CaliberConfig())
    memory = next((g for g in groups if g["id"] == "memory"), None)
    assert memory is not None, "Agent Memory settings group missing"
    keys = {s["key"] for s in memory["settings"]}
    assert {"memory_enabled", "memory_llm_model", "memory_collection"} <= keys
