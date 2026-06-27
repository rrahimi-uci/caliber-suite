"""Artifact comparison scorers for file-based eval (storage doc §7.5).

Given a ``match_spec`` (the ``match`` block on a dataset example's expected
artifact) plus the expected and actual bytes, :func:`compare_artifact` returns a
structured ``{match_type, passed, score, detail}`` verdict. Pure + dependency-free
so it is trivially unit-testable and usable from the eval replay path.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

ComparisonResult = dict[str, Any]


def compare_artifact(
    match_spec: dict[str, Any], expected: bytes | None, actual: bytes | None
) -> ComparisonResult:
    """Compare actual artifact bytes to the expected per a ``match_spec``."""
    mtype = str(match_spec.get("type", "exact_hash"))
    if actual is None:
        return _result(mtype, False, 0.0, "no actual artifact produced")
    try:
        handler = _HANDLERS.get(mtype)
        if handler is None:
            return _result(mtype, False, 0.0, f"unknown match type {mtype!r}")
        return handler(match_spec, expected, actual)
    except Exception as exc:  # a malformed artifact is a failed comparison, not a crash
        return _result(mtype, False, 0.0, f"comparison error: {exc}")


def _result(match_type: str, passed: bool, score: float, detail: str) -> ComparisonResult:
    return {"match_type": match_type, "passed": passed, "score": score, "detail": detail}


def _text(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def _exact_hash(_spec: dict[str, Any], expected: bytes | None, actual: bytes) -> ComparisonResult:
    import hashlib  # noqa: PLC0415

    if expected is None:
        return _result("exact_hash", False, 0.0, "no expected bytes")
    ok = hashlib.sha256(expected).digest() == hashlib.sha256(actual).digest()
    return _result("exact_hash", ok, 1.0 if ok else 0.0, "byte-identical" if ok else "bytes differ")


def _text_exact(_spec: dict[str, Any], expected: bytes | None, actual: bytes) -> ComparisonResult:
    ok = _text(expected) == _text(actual)
    return _result(
        "text_exact", ok, 1.0 if ok else 0.0, "exact text match" if ok else "text differs"
    )


def _text_contains(spec: dict[str, Any], expected: bytes | None, actual: bytes) -> ComparisonResult:
    needles = spec.get("contains") or ([_text(expected)] if expected else [])
    haystack = _text(actual)
    missing = [n for n in needles if n not in haystack]
    ok = not missing
    return _result(
        "text_contains",
        ok,
        1.0 if ok else 0.0,
        "all substrings present" if ok else f"missing: {missing}",
    )


def _regex(spec: dict[str, Any], _expected: bytes | None, actual: bytes) -> ComparisonResult:
    pattern = spec.get("pattern")
    if not pattern:
        return _result("regex", False, 0.0, "no pattern in match spec")
    ok = re.search(pattern, _text(actual)) is not None
    return _result(
        "regex", ok, 1.0 if ok else 0.0, "pattern matched" if ok else "pattern not found"
    )


def _json_exact(_spec: dict[str, Any], expected: bytes | None, actual: bytes) -> ComparisonResult:
    exp = json.loads(_text(expected)) if expected else None
    act = json.loads(_text(actual))
    ok = exp == act
    return _result("json_exact", ok, 1.0 if ok else 0.0, "json equal" if ok else "json differs")


def _json_field_subset(
    spec: dict[str, Any], expected: bytes | None, actual: bytes
) -> ComparisonResult:
    act = json.loads(_text(actual))
    # Either compare against the expected json (subset), or a {field: value}/field+value spec.
    if "field" in spec:
        field, value = spec.get("field"), spec.get("value")
        present = isinstance(act, dict) and field in act
        container = act.get(field) if present else None
        ok = present and (
            value is None
            or value == container
            or (isinstance(container, (list, str)) and value in container)
        )
        return _result(
            "json_field_subset",
            ok,
            1.0 if ok else 0.0,
            f"field {field!r} satisfied" if ok else f"field {field!r} missing/!= {value!r}",
        )
    exp = json.loads(_text(expected)) if expected else {}
    ok = _is_subset(exp, act)
    return _result(
        "json_field_subset",
        ok,
        1.0 if ok else 0.0,
        "expected fields are a subset" if ok else "expected fields not all present",
    )


def _csv_rows_equal(
    spec: dict[str, Any], expected: bytes | None, actual: bytes
) -> ComparisonResult:
    required = spec.get("required_columns")
    ignore_order = bool(spec.get("ignore_row_order", False))
    tol = float(spec.get("numeric_tolerance", 0.0))
    exp_rows = _read_csv(expected, required)
    act_rows = _read_csv(actual, required)
    if len(exp_rows) != len(act_rows):
        return _result(
            "csv_rows_equal",
            False,
            0.0,
            f"row count differs ({len(exp_rows)} vs {len(act_rows)})",
        )
    if ignore_order:
        exp_rows = sorted(exp_rows, key=lambda r: json.dumps(r, sort_keys=True))
        act_rows = sorted(act_rows, key=lambda r: json.dumps(r, sort_keys=True))
    for i, (er, ar) in enumerate(zip(exp_rows, act_rows, strict=False)):
        if not _rows_match(er, ar, tol):
            return _result("csv_rows_equal", False, 0.0, f"row {i} differs: {er} vs {ar}")
    return _result("csv_rows_equal", True, 1.0, f"{len(exp_rows)} row(s) matched")


def _read_csv(data: bytes | None, required: list[str] | None) -> list[dict[str, str]]:
    rows = list(csv.DictReader(io.StringIO(_text(data))))
    if required:
        rows = [{k: r.get(k, "") for k in required} for r in rows]
    return rows


def _rows_match(a: dict[str, str], b: dict[str, str], tol: float) -> bool:
    if set(a) != set(b):
        return False
    for k, av in a.items():
        bv = b[k]
        if av == bv:
            continue
        if tol > 0:
            try:
                if abs(float(av) - float(bv)) <= tol:
                    continue
            except (TypeError, ValueError):
                pass
        return False
    return True


def _is_subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(k in actual and _is_subset(v, actual[k]) for k, v in expected.items())
    return bool(expected == actual)


_HANDLERS = {
    "exact_hash": _exact_hash,
    "text_exact": _text_exact,
    "text_contains": _text_contains,
    "regex": _regex,
    "json_exact": _json_exact,
    "json_field_subset": _json_field_subset,
    "csv_rows_equal": _csv_rows_equal,
}

SUPPORTED_MATCH_TYPES = frozenset(_HANDLERS)
