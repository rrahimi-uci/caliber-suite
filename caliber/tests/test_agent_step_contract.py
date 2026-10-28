"""The AgentStepRequest / AgentStepResult v1 contracts (B08.1a, #713).

The contracts are strict on purpose: they are the gate in front of every agent step, so an extra field, an unknown
version, an invalid effect class, a missing target or a claim that an effect was committed is refused before anything
is dispatched. The proposed-action shape is the one the effect proposal (B05) already stores, and these tests pin that
the two cannot drift apart.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from caliber.contracts import (
    ContractValidationError,
    UnsupportedSchemaVersion,
    load_schema,
    validate_record,
)
from caliber.contracts.agent_step import (
    MAX_PROPOSED_ACTIONS,
    REQUEST_KIND,
    RESULT_KIND,
    STOP_REASONS,
    AgentStepRequest,
    AgentStepResult,
    ProposedAction,
    StepLimits,
    StepUsage,
    check_result_answers_request,
    parse_step_request,
    parse_step_result,
)
from caliber.effects.inventory import EFFECT_CLASSES
from caliber.effects.records import MAX_ARGS_DEPTH, MAX_INTENT_BYTES, Intent, ProposalError

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "contracts"
MAX_COUNT = 2**31 - 1


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


def refused(kind: str, payload: dict[str, Any]) -> ContractValidationError:
    parse = parse_step_request if kind == REQUEST_KIND else parse_step_result
    with pytest.raises(ContractValidationError) as raised:
        parse(payload)
    assert raised.value.kind == kind and raised.value.errors
    return raised.value


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
    result = parse_step_result(record)
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
    result = parse_step_result(record)
    assert result.proposed_actions == () and result.usage.cached_prompt_tokens == 400
    assert result.record() == record


def test_limits_of_zero_and_the_largest_count_are_valid() -> None:
    zero = {"max_turns": 0, "max_tool_calls": 0, "max_tokens": 0}
    limits = {**fixture(REQUEST_KIND)["limits"], **zero}
    assert parse_step_request(request_record(limits=limits)).limits.max_tokens == 0
    limits = {**limits, "max_tokens": MAX_COUNT, "max_turns": MAX_COUNT}
    assert parse_step_request(request_record(limits=limits)).limits.max_turns == MAX_COUNT


def test_an_integral_float_is_read_as_the_integer() -> None:
    request = parse_step_request(request_record(attempt=2.0))
    assert request.attempt == 2 and type(request.attempt) is int
    usage = {**fixture(RESULT_KIND)["usage"], "turns": 3.0, "cached_prompt_tokens": 7.0}
    result = parse_step_result(result_record(usage=usage))
    assert type(result.usage.turns) is int and result.usage.cached_prompt_tokens == 7


def test_every_stop_reason_without_proposals_is_valid() -> None:
    for reason in STOP_REASONS:
        if reason == "proposed_action":
            continue
        record = result_record(stop_reason=reason, proposed_actions=[])
        assert parse_step_result(record).stop_reason == reason


# ---------------------------------------------------------------- what the gate refuses


@pytest.mark.parametrize("kind", [REQUEST_KIND, RESULT_KIND])
@pytest.mark.parametrize("version", ["v2", "V1", "", "1", None, 1, ["v1"]])
def test_unsupported_version(kind: str, version: object) -> None:
    record = {**fixture(kind), "schema_version": version}
    parse = parse_step_request if kind == REQUEST_KIND else parse_step_result
    with pytest.raises(UnsupportedSchemaVersion) as raised:
        parse(record)
    assert raised.value.kind == kind and raised.value.supported == ("v1",)
    assert str(raised.value).startswith(f"{kind} schema_version {version} is not supported")


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
        parse_step_result(payload)


def test_an_extra_field_is_refused_at_every_level() -> None:
    surprise = {"surprise": 1}
    request = fixture(REQUEST_KIND)
    for broken in (
        {**request, **surprise},
        {**request, "limits": {**request["limits"], **surprise}},
    ):
        assert "surprise" in refused(REQUEST_KIND, broken).errors[0]
    result = fixture(RESULT_KIND)
    action = result["proposed_actions"][0]
    for broken in (
        {**result, **surprise},
        {**result, "usage": {**result["usage"], **surprise}},
        {**result, "proposed_actions": [{**action, **surprise}]},
        {**result, "proposed_actions": [{**action, "target": {**action["target"], **surprise}}]},
    ):
        assert "surprise" in refused(RESULT_KIND, broken).errors[0]


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
    action = base["proposed_actions"][0]
    claims = [
        {**base, "state": "committed"},
        {**base, "committed": True},
        {**base, "receipt_id": "rc-1"},
        {**base, "receipt": {"receipt_id": "rc-1"}},
        {**base, "approval_id": "ap-1"},
        {**base, "stop_reason": "committed"},
        {**base, "proposed_actions": [{**action, "state": "committed"}]},
        {**base, "proposed_actions": [{**action, "receipt_id": "rc-1"}]},
        {**base, "proposed_actions": [{**action, "proposal_id": "pr-1", "state": "approved"}]},
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
    "effect_class", ["write", "destroy", "READ", "Read", "", None, 1, ["read"], "read "]
)
def test_an_invalid_effect_class_is_refused(effect_class: object) -> None:
    assert "effect_class" in refused(RESULT_KIND, with_action(effect_class=effect_class)).errors[0]


@pytest.mark.parametrize("effect_class", EFFECT_CLASSES)
def test_every_effect_class_of_the_reference_profile_is_accepted(effect_class: str) -> None:
    assert parse_step_result(with_action(effect_class=effect_class)).proposed_actions


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
    assert parse_step_result(with_action(expected_resource_version=None)).proposed_actions


# ---------------------------------------------------------------- size and format limits


@pytest.mark.parametrize("name", ["step_request_id", "node_id", "occurrence_id"])
def test_a_step_identifier_is_one_to_128_characters(name: str) -> None:
    assert parse_step_request(request_record(**{name: "x" * 128}))
    assert parse_step_result(result_record(**{name: "x" * 128}))
    for bad in ("", "x" * 129, None, 5):
        refused(REQUEST_KIND, request_record(**{name: bad}))
        refused(RESULT_KIND, result_record(**{name: bad}))


def test_the_run_id_and_the_grant_id_have_the_lengths_the_manifest_gives_them() -> None:
    assert parse_step_request(request_record(workflow_run_id="x" * 64, grant_id="g" * 64))
    refused(REQUEST_KIND, request_record(workflow_run_id="x" * 65))
    refused(REQUEST_KIND, request_record(grant_id="g" * 65))
    refused(REQUEST_KIND, request_record(grant_id=""))
    refused(RESULT_KIND, result_record(workflow_run_id="x" * 65))


@pytest.mark.parametrize("attempt", [0, -1, True, 1.5, "1", None, MAX_COUNT + 1])
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


@pytest.mark.parametrize(
    "deadline",
    [
        "2026-13-45T00:00:00Z",
        "2026-02-30T00:00:00Z",
        "2026-10-26T09:05:00",
        "2026-10-26 09:05:00Z",
        "2026-10-26T09:05:00Z\n",
        "tomorrow",
        "",
        None,
        1_800_000_000,
    ],
)
def test_a_deadline_is_a_real_rfc_3339_instant_with_an_offset(deadline: object) -> None:
    limits = {**fixture(REQUEST_KIND)["limits"], "deadline": deadline}
    refused(REQUEST_KIND, request_record(limits=limits))


@pytest.mark.parametrize("deadline", ["2026-10-26T09:05:00-08:00", "2028-02-29T23:59:59.5+05:30"])
def test_a_deadline_with_an_offset_is_valid(deadline: str) -> None:
    limits = {**fixture(REQUEST_KIND)["limits"], "deadline": deadline}
    assert parse_step_request(request_record(limits=limits)).limits.deadline == deadline


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


def test_at_most_sixteen_proposals_are_allowed() -> None:
    assert (
        load_schema(RESULT_KIND, "v1")["properties"]["proposed_actions"]["maxItems"]
        == MAX_PROPOSED_ACTIONS
    )
    many = [action_record(args={"n": n}) for n in range(MAX_PROPOSED_ACTIONS)]
    assert len(parse_step_result(result_record(proposed_actions=many)).proposed_actions) == 16
    refused(RESULT_KIND, result_record(proposed_actions=[*many, action_record()]))


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
        action = action_record()
        node = action
        for step in path[:-1]:
            node = node[step]
        node[path[-1]] = value
        return result_record(proposed_actions=[action])

    assert parse_step_result(set_at("x" * limit))
    error = refused(RESULT_KIND, set_at("x" * (limit + 1)))
    # the schema says so itself, ahead of (and not only through) the intent's own check
    assert not error.errors[0].startswith("proposed_actions/0: ")


def test_the_schema_bounds_are_the_ones_the_intent_enforces() -> None:
    """Whatever the schema lets through at its limits the Intent accepts, so the two checks never disagree."""
    action = action_record(
        target={"connector": "c" * 128, "resource": "r" * 512},
        connector_version="v" * 64,
        expected_resource_version="e" * 256,
    )
    assert ProposedAction.from_record(action).intent.digest.startswith("sha256:")


# ---------------------------------------------------------------- stop reason and proposals agree


@pytest.mark.parametrize("reason", [r for r in STOP_REASONS if r != "proposed_action"])
def test_proposals_with_any_other_stop_reason_are_refused(reason: str) -> None:
    refused(RESULT_KIND, result_record(stop_reason=reason))


def test_the_proposed_action_stop_reason_needs_a_proposal() -> None:
    refused(RESULT_KIND, result_record(stop_reason="proposed_action", proposed_actions=[]))


def test_the_stop_reasons_are_exactly_the_documented_ones() -> None:
    schema = load_schema(RESULT_KIND, "v1")
    assert tuple(schema["properties"]["stop_reason"]["enum"]) == STOP_REASONS
    for bad in ("committed", "complete", "", None, "Completed", "end_turn"):
        refused(RESULT_KIND, result_record(stop_reason=bad, proposed_actions=[]))


# ---------------------------------------------------------------- one shape with the proposal (B05)


def test_a_proposed_action_has_the_shape_of_the_proposal_it_becomes() -> None:
    proposal = load_schema("proposal", "v1")["properties"]
    action = load_schema(RESULT_KIND, "v1")["properties"]["proposed_actions"]["items"]["properties"]

    def without_bounds(schema: dict[str, Any]) -> dict[str, Any]:
        """Drop ``maxLength`` (the step contract bounds what the proposal schema leaves to the Intent)."""
        return {
            k: without_bounds(v) if isinstance(v, dict) else v
            for k, v in schema.items()
            if k != "maxLength"
        }

    assert set(action) == {
        "effect_class",
        "target",
        "connector_version",
        "args",
        "expected_resource_version",
    }
    for name in action:
        assert without_bounds(action[name]) == without_bounds(proposal[name]), name
    assert action["effect_class"]["enum"] == list(EFFECT_CLASSES) or set(
        action["effect_class"]["enum"]
    ) == set(EFFECT_CLASSES)
    required_in_proposal = set(load_schema("proposal", "v1")["required"])
    assert {"effect_class", "target", "args", "expected_resource_version"} <= required_in_proposal


def test_a_proposed_action_drops_into_a_valid_proposal_record() -> None:
    action = parse_step_result(fixture(RESULT_KIND)).proposed_actions[0]
    record = action.record()
    proposal = {
        "schema_version": "v1",
        "proposal_id": "pr-1",
        "workflow_run_id": "run-0001",
        "node_id": "request_document",
        "logical_key": "k",
        "intent_hash": action.intent_hash,
        "state": "proposed",
        "tenant_id": "local",
        "region": "local",
        **record,
    }
    validate_record("proposal", proposal)  # raises when the proposal record is not valid


def test_the_intent_hash_is_the_digest_of_the_shared_intent() -> None:
    action = parse_step_result(fixture(RESULT_KIND)).proposed_actions[0]
    intent = Intent(
        connector="document-request",
        connector_version="1.0.0",
        effect_class="irreversible_write",
        resource="cases/MORT-2026-000184/document-requests",
        args={"document_type": "pay_statement"},
        expected_resource_version="1",
    )
    assert action.intent == intent and action.intent_hash == intent.digest
    # every field of the intent changes the digest: a different connector version is a different approval
    other = replace(action, connector_version="1.0.1")
    assert other.intent_hash != action.intent_hash
    other = replace(action, effect_class="export")
    assert other.intent_hash != action.intent_hash


def test_key_order_in_args_does_not_change_the_digest() -> None:
    a = ProposedAction.from_record(action_record(args={"a": 1, "b": {"c": 2, "d": 3}}))
    b = ProposedAction.from_record(action_record(args={"b": {"d": 3, "c": 2}, "a": 1}))
    assert a.intent_hash == b.intent_hash
    assert list(a.record()["args"]) == ["a", "b"]


def test_a_later_change_to_the_args_passed_in_or_read_out_does_not_change_the_action() -> None:
    args: dict[str, Any] = {"n": [1]}
    action = ProposedAction.from_record(action_record(args=args))
    before, digest = copy.deepcopy(action.record()), action.intent_hash
    args["n"].append(2)  # the caller's own mapping
    assert action.args == {"n": [1]} and action.record() == before
    read_out = action.record()
    read_out["args"]["n"].append(3)  # a record handed out is a copy
    assert action.record() == before and action.intent_hash == digest


# ---------------------------------------------------------------- intents the schema cannot judge


def deep(levels: int) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for _ in range(levels):
        value = {"k": value}
    return value


def test_args_nested_beyond_the_intent_depth_are_refused_with_the_action_named() -> None:
    assert parse_step_result(with_action(args=deep(MAX_ARGS_DEPTH - 1)))
    error = refused(RESULT_KIND, with_action(args=deep(MAX_ARGS_DEPTH + 1)))
    assert error.errors[0].startswith("proposed_actions/0: ")


def test_args_so_deep_that_python_recurses_are_a_typed_refusal() -> None:
    payload = with_action(args=deep(100_000))
    assert "not plain JSON" in refused(RESULT_KIND, payload).errors[0]


def test_args_larger_than_the_intent_limit_are_refused() -> None:
    big = {"blob": "x" * MAX_INTENT_BYTES}
    assert "exceeds" in refused(RESULT_KIND, with_action(args=big)).errors[0]


def test_a_control_character_in_an_identifier_is_refused_by_the_intent() -> None:
    error = refused(RESULT_KIND, with_action(target={"connector": "c", "resource": "a\x00b"}))
    assert error.errors[0].startswith("proposed_actions/0: ")
    refused(RESULT_KIND, with_action(connector_version="1\n2"))


def test_a_lone_surrogate_is_refused_not_stored() -> None:
    lone = "a" + chr(0xD800)
    refused(RESULT_KIND, with_action(target={"connector": "c", "resource": lone}))
    refused(RESULT_KIND, with_action(args={"k": lone}))


def test_the_failing_action_is_named_by_its_index() -> None:
    record = result_record(
        proposed_actions=[
            action_record(),
            action_record(target={"connector": "c", "resource": "\x07"}),
        ]
    )
    assert refused(RESULT_KIND, record).errors[0].startswith("proposed_actions/1: ")


def test_an_intent_error_does_not_echo_the_arguments() -> None:
    secret = "do-not-echo-this-value"
    error = refused(
        RESULT_KIND, with_action(args={"nested": deep(MAX_ARGS_DEPTH + 5), "v": secret})
    )
    assert secret not in str(error)


def test_the_stop_reason_rule_does_not_echo_the_arguments_either() -> None:
    secret = "do-not-echo-this-value"
    error = refused(RESULT_KIND, with_action(args={"v": secret}) | {"stop_reason": "completed"})
    assert error.errors == ["proposed_actions: [] was expected"]
    assert secret not in str(error)


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
    with pytest.raises(ProposalError):
        ProposedAction("c", "1", "destroy", "r", {})
    nothing = AgentStepResult(**ids, stop_reason="proposed_action", usage=usage)
    with pytest.raises(ContractValidationError, match="non-empty|minItems|too short"):
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


# ---------------------------------------------------------------- a result answers its request


def test_a_result_must_answer_the_request_it_was_asked_for() -> None:
    request = parse_step_request(fixture(REQUEST_KIND))
    assert parse_step_result(fixture(RESULT_KIND), request=request)
    for name, value in (
        ("step_request_id", "sr-other"),
        ("workflow_run_id", "run-other"),
        ("node_id", "other"),
        ("occurrence_id", "occ-2"),
        ("attempt", 2),
    ):
        with pytest.raises(ContractValidationError) as raised:
            parse_step_result(result_record(**{name: value}), request=request)
        assert raised.value.kind == RESULT_KIND
        assert len(raised.value.errors) == 1 and raised.value.errors[0].startswith(f"{name}: ")


def test_every_mismatch_is_reported() -> None:
    request = parse_step_request(fixture(REQUEST_KIND))
    result = parse_step_result(result_record(node_id="x", attempt=9, occurrence_id="y"))
    with pytest.raises(ContractValidationError) as raised:
        check_result_answers_request(request, result)
    assert [e.split(":")[0] for e in raised.value.errors] == ["node_id", "occurrence_id", "attempt"]


def test_a_result_is_not_checked_against_a_request_when_none_is_given() -> None:
    assert parse_step_result(result_record(attempt=99)).attempt == 99


def test_a_payload_is_not_changed_by_parsing() -> None:
    record = fixture(RESULT_KIND)
    before = copy.deepcopy(record)
    parse_step_result(record)
    assert record == before
