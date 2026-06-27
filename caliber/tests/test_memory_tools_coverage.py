"""Coverage tests for the built-in agent memory tools.

These target the uncovered error/empty/disabled/alternate-arg branches not
exercised by ``tests/test_memory_tools.py``: ``_coerce_top_k`` loose inputs,
non-dict item formatting in ``_format_search``/``_format_entities``, and the
typed-error (``MemoryDisabledError`` / ``MemoryDependencyError``) handlers in
the bound ``memory_search`` / ``memory_add`` / ``memory_list`` /
``list_entities`` closures plus their generic-exception fallbacks. The mem0
service is mocked — no real pgvector/LLM.
"""

from __future__ import annotations

from typing import Any

from caliber.config import CaliberConfig
from caliber.memory import (
    MemoryDependencyError,
    MemoryDisabledError,
    MemoryService,
)
from caliber.workflows.memory_tools import (
    _coerce_top_k,
    _format_entities,
    _format_search,
    bind_run_memory_tools,
)

_DEFAULT = 20


def _enabled() -> CaliberConfig:
    return CaliberConfig(memory_enabled=True)


# ---------------------------------------------------------------------------
# _coerce_top_k — loose argument coercion
# ---------------------------------------------------------------------------


def test_coerce_top_k_bool_returns_default() -> None:
    # bool is an int subclass, but must be ignored (line 99).
    assert _coerce_top_k(True, _DEFAULT) == _DEFAULT
    assert _coerce_top_k(False, _DEFAULT) == _DEFAULT


def test_coerce_top_k_int_below_one_returns_default() -> None:
    # int >= 1 passes through; int < 1 falls back to default (line 101).
    assert _coerce_top_k(5, _DEFAULT) == 5
    assert _coerce_top_k(0, _DEFAULT) == _DEFAULT
    assert _coerce_top_k(-3, _DEFAULT) == _DEFAULT


def test_coerce_top_k_dict_non_int_and_bool_values_return_default() -> None:
    # dict with a bool value (rejected) and a non-int value falls through.
    assert _coerce_top_k({"top_k": True}, _DEFAULT) == _DEFAULT
    assert _coerce_top_k({"limit": "12"}, _DEFAULT) == _DEFAULT
    # ...but a valid alternate key wins.
    assert _coerce_top_k({"count": 7}, _DEFAULT) == 7


def test_coerce_top_k_string_nondigit_and_negative_return_default() -> None:
    # numeric string parses; non-digit / signed strings fall to default (line 112).
    assert _coerce_top_k("9", _DEFAULT) == 9
    assert _coerce_top_k("abc", _DEFAULT) == _DEFAULT
    assert _coerce_top_k("-4", _DEFAULT) == _DEFAULT
    assert _coerce_top_k("  ", _DEFAULT) == _DEFAULT


def test_coerce_top_k_unsupported_type_returns_default() -> None:
    # Anything else (None, list, float) hits the trailing default (line 112).
    assert _coerce_top_k(None, _DEFAULT) == _DEFAULT
    assert _coerce_top_k([1, 2], _DEFAULT) == _DEFAULT
    assert _coerce_top_k(3.5, _DEFAULT) == _DEFAULT


# ---------------------------------------------------------------------------
# _format_search / _format_entities — non-dict items
# ---------------------------------------------------------------------------


def test_format_search_non_dict_items_are_stringified() -> None:
    # Bare strings/ints in the result list are wrapped (lines 121-122).
    result = _format_search(["just a string", 42])
    assert result == {
        "memories": [{"memory": "just a string"}, {"memory": "42"}],
        "count": 2,
    }


def test_format_search_none_result_is_empty() -> None:
    assert _format_search(None) == {"memories": [], "count": 0}


def test_format_entities_non_dict_items_are_stringified() -> None:
    # Bare strings in the entity list are wrapped (lines 143-144).
    result = _format_entities(["alice", 7])
    assert result == {
        "entities": [{"entity": "alice"}, {"entity": "7"}],
        "count": 2,
    }


def test_format_entities_none_result_is_empty() -> None:
    assert _format_entities(None) == {"entities": [], "count": 0}


# ---------------------------------------------------------------------------
# Typed-error (MemoryDisabledError / MemoryDependencyError) handlers
# ---------------------------------------------------------------------------


class _TypedErrorService:
    """Every operation raises a typed memory error to hit the typed handlers."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def search(self, *a: Any, **k: Any) -> dict[str, Any]:
        raise self._exc

    def add(self, *a: Any, **k: Any) -> dict[str, Any]:
        raise self._exc

    def get_all(self, *a: Any, **k: Any) -> dict[str, Any]:
        raise self._exc

    def list_entities(self, *a: Any, **k: Any) -> dict[str, Any]:
        raise self._exc


def test_search_disabled_error_returns_error_dict(monkeypatch) -> None:
    exc = MemoryDisabledError("memory is disabled")
    monkeypatch.setattr(MemoryService, "from_config", lambda config: _TypedErrorService(exc))
    tools = bind_run_memory_tools(_enabled(), agent_id="wf-1")
    result = tools["memory_search"]("q")
    assert result == {"error": "memory is disabled", "memories": [], "count": 0}


def test_add_dependency_error_returns_error_dict(monkeypatch) -> None:
    exc = MemoryDependencyError("mem0 not installed")
    monkeypatch.setattr(MemoryService, "from_config", lambda config: _TypedErrorService(exc))
    tools = bind_run_memory_tools(_enabled(), agent_id="wf-1")
    result = tools["memory_add"]("remember this")
    assert result == {"error": "mem0 not installed", "added": False}


def test_list_disabled_error_returns_error_dict(monkeypatch) -> None:
    exc = MemoryDisabledError("memory is disabled")
    monkeypatch.setattr(MemoryService, "from_config", lambda config: _TypedErrorService(exc))
    tools = bind_run_memory_tools(_enabled(), agent_id="wf-1")
    result = tools["memory_list"]({})
    assert result == {"error": "memory is disabled", "memories": [], "count": 0}


def test_list_entities_dependency_error_returns_error_dict(monkeypatch) -> None:
    exc = MemoryDependencyError("graph store unavailable")
    monkeypatch.setattr(MemoryService, "from_config", lambda config: _TypedErrorService(exc))
    tools = bind_run_memory_tools(_enabled(), agent_id="wf-1")
    result = tools["list_entities"]({})
    assert result == {"error": "graph store unavailable", "entities": [], "count": 0}


# ---------------------------------------------------------------------------
# Generic-exception fallbacks (the broad `except Exception` arms)
# ---------------------------------------------------------------------------


class _BoomService:
    def add(self, *a: Any, **k: Any) -> dict[str, Any]:
        raise RuntimeError("write store down")

    def get_all(self, *a: Any, **k: Any) -> dict[str, Any]:
        raise RuntimeError("read store down")


def test_add_generic_failure_returns_error_dict(monkeypatch) -> None:
    monkeypatch.setattr(MemoryService, "from_config", lambda config: _BoomService())
    tools = bind_run_memory_tools(_enabled(), agent_id="wf-1")
    result = tools["memory_add"]("remember this")
    assert result["added"] is False
    assert "memory_add failed" in result["error"]
    assert "write store down" in result["error"]


def test_list_generic_failure_returns_error_dict(monkeypatch) -> None:
    monkeypatch.setattr(MemoryService, "from_config", lambda config: _BoomService())
    tools = bind_run_memory_tools(_enabled(), agent_id="wf-1")
    result = tools["memory_list"]({})
    assert result["count"] == 0
    assert "memory_list failed" in result["error"]
    assert "read store down" in result["error"]
