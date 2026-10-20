"""``evaluate_assertion``: how a saved tool fixture is scored (cookbook 03, Fixtures stage).

The ``equals`` assertion used to demand the server's own canonical string (``json.dumps`` with
sorted keys and ``", "`` / ``": "`` separators), which nobody types by hand. A first user who typed the
same object in a different key order or without spaces saw a failing case for no reason.
"""

from __future__ import annotations

import pytest

from caliber.calibration import aggregate, evaluate_assertion

OUTPUT = {"decision": "approve", "reason_code": "ELIGIBLE_AUTO_APPROVE", "requires_approval": False}


def equals(value: object) -> dict[str, object]:
    return {"type": "equals", "value": value}


@pytest.mark.parametrize(
    "value",
    [
        # the canonical string the SDK script writes (still accepted, unchanged)
        '{"decision": "approve", "reason_code": "ELIGIBLE_AUTO_APPROVE", "requires_approval": false}',
        # the same object, any key order, any spacing, a pretty-printed paste
        '{"requires_approval":false,"reason_code":"ELIGIBLE_AUTO_APPROVE","decision":"approve"}',
        '{\n  "reason_code": "ELIGIBLE_AUTO_APPROVE",\n  "decision": "approve",\n  "requires_approval": false\n}',
    ],
)
def test_equals_accepts_the_same_json_object_however_it_is_written(value: str) -> None:
    assert evaluate_assertion(equals(value), OUTPUT, None) is True


@pytest.mark.parametrize(
    "value",
    [
        '{"decision": "deny", "reason_code": "ELIGIBLE_AUTO_APPROVE", "requires_approval": false}',
        '{"decision": "approve", "reason_code": "ELIGIBLE_AUTO_APPROVE"}',  # a missing key is not equal
        '{"decision": "approve", "reason_code": "ELIGIBLE_AUTO_APPROVE", "requires_approval": 0}',  # false is not 0
        '{"decision": "approve", "reason_code": "ELIGIBLE_AUTO_APPROVE", "requires_approval": false, "x": 1}',
        "not json at all",
        "[1, 2",
    ],
)
def test_equals_still_rejects_a_different_object(value: str) -> None:
    assert evaluate_assertion(equals(value), OUTPUT, None) is False


def test_equals_treats_49_and_49_point_0_as_the_same_json_number() -> None:
    # JSON has one number type, and a file written in JavaScript prints 49.0 as 49.
    assert evaluate_assertion(equals('{"amount": 49.0}'), {"amount": 49.0}, None) is True
    assert evaluate_assertion(equals('{"amount": 49}'), {"amount": 49.0}, None) is True
    assert evaluate_assertion(equals('{"amount": 50}'), {"amount": 49.0}, None) is False
    assert evaluate_assertion(equals('{"amount": "49"}'), {"amount": 49.0}, None) is False
    assert evaluate_assertion(
        equals('{"a": {"b": [1, {"c": null}]}}'), {"a": {"b": [1, {"c": None}]}}, None
    )
    assert not evaluate_assertion(equals('{"a": [1]}'), {"a": {"x": 1}}, None)
    assert not evaluate_assertion(equals('{"a": {"x": 1}}'), {"a": [1]}, None)
    assert not evaluate_assertion(equals('{"a": 1}'), {"a": True}, None)


def test_equals_on_a_list_output() -> None:
    assert evaluate_assertion(equals("[1,  2, 3]"), [1, 2, 3], None) is True
    assert evaluate_assertion(equals("[3, 2, 1]"), [1, 2, 3], None) is False


def test_equals_on_a_string_output_is_exact_text_not_json() -> None:
    assert evaluate_assertion(equals("approve"), "approve", None) is True
    # a string output is compared as text: JSON that happens to decode to it is not a match
    assert evaluate_assertion(equals('"approve"'), "approve", None) is False


def test_equals_without_a_value_or_with_an_error_fails() -> None:
    assert evaluate_assertion({"type": "equals"}, OUTPUT, None) is False
    assert (
        evaluate_assertion(equals('{"decision": "approve"}'), {"decision": "approve"}, "boom")
        is False
    )


def test_other_assertion_types_are_unchanged() -> None:
    assert evaluate_assertion(None, OUTPUT, None) is True
    assert evaluate_assertion({"type": "no_error"}, OUTPUT, "boom") is False
    assert (
        evaluate_assertion({"type": "output_contains", "value": "ELIGIBLE"}, OUTPUT, None) is True
    )
    assert evaluate_assertion({"type": "output_contains", "value": "DENIED"}, OUTPUT, None) is False
    assert evaluate_assertion({"type": "unknown_kind", "value": "x"}, OUTPUT, None) is False


def test_aggregate_counts_passes() -> None:
    result = aggregate([{"passed": True}, {"passed": False}, {"passed": True}, {"passed": True}])
    assert (result["total"], result["passed"], result["pass_rate"]) == (4, 3, 0.75)
    assert aggregate([])["pass_rate"] == 0.0
