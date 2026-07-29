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

try:
    import resource as _resource
except ImportError:  # pragma: no cover - Windows has no resource module
    _resource = None  # type: ignore[assignment]
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


class _BoundedWriter(io.TextIOBase):
    """Capture at most ``limit`` characters, counting everything written.

    The runner previously accumulated an unbounded ``StringIO`` and the *parent*
    clipped the result afterwards, so a tool printing megabytes both ballooned the
    child's memory and produced a JSON envelope larger than any bound the parent
    could safely apply — truncating it mid-string.

    Writes past the limit are counted and dropped rather than raising: a tool must
    not fail *because* it was chatty, and the counter is what lets the report say how
    much was omitted instead of silently presenting a prefix as the whole output.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(int(limit), 0)
        self._parts: list[str] = []
        self._kept = 0
        self.dropped = 0

    def write(self, text: str) -> int:
        chunk = str(text)
        room = self._limit - self._kept
        if room > 0:
            keep = chunk[:room]
            self._parts.append(keep)
            self._kept += len(keep)
            self.dropped += len(chunk) - len(keep)
        else:
            self.dropped += len(chunk)
        return len(chunk)

    def writable(self) -> bool:
        return True

    def getvalue(self) -> str:
        text = "".join(self._parts)
        if self.dropped:
            text += f"\n... [{self.dropped} more characters omitted by the sandbox output limit]"
        return text


#: Fallback when the parent sends no limit (older payloads / direct invocation).
_DEFAULT_CAPTURE_LIMIT = 65_536


def main() -> None:
    request = json.loads(sys.stdin.read() or "{}")
    limits = request.get("_limits")
    _apply_resource_limits(limits)
    capture_limit = _DEFAULT_CAPTURE_LIMIT
    if isinstance(limits, dict):
        raw_limit = limits.get("output_chars")
        if isinstance(raw_limit, int) and raw_limit > 0:
            capture_limit = raw_limit
    # Bounded in the child, so the JSON envelope the parent parses is bounded too.
    stdout = _BoundedWriter(capture_limit)
    stderr = _BoundedWriter(capture_limit)
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


def _apply_resource_limits(raw: Any) -> None:
    """Apply best-effort POSIX limits before any user-authored code runs.

    These caps harden the local subprocess containment backend; they are not a
    replacement for a container, VM, seccomp profile, or equivalent OS policy.
    Unsupported limits are ignored so the same runner remains portable.
    """

    if _resource is None or not isinstance(raw, dict):
        return
    names = {
        "cpu_seconds": "RLIMIT_CPU",
        "memory_bytes": "RLIMIT_AS",
        "file_bytes": "RLIMIT_FSIZE",
        "open_files": "RLIMIT_NOFILE",
    }
    for key, resource_name in names.items():
        value = raw.get(key)
        limit_kind = getattr(_resource, resource_name, None)
        if limit_kind is None or not isinstance(value, int) or value <= 0:
            continue
        try:
            _soft, hard = _resource.getrlimit(limit_kind)
            effective = value if hard == _resource.RLIM_INFINITY else min(value, hard)
            _resource.setrlimit(limit_kind, (effective, effective))
        except (OSError, ValueError):
            # Some platforms expose a limit but refuse changes for unprivileged
            # processes. Wall timeout + process-group termination still apply.
            continue


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


def _resolve_callable(request: dict[str, Any]) -> Any:
    """Return the callable to invoke, from source **or** from an installed module.

    Two modes, because registered tools and authored tools are different things:

    ``source_code``
        User-authored Python, exec'd in a restricted namespace. The original mode.
    ``module_path``
        An installed, admin-registered callable, imported **inside this subprocess**
        (C8). Previously the control plane imported it into its own address space, so
        a registered tool shared memory, file descriptors, and credentials with the API
        server. Importing here means module-level code and the call itself run under
        the same ``python -I``, empty environment, private working directory, POSIX
        resource limits, and hard timeout as authored code.

    The import happens after :func:`_apply_resource_limits`, so a module whose *import*
    is the hostile part is already bounded.
    """
    module_path = request.get("module_path")
    if module_path:
        import importlib  # noqa: PLC0415 - only needed in module mode

        module = importlib.import_module(str(module_path))
        name = str(request["callable_name"])
        fn = getattr(module, name, None)
        if fn is None:
            raise AttributeError(f"module {module_path!r} has no attribute {name!r}")
        if not callable(fn):
            raise TypeError(f"{module_path}.{name} is not callable")
        return fn
    _namespace_obj, fn = _namespace(request["source_code"], request["callable_name"])
    return fn


def _run_once(request: dict[str, Any]) -> dict[str, Any]:
    fn = _resolve_callable(request)
    # Positional args are carried separately from keyword input: the runtime calls
    # some tools positionally, and collapsing both into one dict would silently
    # reorder or drop them.
    args = request.get("args") or []
    output = fn(*args, **request.get("input", {}))
    return {"status": "completed", "output": output}


def _run_tests(request: dict[str, Any]) -> dict[str, Any]:
    fn = _resolve_callable(request)
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
