"""Unit tests for the tool-sandbox subprocess runner (``tool_sandbox/_runner``).

The runner is normally launched as a separate ``python -I`` process, so the
parent's tests never exercise its lines (0% coverage). These tests call its
functions directly — including assertions that the restricted ``SAFE_BUILTINS``
namespace actually blocks dangerous operations (``open``, ``import``), which is
the whole point of the sandbox.
"""

from __future__ import annotations

import io
import json

import pytest

from caliber.tool_sandbox import _runner


# --------------------------------------------------------------------------- #
# _namespace — restricted execution environment
# --------------------------------------------------------------------------- #
def test_namespace_defines_and_returns_callable() -> None:
    ns, fn = _runner._namespace("def handler(x):\n    return x + 1", "handler")
    assert callable(fn)
    assert fn(1) == 2
    assert ns["__name__"] == "__caliber_tool__"


def test_namespace_missing_callable_raises() -> None:
    with pytest.raises(ValueError, match="was not defined"):
        _runner._namespace("y = 1", "handler")


def test_namespace_exposes_allowed_modules() -> None:
    src = "def handler():\n    return math.sqrt(16) + len(re.findall('a', 'aaa'))"
    _ns, fn = _runner._namespace(src, "handler")
    assert fn() == 4.0 + 3


def test_namespace_blocks_open_builtin() -> None:
    # `open` is not in SAFE_BUILTINS, so a tool that calls it must fail at runtime.
    _ns, fn = _runner._namespace("def handler():\n    return open('/etc/passwd').read()", "handler")
    with pytest.raises(NameError):
        fn()


def test_namespace_blocks_import_statement() -> None:
    # No __import__ in SAFE_BUILTINS => `import os` inside a tool fails.
    _ns, fn = _runner._namespace("def handler():\n    import os\n    return os.getcwd()", "handler")
    with pytest.raises(ImportError):
        fn()


def test_namespace_blocks_dunder_subclasses_escape() -> None:
    """Regression (#11): the classic restricted-eval escape — walk
    ``__class__``/``__subclasses__`` back to the real builtins to recover
    ``__import__`` — must be rejected at compile time, not silently allowed."""
    escape = "def handler():\n    return ().__class__.__bases__[0].__subclasses__()\n"
    with pytest.raises(ValueError, match="private/dunder attribute"):
        _runner._namespace(escape, "handler")


def test_namespace_object_builtin_removed() -> None:
    """`object` is no longer exposed, so `object.__subclasses__()` can't even
    name `object` (and the dunder access is blocked regardless)."""
    with pytest.raises(ValueError, match="private/dunder attribute"):
        _runner._namespace("def handler():\n    return object.__subclasses__()", "handler")


def test_namespace_allows_ordinary_public_attribute_access() -> None:
    """The guard must NOT break normal tool code using public attributes/methods."""
    src = "def handler(items):\n    items.append(99)\n    return math.sqrt(sum(items))\n"
    _ns, fn = _runner._namespace(src, "handler")
    assert fn(items=[1, 2]) == _runner._math.sqrt(102)


# --------------------------------------------------------------------------- #
# _run_once
# --------------------------------------------------------------------------- #
def test_run_once_passes_input_kwargs() -> None:
    req = {
        "source_code": "def add(a, b):\n    return a + b",
        "callable_name": "add",
        "input": {"a": 2, "b": 3},
    }
    assert _runner._run_once(req) == {"status": "completed", "output": 5}


# --------------------------------------------------------------------------- #
# _run_tests
# --------------------------------------------------------------------------- #
def test_run_tests_all_pass() -> None:
    req = {
        "source_code": "def sq(x):\n    return x * x",
        "callable_name": "sq",
        "tests": [
            {"name": "two", "input": {"x": 2}, "expected": 4},
            {"name": "three", "input": {"x": 3}, "expected": 9},
        ],
    }
    result = _runner._run_tests(req)
    assert result["status"] == "passed"
    assert [t["status"] for t in result["tests"]] == ["passed", "passed"]


def test_run_tests_reports_mismatch() -> None:
    req = {
        "source_code": "def sq(x):\n    return x * x",
        "callable_name": "sq",
        "tests": [{"name": "wrong", "input": {"x": 2}, "expected": 5}],
    }
    result = _runner._run_tests(req)
    assert result["status"] == "failed"
    t = result["tests"][0]
    assert t["status"] == "failed"
    assert t["output"] == 4
    assert "did not match" in t["error"]


def test_run_tests_compare_output_false_always_passes() -> None:
    req = {
        "source_code": "def sq(x):\n    return x * x",
        "callable_name": "sq",
        "tests": [{"name": "smoke", "input": {"x": 2}, "expected": 999, "compare_output": False}],
    }
    result = _runner._run_tests(req)
    assert result["status"] == "passed"


def test_run_tests_captures_exception_per_case() -> None:
    req = {
        "source_code": "def boom(x):\n    raise ValueError('nope')",
        "callable_name": "boom",
        "tests": [{"name": "explodes", "input": {"x": 1}, "expected": 1}],
    }
    result = _runner._run_tests(req)
    assert result["status"] == "failed"
    assert "ValueError: nope" in result["tests"][0]["error"]


# --------------------------------------------------------------------------- #
# _json_safe
# --------------------------------------------------------------------------- #
def test_json_safe_primitives_and_containers() -> None:
    assert _runner._json_safe({"a": [1, "x", True], "b": (2, 3)}) == {
        "a": [1, "x", True],
        "b": [2, 3],
    }
    # non-int dict keys are stringified
    assert _runner._json_safe({1: "v"}) == {"1": "v"}


def test_json_safe_falls_back_to_repr() -> None:
    obj = object()
    assert _runner._json_safe(obj) == repr(obj)


# --------------------------------------------------------------------------- #
# main — end-to-end via stdin/stdout
# --------------------------------------------------------------------------- #
def test_main_run_once_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    request = {
        "mode": "once",
        "source_code": "def handler(name):\n    print('hi')\n    return {'greeting': 'hello ' + name}",
        "callable_name": "handler",
        "input": {"name": "reza"},
    }
    out = io.StringIO()
    monkeypatch.setattr(_runner.sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(_runner.sys, "__stdout__", out)

    _runner.main()

    payload = json.loads(out.getvalue())
    assert payload["status"] == "completed"
    assert payload["output"] == {"greeting": "hello reza"}
    assert payload["stdout"] == "hi\n"  # captured, not leaked to the real stdout
    assert "duration_ms" in payload


def test_main_top_level_failure_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    request = {
        "mode": "once",
        "source_code": "def handler():\n    return 1",
        "callable_name": "missing",  # not defined -> ValueError in _namespace
        "input": {},
    }
    out = io.StringIO()
    monkeypatch.setattr(_runner.sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(_runner.sys, "__stdout__", out)

    _runner.main()

    payload = json.loads(out.getvalue())
    assert payload["status"] == "failed"
    assert "ValueError" in payload["error"]
    assert "traceback" in payload


def test_json_safe_handles_self_referential_structure() -> None:
    """Regression (#26): a self-referential container must not recurse until
    RecursionError (which escaped main() and crashed the runner with an opaque
    exit status); the cycle guard returns a marker instead."""
    a: list[object] = []
    a.append(a)
    out = _runner._json_safe(a)
    assert out == ["<circular reference>"]
