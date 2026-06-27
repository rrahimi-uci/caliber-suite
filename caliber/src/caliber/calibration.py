"""Shared scoring for component calibration runs (tools + MCP tools).

A *calibration* runs a saved set of test cases through a component's existing
invocation path and scores each case against its assertion. The invocation
itself stays in the route layer (tools use the sandbox; MCP tools use the
gateway) — this module only owns the assertion-evaluation + aggregation logic so
both surfaces score identically.
"""

from __future__ import annotations

import json
from typing import Any


def _stringify(output: object) -> str:
    """Best-effort stringification of an invocation output for comparison.

    JSON for structured values (stable key ordering not required for the
    contains/equals checks), falling back to ``str`` for anything non-JSON.
    """
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return str(output)


def evaluate_assertion(assertion: dict[str, Any] | None, output: object, error: str | None) -> bool:
    """Return whether one case passed given its assertion + invocation result.

    * ``no_error`` (default) — passes when ``error`` is falsy.
    * ``output_contains`` — passes when the stringified output contains ``value``.
    * ``equals`` — passes when the stringified output equals ``value``.

    An invocation error fails every assertion type (you can't satisfy an
    output-comparison when there was no successful output).
    """
    spec = assertion or {}
    kind = spec.get("type", "no_error")
    if error:
        return False
    if kind == "no_error":
        return True
    value = spec.get("value")
    if value is None:
        return False
    rendered = _stringify(output)
    if kind == "output_contains":
        return str(value) in rendered
    if kind == "equals":
        return rendered == str(value)
    return False


def aggregate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-case results into ``{pass_rate, total, passed, cases}``.

    ``cases`` are already-scored dicts each carrying a ``passed`` bool.
    """
    total = len(cases)
    passed = sum(1 for case in cases if case.get("passed"))
    pass_rate = round(passed / total, 4) if total else 0.0
    return {"pass_rate": pass_rate, "total": total, "passed": passed, "cases": cases}
