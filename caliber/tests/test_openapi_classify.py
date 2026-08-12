"""Unit tests for deterministic OpenAPI operation classification."""

from __future__ import annotations

from caliber.integrations.openapi.classify import classify_operation


def _operation(
    *,
    method: str = "GET",
    path: str = "/tickets/{ticket_id}",
    spec_operation_id: str | None = None,
    summary: str = "",
    parameters: list[dict] | None = None,
    responses: dict | None = None,
) -> dict:
    return {
        "method": method,
        "path": path,
        "spec_operation_id": spec_operation_id,
        "summary": summary,
        "deprecated": False,
        "auth_schemes": [],
        "normalized_operation": {
            "parameters": parameters or [],
            "responses": responses or {},
        },
    }


def test_read_methods_are_always_read_even_on_an_admin_path() -> None:
    op = _operation(method="GET", path="/admin/api-keys", spec_operation_id="listApiKeys")
    result = classify_operation(op)
    assert result["side_effect_level"] == "read"
    assert result["is_admin"] is True


def test_write_on_admin_path_escalates_to_external_action() -> None:
    op = _operation(method="POST", path="/admin/api-keys", spec_operation_id="rotateApiKey")
    result = classify_operation(op)
    assert result["side_effect_level"] == "external_action"
    assert result["is_admin"] is True


def test_ordinary_write_stays_write() -> None:
    op = _operation(method="POST", path="/tickets", spec_operation_id="createTicket")
    result = classify_operation(op)
    assert result["side_effect_level"] == "write"
    assert result["is_admin"] is False


def test_send_notification_verb_escalates_to_external_action() -> None:
    op = _operation(
        method="POST", path="/tickets/{id}/notify", spec_operation_id="sendTicketNotification"
    )
    result = classify_operation(op)
    assert result["side_effect_level"] == "external_action"


def test_delete_is_flagged_destructive() -> None:
    op = _operation(method="DELETE", path="/tickets/{ticket_id}", spec_operation_id="deleteTicket")
    result = classify_operation(op)
    assert result["is_destructive"] is True
    assert result["operation_kind"] == "delete"


def test_collection_vs_member_get_are_distinguished() -> None:
    listing = classify_operation(_operation(method="GET", path="/tickets"))
    member = classify_operation(_operation(method="GET", path="/tickets/{ticket_id}"))
    assert listing["operation_kind"] == "list"
    assert listing["is_collection"] is True
    assert member["operation_kind"] == "get"
    assert member["is_collection"] is False


def test_resource_type_is_derived_from_the_last_static_path_segment() -> None:
    op = _operation(method="GET", path="/v1/tickets/{ticket_id}/comments")
    result = classify_operation(op)
    assert result["resource_type"] == "comment"


def test_cursor_pagination_is_detected_from_query_parameters() -> None:
    op = _operation(
        method="GET",
        path="/tickets",
        parameters=[
            {"in": "query", "name": "cursor"},
            {"in": "query", "name": "limit"},
        ],
    )
    result = classify_operation(op)
    assert result["pagination"]["style"] == "cursor"
    assert result["pagination"]["cursor_param"] == "cursor"
    assert result["pagination"]["limit_param"] == "limit"
    assert result["is_paginated"] is True


def test_offset_pagination_is_detected_when_no_cursor_or_page_param_exists() -> None:
    op = _operation(
        method="GET",
        path="/tickets",
        parameters=[{"in": "query", "name": "offset"}, {"in": "query", "name": "count"}],
    )
    result = classify_operation(op)
    assert result["pagination"]["style"] == "offset"


def test_post_is_never_classified_as_paginated() -> None:
    op = _operation(
        method="POST", path="/tickets/search", parameters=[{"in": "query", "name": "cursor"}]
    )
    result = classify_operation(op)
    assert result["pagination"]["style"] == "none"
    assert result["is_paginated"] is False


def test_202_response_marks_the_operation_async() -> None:
    op = _operation(method="POST", path="/exports", responses={"202": {"description": "accepted"}})
    result = classify_operation(op)
    assert result["is_async"] is True
    assert result["async_job"]["accepted_202"] is True


def test_job_handle_field_in_response_body_marks_the_operation_async() -> None:
    op = _operation(
        method="POST",
        path="/exports",
        responses={
            "201": {
                "content": {
                    "application/json": {
                        "schema": {"type": "object", "properties": {"job_id": {"type": "string"}}}
                    }
                }
            }
        },
    )
    result = classify_operation(op)
    assert result["is_async"] is True
    assert result["async_job"]["job_handle_field"] == "job_id"


def test_status_poll_path_is_flagged() -> None:
    op = _operation(
        method="GET", path="/exports/{export_id}/status", spec_operation_id="getExportStatus"
    )
    result = classify_operation(op)
    assert result["is_status_poll"] is True


def test_classification_is_deterministic_across_repeated_calls() -> None:
    op = _operation(method="POST", path="/tickets", spec_operation_id="createTicket")
    first = classify_operation(op)
    second = classify_operation(dict(op))
    assert first == second
