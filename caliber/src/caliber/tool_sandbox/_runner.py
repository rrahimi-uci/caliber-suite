"""Subprocess runner for user-authored tool code.

This module is executed as a script by :class:`LocalSubprocessToolSandbox`.
It deliberately depends only on the Python standard library so the parent can
start it with ``python -I`` and an empty environment.
"""

from __future__ import annotations

import ast as _ast
import contextlib
import datetime as _datetime
import io
import json
import math as _math
import re as _re
import statistics as _statistics
import sys
import time
import traceback
from typing import Any

SAFE_BUILTINS = {
    "__build_class__": __build_class__,
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "Exception": Exception,
    "filter": filter,
    "float": float,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    # NOTE: ``object`` is deliberately NOT exposed — ``object.__subclasses__()``
    # is the classic restricted-eval escape to recover the real builtins.
    "print": print,
    "range": range,
    "repr": repr,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "ValueError": ValueError,
    "zip": zip,
}


def main() -> None:
    request = json.loads(sys.stdin.read() or "{}")
    stdout = io.StringIO()
    stderr = io.StringIO()
    start = time.monotonic()
    result: dict[str, Any]
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            result = _run_tests(request) if request.get("mode") == "tests" else _run_once(request)
        except Exception as exc:
            result = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=5),
            }
    result["stdout"] = stdout.getvalue()
    result["stderr"] = stderr.getvalue()
    result["duration_ms"] = round((time.monotonic() - start) * 1000, 1)
    # Serialize inside a guard: a tool returning a non-JSON-serializable or
    # self-referential value must surface as a structured failure (preserving
    # captured stdout/stderr), not crash the runner with an opaque exit status.
    try:
        payload = json.dumps(_json_safe(result), sort_keys=True)
    except (TypeError, ValueError, RecursionError) as exc:
        payload = json.dumps(
            {
                "status": "failed",
                "error": f"tool output is not JSON-serializable: {type(exc).__name__}: {exc}",
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
                "duration_ms": result.get("duration_ms", 0.0),
            },
            sort_keys=True,
        )
    print(payload, file=sys.__stdout__)


def _reject_unsafe_ast(tree: _ast.AST) -> None:
    """Reject access to private/dunder attributes in tool code.

    The restricted-``__builtins__`` allow-list is not, on its own, a security
    boundary: dunder traversal (``().__class__.__bases__[0].__subclasses__()``)
    walks from any object back to the real builtins and recovers ``__import__``.
    ``getattr`` is not exposed, so attribute *syntax* is the only way to reach a
    dunder — blocking ``Attribute`` nodes whose name starts with ``_`` closes the
    escape while leaving ordinary public-attribute/method calls untouched.
    """
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute) and node.attr.startswith("_"):
            raise ValueError(
                f"access to private/dunder attribute {node.attr!r} is not allowed in tool code"
            )


def _namespace(source_code: str, callable_name: str) -> tuple[dict[str, Any], Any]:
    namespace: dict[str, Any] = {
        "__builtins__": SAFE_BUILTINS,
        "__name__": "__caliber_tool__",
        "datetime": _datetime,
        "json": json,
        "math": _math,
        "re": _re,
        "statistics": _statistics,
    }
    tree = _ast.parse(source_code, "<tool-source>", "exec")
    _reject_unsafe_ast(tree)
    exec(compile(tree, "<tool-source>", "exec"), namespace)  # noqa: S102
    fn = namespace.get(callable_name)
    if not callable(fn):
        raise ValueError(f"callable {callable_name!r} was not defined")
    return namespace, fn


def _run_once(request: dict[str, Any]) -> dict[str, Any]:
    _namespace_obj, fn = _namespace(request["source_code"], request["callable_name"])
    output = fn(**request.get("input", {}))
    return {"status": "completed", "output": output}


def _run_tests(request: dict[str, Any]) -> dict[str, Any]:
    _namespace_obj, fn = _namespace(request["source_code"], request["callable_name"])
    results: list[dict[str, Any]] = []
    for test in request.get("tests", []):
        started = time.monotonic()
        try:
            output = fn(**test.get("input", {}))
            expected = test.get("expected")
            compare_output = bool(test.get("compare_output", True))
            passed = True if not compare_output else output == expected
            results.append(
                {
                    "name": test.get("name", "unnamed"),
                    "status": "passed" if passed else "failed",
                    "output": output,
                    "expected": expected,
                    "error": None if passed else "output did not match expected value",
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "name": test.get("name", "unnamed"),
                    "status": "failed",
                    "output": None,
                    "expected": test.get("expected"),
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                }
            )
    status = "passed" if all(item["status"] == "passed" for item in results) else "failed"
    return {"status": status, "tests": results}


def _json_safe(value: Any, _seen: frozenset[int] = frozenset()) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, dict)):
        # Cycle guard: track container ids along the current path so a
        # self-referential structure can't recurse until RecursionError.
        if id(value) in _seen:
            return "<circular reference>"
        seen = _seen | {id(value)}
        if isinstance(value, dict):
            return {str(key): _json_safe(item, seen) for key, item in value.items()}
        return [_json_safe(item, seen) for item in value]
    return repr(value)


if __name__ == "__main__":
    main()
