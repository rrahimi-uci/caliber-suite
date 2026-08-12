"""Unit tests for deterministic diffing between two imported OpenAPI versions."""

from __future__ import annotations

from caliber.integrations.openapi.diff import diff_operations


def _op(key: str, **overrides) -> dict:
    base = {
        "operation_key": key,
        "summary": "",
        "side_effect_level": "read",
        "deprecated": False,
        "auth_schemes": [],
        "request_body_required": False,
        "request_content_types": [],
        "response_statuses": ["200"],
        "tags": [],
    }
    base.update(overrides)
    return base


def test_added_and_removed_operations_are_detected() -> None:
    result = diff_operations(
        [_op("GET /tickets")],
        [_op("GET /tickets"), _op("POST /tickets", side_effect_level="write")],
    )
    assert result["added"] == ["POST /tickets"]
    assert result["removed"] == []
    assert result["summary"]["added_count"] == 1


def test_removed_operation_is_detected() -> None:
    result = diff_operations(
        [_op("GET /tickets"), _op("DELETE /tickets/{id}")],
        [_op("GET /tickets")],
    )
    assert result["removed"] == ["DELETE /tickets/{id}"]


def test_identical_operations_are_unchanged() -> None:
    result = diff_operations([_op("GET /tickets")], [_op("GET /tickets")])
    assert result["unchanged"] == ["GET /tickets"]
    assert result["changed"] == []


def test_free_text_description_churn_is_not_tracked() -> None:
    """Only the fixed field list is compared; unlisted fields never trigger a diff."""

    before = _op("GET /tickets")
    after = dict(before)
    after["description"] = "a completely different description"
    result = diff_operations([before], [after])
    assert result["unchanged"] == ["GET /tickets"]


def test_newly_deprecated_operation_is_flagged_breaking() -> None:
    result = diff_operations(
        [_op("GET /tickets", deprecated=False)],
        [_op("GET /tickets", deprecated=True)],
    )
    assert result["changed"][0]["changes"]["deprecated"] == {"from": False, "to": True}
    assert result["breaking"] == [
        {"operation_key": "GET /tickets", "reason": "operation was deprecated"}
    ]


def test_auth_requirement_change_is_flagged_breaking() -> None:
    result = diff_operations(
        [_op("GET /tickets", auth_schemes=[])],
        [_op("GET /tickets", auth_schemes=["bearerAuth"])],
    )
    assert result["breaking"][0]["reason"] == "authentication requirement changed"


def test_list_field_order_does_not_trigger_a_false_diff() -> None:
    """Lists compare as sets: reordered tags/response codes are not a real change."""

    result = diff_operations(
        [_op("GET /tickets", response_statuses=["200", "404"])],
        [_op("GET /tickets", response_statuses=["404", "200"])],
    )
    assert result["unchanged"] == ["GET /tickets"]


def test_side_effect_level_change_is_breaking() -> None:
    result = diff_operations(
        [_op("POST /tickets/{id}/close", side_effect_level="write")],
        [_op("POST /tickets/{id}/close", side_effect_level="external_action")],
    )
    assert result["breaking"][0]["reason"] == "side-effect classification changed"


def test_works_over_plain_dicts_or_orm_style_attribute_access() -> None:
    class Row:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    before = Row(**_op("GET /tickets"))
    after = Row(**_op("GET /tickets", deprecated=True))
    result = diff_operations([before], [after])
    assert result["breaking"]


def test_empty_to_empty_is_a_no_op_diff() -> None:
    result = diff_operations([], [])
    assert result == {
        "added": [],
        "removed": [],
        "changed": [],
        "unchanged": [],
        "breaking": [],
        "summary": {
            "added_count": 0,
            "removed_count": 0,
            "changed_count": 0,
            "unchanged_count": 0,
            "breaking_count": 0,
        },
    }
