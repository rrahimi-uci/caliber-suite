"""Deterministic, no-code data transformations for workflow execution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator


class DataTransformError(ValueError):
    """Raised when a transform configuration or input is invalid."""


def _path(value: Any, path: str) -> Any:
    current = value
    if not path:
        return current
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        return None
    return current


def _condition_matches(  # noqa: PLR0911 - explicit closed operator dispatch
    value: Any, condition: Mapping[str, Any]
) -> bool:
    actual = _path(value, str(condition.get("path", "")))
    operator = str(condition.get("operator", "equals"))
    expected = condition.get("value")
    if operator == "equals":
        return bool(actual == expected)
    if operator == "not_equals":
        return bool(actual != expected)
    if operator == "exists":
        expected_exists = True if expected is None else bool(expected)
        return (actual is not None) == expected_exists
    if operator == "contains":
        return (
            bool(expected in actual) if isinstance(actual, (str, list, tuple, set, dict)) else False
        )
    if operator == "in":
        return (
            bool(actual in expected)
            if isinstance(expected, (list, tuple, set, dict, str))
            else False
        )
    if operator in {"greater_than", "greater_or_equal", "less_than", "less_or_equal"}:
        if actual is None or expected is None:
            return False
        try:
            left, right = float(actual), float(expected)
        except (TypeError, ValueError):
            return False
        comparisons = {
            "greater_than": left > right,
            "greater_or_equal": left >= right,
            "less_than": left < right,
            "less_or_equal": left <= right,
        }
        return comparisons[operator]
    raise DataTransformError(f"unsupported condition operator {operator!r}")


def _mapping(value: Any, config: Mapping[str, Any]) -> dict[str, Any]:
    fields = config.get("fields", {})
    defaults = config.get("defaults", {})
    if not isinstance(fields, Mapping) or not isinstance(defaults, Mapping):
        raise DataTransformError("mapping config requires object fields and defaults")
    result = dict(defaults)
    for output_key, source_path in fields.items():
        resolved = _path(value, str(source_path))
        if resolved is not None:
            result[str(output_key)] = resolved
    return result


def _validate_schema(value: Any, config: Mapping[str, Any]) -> dict[str, Any]:
    schema = config.get("schema", {})
    if not isinstance(schema, Mapping):
        raise DataTransformError("json_schema config requires a schema object")
    validator = Draft202012Validator(dict(schema))
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
    return {
        "value": value,
        "valid": not errors,
        "errors": [
            {
                "path": ".".join(str(part) for part in error.absolute_path),
                "message": error.message,
                "validator": error.validator,
            }
            for error in errors
        ],
    }


def _decision_table(value: Any, config: Mapping[str, Any]) -> dict[str, Any]:
    rules = config.get("rules", [])
    if not isinstance(rules, list):
        raise DataTransformError("decision_table config requires a rules list")
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping):
            raise DataTransformError(f"decision rule {index + 1} must be an object")
        conditions = rule.get("when", [])
        if isinstance(conditions, Mapping):
            conditions = [
                {"path": path, "operator": "equals", "value": expected}
                for path, expected in conditions.items()
            ]
        if not isinstance(conditions, list):
            raise DataTransformError(f"decision rule {index + 1} when must be a list or object")
        if all(
            isinstance(condition, Mapping) and _condition_matches(value, condition)
            for condition in conditions
        ):
            return {
                "decision": rule.get("result"),
                "matched": True,
                "matched_rule": str(rule.get("name") or f"rule-{index + 1}"),
            }
    return {
        "decision": config.get("default"),
        "matched": False,
        "matched_rule": None,
    }


def _confidence(value: Any, config: Mapping[str, Any]) -> dict[str, Any]:
    signals = config.get("signals", [])
    if not isinstance(signals, list):
        raise DataTransformError("confidence config requires a signals list")
    score = float(config.get("bias", 0.0))
    matched: list[str] = []
    for index, signal in enumerate(signals):
        if not isinstance(signal, Mapping):
            raise DataTransformError(f"confidence signal {index + 1} must be an object")
        if _condition_matches(value, signal):
            score += float(signal.get("weight", 0.0))
            matched.append(str(signal.get("name") or f"signal-{index + 1}"))
    score = max(0.0, min(1.0, score))
    threshold = float(config.get("review_threshold", 0.5))
    return {
        "confidence": score,
        "needs_review": score < threshold,
        "review_threshold": threshold,
        "matched_signals": matched,
    }


def apply_data_transform(operation: str, value: Any, config: Mapping[str, Any]) -> dict[str, Any]:
    """Apply one closed-vocabulary transform and return a structured result."""

    if operation == "fixture":
        result = config.get("fixture")
        return {"result": result, "valid": True, "metadata": {"operation": operation}}
    if operation == "mapping":
        result = _mapping(value, config)
        return {"result": result, "valid": True, "metadata": {"operation": operation}}
    if operation == "json_schema":
        checked = _validate_schema(value, config)
        return {
            "result": checked["value"],
            "valid": checked["valid"],
            "metadata": {"operation": operation, "errors": checked["errors"]},
        }
    if operation == "decision_table":
        result = _decision_table(value, config)
        return {"result": result, "valid": True, "metadata": {"operation": operation}}
    if operation == "confidence":
        result = _confidence(value, config)
        return {"result": result, "valid": True, "metadata": {"operation": operation}}
    raise DataTransformError(f"unsupported data transform operation {operation!r}")


def transform_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, sort_keys=True, ensure_ascii=False)


__all__ = ["DataTransformError", "apply_data_transform", "transform_text"]
