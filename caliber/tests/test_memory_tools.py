"""Tests for the built-in agent memory tools.

The mem0 service is mocked — these assert the run-scoped factory's wiring
(scope binding, formatting, laziness, graceful failure), the registry stubs,
and the builtin registration. No real pgvector/LLM.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.config import CaliberConfig
from caliber.db.models import CaliberToolRegistry
from caliber.memory import MemoryService
from caliber.workflows.builtin_tools import BUILTIN_TOOL_VERSION, register_builtin_tools
from caliber.workflows.memory_tools import (
    _coerce_text,
    bind_run_memory_tools,
    list_entities,
    memory_add,
    memory_list,
    memory_search,
)


class _FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def search(self, query: str, *, agent_id=None, run_id=None, top_k=10) -> dict[str, Any]:
        self.calls.append(("search", query, agent_id, run_id, top_k))
        return {"results": [{"memory": "user prefers blue", "score": 0.91, "id": "m1"}]}

    def add(
        self, text: str, *, agent_id=None, run_id=None, metadata=None, infer=None
    ) -> dict[str, Any]:
        self.calls.append(("add", text, agent_id, run_id))
        return {"results": [{"id": "m1", "memory": text, "event": "ADD"}]}

    def get_all(self, *, agent_id=None, run_id=None, top_k=50) -> dict[str, Any]:
        self.calls.append(("get_all", agent_id, run_id, top_k))
        return {"results": [{"memory": "user prefers blue", "id": "m1"}]}

    def list_entities(self, *, agent_id=None, run_id=None, top_k=50) -> dict[str, Any]:
        self.calls.append(("list_entities", agent_id, run_id, top_k))
        return {
            "results": [
                {
                    "id": "e1",
                    "entity": "blue",
                    "entity_type": "preference",
                    "linked_memory_ids": ["m1"],
                    "memory_count": 1,
                    "agent_id": agent_id,
                    "run_id": run_id,
                }
            ]
        }


def _enabled() -> CaliberConfig:
    return CaliberConfig(memory_enabled=True)


# ---------------------------------------------------------------------------
# bind_run_memory_tools — gating
# ---------------------------------------------------------------------------


def test_bind_empty_when_disabled() -> None:
    assert bind_run_memory_tools(CaliberConfig(memory_enabled=False), agent_id="wf-1") == {}


def test_bind_empty_without_agent_id() -> None:
    assert bind_run_memory_tools(_enabled(), agent_id=None) == {}
    assert bind_run_memory_tools(_enabled(), agent_id="") == {}


def test_bind_returns_both_tools_when_enabled() -> None:
    tools = bind_run_memory_tools(_enabled(), agent_id="wf-1")
    assert set(tools) == {
        "memory_search",
        "memory_add",
        "memory_list",
        "list_entities",
    }


# ---------------------------------------------------------------------------
# search / add behavior (service mocked)
# ---------------------------------------------------------------------------


def test_search_scopes_and_formats(monkeypatch) -> None:
    fake = _FakeService()
    monkeypatch.setattr(MemoryService, "from_config", lambda config: fake)
    tools = bind_run_memory_tools(_enabled(), agent_id="wf-1")

    result = tools["memory_search"]("what color?")
    assert result == {
        "memories": [{"memory": "user prefers blue", "score": 0.91, "id": "m1"}],
        "count": 1,
    }
    _name, query, agent_id, run_id, top_k = fake.calls[0]
    assert query == "what color?"
    assert agent_id == "wf-1"
    assert top_k == 8  # default


def test_add_scopes(monkeypatch) -> None:
    fake = _FakeService()
    monkeypatch.setattr(MemoryService, "from_config", lambda config: fake)
    tools = bind_run_memory_tools(_enabled(), agent_id="wf-1")

    result = tools["memory_add"]("the user prefers blue")
    assert result == {"added": True, "count": 1}
    assert fake.calls[0] == ("add", "the user prefers blue", "wf-1", None)


def test_list_scopes_and_formats(monkeypatch) -> None:
    fake = _FakeService()
    monkeypatch.setattr(MemoryService, "from_config", lambda config: fake)
    tools = bind_run_memory_tools(_enabled(), agent_id="wf-1")

    result = tools["memory_list"]({"top_k": 3})
    assert result == {
        "memories": [{"memory": "user prefers blue", "score": None, "id": "m1"}],
        "count": 1,
    }
    assert fake.calls[0] == ("get_all", "wf-1", None, 3)


def test_list_entities_scopes_and_formats(monkeypatch) -> None:
    fake = _FakeService()
    monkeypatch.setattr(MemoryService, "from_config", lambda config: fake)
    tools = bind_run_memory_tools(_enabled(), agent_id="wf-1", run_id="run-1")

    result = tools["list_entities"]("5")
    assert result == {
        "entities": [
            {
                "entity": "blue",
                "entity_type": "preference",
                "id": "e1",
                "linked_memory_ids": ["m1"],
                "memory_count": 1,
                "agent_id": "wf-1",
                "run_id": "run-1",
            }
        ],
        "count": 1,
    }
    assert fake.calls[0] == ("list_entities", "wf-1", "run-1", 5)


def test_search_empty_arg_is_error_without_service(monkeypatch) -> None:
    fake = _FakeService()
    monkeypatch.setattr(MemoryService, "from_config", lambda config: fake)
    tools = bind_run_memory_tools(_enabled(), agent_id="wf-1")

    result = tools["memory_search"]("   ")
    assert result["count"] == 0 and "requires a query" in result["error"]
    assert fake.calls == []  # never touched the service


def test_add_empty_arg_is_error() -> None:
    tools = bind_run_memory_tools(_enabled(), agent_id="wf-1")
    result = tools["memory_add"]("")
    assert result["added"] is False and "requires text" in result["error"]


def test_list_entities_service_failure_returns_error_dict(monkeypatch) -> None:
    class _Boom:
        def list_entities(self, **kwargs):
            raise RuntimeError("entity store down")

    monkeypatch.setattr(MemoryService, "from_config", lambda config: _Boom())
    tools = bind_run_memory_tools(_enabled(), agent_id="wf-1")
    result = tools["list_entities"]({})
    assert result["count"] == 0 and "entity store down" in result["error"]


def test_search_service_failure_returns_error_dict(monkeypatch) -> None:
    class _Boom:
        def search(self, *a, **k):
            raise RuntimeError("pgvector down")

    monkeypatch.setattr(MemoryService, "from_config", lambda config: _Boom())
    tools = bind_run_memory_tools(_enabled(), agent_id="wf-1")
    result = tools["memory_search"]("q")
    assert result["count"] == 0 and "pgvector down" in result["error"]


def test_service_built_lazily(monkeypatch) -> None:
    built = {"n": 0}

    def _factory(config):
        built["n"] += 1
        return _FakeService()

    monkeypatch.setattr(MemoryService, "from_config", _factory)
    tools = bind_run_memory_tools(_enabled(), agent_id="wf-1")
    assert built["n"] == 0  # not built at bind time

    tools["memory_search"]("q")
    assert built["n"] == 1
    tools["memory_add"]("x")
    tools["memory_list"]({})
    tools["list_entities"]({})
    assert built["n"] == 1  # reused, not rebuilt


# ---------------------------------------------------------------------------
# helpers + stubs
# ---------------------------------------------------------------------------


def test_coerce_text() -> None:
    assert _coerce_text("  hi  ") == "hi"
    assert _coerce_text({"query": "q"}) == "q"
    assert _coerce_text({"text": "t"}) == "t"
    assert _coerce_text({"nope": 1}) == ""
    assert _coerce_text(None) == ""
    assert _coerce_text(42) == "42"


def test_registry_stubs_report_unavailable() -> None:
    assert memory_search("q")["memories"] == []
    assert "unavailable" in memory_search("q")["error"]
    assert memory_add("x")["added"] is False
    assert "unavailable" in memory_add("x")["error"]
    assert memory_list({})["memories"] == []
    assert "unavailable" in memory_list({})["error"]
    assert list_entities({})["entities"] == []
    assert "unavailable" in list_entities({})["error"]


# ---------------------------------------------------------------------------
# builtin registration
# ---------------------------------------------------------------------------


def test_register_builtin_tools_includes_memory(db_session: Session) -> None:
    register_builtin_tools(db_session)
    db_session.commit()

    rows = {
        r.name: r
        for r in db_session.execute(
            select(CaliberToolRegistry).where(
                CaliberToolRegistry.name.in_(
                    ["memory_search", "memory_add", "memory_list", "list_entities"]
                ),
                CaliberToolRegistry.version == BUILTIN_TOOL_VERSION,
            )
        ).scalars()
    }
    assert set(rows) == {"memory_search", "memory_add", "memory_list", "list_entities"}
    assert rows["memory_search"].module_path == "caliber.workflows.memory_tools"
    assert rows["memory_search"].side_effect_level == "read"
    assert rows["memory_search"].allow_in_preview is True
    assert rows["memory_list"].side_effect_level == "read"
    assert rows["memory_list"].allow_in_preview is True
    assert rows["list_entities"].side_effect_level == "read"
    assert rows["list_entities"].allow_in_preview is True
    assert rows["memory_add"].side_effect_level == "write"
    assert rows["memory_add"].requires_approval is True


def test_register_builtin_tools_is_idempotent(db_session: Session) -> None:
    register_builtin_tools(db_session)
    db_session.commit()
    second = register_builtin_tools(db_session)
    db_session.commit()
    assert second == 0
