# ruff: noqa: RUF001 - fullwidth digits and exotic spaces are the point of some of these cases
"""The AgentStepRequest / AgentStepResult v1 contracts (B08.1a, #713).

The contracts are strict on purpose: they are the gate in front of every agent step, so an extra field, an unknown
version, an invalid effect class, a missing target or a claim that an effect was committed is refused before anything
is dispatched. The proposed-action shape is the one the effect proposal (B05) already stores, and these tests pin that
the two cannot drift apart. Everything in a result is model output, so the refusals are also *quiet*: no message carries a
value from the record.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
import json
import re
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from caliber.contracts import (
    ContractValidationError,
    UnsupportedSchemaVersion,
    load_schema,
    registry,
    validate_record,
)
from caliber.contracts.agent_step import (
    CLAIM_MISMATCH,
    MAX_PROPOSED_ACTIONS,
    REQUEST_KIND,
    RESULT_KIND,
    STOP_REASONS,
    USAGE_SOURCES,
    AgentStepRequest,
    AgentStepResult,
    ClaimMismatch,
    ProposedAction,
    StepLimits,
    StepUsage,
    check_result_answers_request,
    parse_deadline,
    parse_step_request,
    parse_step_result,
)
from caliber.effects.inventory import EFFECT_CLASSES
from caliber.effects.records import (
    _PRINTABLE,
    MAX_ARGS_DEPTH,
    MAX_INTENT_BYTES,
    Intent,
    ProposalError,
    _text,
    intent_digest,
    read_proposal,
    record_proposal,
)
from caliber.workflows import runtime
from caliber.workflows.agent_step import ADAPTER_NOT_INTEGRATED, AgentStepAdapter, run_agent_step
from tests.agent_step_conformance import EXECUTOR_NAMES, every_executor, make_context
from tests.dialect_helpers import DIALECTS
from tests.test_effect_proposals import _seed_run
from tests.test_event_outbox import _database

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "contracts"
MAX_COUNT = 2**31 - 1
SECRET = "sk-looking-secret-0123456789"


def fixture(kind: str, name: str = "valid_1") -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / kind / f"{name}.json").read_text("utf-8"))
    return data


def request_record(**changes: Any) -> dict[str, Any]:
    return {**fixture(REQUEST_KIND), **changes}


def result_record(**changes: Any) -> dict[str, Any]:
    return {**fixture(RESULT_KIND), **changes}


def action_record(**changes: Any) -> dict[str, Any]:
    return {**fixture(RESULT_KIND)["proposed_actions"][0], **changes}


def with_action(**changes: Any) -> dict[str, Any]:
    return result_record(proposed_actions=[action_record(**changes)])


def the_request() -> AgentStepRequest:
    return parse_step_request(fixture(REQUEST_KIND))


def parse_result(payload: dict[str, Any]) -> AgentStepResult:
    """Parse a result as the answer to the fixture request, which its identifying fields match."""
    return parse_step_result(payload, the_request())


def refused(kind: str, payload: dict[str, Any]) -> ContractValidationError:
    with pytest.raises(ContractValidationError) as raised:
        if kind == REQUEST_KIND:
            parse_step_request(payload)
        else:
            parse_result(payload)
    assert raised.value.kind == kind and raised.value.errors
    return raised.value


def action(**changes: Any) -> ProposedAction:
    return ProposedAction.from_record(action_record(**changes))


# ---------------------------------------------------------------- the valid records


def test_a_valid_request_is_read_and_written_back_unchanged() -> None:
    record = fixture(REQUEST_KIND)
    request = parse_step_request(record)
    assert request == AgentStepRequest(
        step_request_id="sr-0001",
        workflow_run_id="run-0001",
        node_id="request_document",
        occurrence_id="occ-0001",
        attempt=1,
        run_manifest_digest="a" * 64,
        context_manifest_digest="b" * 64,
        grant_id="grant-0001",
        limits=StepLimits(
            max_turns=8, max_tool_calls=4, max_tokens=20000, deadline="2026-10-26T09:10:00Z"
        ),
    )
    assert request.record() == record


def test_a_valid_result_is_read_and_written_back_unchanged() -> None:
    record = fixture(RESULT_KIND)
    result = parse_result(record)
    assert result.stop_reason == "proposed_action"
    assert result.usage == StepUsage(1200, 85, 1285, 2, 1)
    assert result.proposed_actions == (
        ProposedAction(
            connector="document-request",
            connector_version="1.0.0",
            effect_class="irreversible_write",
            resource="cases/MORT-2026-000184/document-requests",
            args={"document_type": "pay_statement"},
            expected_resource_version="1",
        ),
    )
    assert result.record() == record


def test_a_completed_step_proposes_nothing_and_may_report_cached_tokens() -> None:
    record = fixture(RESULT_KIND, "valid_completed")
    record["usage"]["cached_prompt_tokens"] = 400
    result = parse_result(record)
    assert result.proposed_actions == () and result.usage.cached_prompt_tokens == 400
    assert result.record() == record


def test_limits_of_zero_and_the_largest_count_are_valid() -> None:
    zero = {"max_turns": 0, "max_tool_calls": 0, "max_tokens": 0}
    limits = {**fixture(REQUEST_KIND)["limits"], **zero}
    assert parse_step_request(request_record(limits=limits)).limits.max_tokens == 0
    limits = {**limits, "max_tokens": MAX_COUNT, "max_turns": MAX_COUNT}
    assert parse_step_request(request_record(limits=limits)).limits.max_turns == MAX_COUNT


def test_every_stop_reason_without_proposals_is_valid() -> None:
    for reason in STOP_REASONS:
        if reason != "proposed_action":
            record = result_record(stop_reason=reason, proposed_actions=[])
            assert parse_result(record).stop_reason == reason


# ---------------------------------------------------------------- what the gate refuses


@pytest.mark.parametrize("kind", [REQUEST_KIND, RESULT_KIND])
@pytest.mark.parametrize("version", ["v2", "V1", "1", "", None, 1, ["v1"], SECRET])
def test_unsupported_version(kind: str, version: object) -> None:
    record = {**fixture(kind), "schema_version": version}
    with pytest.raises(UnsupportedSchemaVersion) as raised:
        if kind == REQUEST_KIND:
            parse_step_request(record)
        else:
            parse_result(record)
    assert raised.value.kind == kind and raised.value.supported == ("v1",)
    # a short plain token is named, anything else only by its type, so a hostile value is never echoed
    plain = isinstance(version, str) and bool(re.fullmatch(r"[A-Za-z0-9._-]{1,16}", version))
    label = version if plain else f"<{type(version).__name__}>"
    assert str(raised.value) == f"{kind} schema_version {label} is not supported; supported: v1"
    assert SECRET not in str(raised.value)


@pytest.mark.parametrize("kind", [REQUEST_KIND, RESULT_KIND])
def test_a_record_without_a_version_is_refused(kind: str) -> None:
    record = fixture(kind)
    del record["schema_version"]
    assert "'schema_version' is a required property" in refused(kind, record).errors[0]


@pytest.mark.parametrize("payload", [None, [], "text", 7])
def test_a_payload_that_is_not_an_object_is_refused(payload: Any) -> None:
    with pytest.raises(ContractValidationError, match="expected an object"):
        parse_step_request(payload)
    with pytest.raises(ContractValidationError, match="expected an object"):
        parse_result(payload)


def test_an_extra_field_is_refused_at_every_level_without_naming_it() -> None:
    surprise = {SECRET: 1}
    request = fixture(REQUEST_KIND)
    for broken in (
        {**request, **surprise},
        {**request, "limits": {**request["limits"], **surprise}},
    ):
        error = refused(REQUEST_KIND, broken)
        assert error.errors == [
            f"{'limits' if 'limits' in error.errors[0] else '<record>'}: additional properties are not allowed"
        ]
        assert SECRET not in str(error)
    result = fixture(RESULT_KIND)
    item = result["proposed_actions"][0]
    for broken, path in (
        ({**result, **surprise}, "<record>"),
        ({**result, "usage": {**result["usage"], **surprise}}, "usage"),
        ({**result, "proposed_actions": [{**item, **surprise}]}, "proposed_actions/0"),
        (
            {**result, "proposed_actions": [{**item, "target": {**item["target"], **surprise}}]},
            "proposed_actions/0/target",
        ),
    ):
        error = refused(RESULT_KIND, broken)
        assert error.errors == [f"{path}: additional properties are not allowed"]
        assert SECRET not in str(error)


@pytest.mark.parametrize("kind", [REQUEST_KIND, RESULT_KIND])
def test_every_required_field_is_required(kind: str) -> None:
    for name in load_schema(kind, "v1")["required"]:
        if name == "schema_version":
            continue
        record = fixture(kind)
        del record[name]
        assert f"'{name}' is a required property" in refused(kind, record).errors[0]


def test_every_nested_required_field_is_required() -> None:
    for name in ("max_turns", "max_tool_calls", "max_tokens", "deadline"):
        record = fixture(REQUEST_KIND)
        del record["limits"][name]
        assert name in refused(REQUEST_KIND, record).errors[0]
    for name in ("prompt_tokens", "completion_tokens", "total_tokens", "turns", "tool_calls"):
        record = fixture(RESULT_KIND)
        del record["usage"][name]
        assert name in refused(RESULT_KIND, record).errors[0]
    for name in (
        "effect_class",
        "target",
        "connector_version",
        "args",
        "expected_resource_version",
    ):
        record = fixture(RESULT_KIND)
        del record["proposed_actions"][0][name]
        assert name in refused(RESULT_KIND, record).errors[0]


def test_result_rejects_commit_claim() -> None:
    """A step proposes; a result that says anything was committed, received or approved is invalid."""
    base = fixture(RESULT_KIND)
    item = base["proposed_actions"][0]
    claims = [
        {**base, "state": "committed"},
        {**base, "committed": True},
        {**base, "receipt_id": "rc-1"},
        {**base, "receipt": {"receipt_id": "rc-1"}},
        {**base, "approval_id": "ap-1"},
        {**base, "stop_reason": "committed"},
        {**base, "proposed_actions": [{**item, "state": "committed"}]},
        {**base, "proposed_actions": [{**item, "receipt_id": "rc-1"}]},
        {**base, "proposed_actions": [{**item, "proposal_id": "pr-1", "state": "approved"}]},
    ]
    for claim in claims:
        refused(RESULT_KIND, claim)
    # the same refusal at the writing side: a result object cannot be turned into a record that claims a commit
    result = AgentStepResult(
        step_request_id="sr-0001",
        workflow_run_id="run-0001",
        node_id="n",
        occurrence_id="o",
        attempt=1,
        stop_reason="committed",
        usage=StepUsage(0, 0, 0, 0, 0),
    )
    with pytest.raises(ContractValidationError, match="stop_reason"):
        result.record()


@pytest.mark.parametrize(
    "effect_class", ["write", "destroy", "READ", "Read", "", None, 1, ["read"], "read ", SECRET]
)
def test_an_invalid_effect_class_is_refused_without_echoing_it(effect_class: object) -> None:
    error = refused(RESULT_KIND, with_action(effect_class=effect_class))
    assert error.errors[0].startswith("proposed_actions/0/effect_class: ")
    assert SECRET not in str(error)


@pytest.mark.parametrize("effect_class", EFFECT_CLASSES)
def test_every_effect_class_of_the_reference_profile_is_accepted(effect_class: str) -> None:
    assert parse_result(with_action(effect_class=effect_class)).proposed_actions


@pytest.mark.parametrize(
    "target",
    [
        None,
        "document-request",
        [],
        {},
        {"connector": "c"},
        {"resource": "r"},
        {"connector": "", "resource": "r"},
        {"connector": "c", "resource": ""},
        {"connector": "c", "resource": None},
        {"connector": 1, "resource": "r"},
    ],
)
def test_a_missing_or_incomplete_target_is_refused(target: object) -> None:
    assert "target" in " ".join(refused(RESULT_KIND, with_action(target=target)).errors)


def test_a_missing_target_key_is_refused() -> None:
    record = fixture(RESULT_KIND, "invalid_missing_target")
    assert refused(RESULT_KIND, record).errors == [
        "proposed_actions/0: 'target' is a required property"
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"args": "text"},
        {"args": [1]},
        {"args": None},
        {"expected_resource_version": 3},
        {"expected_resource_version": ""},
        {"connector_version": ""},
        {"connector_version": None},
    ],
)
def test_a_malformed_action_field_is_refused(changes: dict[str, Any]) -> None:
    refused(RESULT_KIND, with_action(**changes))


def test_an_unversioned_target_is_allowed() -> None:
    assert parse_result(with_action(expected_resource_version=None)).proposed_actions


# ---------------------------------------------------------------- integers are integers


@pytest.mark.parametrize("value", [1.0, 2.0, 0.5, True, "1", None])
def test_a_float_is_never_an_integer_even_when_it_is_whole(value: object) -> None:
    refused(REQUEST_KIND, request_record(attempt=value))
    refused(RESULT_KIND, result_record(attempt=value))
    limits = {**fixture(REQUEST_KIND)["limits"], "max_turns": value}
    refused(REQUEST_KIND, request_record(limits=limits))
    usage = {**fixture(RESULT_KIND)["usage"], "turns": value}
    refused(RESULT_KIND, result_record(usage=usage))


def test_the_integer_rule_is_the_agent_step_kinds_alone() -> None:
    receipt = {**json.loads((FIXTURES / "receipt" / "valid_1.json").read_text()), "attempt": 1.0}
    validate_record("receipt", receipt)  # the other kinds keep their schemas' reading of an integer
    error = refused(REQUEST_KIND, request_record(attempt=1.0))
    assert error.errors == ["attempt: must be of type integer"]


# ---------------------------------------------------------------- size and format limits


@pytest.mark.parametrize("name", ["step_request_id", "node_id", "occurrence_id"])
def test_a_step_identifier_is_one_to_128_characters(name: str) -> None:
    assert parse_step_request(request_record(**{name: "x" * 128}))
    for bad in ("", "x" * 129, None, 5):
        refused(REQUEST_KIND, request_record(**{name: bad}))
        refused(RESULT_KIND, result_record(**{name: bad}))


def test_the_run_id_and_the_grant_id_have_the_lengths_the_manifest_gives_them() -> None:
    assert parse_step_request(request_record(workflow_run_id="x" * 64, grant_id="g" * 64))
    refused(REQUEST_KIND, request_record(workflow_run_id="x" * 65))
    refused(REQUEST_KIND, request_record(grant_id="g" * 65))
    refused(REQUEST_KIND, request_record(grant_id=""))
    refused(RESULT_KIND, result_record(workflow_run_id="x" * 65))


@pytest.mark.parametrize("attempt", [0, -1, True, "1", None, MAX_COUNT + 1])
def test_an_attempt_is_a_positive_integer(attempt: object) -> None:
    refused(REQUEST_KIND, request_record(attempt=attempt))
    refused(RESULT_KIND, result_record(attempt=attempt))


def test_the_largest_attempt_is_valid() -> None:
    assert parse_step_request(request_record(attempt=MAX_COUNT)).attempt == MAX_COUNT


@pytest.mark.parametrize("name", ["run_manifest_digest", "context_manifest_digest"])
@pytest.mark.parametrize(
    "digest",
    ["a" * 63, "a" * 65, "A" * 64, "g" * 64, "sha256:" + "a" * 64, "a" * 64 + "\n", "", None, 7],
)
def test_a_digest_is_64_lowercase_hex_digits(name: str, digest: object) -> None:
    refused(REQUEST_KIND, request_record(**{name: digest}))


@pytest.mark.parametrize("name", ["max_turns", "max_tool_calls", "max_tokens"])
@pytest.mark.parametrize("value", [-1, 0.5, True, "5", None, MAX_COUNT + 1])
def test_a_limit_is_a_non_negative_integer_within_the_database_range(
    name: str, value: object
) -> None:
    limits = {**fixture(REQUEST_KIND)["limits"], name: value}
    refused(REQUEST_KIND, request_record(limits=limits))


@pytest.mark.parametrize("name", ["prompt_tokens", "completion_tokens", "total_tokens", "turns"])
@pytest.mark.parametrize("value", [-1, 1.5, False, "1", None, MAX_COUNT + 1])
def test_a_usage_count_is_a_non_negative_integer(name: str, value: object) -> None:
    usage = {**fixture(RESULT_KIND)["usage"], name: value}
    refused(RESULT_KIND, result_record(usage=usage))


@pytest.mark.parametrize("name", ["tool_calls", "cached_prompt_tokens"])
def test_the_other_usage_counts_are_checked_the_same_way(name: str) -> None:
    for value in (-1, 0.5, None, "2"):
        usage = {**fixture(RESULT_KIND)["usage"], name: value}
        refused(RESULT_KIND, result_record(usage=usage))


@pytest.mark.parametrize(
    ("path", "limit"),
    [
        (("target", "connector"), 128),
        (("connector_version",), 64),
        (("target", "resource"), 512),
        (("expected_resource_version",), 256),
    ],
)
def test_action_identifiers_have_the_lengths_the_intent_allows(
    path: tuple[str, ...], limit: int
) -> None:
    def set_at(value: str) -> dict[str, Any]:
        item = action_record()
        node = item
        for step in path[:-1]:
            node = node[step]
        node[path[-1]] = value
        return result_record(proposed_actions=[item])

    assert parse_result(set_at("x" * limit))
    error = refused(RESULT_KIND, set_at("x" * (limit + 1)))
    # the schema says so itself, ahead of (and not only through) the intent's own check
    assert f"violates maxLength {limit}" in error.errors[0]


def test_the_schema_bounds_are_the_ones_the_intent_enforces() -> None:
    """Whatever the schema lets through at its limits the Intent accepts, so the two checks never disagree."""
    item = action_record(
        target={"connector": "c" * 128, "resource": "r" * 512},
        connector_version="v" * 64,
        expected_resource_version="e" * 256,
    )
    assert ProposedAction.from_record(item).intent_hash.startswith("sha256:")


# ---------------------------------------------------------------- identifiers: the rule records._text applies


ID_BATTERY = [
    "a",
    "é",
    "a b",
    "x" * 128,
    " ",
    "   ",
    "\t",
    " ",
    " ",
    " x",
    " a ",
    "a\nb",
    "a\x00b",
    "a\x7fb",
    "a\x85b",
    "<surrogate:a+D800>",
    "<surrogate:DFFF>",
    "",
]
_SURROGATES = {"<surrogate:a+D800>": "a" + chr(0xD800), "<surrogate:DFFF>": chr(0xDFFF)}


def battery_value(case: str) -> str:
    """A lone surrogate must not be a pytest parameter value (the Allure listener writes parameters to UTF-8 JSON and aborts the
    whole run), so the battery names it and the test builds it."""
    return _SURROGATES.get(case, case)


def id_pattern() -> str:
    pattern: str = load_schema(REQUEST_KIND, "v1")["properties"]["node_id"]["pattern"]
    return pattern


def test_the_identifier_pattern_is_the_one_the_intent_uses() -> None:
    assert id_pattern() == "^(?=[\\s\\S]*[^\\s])" + _PRINTABLE.pattern + "\\Z"
    for kind in (REQUEST_KIND, RESULT_KIND):
        schema = load_schema(kind, "v1")
        for name in ("step_request_id", "workflow_run_id", "node_id", "occurrence_id"):
            assert schema["properties"][name]["pattern"] == id_pattern()
    assert load_schema(REQUEST_KIND, "v1")["properties"]["grant_id"]["pattern"] == id_pattern()
    item = load_schema(RESULT_KIND, "v1")["properties"]["proposed_actions"]["items"]["properties"]
    for name in ("connector_version", "expected_resource_version"):
        assert item[name]["pattern"] == id_pattern()
    for name in ("connector", "resource"):
        assert item["target"]["properties"][name]["pattern"] == id_pattern()


@pytest.mark.parametrize("case", ID_BATTERY)
def test_the_schema_accepts_an_identifier_exactly_when_the_intent_does(case: str) -> None:
    value = battery_value(case)
    try:
        _text("node_id", value)
        intent_ok = True
    except ProposalError:
        intent_ok = False
    schema_ok = (
        bool(re.search(id_pattern(), value))
        and 1 <= len(value) <= 128  # the schema's own bounds are checked separately
    )
    assert schema_ok == intent_ok, repr(value)


@pytest.mark.parametrize("case", [v for v in ID_BATTERY if v and len(v) <= 64])
def test_an_identifier_the_intent_would_refuse_is_refused_at_the_contract(case: str) -> None:
    value = battery_value(case)
    valid = bool(re.search(id_pattern(), value))
    for kind, make in ((REQUEST_KIND, request_record), (RESULT_KIND, result_record)):
        for name in ("step_request_id", "workflow_run_id", "node_id", "occurrence_id"):
            payload = make(**{name: value})
            if valid:
                validate_record(kind, payload)
            else:
                with pytest.raises(ContractValidationError) as raised:
                    validate_record(kind, payload)
                assert raised.value.errors == [f"{name}: does not match the required pattern"]
    grant = request_record(grant_id=value)
    if valid:
        validate_record(REQUEST_KIND, grant)
    else:
        with pytest.raises(ContractValidationError):
            validate_record(REQUEST_KIND, grant)


# ---------------------------------------------------------------- the deadline


@pytest.mark.parametrize(
    "deadline",
    [
        "2026-13-45T00:00:00Z",
        "2026-02-30T00:00:00Z",
        "2026-10-26T09:05:00",
        "2026-10-26 09:05:00Z",
        "2026-10-26T09:05:00Z\n",
        "2026-10-26T09:05:60Z",  # a leap second is not accepted here
        "2026-12-31T23:59:60Z",
        "２０２６-10-26T09:05:00Z",  # fullwidth digits
        "2026-10-26T09:05:00.1234567890Z",  # ten fractional digits
        "2026-10-26T09:05:00.Z",
        "2026-10-26T24:00:00Z",
        "2026-10-26T09:05:00+24:00",
        "tomorrow",
        "",
        None,
        1_800_000_000,
    ],
)
def test_a_deadline_is_a_real_rfc_3339_instant_with_an_offset(deadline: object) -> None:
    limits = {**fixture(REQUEST_KIND)["limits"], "deadline": deadline}
    refused(REQUEST_KIND, request_record(limits=limits))


@pytest.mark.parametrize(
    "deadline",
    ["２０２６-10-26T09:05:00Z", "2026-１０-26T09:05:00Z", "2026-10-26T09:05:00.１Z"],
)
def test_the_deadline_pattern_itself_refuses_non_ascii_digits(deadline: str) -> None:
    limits = {**fixture(REQUEST_KIND)["limits"], "deadline": deadline}
    error = refused(REQUEST_KIND, request_record(limits=limits))
    assert "limits/deadline: does not match the required pattern" in error.errors


@pytest.mark.parametrize(
    ("deadline", "expected"),
    [
        ("2026-10-26T09:10:00Z", datetime(2026, 10, 26, 9, 10, tzinfo=timezone.utc)),
        (
            "2026-10-26T09:05:00-08:00",
            datetime(2026, 10, 26, 9, 5, tzinfo=timezone(timedelta(hours=-8))),
        ),
        (
            "2028-02-29T23:59:59.5+05:30",
            datetime(
                2028, 2, 29, 23, 59, 59, 500000, tzinfo=timezone(timedelta(hours=5, minutes=30))
            ),
        ),
        (
            "2026-10-26T09:05:00.123456789Z",
            datetime(2026, 10, 26, 9, 5, 0, 123456, tzinfo=timezone.utc),
        ),
        ("2026-10-26T09:05:00.000001Z", datetime(2026, 10, 26, 9, 5, 0, 1, tzinfo=timezone.utc)),
    ],
)
def test_a_deadline_is_exposed_as_an_aware_datetime(deadline: str, expected: datetime) -> None:
    limits = {**fixture(REQUEST_KIND)["limits"], "deadline": deadline}
    parsed = parse_step_request(request_record(limits=limits)).limits
    assert parsed.deadline == deadline
    assert parsed.deadline_at == expected and parsed.deadline_at.utcoffset() is not None
    assert parsed.deadline_at.utcoffset() == expected.utcoffset()


def test_the_deadline_reader_refuses_what_the_schema_refuses() -> None:
    for bad in ("２０２６-10-26T09:05:00Z", "2026-10-26T09:05:00", "nope"):
        with pytest.raises(ValueError, match="deadline"):
            parse_deadline(bad)
    assert StepLimits(1, 1, 1, "2026-10-26T09:10:00Z").deadline_at.tzinfo is timezone.utc


def test_other_kinds_keep_leap_seconds_but_no_kind_reads_fullwidth_digits() -> None:
    record = json.loads((FIXTURES / "approval_request" / "valid_1.json").read_text())
    validate_record("approval_request", {**record, "expires_at": "2026-12-31T23:59:60Z"})
    with pytest.raises(ContractValidationError):
        validate_record("approval_request", {**record, "expires_at": "２０２６-10-26T09:05:00Z"})


# ---------------------------------------------------------------- usage


def test_usage_may_not_understate_the_total_or_cache_more_than_the_prompt() -> None:
    assert StepUsage(10, 5, 15, 1, 0)
    assert StepUsage(
        10, 5, 40, 1, 0, 10
    )  # a provider may charge more than the sum; cached may equal the prompt
    with pytest.raises(ContractValidationError) as short:
        StepUsage(10, 5, 14, 1, 0)
    assert short.value.errors == [
        "usage/total_tokens: is less than prompt_tokens + completion_tokens"
    ]
    with pytest.raises(ContractValidationError) as cached:
        StepUsage(10, 5, 15, 1, 0, 11)
    assert cached.value.errors == ["usage/cached_prompt_tokens: exceeds prompt_tokens"]
    with pytest.raises(ContractValidationError) as both:
        StepUsage(10, 5, 14, 1, 0, 11)
    assert len(both.value.errors) == 2


def test_a_result_that_understates_its_usage_is_refused_when_read() -> None:
    usage = {**fixture(RESULT_KIND)["usage"], "total_tokens": 1284}
    error = refused(RESULT_KIND, result_record(usage=usage))
    assert error.errors == ["usage/total_tokens: is less than prompt_tokens + completion_tokens"]
    usage = {**fixture(RESULT_KIND)["usage"], "cached_prompt_tokens": 1201}
    assert refused(RESULT_KIND, result_record(usage=usage)).errors == [
        "usage/cached_prompt_tokens: exceeds prompt_tokens"
    ]
    usage = {**fixture(RESULT_KIND)["usage"], "total_tokens": 5000, "cached_prompt_tokens": 1200}
    assert parse_result(result_record(usage=usage)).usage.total_tokens == 5000


@pytest.mark.parametrize("source", USAGE_SOURCES)
def test_the_usage_source_is_optional_and_one_of_three(source: str) -> None:
    result = parse_result(result_record(usage_source=source))
    assert result.usage_source == source and result.record()["usage_source"] == source
    assert parse_result(result_record()).usage_source is None
    assert "usage_source" not in parse_result(result_record()).record()


@pytest.mark.parametrize("source", ["", "Provider", "guess", None, 1, SECRET])
def test_an_unknown_usage_source_is_refused(source: object) -> None:
    error = refused(RESULT_KIND, result_record(usage_source=source))
    assert error.errors[0].startswith("usage_source: ") and SECRET not in str(error)


def test_zero_usage_that_says_it_is_unknown_is_valid_for_a_failed_call() -> None:
    record = result_record(
        stop_reason="error",
        proposed_actions=[],
        usage={
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "turns": 1,
            "tool_calls": 0,
        },
        usage_source="unknown",
        error_code="provider_timeout",
    )
    result = parse_result(record)
    assert (result.usage_source, result.error_code) == ("unknown", "provider_timeout")
    assert result.record() == record


@pytest.mark.parametrize("reason", ["error", "refused"])
def test_an_error_code_goes_with_error_or_refused(reason: str) -> None:
    assert parse_result(
        result_record(stop_reason=reason, proposed_actions=[], error_code="policy.denied-1")
    )


@pytest.mark.parametrize("reason", ["completed", "max_tokens", "deadline"])
def test_an_error_code_with_another_stop_reason_is_refused(reason: str) -> None:
    refused(RESULT_KIND, result_record(stop_reason=reason, proposed_actions=[], error_code="x"))
    refused(RESULT_KIND, result_record(error_code="x"))  # proposed_action


@pytest.mark.parametrize("code", ["", "Upper", "1abc", "has space", "x" * 65, "a\n", None, 3])
def test_an_error_code_is_a_short_lowercase_token(code: object) -> None:
    refused(RESULT_KIND, result_record(stop_reason="error", proposed_actions=[], error_code=code))


def test_a_refused_result_is_the_end_of_it_and_records_nothing_to_release() -> None:
    """A result that fails the gate never becomes an AgentStepResult: there is no object to settle usage from."""
    broken = result_record(stop_reason="committed")
    with pytest.raises(ContractValidationError):
        parse_result(broken)


# ---------------------------------------------------------------- one proposal per step


def test_one_proposed_action_per_step_in_v1() -> None:
    assert MAX_PROPOSED_ACTIONS == 1
    schema = load_schema(RESULT_KIND, "v1")["properties"]["proposed_actions"]
    assert schema["maxItems"] == MAX_PROPOSED_ACTIONS
    assert len(parse_result(result_record()).proposed_actions) == 1


def test_two_actions_are_refused_whether_they_differ_or_not() -> None:
    different = [action_record(), action_record(args={"document_type": "w2"})]
    error = refused(RESULT_KIND, result_record(proposed_actions=different))
    assert error.errors == ["proposed_actions: violates maxItems 1"]
    refused(RESULT_KIND, result_record(proposed_actions=[action_record(), action_record()]))
    refused(RESULT_KIND, result_record(proposed_actions=[action_record() for _ in range(17)]))


def test_the_writing_side_cannot_emit_two_either() -> None:
    ids: dict[str, Any] = {
        "step_request_id": "sr",
        "workflow_run_id": "run",
        "node_id": "n",
        "occurrence_id": "o",
        "attempt": 1,
    }
    result = AgentStepResult(
        **ids,
        stop_reason="proposed_action",
        usage=StepUsage(1, 1, 2, 1, 0),
        proposed_actions=(action(), action(args={"other": 1})),
    )
    with pytest.raises(ContractValidationError, match="maxItems"):
        result.record()


# ---------------------------------------------------------------- stop reason and proposals agree


@pytest.mark.parametrize("reason", [r for r in STOP_REASONS if r != "proposed_action"])
def test_a_proposal_with_any_other_stop_reason_is_refused(reason: str) -> None:
    assert refused(RESULT_KIND, result_record(stop_reason=reason)).errors == [
        "proposed_actions: must equal []"
    ]


def test_the_proposed_action_stop_reason_needs_a_proposal() -> None:
    error = refused(RESULT_KIND, result_record(stop_reason="proposed_action", proposed_actions=[]))
    assert error.errors == ["proposed_actions: violates minItems 1"]


def test_the_stop_reasons_are_exactly_the_documented_ones() -> None:
    schema = load_schema(RESULT_KIND, "v1")
    assert tuple(schema["properties"]["stop_reason"]["enum"]) == STOP_REASONS
    for bad in ("committed", "complete", "", None, "Completed", "end_turn"):
        refused(RESULT_KIND, result_record(stop_reason=bad, proposed_actions=[]))


# ---------------------------------------------------------------- one shape with the proposal (B05)


def test_a_proposed_action_has_the_shape_of_the_proposal_it_becomes() -> None:
    proposal = load_schema("proposal", "v1")["properties"]
    properties = load_schema(RESULT_KIND, "v1")["properties"]["proposed_actions"]["items"][
        "properties"
    ]

    def comparable(schema: dict[str, Any]) -> dict[str, Any]:
        """Drop what the step contract adds on top of the proposal schema: length bounds, the identifier rule, notes."""
        return {
            k: comparable(v) if isinstance(v, dict) else v
            for k, v in schema.items()
            if k not in {"maxLength", "pattern", "description"}
        }

    assert set(properties) == {
        "effect_class",
        "target",
        "connector_version",
        "args",
        "expected_resource_version",
    }
    for name in properties:
        assert comparable(properties[name]) == comparable(proposal[name]), name
    assert set(properties["effect_class"]["enum"]) == set(EFFECT_CLASSES)
    required_in_proposal = set(load_schema("proposal", "v1")["required"])
    assert {"effect_class", "target", "args", "expected_resource_version"} <= required_in_proposal


def test_a_proposed_action_drops_into_a_valid_proposal_record() -> None:
    proposed = parse_result(fixture(RESULT_KIND)).proposed_actions[0]
    proposal = {
        "schema_version": "v1",
        "proposal_id": "pr-1",
        "workflow_run_id": "run-0001",
        "node_id": "request_document",
        "logical_key": "k",
        "intent_hash": proposed.intent_hash,
        "state": "proposed",
        "tenant_id": "local",
        "region": "local",
        **proposed.record(),
    }
    validate_record("proposal", proposal)  # raises when the proposal record is not valid


def test_the_intent_hash_is_the_digest_of_the_shared_intent() -> None:
    proposed = parse_result(fixture(RESULT_KIND)).proposed_actions[0]
    intent = Intent(
        connector="document-request",
        connector_version="1.0.0",
        effect_class="irreversible_write",
        resource="cases/MORT-2026-000184/document-requests",
        args={"document_type": "pay_statement"},
        expected_resource_version="1",
    )
    assert proposed.intent == intent and proposed.intent_hash == intent.digest
    # every field of the intent changes the digest: a different connector version is a different approval
    other = dataclasses.replace(proposed, connector_version="1.0.1")
    assert other.intent_hash != proposed.intent_hash
    other = dataclasses.replace(proposed, effect_class="export")
    assert other.intent_hash != proposed.intent_hash


def test_key_order_in_args_does_not_change_the_digest() -> None:
    a = action(args={"a": 1, "b": {"c": 2, "d": 3}})
    b = action(args={"b": {"d": 3, "c": 2}, "a": 1})
    assert a.intent_hash == b.intent_hash and a == b
    assert list(a.record()["args"]) == ["a", "b"]


# ---------------------------------------------------------------- an action is immutable and compares by its intent


def test_equality_and_hash_follow_the_canonical_intent() -> None:
    assert action(args={"a": True}) != action(
        args={"a": 1}
    )  # 1 == True in Python, not in the intent
    assert action(args={"a": 1}) != action(args={"a": 1.0})
    assert action(args={"a": [1]}) == action(args={"a": [1]})
    assert hash(action(args={"a": [1, {"b": 2}]})) == hash(action(args={"a": [1, {"b": 2}]}))
    assert hash(action(args={"a": True})) != hash(action(args={"a": 1}))
    assert len({action(args={"a": 1}), action(args={"a": 1}), action(args={"a": True})}) == 2
    assert action() != "not an action" and action() != action(effect_class="export")


def test_args_are_a_read_only_deep_view() -> None:
    source: dict[str, Any] = {"n": [1, {"deep": [2]}], "m": {"k": 1}}
    proposed = action(args=source)
    with pytest.raises(TypeError):
        proposed.args["x"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        proposed.args["m"]["k"] = 2
    assert isinstance(proposed.args["n"], tuple)
    with pytest.raises(AttributeError):
        proposed.args["n"].append(3)  # type: ignore[attr-defined]
    with pytest.raises(dataclasses.FrozenInstanceError):
        proposed.args = {}  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        proposed.effect_class = "read"  # type: ignore[misc]
    source["n"].append("later")  # the caller's own mapping is not shared either
    source["m"]["k"] = "later"
    assert proposed.record()["args"] == {"n": [1, {"deep": [2]}], "m": {"k": 1}}


def test_nothing_handed_out_can_make_the_args_disagree_with_the_digest() -> None:
    proposed = action(args={"n": [1], "m": {"k": 1}})
    digest, before = proposed.intent_hash, copy.deepcopy(proposed.record())
    handed = proposed.intent
    handed.plain_args["n"].append(2)  # an Intent that was handed out is a copy
    handed.plain_args["injected"] = True
    record = proposed.record()
    record["args"]["n"].append(3)  # and so is a record
    record["args"]["m"]["k"] = 9
    assert proposed.intent_hash == digest and proposed.record() == before
    assert proposed.intent.plain_args == before["args"]
    assert proposed.intent.digest == digest == proposed.intent_hash
    assert intent_digest(proposed.intent.canonical) == digest


def test_the_repr_does_not_show_the_arguments() -> None:
    assert SECRET not in repr(action(args={"token": SECRET}))


@pytest.mark.parametrize("dialect", DIALECTS)
def test_the_intent_of_an_action_is_exactly_what_is_stored(
    dialect: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposed = action(args={"b": [1, {"y": 2, "x": 1}], "a": "é", "t": True}).bound_to(
        "irreversible_write"
    )
    with _database(dialect, tmp_path, monkeypatch) as (_, factory):
        _seed_run(factory)
        with factory() as session:
            tampered = proposed.intent
            tampered.plain_args["a"] = "changed"  # a throwaway Intent: the action is unaffected
            recorded = record_proposal(
                session,
                workflow_run_id="RUN-ep",
                node_id="request_document",
                occurrence_id="occ-0001",
                intent=proposed.intent,
            )
            session.commit()
        with factory() as session:
            stored = read_proposal(session, recorded.proposal_id)
        assert recorded.intent_hash == proposed.intent_hash == stored["intent_hash"]
        record = proposed.record()
        for name in (
            "effect_class",
            "target",
            "connector_version",
            "args",
            "expected_resource_version",
        ):
            assert stored[name] == record[name], name
        assert stored["args"]["a"] == "é" and stored["args"]["t"] is True
        validate_record("proposal", stored)


# ---------------------------------------------------------------- the model's claims are not the platform's


def test_a_matching_claim_is_bound_and_marked() -> None:
    proposed = action()
    assert proposed.platform_bound is False
    for effect_class in EFFECT_CLASSES:
        claimed = action(effect_class=effect_class)
        bound = claimed.bound_to(effect_class)
        assert bound.platform_bound is True and claimed.platform_bound is False
        assert bound == claimed and bound.intent_hash == claimed.intent_hash
        assert bound.effect_class == effect_class
        assert bound.args is not claimed.args  # a copy, not a shared view
    assert action().bound_to("irreversible_write", connector_version="1.0.0").platform_bound


def test_a_model_that_calls_an_irreversible_write_a_read_is_refused() -> None:
    claimed = action(effect_class="read")
    with pytest.raises(ClaimMismatch) as refusal:
        claimed.bound_to("irreversible_write")
    assert isinstance(refusal.value, ProposalError)
    assert (
        refusal.value.code == CLAIM_MISMATCH == "claim_mismatch"
        and refusal.value.name == "effect_class"
    )
    assert str(refusal.value) == (
        "the step claimed effect_class 'read'; the platform's is 'irreversible_write'"
    )


@pytest.mark.parametrize("inventory", [c for c in EFFECT_CLASSES if c != "export"] + ["write", ""])
def test_any_class_other_than_the_claimed_one_is_a_mismatch(inventory: str) -> None:
    with pytest.raises(ClaimMismatch):
        action(effect_class="export").bound_to(inventory)


def test_a_connector_version_other_than_the_platforms_is_a_mismatch_and_not_echoed() -> None:
    claimed = action(connector_version=SECRET[:20])
    with pytest.raises(ClaimMismatch) as refusal:
        claimed.bound_to("irreversible_write", connector_version="1.0.0")
    assert refusal.value.name == "connector_version"
    assert SECRET[:20] not in str(refusal.value) and "'1.0.0'" in str(refusal.value)


# ---------------------------------------------------------------- the request binds the result


def test_a_result_cannot_be_read_without_the_request_it_answers() -> None:
    with pytest.raises(TypeError):
        parse_step_result(fixture(RESULT_KIND))  # type: ignore[call-arg]


def test_a_result_must_answer_the_request_it_was_asked_for() -> None:
    request = the_request()
    assert parse_step_result(fixture(RESULT_KIND), request)
    for name, value in (
        ("workflow_run_id", "run-other"),
        ("node_id", "other"),
        ("occurrence_id", "occ-2"),
        ("attempt", 2),
    ):
        with pytest.raises(ContractValidationError) as raised:
            parse_step_result(result_record(**{name: value}), request)
        assert raised.value.kind == RESULT_KIND
        assert raised.value.errors == [
            f"{name}: differs from the request's, although step_request_id is the same"
        ]


def test_the_same_tuple_under_another_step_id_is_refused() -> None:
    with pytest.raises(ContractValidationError) as raised:
        parse_step_result(result_record(step_request_id="sr-other"), the_request())
    assert raised.value.errors == [
        "step_request_id: names another step than the request's, "
        "although run, node, occurrence and attempt are the same"
    ]


def test_another_tuple_and_another_step_id_are_each_reported() -> None:
    request = the_request()
    other = dataclasses.replace(
        parse_result(fixture(RESULT_KIND)),
        step_request_id="x",
        node_id="x",
        occurrence_id="y",
        attempt=9,
    )
    with pytest.raises(ContractValidationError) as raised:
        check_result_answers_request(request, other)
    assert raised.value.errors == [
        "step_request_id: does not match the request's",
        "node_id: does not match the request's",
        "occurrence_id: does not match the request's",
        "attempt: does not match the request's",
    ]


def test_a_matching_result_passes_the_check() -> None:
    check_result_answers_request(
        the_request(), parse_result(fixture(RESULT_KIND))
    )  # raises when not


def test_a_payload_is_not_changed_by_parsing() -> None:
    record = fixture(RESULT_KIND)
    before = copy.deepcopy(record)
    parse_result(record)
    assert record == before


# ---------------------------------------------------------------- intents the schema cannot judge


def deep(levels: int) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for _ in range(levels):
        value = {"k": value}
    return value


def test_args_nested_beyond_the_intent_depth_are_refused_with_the_action_named() -> None:
    assert parse_result(with_action(args=deep(MAX_ARGS_DEPTH - 1)))
    error = refused(RESULT_KIND, with_action(args=deep(MAX_ARGS_DEPTH + 1)))
    assert error.errors[0].startswith("proposed_actions/0: ")


def test_args_so_deep_that_python_recurses_are_a_typed_refusal() -> None:
    payload = with_action(args=deep(100_000))
    error = refused(RESULT_KIND, payload)
    assert error.errors[0].startswith("not plain JSON") and len(str(error)) < 400


def test_args_larger_than_the_intent_limit_are_refused() -> None:
    big = {"blob": "x" * MAX_INTENT_BYTES}
    assert "exceeds" in refused(RESULT_KIND, with_action(args=big)).errors[0]


def test_a_control_character_in_an_identifier_is_refused() -> None:
    error = refused(RESULT_KIND, with_action(target={"connector": "c", "resource": "a\x00b"}))
    assert error.errors == [
        "proposed_actions/0/target/resource: does not match the required pattern"
    ]
    refused(RESULT_KIND, with_action(connector_version="1\n2"))


def test_a_lone_surrogate_is_refused_not_stored() -> None:
    lone = "a" + chr(0xD800)
    refused(RESULT_KIND, with_action(target={"connector": "c", "resource": lone}))
    # in args the schema has no rule; the intent refuses what no store could hold
    error = refused(RESULT_KIND, with_action(args={"k": lone}))
    assert not error.errors[0].startswith("not plain JSON") or lone not in str(error)


def test_the_intent_refuses_what_the_pattern_does_not_see() -> None:
    # args are free-form to the schema, so the intent is the check that names the action
    nested = {"a": [{"b": float("inf")}]}
    refused(RESULT_KIND, with_action(args=nested))
    with pytest.raises(ProposalError):
        ProposedAction("c", "1", "export", "r", {"a": (1,)})  # a tuple is not plain JSON
    with pytest.raises(ProposalError):
        ProposedAction("c", "1", "destroy", "r", {})


def test_the_failing_action_is_named_by_its_index() -> None:
    with pytest.raises(ContractValidationError) as raised:
        parse_result(with_action(args=deep(MAX_ARGS_DEPTH + 5)))
    assert raised.value.errors[0].startswith("proposed_actions/0: ")


def test_args_that_are_not_plain_json_are_refused() -> None:
    for bad in ({"n": float("nan")}, {"n": float("inf")}, {"n": {1, 2}}, {"n": b"x"}):
        refused(RESULT_KIND, with_action(args=bad))


def test_the_writing_side_refuses_what_the_reading_side_refuses() -> None:
    usage = StepUsage(1, 1, 2, 1, 0)
    ids: dict[str, Any] = {
        "step_request_id": "sr",
        "workflow_run_id": "run",
        "node_id": "n",
        "occurrence_id": "o",
        "attempt": 1,
    }
    with pytest.raises(
        ProposalError
    ):  # an action without an intent cannot exist, so no result can carry it
        ProposedAction("c", "1", "export", "a\x00b", {})
    nothing = AgentStepResult(**ids, stop_reason="proposed_action", usage=usage)
    with pytest.raises(ContractValidationError, match="minItems"):
        nothing.record()
    ok = AgentStepResult(**ids, stop_reason="completed", usage=StepUsage(1, 1, 2, 1, 0, 1))
    assert ok.record()["usage"]["cached_prompt_tokens"] == 1
    request = AgentStepRequest(
        **ids,
        run_manifest_digest="A" * 64,
        context_manifest_digest="b" * 64,
        grant_id="g",
        limits=StepLimits(1, 1, 1, "2026-10-26T09:10:00Z"),
    )
    with pytest.raises(ContractValidationError, match="run_manifest_digest"):
        request.record()
    with_codes = AgentStepResult(
        **ids,
        stop_reason="error",
        usage=usage,
        usage_source="estimated",
        error_code="provider_timeout",
    )
    assert with_codes.record()["error_code"] == "provider_timeout"


# ---------------------------------------------------------------- refusals carry no value from the record


def quiet(error: ContractValidationError, *values: str, limit: int = 2500) -> None:
    text = str(error) + " ".join(error.errors)
    for value in values:
        assert value not in text, value
    assert len(str(error)) < limit and all(len(e) <= 200 for e in error.errors)


def test_an_argument_that_is_not_an_object_is_not_echoed() -> None:
    error = refused(RESULT_KIND, with_action(args=SECRET))
    assert error.errors == ["proposed_actions/0/args: must be of type object"]
    quiet(error, SECRET)


def test_seventeen_actions_are_refused_without_quoting_any_of_them() -> None:
    many = [action_record(args={"token": SECRET}) for _ in range(17)]
    error = refused(RESULT_KIND, result_record(proposed_actions=many))
    assert error.errors == ["proposed_actions: violates maxItems 1"]
    quiet(error, SECRET)


def test_a_huge_identifier_is_refused_by_size_before_it_is_validated() -> None:
    error = refused(RESULT_KIND, result_record(node_id="x" * 2_000_000))
    assert error.errors == [
        f"the record is larger than {registry.MAX_QUIET_RECORD_CHARS} characters of JSON"
    ]
    quiet(error, "xxxxxxxx", limit=300)
    request_error = refused(REQUEST_KIND, request_record(grant_id="g" * 2_000_000))
    assert "larger than" in request_error.errors[0]
    quiet(request_error, "gggggggg", limit=300)


def test_an_identifier_under_the_size_cap_but_over_its_own_is_refused_without_echo() -> None:
    error = refused(RESULT_KIND, result_record(node_id="x" * 500_000))
    assert error.errors == ["node_id: violates maxLength 128"]
    quiet(error, "xxxxxxxx", limit=300)


def test_the_size_cap_is_exactly_the_documented_one() -> None:
    assert registry.MAX_QUIET_RECORD_CHARS == 1 << 20
    pad = registry.MAX_QUIET_RECORD_CHARS - len(
        json.dumps(with_action(args={"p": ""}), ensure_ascii=False)
    )
    # a record just over the cap is refused by size, just under it reaches the schema (and the intent limit)
    over = refused(RESULT_KIND, with_action(args={"p": "x" * (pad + 1)}))
    assert "larger than" in over.errors[0]
    under = refused(RESULT_KIND, with_action(args={"p": "x" * pad}))
    assert "larger than" not in under.errors[0] and "exceeds" in under.errors[0]


def test_an_unknown_key_with_a_secret_looking_name_is_not_named() -> None:
    for broken in (
        result_record(**{SECRET: 1}),
        with_action(**{SECRET: 1}),
        with_action(target={"connector": "c", "resource": "r", SECRET: 1}),
    ):
        quiet(refused(RESULT_KIND, broken), SECRET)
    quiet(refused(REQUEST_KIND, request_record(**{SECRET: 1})), SECRET)


def test_an_enormous_integer_is_not_echoed() -> None:
    error = refused(REQUEST_KIND, request_record(attempt=10**400))
    assert error.errors == [f"attempt: violates maximum {MAX_COUNT}"]
    quiet(error, "0" * 40)
    usage = {**fixture(RESULT_KIND)["usage"], "total_tokens": 10**400}
    quiet(refused(RESULT_KIND, result_record(usage=usage)), "0" * 40)


def test_a_secret_looking_value_in_a_format_or_pattern_check_is_not_echoed() -> None:
    limits = {**fixture(REQUEST_KIND)["limits"], "deadline": SECRET}
    error = refused(REQUEST_KIND, request_record(limits=limits))
    assert error.errors[0].startswith("limits/deadline: ")
    quiet(error, SECRET)
    quiet(refused(REQUEST_KIND, request_record(grant_id=SECRET + "\n")), SECRET)
    quiet(refused(REQUEST_KIND, request_record(run_manifest_digest=SECRET)), SECRET)


def test_a_value_that_is_not_plain_json_is_named_by_type_only() -> None:
    error = refused(RESULT_KIND, with_action(args={"n": {SECRET}}))
    assert error.errors == ["not plain JSON: Object of type set is not JSON serializable"]
    quiet(error, SECRET)


def test_the_text_of_a_failure_inside_the_record_is_bounded() -> None:
    class Hostile(Mapping[str, Any]):
        """A mapping whose iteration fails with a long message, as a hostile or broken object can."""

        def __getitem__(self, key: str) -> Any:
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            raise ValueError(SECRET * 100)

        def __len__(self) -> int:
            return 1

    error = refused(RESULT_KIND, with_action(args=Hostile()))
    assert error.errors[0].startswith("not plain JSON: ") and len(error.errors[0]) <= 200


def test_the_number_of_messages_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_MAX_QUIET_ERRORS", 2)
    broken = request_record(
        attempt=0,
        node_id="",
        grant_id="",
        run_manifest_digest="x",
        context_manifest_digest="y",
    )
    error = refused(REQUEST_KIND, broken)
    assert len(error.errors) == 3 and error.errors[-1] == "and 5 more violations"


def test_other_kinds_keep_quoting_the_value_the_agent_step_kinds_do_not() -> None:
    proposal = json.loads((FIXTURES / "proposal" / "valid_1.json").read_text())
    with pytest.raises(ContractValidationError) as loud:
        validate_record("proposal", {**proposal, "effect_class": SECRET})
    assert SECRET in str(loud.value)
    with pytest.raises(ContractValidationError) as silent:
        validate_record(RESULT_KIND, with_action(effect_class=SECRET))
    assert SECRET not in str(silent.value)


# ---------------------------------------------------------------- every executor takes part in governed execution (B08.1b, #720)


def test_every_executor_implements_run_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ``WorkflowExecutor`` in the runtime is an ``AgentStepAdapter``; the scripted fake and the Anthropic executor run a step so far.

    The provider executors not yet integrated answer with a ``refused`` result (``adapter_not_integrated``) and touch no provider: their
    clients are tripwires, and the Agents SDK is never imported. The discovery makes the test fail for an executor that is
    added later without ``run_step``, so it cannot be left out of governed execution by accident.
    """
    executors = [
        cls
        for _, cls in inspect.getmembers(runtime, inspect.isclass)
        if cls.__module__ == runtime.__name__
        and cls is not runtime.WorkflowExecutor
        and callable(getattr(cls, "run_agent", None))
    ]
    assert {cls.__name__ for cls in executors} == {
        "FakeWorkflowExecutor",
        "OpenAIChatWorkflowExecutor",
        "OpenAIResponsesWorkflowExecutor",
        "OpenAIAgentsWorkflowExecutor",
        "AnthropicChatWorkflowExecutor",
    }
    for cls in executors:
        assert callable(getattr(cls, "run_step", None)), f"{cls.__name__} has no run_step"

    def no_sdk() -> None:
        raise AssertionError("the Agents SDK was imported")

    monkeypatch.setattr(runtime.OpenAIAgentsWorkflowExecutor, "_sdk", staticmethod(no_sdk))
    built = every_executor()
    assert tuple(built) == EXECUTOR_NAMES
    for name, executor in built.items():
        # An integrated executor reads the clock against the deadline, so the deadline is one that no date reaches.
        ctx = make_context(
            limits={
                "max_turns": 8,
                "max_tool_calls": 4,
                "max_tokens": 20000,
                "deadline": "2999-01-01T00:00:00Z",
            }
        )
        assert isinstance(executor, AgentStepAdapter), name
        result = run_agent_step(executor, ctx.request, ctx.step_input)
        assert result.workflow_run_id == ctx.request.workflow_run_id
        assert result.proposed_actions == ()
        if name == "fake":
            assert result.stop_reason == "completed", name
        elif name == "anthropic":
            # Integrated (B08.anthropic, #727): it runs the step (here the harness deadline is long past, so it stops).
            assert result.error_code != ADAPTER_NOT_INTEGRATED, name
        elif name == "openai_chat":
            # Integrated (#729): it runs the step, and the tripwire client's failure is a typed provider error.
            assert (result.stop_reason, result.error_code) == ("error", "provider_error"), name
        else:
            assert (result.stop_reason, result.error_code) == ("refused", ADAPTER_NOT_INTEGRATED), (
                name
            )
