"""Typed transport outcomes and the document-request capability record (B05.3, #711).

Everything that talks to the downstream goes through the real executor and the downstream fixture process (no mocked
transport), like ``test_connector_capabilities.py``; the classification rules are pure and tested exhaustively.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar

import httpx
import pytest

from caliber.effects.connectors import (
    KEY_RETENTIONS,
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
    _send_raw,
    bind_egress_policy,
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
    return _send_raw(
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
    return _send_raw(
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
        # other 2xx are not what a write answers with (202 is "accepted for later processing")
        (202, TransportOutcome.AMBIGUOUS),
        (203, TransportOutcome.AMBIGUOUS),
        (205, TransportOutcome.AMBIGUOUS),
        (206, TransportOutcome.AMBIGUOUS),
        (299, TransportOutcome.AMBIGUOUS),
        # the 4xx this connector documents as "refused, nothing committed"
        (400, TransportOutcome.DEFINITIVE_REJECTION),
        (404, TransportOutcome.DEFINITIVE_REJECTION),
        (412, TransportOutcome.DEFINITIVE_REJECTION),
        (422, TransportOutcome.DEFINITIVE_REJECTION),
        # a 4xx it does not name proves nothing (a gateway, a "request in progress")
        (401, TransportOutcome.AMBIGUOUS),
        (403, TransportOutcome.AMBIGUOUS),
        (409, TransportOutcome.AMBIGUOUS),
        (413, TransportOutcome.AMBIGUOUS),
        (429, TransportOutcome.AMBIGUOUS),
        (499, TransportOutcome.AMBIGUOUS),
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
def test_an_http_answer_is_never_more_optimistic_than_its_status_and_the_capability(
    status: int, outcome: TransportOutcome
) -> None:
    classified = classify_status(status, capability=CAPABILITY)
    assert classified.outcome is outcome
    assert classified.status_code == status
    if outcome is TransportOutcome.AMBIGUOUS and 400 <= status < 500 and status != 408:
        assert classified.code == "unrecognised_4xx", "a 4xx the connector does not name says so"
    else:
        assert classified.code == f"http_{status}"


@pytest.mark.parametrize("status", [400, 404, 412, 422])
def test_a_documented_rejection_is_ambiguous_when_something_earlier_could_have_committed(
    status: int,
) -> None:
    assert (
        classify_status(status, capability=CAPABILITY, attempts=1).outcome
        is TransportOutcome.DEFINITIVE_REJECTION
    )
    # the executor's own earlier attempt (it tells us whether that attempt may have reached the downstream)
    assert (
        classify_status(status, capability=CAPABILITY, attempts=2).outcome
        is TransportOutcome.AMBIGUOUS
    )
    assert (
        classify_status(
            status, capability=CAPABILITY, attempts=2, earlier_attempt_may_have_been_sent=False
        ).outcome
        is TransportOutcome.DEFINITIVE_REJECTION
    ), "earlier attempts that never connected prove nothing was sent"
    assert (
        classify_status(
            status, capability=CAPABILITY, attempts=1, earlier_attempt_may_have_been_sent=True
        ).outcome
        is TransportOutcome.AMBIGUOUS
    )
    # an earlier dispatch of the same effect that this call cannot see (a recovery after a crash)
    recovery = classify_status(status, capability=CAPABILITY, earlier_dispatch_possible=True)
    assert recovery.outcome is TransportOutcome.AMBIGUOUS and recovery.code == f"http_{status}"
    assert classify_status(
        201, capability=CAPABILITY, attempts=3, earlier_dispatch_possible=True
    ).outcome is (TransportOutcome.COMMITTED_RECEIPT), "a commit claim is a commit claim"


@pytest.mark.parametrize("status", ["200", 200.0, None, True])
def test_a_status_that_is_not_an_integer_is_refused(status: Any) -> None:
    with pytest.raises(TypeError):
        classify_status(status, capability=CAPABILITY)


def test_a_tool_result_is_classified_from_its_status_and_what_the_executor_knows_of_earlier_attempts() -> (
    None
):
    def classify(**result: Any) -> Classified:
        return classify_result(result, capability=CAPABILITY)

    assert classify(status_code=412, attempts=1).outcome is TransportOutcome.DEFINITIVE_REJECTION
    assert classify(status_code=412, attempts=3).outcome is TransportOutcome.AMBIGUOUS
    assert classify(
        status_code=412, attempts=3, earlier_attempts_may_have_been_sent=False
    ).outcome is (TransportOutcome.DEFINITIVE_REJECTION)
    assert classify(
        status_code=412, attempts=1, earlier_attempts_may_have_been_sent=True
    ).outcome is (TransportOutcome.AMBIGUOUS)
    assert classify(status_code=201).outcome is TransportOutcome.COMMITTED_RECEIPT
    assert classify(status_code=422, attempts=0).outcome is TransportOutcome.DEFINITIVE_REJECTION
    assert classify(status_code=422, attempts="many").attempts == 1
    assert classify(status_code=422, earlier_attempts_may_have_been_sent="yes").outcome is (
        TransportOutcome.DEFINITIVE_REJECTION
    ), "a flag that is not a boolean is ignored"
    recovered = classify_result(
        {"status_code": 412}, capability=CAPABILITY, earlier_dispatch_possible=True
    )
    assert recovered.outcome is TransportOutcome.AMBIGUOUS
    with pytest.raises(KeyError):
        classify_result({}, capability=CAPABILITY)


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
        _send_raw(
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
            _send_raw(execution_config=config, input_schema=schema, input_data=data)
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
    downstream.set_fault("delay_response_seconds", 3.0)
    with pytest.raises(OpenApiExecutionError) as late:
        _submit(downstream, "k-late", version=1, timeout_seconds=0.6)
    assert isinstance(late.value.__cause__, httpx.ReadTimeout), (
        "the connect succeeded; the answer did not come"
    )
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
            _send_raw(
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
    assert isinstance(mixed.value.__cause__, httpx.ConnectError), (
        "the retry failed to connect, which alone proves nothing"
    )
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
        _send_raw(
            execution_config=submit_config("http://127.0.0.1:9", max_attempts=3),
            input_schema=None,
            input_data=submit_input(
                case_id=CASE, idempotency_key="k-egress", expected_resource_version=None, body=BODY
            ),
        )
    assert blocked.value.never_sent is never_sent
    assert blocked.value.attempts == (2 if answered_first else 1)
    assert not script, "a block is not retried"


# ---------------------------------------------------------------- the executor's one-key rule and its pre-flight refusals


def _post_with(data: dict[str, Any], **config: Any) -> dict[str, Any]:
    """POST to a scripted client that answers 201 and records the headers it was sent."""
    return _send_raw(
        execution_config=submit_config("http://127.0.0.1:9", **config),
        input_schema=None,
        input_data={"path_params": {"case_id": CASE}, **data},
    )


class _RecordingClient(_ScriptedClient):
    sent: ClassVar[list[dict[str, str]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        type(self).sent.append(dict(kwargs.get("headers") or {}))
        return super().request(method, url, **kwargs)


class _Created(_Answer):
    status_code = 201


def _recording(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> None:
    from caliber.integrations.openapi import executor

    _RecordingClient.sent = []
    monkeypatch.setattr(executor, "build_client", lambda **_: _RecordingClient(script))
    monkeypatch.setattr(executor.time, "sleep", lambda _seconds: None)


@pytest.mark.parametrize(
    "data",
    [
        {
            "idempotency_key": "k-1",
            "header_params": {"Idempotency-Key": "k-1"},
        },  # the same key twice
        {
            "header_params": {"idempotency-key": "k-1", "IDEMPOTENCY-KEY": "k-1"}
        },  # two spellings, one key
        {"idempotency_key": " k-1 ", "header_params": {"Idempotency-Key": "k-1"}},
    ],
)
def test_the_same_key_supplied_twice_is_emitted_once(
    monkeypatch: pytest.MonkeyPatch, data: dict[str, Any]
) -> None:
    _recording(monkeypatch, [_Created()])
    result = _post_with({"body": BODY, **data})
    assert result["status_code"] == 201
    (headers,) = _RecordingClient.sent
    assert [name for name in headers if name.lower() == "idempotency-key"] == ["Idempotency-Key"]
    assert headers["Idempotency-Key"] == "k-1"


@pytest.mark.parametrize(
    "data",
    [
        {"idempotency_key": "k-1", "header_params": {"idempotency-key": "k-2"}},
        {"header_params": {"Idempotency-Key": "k-1", "idempotency-key": "k-2"}},
    ],
)
def test_two_different_keys_are_refused_before_anything_is_sent(
    monkeypatch: pytest.MonkeyPatch, data: dict[str, Any]
) -> None:
    _recording(monkeypatch, [_Created()])
    with pytest.raises(OpenApiExecutionError, match="two different idempotency keys") as refused:
        _post_with({"body": BODY, **data})
    assert refused.value.never_sent is True and _RecordingClient.sent == []


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_key_header_is_refused_not_dropped(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """Dropping it would turn a bad key into a keyless request."""
    _recording(monkeypatch, [_Created()])
    with pytest.raises(OpenApiExecutionError, match="must not be blank") as refused:
        _post_with(
            {"body": BODY, "header_params": {"Idempotency-Key": blank}, "idempotency_key": "k-1"}
        )
    assert refused.value.never_sent is True and _RecordingClient.sent == []


@pytest.mark.parametrize("segment", [".", ".."])
def test_a_dot_segment_cannot_retarget_the_request(
    monkeypatch: pytest.MonkeyPatch, segment: str
) -> None:
    _recording(monkeypatch, [_Created()])
    with pytest.raises(OpenApiExecutionError, match="dot segment") as refused:
        _post_with({"path_params": {"case_id": segment}, "body": BODY, "idempotency_key": "k-1"})
    assert refused.value.never_sent is True and _RecordingClient.sent == []


def test_any_exception_raised_before_a_request_is_built_is_marked_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _recording(monkeypatch, [_Created()])
    with pytest.raises(ValueError) as malformed:
        _send_raw(
            execution_config={**submit_config("http://127.0.0.1:9"), "timeout_seconds": "soon"},
            input_schema=None,
            input_data={"path_params": {"case_id": CASE}, "body": BODY, "idempotency_key": "k-1"},
        )
    assert malformed.value.never_sent is True and malformed.value.attempts == 0  # type: ignore[attr-defined]
    assert classify_error(malformed.value).outcome is TransportOutcome.PROVEN_NOT_SENT
    assert _RecordingClient.sent == []


def test_a_bound_tool_called_with_args_and_kwargs_is_refused_as_not_sent() -> None:
    from types import SimpleNamespace

    from caliber.integrations.openapi.executor import bind_openapi_http_tool

    tool = bind_openapi_http_tool(
        SimpleNamespace(
            local_name="t", backend_config=submit_config("http://127.0.0.1:9"), input_schema=None
        )
    )
    for call in (
        lambda: tool({"body": BODY}, body=BODY),
        lambda: tool("not an object"),
        lambda: tool({}, {}),
    ):
        with pytest.raises(OpenApiExecutionError) as refused:
            call()
        assert refused.value.never_sent is True


def test_whether_an_earlier_attempt_may_have_been_sent_is_reported_with_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(script: list[Any]) -> dict[str, Any]:
        _recording(monkeypatch, script)
        return _post_with({"body": BODY, "idempotency_key": "k-1"}, max_attempts=3)

    first = run([_Created()])
    assert (first["attempts"], first["earlier_attempts_may_have_been_sent"]) == (1, False)
    after_503 = run([_Answer(), _Created()])
    assert (after_503["attempts"], after_503["earlier_attempts_may_have_been_sent"]) == (2, True)
    after_refusals = run([httpx.ConnectError("refused"), httpx.ConnectError("refused"), _Created()])
    assert (after_refusals["attempts"], after_refusals["earlier_attempts_may_have_been_sent"]) == (
        3,
        False,
    )
    after_a_lost_read = run([httpx.ReadError("dropped"), _Created()])
    assert after_a_lost_read["earlier_attempts_may_have_been_sent"] is True
    assert _RecordingClient.sent[0].get("Idempotency-Key") == "k-1"


# ---------------------------------------------------------------- the document-request connector against its fixture


def test_the_stable_key_replays_and_both_answers_are_a_committed_receipt_claim(
    downstream: Downstream,
) -> None:
    first = _submit(downstream, "k-1", version=1)
    again = _submit(downstream, "k-1", version=1)
    assert (first["status_code"], again["status_code"]) == (201, 200)
    assert first["json"] == again["json"]
    assert downstream.mutations() == 1
    outcomes = {classify_result(r, capability=CAPABILITY).outcome for r in (first, again)}
    assert outcomes == {TransportOutcome.COMMITTED_RECEIPT}


def test_a_retry_of_a_request_that_committed_is_replayed_even_with_a_stale_precondition(
    downstream: Downstream,
) -> None:
    """Replay comes before the precondition: the same key with the old ``If-Match`` after the commit moved the case is a
    200 replay, not a 412, and commits nothing more."""
    assert _submit(downstream, "k-replay", version=1)["status_code"] == 201
    replay = _submit(downstream, "k-replay", version=1)  # the case is now at version 2
    assert replay["status_code"] == 200
    assert downstream.mutations() == 1


def test_a_stale_precondition_is_a_definitive_rejection_and_commits_nothing(
    downstream: Downstream,
) -> None:
    refused = _submit(downstream, "k-stale", version=999)
    assert refused["status_code"] == CAPABILITY.stale_precondition_status == 412
    assert classify_result(refused, capability=CAPABILITY) == Classified(
        TransportOutcome.DEFINITIVE_REJECTION, "http_412", 412, 1
    )
    assert downstream.mutations() == 0


def test_a_reused_key_for_another_request_is_a_definitive_rejection_and_commits_nothing(
    downstream: Downstream,
) -> None:
    assert _submit(downstream, "k-reuse", version=1)["status_code"] == 201
    conflict = _submit(downstream, "k-reuse", version=1, body={"document_type": "bank_statement"})
    assert conflict["status_code"] == CAPABILITY.key_conflict_status == 422
    assert (
        classify_result(conflict, capability=CAPABILITY).outcome
        is TransportOutcome.DEFINITIVE_REJECTION
    )
    assert downstream.mutations() == 1


def test_the_lookup_reads_its_own_writes_and_a_404_is_unknown_not_proof(
    downstream: Downstream,
) -> None:
    assert CAPABILITY.lookup_consistency == "read_your_writes"
    assert _lookup(downstream, "k-never")["status_code"] == 404
    assert interpret_lookup(CAPABILITY, 404) == "unknown", "absence is not final for this connector"
    _submit(downstream, "k-found", version=1)
    answered = _lookup(downstream, "k-found")
    assert answered["status_code"] == 200
    assert interpret_lookup(CAPABILITY, answered["status_code"]) == "answered"
    assert answered["json"]["request_id"], (
        "what the downstream holds is what B05.4 verifies against the intent"
    )


def test_a_retry_with_the_key_after_a_lost_response_replays_the_commit(
    downstream: Downstream,
) -> None:
    downstream.set_fault("drop_response_after_commit")
    replayed = _submit(downstream, "k-retry", version=1, max_attempts=3)
    assert replayed["attempts"] == 2 and replayed["status_code"] == 200
    assert (
        classify_result(replayed, capability=CAPABILITY).outcome
        is TransportOutcome.COMMITTED_RECEIPT
    )
    assert replayed["earlier_attempts_may_have_been_sent"] is True, (
        "the first attempt was lost after the commit"
    )
    assert downstream.mutations() == 1


def test_a_recovery_dispatch_refused_with_a_stale_precondition_is_not_a_rejection(
    downstream: Downstream,
) -> None:
    """A worker died after its dispatch committed (the case moved to version 2). The recovery dispatch of the same
    effect, with a new key and the old version, is refused with 412 *because* of that commit: with
    ``earlier_dispatch_possible`` it is ambiguous, and without it the same answer would have recorded a false failure."""
    downstream.set_fault("drop_response_after_commit")
    with pytest.raises(OpenApiExecutionError):
        _submit(downstream, "k-first", version=1)
    stale = _submit(downstream, "k-recovery", version=1)
    assert stale["status_code"] == 412
    plain = classify_result(stale, capability=CAPABILITY)
    recovery = classify_result(stale, capability=CAPABILITY, earlier_dispatch_possible=True)
    assert plain.outcome is TransportOutcome.DEFINITIVE_REJECTION
    assert recovery.outcome is TransportOutcome.AMBIGUOUS


# ---------------------------------------------------------------- the capability record


def test_the_document_request_capability_says_what_the_fixture_shows() -> None:
    assert (CAPABILITY.connector, CAPABILITY.version) == ("document-request", "1")
    assert CAPABILITY.idempotency_key_header == KEY_HEADER == "Idempotency-Key"
    assert CAPABILITY.key_scope == "downstream"
    assert (CAPABILITY.key_retention, CAPABILITY.key_retention_seconds) == ("unconfirmed", None)
    assert CAPABILITY.retention_confirmed is False, (
        "the fixture never expires a key; the real system is unknown"
    )
    assert (CAPABILITY.key_conflict_status, CAPABILITY.precondition_header) == (422, "If-Match")
    assert CAPABILITY.stale_precondition_status == 412 and CAPABILITY.lookup_by_key is True
    assert CAPABILITY.definitive_rejection_statuses == frozenset({400, 404, 412, 422})
    assert CAPABILITY.absence_proves_noncommit is False
    assert CAPABILITY.carries_a_write_exactly_once is True


def _capability(**overrides: Any) -> ConnectorCapability:
    fields: dict[str, Any] = {
        "connector": "c",
        "version": "1",
        "idempotency_key_header": "Idempotency-Key",
        "key_scope": "downstream",
        "key_retention": "bounded",
        "key_retention_seconds": 3600,
        "key_conflict_status": 422,
        "precondition_header": "If-Match",
        "stale_precondition_status": 412,
        "definitive_rejection_statuses": frozenset({400, 412, 422}),
        "lookup_by_key": True,
        "lookup_consistency": "read_your_writes",
        "absence_proves_noncommit": False,
    }
    fields.update(overrides)
    return ConnectorCapability(**fields)


_NO_KEY: dict[str, Any] = {
    "idempotency_key_header": None,
    "key_scope": "unknown",
    "key_retention": "unconfirmed",
    "key_retention_seconds": None,
    "key_conflict_status": None,
    "lookup_by_key": False,
    "absence_proves_noncommit": False,
}


@pytest.mark.parametrize(
    "bad",
    [
        {"connector": ""},
        {"connector": "  "},
        {"connector": 5},
        {"version": ""},
        {"key_scope": "tenant"},
        {"key_retention": "forever"},
        {"lookup_consistency": "strong"},
        {"idempotency_key_header": ""},
        {"idempotency_key_header": 5},
        {
            "idempotency_key_header": None
        },  # still has a scope, a conflict status and a lookup: contradictory
        {**_NO_KEY, "key_scope": "downstream"},
        {**_NO_KEY, "key_retention": "unbounded"},
        {**_NO_KEY, "key_conflict_status": 422},
        {**_NO_KEY, "lookup_by_key": True},
        {"precondition_header": None},  # keeps a stale status: contradictory
        {"key_retention": "bounded", "key_retention_seconds": None},
        {"key_retention": "unbounded", "key_retention_seconds": 3600},
        {"key_retention": "unconfirmed", "key_retention_seconds": 3600},
        {"key_retention_seconds": 0},
        {"key_retention_seconds": -5},
        {"key_retention_seconds": 1.5},
        {"key_conflict_status": 200},
        {"key_conflict_status": 500},
        {"key_conflict_status": True},
        {"key_conflict_status": 409},  # not in definitive_rejection_statuses
        {"stale_precondition_status": 399},
        {"stale_precondition_status": 428},
        {"definitive_rejection_statuses": {400}},  # not a frozenset
        {"definitive_rejection_statuses": frozenset({408, 412, 422})},  # a timeout is not a refusal
        {"definitive_rejection_statuses": frozenset({200, 412, 422})},
        {"definitive_rejection_statuses": frozenset({500, 412, 422})},
        {"definitive_rejection_statuses": frozenset({True, 412, 422})},
        {"lookup_by_key": "yes"},
        {"lookup_by_key": 1},
        {"absence_proves_noncommit": "no"},
        {"lookup_by_key": False, "absence_proves_noncommit": True},
        {"lookup_consistency": "eventual", "absence_proves_noncommit": True},
        {"lookup_consistency": "unknown", "absence_proves_noncommit": True},
    ],
)
def test_a_contradictory_or_malformed_capability_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _capability(**bad)


def test_a_connector_with_no_stable_key_is_a_valid_record_that_cannot_carry_a_write() -> None:
    record = _capability(**_NO_KEY, precondition_header=None, stale_precondition_status=None)
    assert record.carries_a_write_exactly_once is False and record.retention_confirmed is False


def test_what_a_connector_must_have_to_carry_a_write_exactly_once() -> None:
    assert _capability().carries_a_write_exactly_once is True
    weak = {
        "no key": _capability(**_NO_KEY),
        "no precondition": _capability(precondition_header=None, stale_precondition_status=None),
        "no lookup": _capability(lookup_by_key=False),
        "unknown scope": _capability(key_scope="unknown"),
        "unknown lookup consistency": _capability(lookup_consistency="unknown"),
        "no documented conflict": _capability(key_conflict_status=None),
    }
    assert {name: c.carries_a_write_exactly_once for name, c in weak.items()} == dict.fromkeys(
        weak, False
    )
    assert set(KEY_SCOPES) >= {"downstream"} and set(LOOKUP_CONSISTENCY) >= {"read_your_writes"}
    assert set(KEY_RETENTIONS) == {"unbounded", "bounded", "unconfirmed"}
    assert (
        _capability().retention_confirmed
        and _capability(key_retention="unbounded", key_retention_seconds=None).retention_confirmed
    )
    assert not _capability(
        key_retention="unconfirmed", key_retention_seconds=None
    ).retention_confirmed


def test_the_digest_changes_with_every_promise_and_not_otherwise() -> None:
    base = _capability()
    assert base.digest() == _capability().digest() and base.digest().startswith("sha256:")
    assert len(base.digest()) == len("sha256:") + 64
    changes = [
        {"connector": "d"},
        {"version": "2"},
        {"idempotency_key_header": "X-Key"},
        {"key_scope": "resource"},
        {"key_retention": "unbounded", "key_retention_seconds": None},
        {"key_retention_seconds": 7200},
        {"key_conflict_status": 400},
        {"precondition_header": "X-Version"},
        {"stale_precondition_status": 422},
        {"definitive_rejection_statuses": frozenset({400, 412, 422, 404})},
        {"lookup_by_key": False},
        {"lookup_consistency": "eventual"},
        {"absence_proves_noncommit": True},
    ]
    digests = {base.digest()}
    for change in changes:
        digests.add(_capability(**change).digest())
    assert len(digests) == len(changes) + 1


def test_absence_is_final_only_for_a_connector_that_proves_it() -> None:
    proving = _capability(absence_proves_noncommit=True)
    assert interpret_lookup(proving, 404) == "not_committed"
    assert interpret_lookup(_capability(), 404) == "unknown"
    for status in (500, 503, 401, 403, 302, 400):
        assert interpret_lookup(proving, status) == "unknown", "only a 404 can say 'not found'"
    assert interpret_lookup(proving, 200) == "answered"
    no_lookup = _capability(lookup_by_key=False)
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
    zero = submit_input(case_id=CASE, idempotency_key="k", expected_resource_version=0, body=BODY)
    assert zero["header_params"]["If-Match"] == "0"
    assert lookup_input("k") == {"query_params": {"idempotency_key": "k"}}
    config = submit_config("http://x", max_attempts=2, timeout_seconds=1.5)
    assert (
        config["idempotency_key_header"],
        config["max_attempts"],
        config["timeout_seconds"],
    ) == (KEY_HEADER, 2, 1.5)
    assert "timeout_seconds" not in submit_config("http://x")
    assert lookup_config("http://x")["method"] == "GET"


@pytest.mark.parametrize(
    "key",
    [
        "",
        "   ",
        " k",
        "k ",
        "\tk",
        "k\n",
        "k\r\nX-Injected: 1",
        "cl\u00e9",
        "k\x00",
        "k\x7f",
        "a" * 256,
        None,
        5,
        b"k",
    ],
)
def test_a_key_that_is_not_printable_ascii_without_surrounding_whitespace_is_refused_up_front(
    key: Any,
) -> None:
    with pytest.raises(ValueError, match="idempotency key"):
        submit_input(case_id=CASE, idempotency_key=key, expected_resource_version=1, body=BODY)
    with pytest.raises(ValueError, match="idempotency key"):
        lookup_input(key)


@pytest.mark.parametrize("key", ["k", "a b", "a" * 255, "~!@#$%^&*()_+-=[]{};':\",./<>?"])
def test_keys_up_to_255_printable_ascii_characters_are_accepted(key: str) -> None:
    assert submit_input(case_id=CASE, idempotency_key=key, expected_resource_version=1, body=BODY)
    assert lookup_input(key)


@pytest.mark.parametrize(
    "case_id",
    ["", ".", "..", "../admin", "a/b", "a b", "-a", ".a", "a" * 129, "caf\u00e9", None, 5],
)
def test_a_case_id_that_is_not_one_safe_path_segment_is_refused(case_id: Any) -> None:
    with pytest.raises(ValueError, match="case_id"):
        submit_input(case_id=case_id, idempotency_key="k", expected_resource_version=1, body=BODY)


@pytest.mark.parametrize("case_id", ["MORT-2026-000184", "a", "A1", "a.b_c-d", "a" * 128])
def test_a_case_id_that_is_one_safe_path_segment_is_accepted(case_id: str) -> None:
    assert submit_input(
        case_id=case_id, idempotency_key="k", expected_resource_version=1, body=BODY
    )


@pytest.mark.parametrize("version", [-1, "1", 1.0, True])
def test_an_expected_version_must_be_a_non_negative_integer_or_none(version: Any) -> None:
    with pytest.raises(ValueError, match="expected_resource_version"):
        submit_input(
            case_id=CASE, idempotency_key="k", expected_resource_version=version, body=BODY
        )
