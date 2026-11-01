"""#1055: the generic ``http`` connector's read-by-key contract, as pure functions (no database, no network).

What is pinned here: which endpoints an operator may list and how they are read, the exact request a lookup is built as, and, above
all, what the platform is willing to believe about a downstream's answer. A receipt is built only for a stored request that is
*this proposal's effect* (its key, method, URL and body, compared as canonical JSON); every other answer is ``None``, which leaves
the proposal ``unknown``. The reconciler, the dispatch-time verifier and the scenario tests build on these.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from typing import Any

import pytest

from caliber.config import CaliberConfig
from caliber.effects.connectors import classify_status, interpret_lookup
from caliber.effects.connectors import http_lookup as http
from caliber.effects.http_verifiers import (
    MAX_LOOKUP_URL_CHARS,
    MAX_RESOURCE_CHARS,
    VerifierEndpoint,
    endpoints_for_project,
    parse_endpoints,
    target_key,
    targets_from_endpoints,
)
from caliber.effects.receipts import ReceiptFacts
from caliber.effects.records import ProposalView

URL = "https://hooks.example.test/notify"
LOOKUP = "https://hooks.example.test/lookups/by-key"
KEY = "k" * 64
BODY = {"case": "MORT-1", "amount": 12, "tags": ["a", "b"]}


def _row(**changes: Any) -> dict[str, Any]:
    row = {"project_id": "PRJ-1", "resource": URL, "lookup_url": LOOKUP}
    row.update(changes)
    return row


def _raw(*rows: dict[str, Any]) -> str:
    return json.dumps(list(rows))


def _view(**changes: Any) -> ProposalView:
    fields: dict[str, Any] = {
        "proposal_id": "p-1",
        "logical_key": KEY,
        "connector": "http",
        "connector_version": "1",
        "effect_class": "irreversible_write",
        "resource": URL,
        "expected_resource_version": None,
        "intent_hash": "sha256:" + "c" * 64,
        "attempt": 1,
        "state": "unknown",
        "args": {"url": URL, "method": "POST", "headers": {}, "body": BODY},
    }
    fields.update(changes)
    return ProposalView(**fields)


def _stored(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "idempotency_key": KEY,
        "request_id": "REQ-7",
        "method": "POST",
        "url": URL,
        "body": BODY,
    }
    body.update(changes)
    return body


def _facts(view: ProposalView | None = None, **changes: Any) -> ReceiptFacts | None:
    body = _stored(**changes)
    return http.facts_from_lookup(view or _view(), lookup_status=200, lookup_body=body)


# --------------------------------------------------------------------------------------- the operator's list


def test_a_listed_endpoint_is_read_exactly_and_keyed_by_project_and_resource() -> None:
    other = _row(project_id="PRJ-2", lookup_url="https://hooks.example.test/other")
    endpoints = parse_endpoints(_raw(_row(), other))
    assert endpoints == (
        VerifierEndpoint("PRJ-1", URL, LOOKUP),
        VerifierEndpoint("PRJ-2", URL, "https://hooks.example.test/other"),
    )
    assert endpoints_for_project(endpoints, "PRJ-1") == {URL: endpoints[0]}
    assert endpoints_for_project(endpoints, "PRJ-2") == {URL: endpoints[1]}
    assert endpoints_for_project(endpoints, "PRJ-3") == {}
    assert endpoints_for_project(endpoints, None) == {}
    assert targets_from_endpoints(endpoints) == {
        (f"http:{URL}", "PRJ-1"): LOOKUP,
        (f"http:{URL}", "PRJ-2"): "https://hooks.example.test/other",
    }
    assert target_key(URL) == f"http:{URL}"
    assert parse_endpoints("[]") == ()


def test_a_resource_may_carry_a_query_but_a_lookup_url_may_not() -> None:
    assert parse_endpoints(_raw(_row(resource=URL + "?tenant=a")))[0].resource == URL + "?tenant=a"
    with pytest.raises(ValueError, match="lookup_url"):
        parse_endpoints(_raw(_row(lookup_url=LOOKUP + "?x=1")))


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "{}",
        "null",
        '["x"]',
        "[{}]",
        _raw({"project_id": "P", "resource": URL}),
        _raw(_row(extra=1)),
        _raw(_row(project_id="")),
        _raw(_row(project_id="bad name")),
        _raw(_row(project_id=1)),
        _raw(_row(resource=1)),
        _raw(_row(resource="ftp://hooks.example.test/x")),
        _raw(_row(resource="https:///x")),
        _raw(_row(resource="https://user:pw@hooks.example.test/x")),
        _raw(_row(resource=URL + "#frag")),
        _raw(_row(resource="https://hooks.example.test/a b")),
        _raw(_row(resource="https://hooks.example.test/é")),
        _raw(_row(resource="https://hooks.example.test/" + "a" * MAX_RESOURCE_CHARS)),
        _raw(_row(lookup_url=None)),
        _raw(_row(lookup_url="file:///etc/passwd")),
        _raw(_row(lookup_url="https://user@hooks.example.test/x")),
        _raw(_row(lookup_url="https://hooks.example.test/{key}")),
        _raw(_row(lookup_url="https://hooks.example.test/x#frag")),
        _raw(_row(lookup_url="https://hooks.example.test/" + "a" * MAX_LOOKUP_URL_CHARS)),
        _raw(_row(), _row()),
        "[" + json.dumps(_row()) + "," + json.dumps(_row(lookup_url=LOOKUP + "2")) + "]",
        _raw(_row(resource="https://hooks.example.test:badport/x")),
    ],
)
def test_a_list_that_is_not_exactly_the_documented_shape_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_endpoints(raw)
    with pytest.raises(ValueError):
        CaliberConfig(effect_http_verifiers=raw)


def test_the_configuration_reads_the_list_from_its_environment_variable() -> None:
    raw = _raw(_row())
    config = CaliberConfig.load(environ={"CALIBER_EFFECT_HTTP_VERIFIERS": raw})
    assert parse_endpoints(config.effect_http_verifiers) == (
        VerifierEndpoint("PRJ-1", URL, LOOKUP),
    )
    assert CaliberConfig().effect_http_verifiers == "[]"


# ---------------------------------------------------------------------------------------- the request


def test_the_lookup_is_one_read_at_the_listed_url_with_the_key_as_the_only_query() -> None:
    config = http.lookup_config("https://hooks.example.test:8443/lookups/by-key")
    assert config == {
        "kind": "openapi_http",
        "method": "GET",
        "path": "/lookups/by-key",
        "server_url": "https://hooks.example.test:8443",
        "auth_binding": None,
        "max_attempts": 1,
        "timeout_seconds": 5.0,
    }
    assert http.lookup_config("http://hooks.example.test")["path"] == "/"
    assert http.lookup_input(KEY) == {"query_params": {"idempotency_key": KEY}}


@pytest.mark.parametrize("key", ["", " k", "k ", "a\nb", "é", "k" * 256, None, 1])
def test_a_key_that_is_not_printable_ascii_is_refused_before_a_request_is_built(key: Any) -> None:
    with pytest.raises(ValueError, match="idempotency key"):
        http.lookup_input(key)


# ------------------------------------------------------------------------------------- the capability record


def test_the_capability_promises_a_lookup_that_reads_its_writes_and_never_that_absence_is_final() -> (
    None
):
    capability = http.CAPABILITY
    assert (capability.connector, capability.version) == ("http", "1")
    assert capability.lookup_by_key and capability.lookup_consistency == "read_your_writes"
    assert not capability.absence_proves_noncommit
    assert not capability.carries_a_write_exactly_once, "the key's scope and retention stay unknown"
    assert interpret_lookup(capability, 200) == "answered"
    assert interpret_lookup(capability, 404) == "unknown", (
        "a 404 never certifies 'it did not happen'"
    )
    for status in (400, 401, 408, 429, 500, 503):
        assert interpret_lookup(capability, status) == "unknown"
    # a dispatch's answers are classified as before: a 4xx is never a documented refusal, a 2xx is only a claim
    assert classify_status(409, capability=capability).outcome.value == "ambiguous"
    assert classify_status(201, capability=capability).outcome.value == "committed_receipt"


# ------------------------------------------------------------------------------------ what is believed


def test_a_stored_request_that_is_this_effect_becomes_the_proposals_receipt() -> None:
    view = _view()
    facts = _facts(view)
    assert facts is not None
    assert facts.attempt == 1 and facts.idempotency_key == KEY
    assert (facts.connector, facts.connector_version, facts.resource) == ("http", "1", URL)
    assert facts.intent_hash == view.intent_hash, (
        "the receipt carries the proposal's own hash, not the downstream's"
    )
    assert facts.downstream_request_id == http.receipt_request_id(URL, "REQ-7")
    assert facts.resource_version is None
    assert facts.response_digest.startswith("sha256:") and len(facts.response_digest) == 71
    # a different stored body is a different digest; the same document in another key order is the same one
    assert _facts(view, request_id="REQ-8").response_digest != facts.response_digest  # type: ignore[union-attr]
    shuffled = dict(reversed(list(_stored().items())))
    again = http.facts_from_lookup(view, lookup_status=200, lookup_body=shuffled)
    assert again is not None and again.response_digest == facts.response_digest


def test_the_method_defaults_to_post_and_is_compared_without_case() -> None:
    no_method = _view(args={"url": URL, "body": BODY})
    assert _facts(no_method, method="post") is not None
    assert _facts(no_method, method="PUT") is None
    put = _view(args={"url": URL, "method": "put", "body": BODY})
    assert _facts(put, method="PUT") is not None
    assert _facts(put, method="POST") is None
    blank = _view(args={"url": URL, "method": "  ", "body": BODY})
    assert _facts(blank, method="POST") is None, "a blank method is not one the sender can send"


def test_a_request_with_no_body_matches_a_stored_null_or_missing_body_only() -> None:
    none = _view(args={"url": URL, "method": "POST"})
    assert _facts(none, body=None) is not None
    stored = _stored()
    del stored["body"]
    assert http.facts_from_lookup(none, lookup_status=200, lookup_body=stored) is not None
    assert _facts(none, body={}) is None
    assert _facts(none, body="") is None


def test_a_text_body_is_compared_as_text() -> None:
    text_body = _view(args={"url": URL, "method": "POST", "body": "hello"})
    assert _facts(text_body, body="hello") is not None
    assert _facts(text_body, body="hello ") is None
    assert _facts(text_body, body=["hello"]) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"idempotency_key": "other"},
        {"idempotency_key": None},
        {"request_id": ""},
        {"request_id": "   "},
        {"request_id": 7},
        {"request_id": None},
        {"request_id": "x" * 257},
        {"request_id": "a\nb"},
        {"url": URL + "/other"},
        {"url": None},
        {"method": "DELETE"},
        {"method": None},
        {"method": 5},
        {"body": {**BODY, "amount": 13}},
        {"body": {**BODY, "extra": True}},
        {"body": {"case": "MORT-1", "amount": 12}},
        {"body": [BODY]},
        {"body": None},
        {"body": {**BODY, "amount": float("nan")}},
    ],
)
def test_a_stored_request_that_differs_in_anything_that_identifies_the_effect_is_not_a_receipt(
    changes: dict[str, Any],
) -> None:
    assert _facts(**changes) is None


def test_a_body_that_is_not_plain_json_is_not_a_receipt() -> None:
    view = _view(args={"url": URL, "method": "POST", "body": {1, 2}})  # type: ignore[arg-type]
    assert _facts(view, body={1, 2}) is None
    nan = _view(args={"url": URL, "method": "POST", "body": {"x": math.inf}})
    assert _facts(nan, body={"x": math.inf}) is None


@pytest.mark.parametrize("status", [201, 204, 202, 404, 408, 500, 503, 199, 302])
def test_only_a_200_answer_can_be_a_receipt(status: int) -> None:
    assert http.facts_from_lookup(_view(), lookup_status=status, lookup_body=_stored()) is None


@pytest.mark.parametrize("body", [None, [], [_stored()], "text", 7, {}, {"detail": "not found"}])
def test_an_answer_that_is_not_a_stored_request_is_not_a_receipt(body: Any) -> None:
    assert http.facts_from_lookup(_view(), lookup_status=200, lookup_body=body) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"attempt": None},
        {"connector": "document-request"},
        {"connector_version": "2"},
        {"expected_resource_version": "3"},
        {"args": None},
        {"args": {"url": "https://hooks.example.test/elsewhere", "method": "POST", "body": BODY}},
    ],
)
def test_a_proposal_this_connector_cannot_verify_is_never_verified(changes: dict[str, Any]) -> None:
    assert _facts(_view(**changes)) is None


def test_a_proposal_view_that_was_built_without_args_cannot_be_verified() -> None:
    bare = replace(_view(), args=None)
    assert _facts(bare) is None


# ------------------------------------------------------------------ request ids, numbers, timeouts (review of #1092)


def test_a_receipts_request_id_is_namespaced_by_the_effects_url() -> None:
    """The database keeps one receipt per (connector, request id) and every http endpoint shares the connector: two endpoints
    that both issue ``1`` must not collide."""
    other = "https://hooks.example.test/other"
    mine = _facts(_view(), request_id="1")
    theirs = _facts(
        _view(resource=other, args={"url": other, "method": "POST", "body": BODY}),
        request_id="1",
        url=other,
    )
    assert mine is not None and theirs is not None
    assert mine.downstream_request_id != theirs.downstream_request_id
    assert mine.downstream_request_id == http.receipt_request_id(URL, "1")
    assert mine.downstream_request_id == http.receipt_request_id(URL, "1"), (
        "stable: a pure function of the URL and the id"
    )
    assert (
        http.receipt_request_id(URL, "1").endswith(":1")
        and len(http.receipt_request_id(URL, "1")) == 18
    )


def test_the_longest_request_id_fits_the_receipt_and_one_more_character_does_not() -> None:
    longest = "x" * http.MAX_REQUEST_ID_CHARS
    facts = _facts(request_id=longest)
    assert facts is not None and len(facts.downstream_request_id) == 256
    assert _facts(request_id=longest + "x") is None


@pytest.mark.parametrize(
    ("stored", "recorded", "same"),
    [
        (12.0, 12, True),
        (12, 12.0, True),
        ({"a": [1.0, {"b": 2.0}]}, {"a": [1, {"b": 2}]}, True),
        (12.5, 12, False),
        (12.5, 12.5, True),
        (True, 1, False),
        (1, True, False),
        (False, 0.0, False),
        ("12", 12, False),
        (float("inf"), float("inf"), False),
        (1e300, int(1e300), True),
    ],
)
def test_numbers_are_compared_as_json_numbers_and_nothing_else_is_normalised(
    stored: Any, recorded: Any, same: bool
) -> None:
    view = _view(args={"url": URL, "method": "POST", "body": recorded})
    assert (_facts(view, body=stored) is not None) is same


def test_a_body_nested_too_deeply_to_compare_is_not_a_receipt() -> None:
    deep: Any = "x"
    for _ in range(5000):
        deep = [deep]
    view = _view(args={"url": URL, "method": "POST", "body": "x"})
    assert _facts(view, body=deep) is None


def test_the_lookup_has_a_short_explicit_timeout() -> None:
    assert http.LOOKUP_TIMEOUT_SECONDS == 5.0
    assert http.lookup_config(LOOKUP)["timeout_seconds"] == 5.0
