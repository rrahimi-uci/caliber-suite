"""Deterministic detection of dependencies between imported OpenAPI operations.

This is the canonical-model half of §5 of the proposal: dependency detection is
mostly deterministic, with agent assistance only as an advisory layer. Every
function here is a pure function of the normalized operation list, so the same
imported spec always produces the same dependency rows — which is what makes
import repeatable, diffable, and auditable rather than a black box.

Detection runs in three tiers, matching §5.3:

* **explicit** — read directly off the spec (OpenAPI ``links``, a security
  requirement) and auto-wire at ``confidence="high"``.
* **inferred deterministic** — rule-based structural matches (path-parameter
  reuse, identifier field matching, async create→poll pairs) at
  ``confidence="medium"``.
* **agent-suggested** — deliberately *not* produced here. This module has no
  LLM call in it; §5.3/5.4 keep the agent advisory and operator-confirmed, never
  a source of canonical rows, so there is nothing to wire in this file for it.
"""

from __future__ import annotations

import re
from typing import Any

DEPENDENCY_TYPES = (
    "produces_identifier_for",
    "consumes_identifier_from",
    "requires_auth",
    "polls",
    "paginates_to",
    "compensates",
    "precondition_for",
    "grouped_with",
)

SOURCES = (
    "openapi_link",
    "schema_match",
    "path_structure",
    "rule_inference",
    "agent_suggestion",
    "operator_confirmed",
)

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

_PATH_PARAM = re.compile(r"\{([^{}]+)\}")
_ID_FIELD = re.compile(r"^(?:(?P<prefix>[a-zA-Z][a-zA-Z0-9]*)_)?id$")


