"""Unit tests for deterministic OpenAPI operation dependency detection."""

from __future__ import annotations

from caliber.integrations.openapi.dependencies import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    detect_dependencies,
)
from caliber.integrations.openapi.normalize import normalize_openapi_document, parse_openapi_text

TICKET_SPEC = """
openapi: 3.0.3
info: {title: Ticket API, version: "1"}
servers: [{url: https://tickets.example.com}]
paths:
  /tickets:
    get:
      operationId: listTickets
      tags: [tickets]
      parameters:
        - {in: query, name: cursor, schema: {type: string}}
      responses: {"200": {description: ok}}
    post:
      operationId: createTicket
      tags: [tickets]
      requestBody:
        required: true
        content:
          application/json:
            schema: {type: object, properties: {title: {type: string}}, required: [title]}
      responses:
        "201":
          description: created
          content:
            application/json:
              schema: {type: object, properties: {id: {type: string}, status: {type: string}}}
  /tickets/{ticket_id}:
    get:
      operationId: getTicket
      tags: [tickets]
      parameters:
        - {in: path, name: ticket_id, required: true, schema: {type: string}}
      responses: {"200": {description: ok}}
"""

LINKED_SPEC = """
openapi: 3.0.3
info: {title: Linked API, version: "1"}
paths:
  /orders:
    post:
      operationId: createOrder
      responses:
        "201":
          description: created
          links:
            GetOrder:
              operationId: getOrder
  /orders/{order_id}:
    get:
      operationId: getOrder
      parameters:
        - {in: path, name: order_id, required: true, schema: {type: string}}
      responses: {"200": {description: ok}}
"""


def _operations(spec_text: str) -> list[dict]:
    document = parse_openapi_text(spec_text)
    _summary, operations, _warnings = normalize_openapi_document(document)
    for index, operation in enumerate(operations):
        operation["operation_id"] = f"OP-{index}"
    return operations


def _find(deps: list[dict], from_key: str, to_key: str, dependency_type: str, by_key: dict) -> dict | None:
    from_id = by_key[from_key]
    to_id = by_key[to_key]
    for dep in deps:
        if (
            dep["from_operation_id"] == from_id
            and dep["to_operation_id"] == to_id
            and dep["dependency_type"] == dependency_type
        ):
            return dep
    return None


def test_openapi_link_produces_high_confidence_dependency() -> None:
    operations = _operations(LINKED_SPEC)
    by_key = {op["operation_key"]: op["operation_id"] for op in operations}
    deps = detect_dependencies(operations)

    row = _find(deps, "POST /orders", "GET /orders/{order_id}", "produces_identifier_for", by_key)
    assert row is not None
    assert row["confidence"] == CONFIDENCE_HIGH
    assert row["source"] == "openapi_link"


def test_path_hierarchy_is_a_medium_confidence_precondition() -> None:
    operations = _operations(TICKET_SPEC)
    by_key = {op["operation_key"]: op["operation_id"] for op in operations}
    deps = detect_dependencies(operations)

    row = _find(deps, "POST /tickets", "GET /tickets/{ticket_id}", "precondition_for", by_key)
    assert row is not None
    assert row["confidence"] == CONFIDENCE_MEDIUM
    assert row["source"] == "path_structure"


def test_identifier_flow_matches_response_id_field_to_path_parameter() -> None:
    operations = _operations(TICKET_SPEC)
    by_key = {op["operation_key"]: op["operation_id"] for op in operations}
    deps = detect_dependencies(operations)

    row = _find(deps, "POST /tickets", "GET /tickets/{ticket_id}", "consumes_identifier_from", by_key)
    assert row is not None
    assert row["confidence"] == CONFIDENCE_MEDIUM
    assert row["source"] == "schema_match"
    assert row["binding_field_map"] == {"ticket_id": "response.id"}
    assert row["required"] is True


def test_grouped_with_is_low_confidence_and_advisory() -> None:
    operations = _operations(TICKET_SPEC)
    deps = detect_dependencies(operations)
    grouped = [dep for dep in deps if dep["dependency_type"] == "grouped_with"]
    assert grouped
    assert all(dep["confidence"] == CONFIDENCE_LOW for dep in grouped)
    assert all(dep["source"] == "rule_inference" for dep in grouped)


def test_a_large_shared_tag_does_not_produce_a_grouping_explosion() -> None:
    """More than 12 operations sharing one tag is not a meaningful signal."""

    operations = []
    for index in range(20):
        operations.append(
            {
                "operation_id": f"OP-{index}",
                "operation_key": f"GET /items/{index}",
                "path": f"/items/{index}",
                "method": "GET",
                "tags": ["shared"],
                "normalized_operation": {"responses": {}},
            }
        )
    deps = detect_dependencies(operations)
    assert not any(dep["dependency_type"] == "grouped_with" for dep in deps)


def test_detection_is_deterministic_across_repeated_calls() -> None:
    operations = _operations(TICKET_SPEC)
    first = detect_dependencies(operations)
    second = detect_dependencies([dict(op) for op in operations])
    assert first == second


def test_no_operations_produces_no_dependencies() -> None:
    assert detect_dependencies([]) == []


def test_async_lifecycle_polling_pair_is_detected() -> None:
    spec = """
openapi: 3.0.3
info: {title: Export API, version: "1"}
paths:
  /exports:
    post:
      operationId: startExport
      responses: {"202": {description: accepted}}
  /exports/{export_id}:
    get:
      operationId: getExportStatus
      parameters:
        - {in: path, name: export_id, required: true, schema: {type: string}}
      responses: {"200": {description: ok}}
"""
    operations = _operations(spec)
    by_key = {op["operation_key"]: op["operation_id"] for op in operations}
    deps = detect_dependencies(operations)
    row = _find(deps, "POST /exports", "GET /exports/{export_id}", "polls", by_key)
    assert row is not None
    assert row["confidence"] == CONFIDENCE_MEDIUM
    assert row["source"] == "rule_inference"
