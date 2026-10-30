"""Helpers for generating curated OpenAPI tool drafts."""

from __future__ import annotations

import re
from typing import Any

from caliber.db.models import (
    CaliberOpenApiIntegration,
    CaliberOpenApiIntegrationVersion,
    CaliberOpenApiOperation,
)

_IDEMPOTENCY_HEADER = "idempotency-key"
_EXPECTED_VERSION_HEADER = "if-match"

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_CAMEL_SPLIT = re.compile(r"(?<!^)(?=[A-Z])")


def build_tool_draft_name(operation: CaliberOpenApiOperation) -> str:
    """Stable snake-case tool name for one imported operation."""

    seed = operation.spec_operation_id or operation.summary or operation.operation_key
    parts = _CAMEL_SPLIT.sub("_", str(seed)).lower()
    text = _NON_ALNUM.sub("_", parts).strip("_")
    return text[:128] or f"{operation.method.lower()}_{_path_stub(operation.path)}"


def build_tool_draft_description(
    integration: CaliberOpenApiIntegration, operation: CaliberOpenApiOperation
) -> str:
    summary = operation.summary.strip() or operation.description.strip() or operation.operation_key
    return f"{integration.name}: {summary}"[:2048]


def build_tool_input_schema(operation: CaliberOpenApiOperation) -> dict[str, Any]:
    """Agent/tool-call input schema for one normalized OpenAPI operation."""

    normalized = dict(operation.normalized_operation or {})
    parameters = normalized.get("parameters")
    request_body = normalized.get("requestBody")

    properties: dict[str, Any] = {}
    required: list[str] = []

    path_schema = _parameter_group_schema(parameters, "path")
    if path_schema is not None:
        properties["path_params"] = path_schema
        required.append("path_params")

    query_schema = _parameter_group_schema(parameters, "query")
    if query_schema is not None:
        properties["query_params"] = query_schema

    idempotency_header, expected_version_header = bound_contract_headers(operation)
    header_schema = _parameter_group_schema(
        parameters,
        "header",
        excluded_names={
            name.lower() for name in (idempotency_header, expected_version_header) if name
        },
    )
    if header_schema is not None:
        properties["header_params"] = header_schema

    # These two values are proposal-bound inputs, not arbitrary headers. Keeping
    # them outside ``header_params`` gives the dispatcher one canonical field to
    # derive and prevents a caller from changing the effect precondition without
    # changing the proposal. The executor still checks conflicts defensively for
    # hand-written bindings that do not use this derived schema.
    if idempotency_header:
        properties["idempotency_key"] = {
            "type": "string",
            "minLength": 1,
            "maxLength": 255,
        }
        if _parameter_required(parameters, "header", idempotency_header):
            required.append("idempotency_key")
    if expected_version_header:
        expected_schema = _parameter_schema(parameters, "header", expected_version_header)
        properties["expected_resource_version"] = expected_schema
        if _parameter_required(parameters, "header", expected_version_header):
            required.append("expected_resource_version")

    if isinstance(request_body, dict):
        body_schema = _request_body_schema(request_body)
        properties["body"] = body_schema
        if bool(request_body.get("required", False)):
            required.append("body")

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def build_tool_pack_name(operations: list[CaliberOpenApiOperation]) -> str:
    """Stable snake-case name for a multi-operation tool pack.

    Draws the shared resource type from the first operation's path stub rather
    than concatenating every operation's name, since a pack of 4-5 operations
    would otherwise produce an unreadable ``list_get_create_update_delete_ticket``.
    """

    stub = _path_stub(operations[0].path) if operations else "operations"
    return f"{stub}_pack"[:128]


def build_tool_pack_description(
    integration: CaliberOpenApiIntegration, operations: list[CaliberOpenApiOperation]
) -> str:
    summaries = [op.summary.strip() or op.operation_key for op in operations]
    return f"{integration.name}: tool pack covering {', '.join(summaries)}"[:2048]