def detect_dependencies(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return deterministic dependency rows for one imported version's operations.

    Each ``operation`` dict must carry ``operation_id`` (assigned by the caller
    once persisted — see ``routes/openapi_integrations.py``), plus the normalized
    fields produced by
    :func:`caliber.integrations.openapi.normalize.normalize_openapi_document`.
    Rows are deduplicated: a pair that is both an explicit link and a structural
    match is reported once, at the higher tier.
    """

    by_key: dict[tuple[str, str], dict[str, Any]] = {}

    for row in _explicit_links(operations):
        by_key[_pair_key(row)] = row
    for row in _path_hierarchy(operations):
        by_key.setdefault(_pair_key(row), row)
    for row in _identifier_flow(operations):
        by_key.setdefault(_pair_key(row), row)
    for row in _async_lifecycle(operations):
        by_key.setdefault(_pair_key(row), row)
    for row in _tag_grouping(operations):
        by_key.setdefault(_pair_key(row), row)

    return sorted(
        by_key.values(),
        key=lambda row: (row["from_operation_id"], row["to_operation_id"], row["dependency_type"]),
    )


def _pair_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (row["from_operation_id"], row["to_operation_id"], row["dependency_type"])


def _make_row(
    *,
    from_op: dict[str, Any],
    to_op: dict[str, Any],
    dependency_type: str,
    confidence: str,
    source: str,
    required: bool,
    binding_field_map: dict[str, str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    return {
        "from_operation_id": from_op["operation_id"],
        "to_operation_id": to_op["operation_id"],
        "dependency_type": dependency_type,
        "confidence": confidence,
        "source": source,
        "required": required,
        "binding_field_map": binding_field_map or {},
        "notes": notes,
    }


def _explicit_links(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAPI ``links`` on a 2xx response, resolved to the operation they name.

    The highest-confidence tier: the spec author wrote this relationship down.
    Resolves ``operationId`` first, then a same-document ``operationRef``
    (``#/paths/~1tickets~1{id}/get``) as a fallback for specs that omit ids.
    """

    by_operation_id = {
        op.get("spec_operation_id"): op for op in operations if op.get("spec_operation_id")
    }
    by_key = {op["operation_key"]: op for op in operations}
    rows: list[dict[str, Any]] = []
    for from_op in operations:
        normalized = from_op.get("normalized_operation")
        responses = normalized.get("responses") if isinstance(normalized, dict) else None
        if not isinstance(responses, dict):
            continue
        for status, response in responses.items():
            if not str(status).startswith("2") or not isinstance(response, dict):
                continue
            links = response.get("links")
            if not isinstance(links, dict):
                continue
            for link_name, link in links.items():
                if not isinstance(link, dict):
                    continue
                to_op = _resolve_link_target(link, by_operation_id, by_key)
                if to_op is None or to_op["operation_id"] == from_op["operation_id"]:
                    continue
                field_map = _link_parameter_map(link)
                rows.append(
                    _make_row(
                        from_op=from_op,
                        to_op=to_op,
                        dependency_type="produces_identifier_for",
                        confidence=CONFIDENCE_HIGH,
                        source="openapi_link",
                        required=False,
                        binding_field_map=field_map,
                        notes=f"OpenAPI link '{link_name}'",
                    )
                )
    return rows


def _resolve_link_target(
    link: dict[str, Any],
    by_operation_id: dict[Any, dict[str, Any]],
    by_key: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    op_id = link.get("operationId")
    if isinstance(op_id, str) and op_id in by_operation_id:
        return by_operation_id[op_id]
    op_ref = link.get("operationRef")
    if isinstance(op_ref, str):
        # "#/paths/~1tickets~1{ticket_id}/get" -> ("GET", "/tickets/{ticket_id}")
        match = re.match(r"^#/paths/(?P<path>[^/]+)/(?P<method>[a-zA-Z]+)$", op_ref)
        if match:
            path = match.group("path").replace("~1", "/").replace("~0", "~")
            key = f"{match.group('method').upper()} {path}"
            return by_key.get(key)
    return None


def _link_parameter_map(link: dict[str, Any]) -> dict[str, str]:
    parameters = link.get("parameters")
    if not isinstance(parameters, dict):
        return {}
    field_map: dict[str, str] = {}
    for target_param, expression in parameters.items():
        if isinstance(expression, str):
            field_map[str(target_param)] = expression
    return field_map


def _path_hierarchy(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A parent collection's ``list``/``create`` precede operations on its members.

    ``/tickets`` (POST) is a precondition for ``/tickets/{ticket_id}`` (any
    method): you cannot fetch a member of a collection whose creation endpoint
    you have not called at least once, in the CRUD sense the paths imply. This is
    the strongest purely structural signal OpenAPI gives, so it lands at
    ``medium`` rather than ``low``.
    """

    rows: list[dict[str, Any]] = []
    for child in operations:
        child_path = child["path"]
        if not _PATH_PARAM.search(child_path):
            continue
        parent_path = _parent_collection_path(child_path)
        if parent_path is None:
            continue
        for parent in operations:
            if parent["path"] != parent_path or parent["operation_id"] == child["operation_id"]:
                continue
            if parent["method"] not in ("POST",):
                continue
            path_param = _PATH_PARAM.findall(child_path)[-1]
            rows.append(
                _make_row(
                    from_op=parent,
                    to_op=child,
                    dependency_type="precondition_for",
                    confidence=CONFIDENCE_MEDIUM,
                    source="path_structure",
                    required=child["method"] != "POST",
                    binding_field_map={path_param: "response.id"},
                    notes=f"{parent['path']} creates the resource {child_path} addresses",
                )
            )
    return rows


def _parent_collection_path(path: str) -> str | None:
    """``/tickets/{ticket_id}/comments`` -> ``/tickets/{ticket_id}``'s collection.

    Strips exactly one trailing ``/{param}`` segment (and, if present, one more
    static segment before it) to reach the collection endpoint. Returns ``None``
    when the path has no such parent to strip.
    """

    trimmed = path.rstrip("/")
    segments = trimmed.split("/")
    if len(segments) < 2 or not (segments[-1].startswith("{") and segments[-1].endswith("}")):
        return None
    return "/".join(segments[:-1]) or "/"


def _identifier_flow(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match a response's identifier-shaped field to another operation's path param.

    ``id -> {resource_id}``-style: a ``create``/``list`` response carrying an
    ``id``/``ticket_id``-shaped field wires to any operation whose path parameter
    matches that field name (allowing ``{id}`` to match a differently-prefixed
    field, since ``{ticket_id}`` on ``/tickets/{ticket_id}`` is the common case).
    """

    rows: list[dict[str, Any]] = []
    producers: list[tuple[dict[str, Any], str]] = []
    for op in operations:
        for field in _response_identifier_fields(op):
            producers.append((op, field))

    for consumer in operations:
        for path_param in _PATH_PARAM.findall(consumer["path"]):
            for producer, field in producers:
                if producer["operation_id"] == consumer["operation_id"]:
                    continue
                if not _identifier_matches(field, path_param, producer.get("path", "")):
                    continue
                rows.append(
                    _make_row(
                        from_op=producer,
                        to_op=consumer,
                        dependency_type="consumes_identifier_from",
                        confidence=CONFIDENCE_MEDIUM,
                        source="schema_match",
                        required=True,
                        binding_field_map={path_param: f"response.{field}"},
                        notes=f"response field '{field}' matches path parameter '{{{path_param}}}'",
                    )
                )
    return rows


def _identifier_matches(response_field: str, path_param: str, producer_path: str) -> bool:
    field = response_field.lower()
    param = path_param.lower()
    if field == param:
        return True
    if field == "id":
        # A bare "id" from the resource this producer's path names, e.g. POST
        # /tickets returning {"id": ...} feeding {ticket_id} on /tickets/{id}.
        resource = producer_path.strip("/").split("/")[0].rstrip("s").lower()
        return param in (f"{resource}_id", "id")
    match = _ID_FIELD.match(param)
    if match and match.group("prefix"):
        return field == f"{match.group('prefix')}_id" or field == match.group("prefix")
    return False


def _response_identifier_fields(op: dict[str, Any]) -> list[str]:
    if op["method"] not in ("POST", "GET"):
        return []
    normalized = op.get("normalized_operation")
    responses = normalized.get("responses") if isinstance(normalized, dict) else None
    if not isinstance(responses, dict):
        return []
    fields: list[str] = []
    for status, response in responses.items():
        if not str(status).startswith("2") or not isinstance(response, dict):
            continue
        content = response.get("content")
        if not isinstance(content, dict):
            continue
        for payload in content.values():
            if not isinstance(payload, dict):
                continue
            schema = payload.get("schema")
            if not isinstance(schema, dict):
                continue
            properties = schema.get("properties")
            if isinstance(properties, dict):
                fields.extend(
                    key for key in properties if _ID_FIELD.match(str(key).lower())
                )
    return fields


def _async_lifecycle(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``create -> get status`` for operations on the same resource path.

    A ``POST`` on a collection paired with a ``GET`` on that collection's member
    path is the canonical async pattern the proposal names in §5.3
    ("async lifecycle patterns like create -> get status -> get result").
    """

    rows: list[dict[str, Any]] = []
    for create_op in operations:
        if create_op["method"] != "POST":
            continue
        for poll_op in operations:
            if poll_op["method"] != "GET" or poll_op["operation_id"] == create_op["operation_id"]:
                continue
            if _parent_collection_path(poll_op["path"]) != create_op["path"].rstrip("/"):
                continue
            rows.append(
                _make_row(
                    from_op=create_op,
                    to_op=poll_op,
                    dependency_type="polls",
                    confidence=CONFIDENCE_MEDIUM,
                    source="rule_inference",
                    required=False,
                    notes=f"{poll_op['path']} plausibly reports on the resource {create_op['path']} started",
                )
            )
    return rows


def _tag_grouping(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same-tag operations are advisory groupings, not hard dependencies.

    Kept at ``confidence="low"`` deliberately: shared tags are a weak, purely
    lexical signal (see §5.3, "weak text-based relationship hints"), so this is
    the one rule-based tier that lands as advisory rather than auto-wired.
    """

    rows: list[dict[str, Any]] = []
    by_tag: dict[str, list[dict[str, Any]]] = {}
    for op in operations:
        for tag in op.get("tags") or []:
            by_tag.setdefault(str(tag), []).append(op)
    for tag, members in by_tag.items():
        if len(members) < 2 or len(members) > 12:
            # A tag shared by too many operations is not a meaningful grouping signal.
            continue
        ordered = sorted(members, key=lambda op: op["operation_id"])
        for i, from_op in enumerate(ordered):
            for to_op in ordered[i + 1 :]:
                rows.append(
                    _make_row(
                        from_op=from_op,
                        to_op=to_op,
                        dependency_type="grouped_with",
                        confidence=CONFIDENCE_LOW,
                        source="rule_inference",
                        required=False,
                        notes=f"both tagged '{tag}'",
                    )
                )
    return rows
