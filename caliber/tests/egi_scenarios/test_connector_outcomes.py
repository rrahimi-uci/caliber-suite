"""Typed transport outcomes and the document-request capability record (B05.3, #711).

Everything that talks to the downstream goes through the real executor and the downstream fixture process (no mocked
transport), like ``test_connector_capabilities.py``; the classification rules are pure and tested exhaustively.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from caliber.effects.connectors import (
    KEY_SCOPES,
    LOOKUP_CONSISTENCY,
    Classified,
    ConnectorCapability,
    TransportOutcome,
    classify_error,
    classify_result,
    classify_status,
    interpret_lookup,
)
from caliber.effects.connectors.document_request import (
    CAPABILITY,
    KEY_HEADER,
    lookup_config,
    lookup_input,
    submit_config,
    submit_input,
)
from caliber.egress import EgressPolicy
from caliber.integrations.openapi.executor import (
    OpenApiExecutionError,
    bind_egress_policy,
    execute_openapi_http_tool,
)
from tests.egi_scenarios.conftest import Downstream

CASE = "MORT-2026-000184"
BODY = {"document_type": "pay_statement"}


@pytest.fixture(autouse=True)
def _loopback_allowed() -> Iterator[None]:
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    yield
    bind_egress_policy(None)


def _submit(
    downstream: Downstream,
    key: str,
    *,
    version: int | None = None,
    body: dict[str, Any] | None = None,
    **cfg: Any,
) -> dict[str, Any]:
    return execute_openapi_http_tool(
        execution_config=submit_config(downstream.base_url, **cfg),
        input_schema=None,
        input_data=submit_input(
            case_id=CASE,
            idempotency_key=key,
            expected_resource_version=version,
            body=BODY if body is None else body,
        ),
    )


def _lookup(downstream: Downstream, key: str) -> dict[str, Any]:
    return execute_openapi_http_tool(
        execution_config=lookup_config(downstream.base_url),
        input_schema=None,
        input_data=lookup_input(key),
    )


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ---------------------------------------------------------------- the classification rules


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (200, TransportOutcome.COMMITTED_RECEIPT),
        (201, TransportOutcome.COMMITTED_RECEIPT),
        (204, TransportOutcome.COMMITTED_RECEIPT),
        (299, TransportOutcome.COMMITTED_RECEIPT),
        (400, TransportOutcome.DEFINITIVE_REJECTION),
        (401, TransportOutcome.DEFINITIVE_REJECTION),
        (403, TransportOutcome.DEFINITIVE_REJECTION),
        (404, TransportOutcome.DEFINITIVE_REJECTION),
        (409, TransportOutcome.DEFINITIVE_REJECTION),
        (412, TransportOutcome.DEFINITIVE_REJECTION),
        (422, TransportOutcome.DEFINITIVE_REJECTION),
        (429, TransportOutcome.DEFINITIVE_REJECTION),
        (499, TransportOutcome.DEFINITIVE_REJECTION),
        (408, TransportOutcome.AMBIGUOUS),
        (100, TransportOutcome.AMBIGUOUS),
        (301, TransportOutcome.AMBIGUOUS),
        (302, TransportOutcome.AMBIGUOUS),
        (500, TransportOutcome.AMBIGUOUS),
        (502, TransportOutcome.AMBIGUOUS),
        (503, TransportOutcome.AMBIGUOUS),
        (504, TransportOutcome.AMBIGUOUS),
        (599, TransportOutcome.AMBIGUOUS),
        (0, TransportOutcome.AMBIGUOUS),
        (999, TransportOutcome.AMBIGUOUS),
    ],
)
def test_an_http_answer_is_never_more_optimistic_than_its_status(
    status: int, outcome: TransportOutcome
) -> None:
    classified = classify_status(status)
    assert (classified.outcome, classified.code, classified.status_code) == (
        outcome,
        f"http_{status}",
        status,
    )


@pytest.mark.parametrize("status", [400, 404, 412, 422, 429])
def test_a_rejection_after_an_earlier_attempt_is_ambiguous_because_that_attempt_may_have_committed(
    status: int,
) -> None:
    assert classify_status(status, attempts=1).outcome is TransportOutcome.DEFINITIVE_REJECTION
    again = classify_status(status, attempts=2)
    assert again.outcome is TransportOutcome.AMBIGUOUS and again.attempts == 2
    assert classify_status(201, attempts=3).outcome is TransportOutcome.COMMITTED_RECEIPT, (
        "a commit is a commit"
    )


@pytest.mark.parametrize("status", ["200", 200.0, None, True])
def test_a_status_that_is_not_an_integer_is_refused(status: Any) -> None:
    with pytest.raises(TypeError):
        classify_status(status)


def test_a_tool_result_is_classified_from_its_status_and_attempts() -> None:
    assert (
        classify_result({"status_code": 412, "attempts": 1}).outcome
        is TransportOutcome.DEFINITIVE_REJECTION
    )
    assert (
        classify_result({"status_code": 412, "attempts": 3}).outcome is TransportOutcome.AMBIGUOUS
    )
    assert classify_result({"status_code": 201}).outcome is TransportOutcome.COMMITTED_RECEIPT
    assert (
        classify_result({"status_code": 422, "attempts": 0}).outcome
        is TransportOutcome.DEFINITIVE_REJECTION
    )
    assert classify_result({"status_code": 422, "attempts": "many"}).attempts == 1
    with pytest.raises(KeyError):
        classify_result({})


def test_an_error_is_proven_not_sent_only_when_the_executor_says_so() -> None:
    never = classify_error(OpenApiExecutionError("refused", never_sent=True, attempts=2))
    assert (never.outcome, never.code, never.attempts) == (
        TransportOutcome.PROVEN_NOT_SENT,
        "not_sent",
        2,
    )
    maybe = classify_error(OpenApiExecutionError("dropped", never_sent=False, attempts=1))
    assert (maybe.outcome, maybe.code) == (TransportOutcome.AMBIGUOUS, "may_have_been_sent")
    for other in (RuntimeError("boom"), TimeoutError(), KeyError("x"), ValueError("v")):
        unclassified = classify_error(other)
        assert (unclassified.outcome, unclassified.code) == (
            TransportOutcome.AMBIGUOUS,
            "unclassified_error",
        )
    assert classify_error(OpenApiExecutionError("x", attempts=True)).attempts == 0, (
        "a bool is not a count"
    )


def test_the_four_outcomes_are_exactly_the_four_the_adr_names() -> None:
    assert {o.value for o in TransportOutcome} == {
        "proven_not_sent",
        "definitive_rejection",
        "committed_receipt",
        "ambiguous",
    }
    assert Classified(TransportOutcome.AMBIGUOUS, "x").attempts == 0


# ---------------------------------------------------------------- what the executor can prove it did not send


def test_a_refused_connection_is_proven_not_sent(downstream: Downstream) -> None:
    config = submit_config(f"http://127.0.0.1:{_closed_port()}", max_attempts=3)
    with pytest.raises(OpenApiExecutionError) as refused:
        execute_openapi_http_tool(
            execution_config=config,
            input_schema=None,
            input_data=submit_input(
                case_id=CASE, idempotency_key="k-refused", expected_resource_version=1, body=BODY
            ),
        )
    assert refused.value.never_sent is True and refused.value.attempts == 3
    assert classify_error(refused.value).outcome is TransportOutcome.PROVEN_NOT_SENT
    assert downstream.mutations() == 0


def test_a_refusal_before_the_request_is_built_is_proven_not_sent(downstream: Downstream) -> None:
    for config, schema, data in (
        (
            submit_config(downstream.base_url),
            {"type": "object", "required": ["nope"]},
            {"body": BODY},
        ),  # input schema
        ({}, None, {}),  # no configuration
        ({**submit_config(downstream.base_url), "path": "no-slash"}, None, {}),  # a malformed path
        (submit_config(""), None, {}),  # no server
    ):
        with pytest.raises(OpenApiExecutionError) as refused:
            execute_openapi_http_tool(execution_config=config, input_schema=schema, input_data=data)
        assert refused.value.never_sent is True and refused.value.attempts == 0
    assert downstream.mutations() == 0


def test_the_egress_policy_blocking_the_destination_is_proven_not_sent(
    downstream: Downstream,
) -> None:
    bind_egress_policy(EgressPolicy())  # loopback is blocked by default
    with pytest.raises(OpenApiExecutionError) as blocked:
        _submit(downstream, "k-blocked")
    assert blocked.value.never_sent is True
    assert classify_error(blocked.value).outcome is TransportOutcome.PROVEN_NOT_SENT
    assert downstream.mutations() == 0


def test_a_response_lost_after_the_commit_is_ambiguous_and_the_effect_did_happen(
    downstream: Downstream,
) -> None:
    downstream.set_fault("drop_response_after_commit")
    with pytest.raises(OpenApiExecutionError) as lost:
        _submit(downstream, "k-lost", version=1)
    assert lost.value.never_sent is False and lost.value.attempts == 1
    assert classify_error(lost.value).outcome is TransportOutcome.AMBIGUOUS
    assert downstream.mutations() == 1, (
        "ambiguous is the honest answer: it committed and nothing told us"
    )


def test_a_timeout_after_the_commit_is_ambiguous(downstream: Downstream) -> None:
    downstream.set_fault("delay_response_seconds", 1.0)
    with pytest.raises(OpenApiExecutionError) as late:
        _submit(downstream, "k-late", version=1, timeout_seconds=0.3)
    assert late.value.never_sent is False
    assert classify_error(late.value).outcome is TransportOutcome.AMBIGUOUS
    assert downstream.mutations() == 1


class _ThenGone(BaseHTTPRequestHandler):
    """Answers 503 to the first POST and then stops listening, so the retry cannot connect."""

    server_ref: HTTPServer | None = None

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")
        self.wfile.flush()
        server = type(self).server_ref
        if server is not None:
            threading.Thread(
                target=lambda: (server.shutdown(), server.server_close()), daemon=True
            ).start()

    def log_message(self, format: str, *args: Any) -> None:
        return


def test_a_failed_connect_after_an_answered_attempt_is_ambiguous_not_proven_not_sent() -> None:
    """The first attempt reached the downstream (it answered 503, which may follow a commit); the retry then could not
    connect. The request is not proven unsent, so the error must say so."""
    server = HTTPServer(("127.0.0.1", 0), _ThenGone)
    _ThenGone.server_ref = server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        with pytest.raises(OpenApiExecutionError) as mixed:
            execute_openapi_http_tool(
                execution_config=submit_config(f"http://127.0.0.1:{port}", max_attempts=3),
                input_schema=None,
                input_data=submit_input(
                    case_id=CASE,
                    idempotency_key="k-mixed",
                    expected_resource_version=None,
                    body=BODY,
                ),
            )
    finally:
        thread.join(timeout=5)
    assert mixed.value.never_sent is False and mixed.value.attempts == 3
    assert classify_error(mixed.value).outcome is TransportOutcome.AMBIGUOUS


class _ScriptedClient:
    """A stand-in for the egress-guarded client: each request does the next scripted thing (an answer or an exception)."""

    def __init__(self, script: list[Any]) -> None:
        self.script = script

    def __enter__(self) -> _ScriptedClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class _Answer:
    status_code = 503
    text = "{}"
    headers: dict[str, str] = {}

    @staticmethod
    def json() -> dict[str, Any]:
        return {}


@pytest.mark.parametrize(("answered_first", "never_sent"), [(False, True), (True, False)])
def test_an_egress_block_found_when_the_connection_is_opened_is_not_sent_unless_an_earlier_attempt_was(
    monkeypatch: pytest.MonkeyPatch, answered_first: bool, never_sent: bool
) -> None:
    """The name re-resolves to a blocked address after the pre-check passed: the transport refuses at connect time. It is
    proven not sent only if no earlier attempt was answered."""
    from caliber.egress import EgressBlockedError
    from caliber.integrations.openapi import executor

    script: list[Any] = [_Answer()] if answered_first else []
    script.append(EgressBlockedError("blocked at connect"))
    monkeypatch.setattr(executor, "build_client", lambda **_: _ScriptedClient(script))
    monkeypatch.setattr(executor.time, "sleep", lambda _seconds: None)
    with pytest.raises(OpenApiExecutionError) as blocked:
        execute_openapi_http_tool(
            execution_config=submit_config("http://127.0.0.1:9", max_attempts=3),
            input_schema=None,
            input_data=submit_input(
                case_id=CASE, idempotency_key="k-egress", expected_resource_version=None, body=BODY
            ),
        )
    assert blocked.value.never_sent is never_sent
    assert blocked.value.attempts == (2 if answered_first else 1)
    assert not script, "a block is not retried"


# ---------------------------------------------------------------- the document-request connector against its fixture


def test_the_stable_key_replays_and_both_answers_are_a_committed_receipt_claim(
    downstream: Downstream,
) -> None:
    first = _submit(downstream, "k-1", version=1)
    again = _submit(downstream, "k-1", version=1)
    assert (first["status_code"], again["status_code"]) == (201, 200)
    assert first["json"] == again["json"]
    assert downstream.mutations() == 1
    assert (
        classify_result(first).outcome
        is classify_result(again).outcome
        is TransportOutcome.COMMITTED_RECEIPT
    )


def test_a_stale_precondition_is_a_definitive_rejection_and_commits_nothing(
    downstream: Downstream,
) -> None:
    refused = _submit(downstream, "k-stale", version=999)
    assert refused["status_code"] == CAPABILITY.stale_precondition_status == 412
    assert classify_result(refused) == Classified(
        TransportOutcome.DEFINITIVE_REJECTION, "http_412", 412, 1
    )
    assert downstream.mutations() == 0


def test_a_reused_key_for_another_request_is_a_definitive_rejection_and_commits_nothing(
    downstream: Downstream,
) -> None:
    assert _submit(downstream, "k-reuse", version=1)["status_code"] == 201
    conflict = _submit(downstream, "k-reuse", version=1, body={"document_type": "bank_statement"})
    assert conflict["status_code"] == CAPABILITY.key_conflict_status == 422
    assert classify_result(conflict).outcome is TransportOutcome.DEFINITIVE_REJECTION
    assert downstream.mutations() == 1


def test_the_lookup_reads_its_own_writes_and_a_404_is_unknown_not_proof(
    downstream: Downstream,
) -> None:
    assert CAPABILITY.lookup_consistency == "read_your_writes"
    assert _lookup(downstream, "k-never")["status_code"] == 404
    assert interpret_lookup(CAPABILITY, 404) == "unknown", "absence is not final for this connector"
    _submit(downstream, "k-found", version=1)
    found = _lookup(downstream, "k-found")
    assert (
        found["status_code"] == 200
        and interpret_lookup(CAPABILITY, found["status_code"]) == "found"
    )


def test_a_retry_with_the_key_after_a_lost_response_replays_the_commit(
    downstream: Downstream,
) -> None:
    downstream.set_fault("drop_response_after_commit")
    replayed = _submit(downstream, "k-retry", version=1, max_attempts=3)
    assert replayed["attempts"] == 2 and replayed["status_code"] == 200
    assert classify_result(replayed).outcome is TransportOutcome.COMMITTED_RECEIPT
    assert downstream.mutations() == 1


def test_a_precondition_refusal_is_a_rejection_alone_and_ambiguous_after_a_retry(
    downstream: Downstream,
) -> None:
    """After a lost response that did commit, the case is at version 2, so a request still carrying ``If-Match: 1`` is
    refused with 412. Alone that is a rejection; on a retry of a request whose first attempt may have committed, the same
    412 is explained by that first attempt, so it proves nothing."""
    downstream.set_fault("drop_response_after_commit")
    with pytest.raises(OpenApiExecutionError):
        _submit(downstream, "k-first", version=1)
    stale = _submit(downstream, "k-other", version=1)
    assert stale["status_code"] == 412
    assert (
        classify_status(stale["status_code"], attempts=1).outcome
        is TransportOutcome.DEFINITIVE_REJECTION
    )
    assert classify_status(stale["status_code"], attempts=2).outcome is TransportOutcome.AMBIGUOUS


# ---------------------------------------------------------------- the capability record


def test_the_document_request_capability_says_what_the_fixture_shows() -> None:
    assert (CAPABILITY.connector, CAPABILITY.version) == ("document-request", "1")
    assert CAPABILITY.idempotency_key_header == KEY_HEADER == "Idempotency-Key"
    assert CAPABILITY.key_scope == "downstream"
    assert CAPABILITY.key_retention_seconds is None, (
        "unconfirmed for the real system: recorded as not bounded"
    )
    assert (CAPABILITY.key_conflict_status, CAPABILITY.precondition_header) == (422, "If-Match")
    assert CAPABILITY.stale_precondition_status == 412 and CAPABILITY.lookup_by_key is True
    assert CAPABILITY.absence_proves_noncommit is False
    assert CAPABILITY.carries_a_write_exactly_once is True


def _capability(**overrides: Any) -> ConnectorCapability:
    fields: dict[str, Any] = {
        "connector": "c",
        "version": "1",
        "idempotency_key_header": "Idempotency-Key",
        "key_scope": "downstream",
        "key_retention_seconds": 3600,
        "key_conflict_status": 422,
        "precondition_header": "If-Match",
        "stale_precondition_status": 412,
        "lookup_by_key": True,
        "lookup_consistency": "read_your_writes",
        "absence_proves_noncommit": False,
    }
    fields.update(overrides)
    return ConnectorCapability(**fields)


@pytest.mark.parametrize(
    "bad",
    [
        {"connector": ""},
        {"connector": "  "},
        {"version": ""},
        {"key_scope": "tenant"},
        {"lookup_consistency": "strong"},
        {"idempotency_key_header": ""},
        {"idempotency_key_header": None},  # keeps a conflict status and a lookup: contradictory
        {"precondition_header": None},  # keeps a stale status: contradictory
        {"key_conflict_status": 200},
        {"key_conflict_status": 500},
        {"key_conflict_status": True},
        {"stale_precondition_status": 399},
        {"key_retention_seconds": 0},
        {"key_retention_seconds": -5},
        {"key_retention_seconds": 1.5},
        {"lookup_by_key": False, "absence_proves_noncommit": True},
    ],
)
def test_a_contradictory_or_malformed_capability_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _capability(**bad)


def test_what_a_connector_must_have_to_carry_a_write_exactly_once() -> None:
    assert _capability().carries_a_write_exactly_once is True
    no_key = _capability(
        idempotency_key_header=None,
        key_conflict_status=None,
        lookup_by_key=False,
        absence_proves_noncommit=False,
    )
    no_precondition = _capability(precondition_header=None, stale_precondition_status=None)
    no_lookup = _capability(lookup_by_key=False, absence_proves_noncommit=False)
    assert [c.carries_a_write_exactly_once for c in (no_key, no_precondition, no_lookup)] == [
        False
    ] * 3
    assert set(KEY_SCOPES) >= {"downstream"} and set(LOOKUP_CONSISTENCY) >= {"read_your_writes"}


def test_absence_is_final_only_for_a_connector_that_proves_it() -> None:
    proving = _capability(absence_proves_noncommit=True)
    assert interpret_lookup(proving, 404) == "not_committed"
    assert interpret_lookup(_capability(), 404) == "unknown"
    for status in (500, 503, 401, 403, 302, 400):
        assert interpret_lookup(proving, status) == "unknown", "only a 404 can say 'not found'"
    assert interpret_lookup(proving, 200) == "found"
    no_lookup = _capability(lookup_by_key=False, absence_proves_noncommit=False)
    assert interpret_lookup(no_lookup, 200) == interpret_lookup(no_lookup, 404) == "unknown"


def test_the_request_builders_put_the_key_and_version_where_the_downstream_reads_them() -> None:
    data = submit_input(case_id=CASE, idempotency_key="k", expected_resource_version=7, body=BODY)
    assert data == {
        "path_params": {"case_id": CASE},
        "header_params": {"Idempotency-Key": "k", "If-Match": "7"},
        "body": BODY,
    }
    unversioned = submit_input(
        case_id=CASE, idempotency_key="k", expected_resource_version=None, body=BODY
    )
    assert "If-Match" not in unversioned["header_params"]
    assert (
        submit_input(case_id=CASE, idempotency_key="k", expected_resource_version=0, body=BODY)[
            "header_params"
        ]["If-Match"]
        == "0"
    )
    assert lookup_input("k") == {"query_params": {"idempotency_key": "k"}}
    for key in ("", "   ", None):
        with pytest.raises(ValueError):
            submit_input(case_id=CASE, idempotency_key=key, expected_resource_version=1, body=BODY)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            lookup_input(key)  # type: ignore[arg-type]
    for version in (-1, "1", 1.0, True):
        with pytest.raises(ValueError):
            submit_input(
                case_id=CASE, idempotency_key="k", expected_resource_version=version, body=BODY
            )  # type: ignore[arg-type]
    config = submit_config("http://x", max_attempts=2, timeout_seconds=1.5)
    assert (
        config["idempotency_key_header"],
        config["max_attempts"],
        config["timeout_seconds"],
    ) == (KEY_HEADER, 2, 1.5)
    assert "timeout_seconds" not in submit_config("http://x")
    assert lookup_config("http://x")["method"] == "GET"