def build_tool_pack_input_schema(operations: list[CaliberOpenApiOperation]) -> dict[str, Any]:
    """Input schema for a tool pack: pick one bound operation, then its arguments.

    A pack cannot use one flat schema the way a single-operation draft does —
    each bound operation may need a different path parameter or body — so the
    schema is a discriminated union: ``operation`` selects which bound call runs,
    and the corresponding branch's schema governs the rest of the payload. This
    keeps every bound operation's input still validated, rather than falling back
    to an unchecked catch-all once more than one operation is involved.
    """

    branches: list[dict[str, Any]] = []
    for operation in operations:
        branch = build_tool_input_schema(operation)
        branch["properties"] = {
            "operation": {"const": operation.operation_key},
            **branch["properties"],
        }
        branch.setdefault("required", [])
        branch["required"] = ["operation", *branch["required"]]
        branches.append(branch)
    return {
        "type": "object",
        "oneOf": branches,
    }


def build_pack_execution_config(
    *,
    integration: CaliberOpenApiIntegration,
    version: CaliberOpenApiIntegrationVersion,
    operations: list[CaliberOpenApiOperation],
    server_url: str,
    auth_binding: dict[str, Any] | None,
    max_attempts: int = 1,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Execution config for a tool pack: the executor dispatches on ``operation``."""

    return {
        "kind": "openapi_http_pack",
        "integration_id": integration.integration_id,
        "integration_version_id": version.version_id,
        "server_url": server_url,
        "auth_binding": auth_binding,
        "max_attempts": max_attempts,
        "timeout_seconds": timeout_seconds,
        "operations": [
            {
                "operation_id": operation.operation_id,
                "operation_key": operation.operation_key,
                "method": operation.method,
                "path": operation.path,
                "request_content_types": list(operation.request_content_types or []),
                "response_statuses": list(operation.response_statuses or []),
                "idempotency_key_header": bound_contract_headers(operation)[0],
                "expected_resource_version_header": bound_contract_headers(operation)[1],
            }
            for operation in operations
        ],
    }


def build_tool_output_schema() -> dict[str, Any]:
    """Generic result envelope for declarative HTTP tools."""

    return {
        "type": "object",
        "properties": {
            "status_code": {"type": "integer"},
            "text": {"type": "string"},
            "json": {},
            "headers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
            "attempts": {"type": "integer", "minimum": 0},
            "attempt_log": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "attempt": {"type": "integer", "minimum": 1},
                        "emitted_idempotency_key": {
                            "anyOf": [{"type": "string"}, {"type": "null"}]
                        },
                        "status": {
                            "type": "string",
                            "enum": ["blocked", "transport_error", "response"],
                        },
                        "status_code": {"type": "integer"},
                        "transport_outcome": {
                            "type": "string",
                            "enum": ["never_sent", "ambiguous", "response"],
                        },
                        "error_type": {"type": "string"},
                    },
                    "required": [
                        "attempt",
                        "emitted_idempotency_key",
                        "status",
                        "transport_outcome",
                    ],
                    "additionalProperties": False,
                },
            },
            "replay_disposition": {
                "type": "string",
                "enum": ["replay_confirmed", "replay_candidate", "not_replay"],
            },
            "receipt_disposition": {
                "type": "string",
                "enum": ["unverified", "verified", "not_applicable"],
            },
            "earlier_attempts_may_have_been_sent": {"type": "boolean"},
        },
        "required": [
            "status_code",
            "headers",
            "attempts",
            "attempt_log",
            "replay_disposition",
            "receipt_disposition",
            "earlier_attempts_may_have_been_sent",
        ],
        "additionalProperties": False,
    }


def build_execution_config(
    *,
    integration: CaliberOpenApiIntegration,
    version: CaliberOpenApiIntegrationVersion,
    operation: CaliberOpenApiOperation,
    server_url: str,
    auth_binding: dict[str, Any] | None,
    max_attempts: int = 1,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    idempotency_header, expected_version_header = bound_contract_headers(operation)
    return {
        "kind": "openapi_http",
        "integration_id": integration.integration_id,
        "integration_version_id": version.version_id,
        "operation_id": operation.operation_id,
        "operation_key": operation.operation_key,
        "method": operation.method,
        "path": operation.path,
        "server_url": server_url,
        "auth_binding": auth_binding,
        "request_content_types": list(operation.request_content_types or []),
        "response_statuses": list(operation.response_statuses or []),
        "idempotency_key_header": idempotency_header,
        "expected_resource_version_header": expected_version_header,
        "max_attempts": max_attempts,
        "timeout_seconds": timeout_seconds,
    }


_SIDE_EFFECT_RANK = {"read": 0, "write": 1, "external_action": 2}


def pack_side_effect_level(operations: list[CaliberOpenApiOperation]) -> str:
    """The most restrictive side-effect level among a pack's bound operations.

    A pack that bundles one read and one write is a write for approval purposes
    — an agent holding the tool can always reach the riskier branch, so the draft
    must be gated as if every call did.
    """

    ranked = [_SIDE_EFFECT_RANK.get(op.side_effect_level, 1) for op in operations]
    level = max(ranked) if ranked else 0
    return next(name for name, rank in _SIDE_EFFECT_RANK.items() if rank == level)


def auth_binding_secret_refs(auth_binding: dict[str, Any] | None) -> list[str]:
    refs: list[str] = []
    if not isinstance(auth_binding, dict):
        return refs
    for key in (
        "secret_ref",
        "password_secret_ref",
        "client_secret_ref",
        "refresh_token_secret_ref",
    ):
        value = auth_binding.get(key)
        if isinstance(value, str) and value.strip():
            refs.append(value.strip())
    # Preserve order while de-duplicating.
    return list(dict.fromkeys(refs))


def bound_contract_headers(operation: CaliberOpenApiOperation) -> tuple[str | None, str | None]:
    """Return the canonical idempotency and expected-version header bindings.

    Only the standard headers used by the admitted Beta connector are promoted
    into proposal-bound fields. Other OpenAPI headers remain ordinary tool input.
    """

    parameters = (operation.normalized_operation or {}).get("parameters")
    return (
        _parameter_name(parameters, "header", _IDEMPOTENCY_HEADER),
        _parameter_name(parameters, "header", _EXPECTED_VERSION_HEADER),
    )


def _parameter_name(parameters: object, location: str, wanted: str) -> str | None:
    if not isinstance(parameters, list):
        return None
    for item in parameters:
        if not isinstance(item, dict) or str(item.get("in") or "") != location:
            continue
        name = str(item.get("name") or "").strip()
        if name.lower() == wanted:
            return name
    return None


def _parameter_schema(parameters: object, location: str, name: str) -> dict[str, Any]:
    if isinstance(parameters, list):
        for item in parameters:
            if not isinstance(item, dict) or str(item.get("in") or "") != location:
                continue
            if str(item.get("name") or "").strip().lower() != name.lower():
                continue
            schema_value = item.get("schema")
            if isinstance(schema_value, dict):
                return dict(schema_value)
    return {"type": "string"}


def _parameter_required(parameters: object, location: str, name: str) -> bool:
    if not isinstance(parameters, list):
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("in") or "") == location
        and str(item.get("name") or "").strip().lower() == name.lower()
        and bool(item.get("required", False))
        for item in parameters
    )


def _parameter_group_schema(
    parameters: object,
    location: str,
    *,
    excluded_names: set[str] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(parameters, list):
        return None
    properties: dict[str, Any] = {}
    required: list[str] = []
    for item in parameters:
        if not isinstance(item, dict) or str(item.get("in") or "") != location:
            continue
        name = str(item.get("name") or "").strip()
        if not name or name.lower() in (excluded_names or set()):
            continue
        schema_value = item.get("schema")
        schema: dict[str, Any] = (
            dict(schema_value) if isinstance(schema_value, dict) else {"type": "string"}
        )
        prop = dict(schema)
        description = str(item.get("description") or "").strip()
        if description:
            prop.setdefault("description", description)
        properties[name] = prop
        if bool(item.get("required", False)):
            required.append(name)
    if not properties:
        return None
    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    return result


def _request_body_schema(request_body: dict[str, Any]) -> dict[str, Any]:
    content = request_body.get("content")
    if isinstance(content, dict):
        for content_type in ("application/json", "application/*+json"):
            candidate = content.get(content_type)
            if isinstance(candidate, dict) and isinstance(candidate.get("schema"), dict):
                return dict(candidate["schema"])
        for candidate in content.values():
            if isinstance(candidate, dict) and isinstance(candidate.get("schema"), dict):
                return dict(candidate["schema"])
    return {"type": "object"}


def _path_stub(path: str) -> str:
    text = path.replace("{", "").replace("}", "")
    text = _NON_ALNUM.sub("_", text.lower()).strip("_")
    return text or "operation"
