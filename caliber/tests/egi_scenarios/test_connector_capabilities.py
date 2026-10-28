"""Can CALIBER's OpenAPI tool path do what exactly-once dispatch needs? (D1 experiment, #567)

Three capabilities decide the Beta connector, each measured against the real downstream process through the
real executor (``execute_openapi_http_tool``, no mocked transport):

1. a **stable key**: the same ``Idempotency-Key`` twice commits once;
2. a **precondition**: an ``If-Match`` that is not the case's current version is refused and commits nothing;
3. **query by key** after a lost response: "did my request commit?" is answerable.

The tool is built the way an imported integration builds it: the downstream's ``/openapi.json`` is normalized,
the input schema and execution config come from ``integrations/openapi/tool_drafts.py``, and the callable
from ``bind_openapi_http_tool``. The findings are in ``docs/reports/egi/D1-connector-capability.md``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Any, ClassVar

import httpx
import pytest

from caliber.egress import EgressPolicy
from caliber.integrations.openapi.executor import (
    OpenApiExecutionError,
    bind_egress_policy,
    bind_openapi_http_tool,
    execute_openapi_http_tool,
)
from caliber.integrations.openapi.normalize import normalize_openapi_document
from caliber.integrations.openapi.tool_drafts import build_execution_config, build_tool_input_schema
from tests.egi_scenarios.conftest import Downstream

CASE = "MORT-2026-000184"
POST_KEY = "POST /cases/{case_id}/document-requests"
QUERY_KEY = "GET /document-requests"
BODY = {"document_type": "pay_statement"}
QUERY_BUDGET_SECONDS = 1.0
#: The header the downstream reads its stable key from (a header name, not a credential).
KEY_HEADER = "Idempotency-Key"  # gitleaks:allow

Tool = Callable[..., dict[str, Any]]


@pytest.fixture(autouse=True)
def _loopback_allowed() -> Iterator[None]:
    """The downstream is on 127.0.0.1, which the default egress policy blocks. Reset afterwards: the policy
    is process-global."""
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    yield
    bind_egress_policy(None)


def build_tool(
    downstream: Downstream, operation_key: str, **config_overrides: Any
) -> tuple[Tool, dict[str, Any]]:
    """The tool an imported integration would give for ``operation_key``, and its execution config."""
    document = downstream.client.get("/openapi.json").json()
    _, operations, _ = normalize_openapi_document(document)
    draft = next(op for op in operations if op["operation_key"] == operation_key)
    operation = SimpleNamespace(
        operation_id="op-1",
        operation_key=draft["operation_key"],
        method=draft["method"],
        path=draft["path"],
        request_content_types=draft["request_content_types"],
        response_statuses=draft["response_statuses"],
        normalized_operation=draft["normalized_operation"],
    )
    config = build_execution_config(
        integration=SimpleNamespace(integration_id="int-1"),
        version=SimpleNamespace(version_id="ver-1"),
        operation=operation,  # type: ignore[arg-type]
        server_url=downstream.base_url,
        auth_binding=None,
    )
    config.update(config_overrides)
    binding = SimpleNamespace(
        backend_config=config,
        input_schema=build_tool_input_schema(operation),  # type: ignore[arg-type]
        local_name="request_missing_document",
    )
    return bind_openapi_http_tool(binding), config


def post_input(key: str, *, version: int | None = None) -> dict[str, Any]:
    headers: dict[str, Any] = {"Idempotency-Key": key}
    if version is not None:
        headers["If-Match"] = version
    return {"path_params": {"case_id": CASE}, "header_params": headers, "body": BODY}


# ---------------------------------------------------------------- 1. stable key


def test_stable_key_mutates_once(downstream: Downstream) -> None:
    request, _ = build_tool(downstream, POST_KEY)
    first = request(**post_input("key-1"))
    second = request(**post_input("key-1"))
    assert first["status_code"] == 201
    assert second["status_code"] == 200, "the same key replays the stored answer"
    assert second["json"] == first["json"]
    assert downstream.mutations() == 1


def test_a_different_key_is_a_different_request(downstream: Downstream) -> None:
    request, _ = build_tool(downstream, POST_KEY)
    assert request(**post_input("key-a"))["status_code"] == 201
    assert request(**post_input("key-b"))["status_code"] == 201
    assert downstream.mutations() == 2


# ---------------------------------------------------------------- 2. precondition


def test_stale_precondition_returns_412_and_no_mutation(downstream: Downstream) -> None:
    request, _ = build_tool(downstream, POST_KEY)
    refused = request(**post_input("key-1", version=999))
    assert refused["status_code"] == 412
    assert downstream.mutations() == 0


def test_the_current_version_is_accepted(downstream: Downstream) -> None:
    request, _ = build_tool(downstream, POST_KEY)
    accepted = request(**post_input("key-1", version=1))
    assert accepted["status_code"] == 201
    assert downstream.mutations() == 1


# ---------------------------------------------------------------- 3. query by key


def test_query_by_key_after_lost_response(downstream: Downstream) -> None:
    request, _ = build_tool(downstream, POST_KEY)
    downstream.set_fault("drop_response_after_commit")
    with pytest.raises(OpenApiExecutionError, match="request failed after 1 attempt") as lost:
        request(**post_input("key-lost"))
    assert isinstance(lost.value.__cause__, httpx.RemoteProtocolError), (
        "the cause is kept, the type is not"
    )
    assert downstream.mutations() == 1, "the downstream committed before the answer was lost"

    stored = httpx.get(
        f"{downstream.base_url}/document-requests", params={"idempotency_key": "key-lost"}
    )
    assert stored.status_code == 200
    assert stored.json()["idempotency_key"] == "key-lost"
    assert downstream.mutations() == 1


def test_the_query_runs_through_the_connector_and_answers_within_a_second(
    downstream: Downstream,
) -> None:
    request, _ = build_tool(downstream, POST_KEY)
    query, _ = build_tool(downstream, QUERY_KEY)
    created = request(**post_input("key-q"))
    started = time.perf_counter()
    found = query(query_params={"idempotency_key": "key-q"})
    elapsed = time.perf_counter() - started
    assert found["status_code"] == 200
    assert found["json"]["idempotency_key"] == "key-q"
    assert found["json"]["request_id"] == created["json"]["request_id"]
    assert elapsed < QUERY_BUDGET_SECONDS, f"query by key took {elapsed:.3f}s"
    missing = query(query_params={"idempotency_key": "never-sent"})
    assert missing["status_code"] == 404, "no request with that key: it did not commit"


# ---------------------------------------------------------------- what the connector does on its own


def test_the_connector_can_recover_a_lost_response_by_retrying_with_the_key(
    downstream: Downstream,
) -> None:
    """With ``max_attempts`` above 1 and ``idempotency_key_header`` bound, the executor's own retry replays the
    stored answer: one attempt is lost, the second returns 200 and the downstream committed once."""
    request, _ = build_tool(downstream, POST_KEY, max_attempts=3, idempotency_key_header=KEY_HEADER)
    downstream.set_fault("drop_response_after_commit")
    # The retry carries the original If-Match: by then the case is at version 2, and the downstream still
    # answers from the key (it checks the key before the version).
    result = request(**post_input("key-retry", version=1))
    assert result["attempts"] == 2
    assert result["status_code"] == 200
    assert downstream.mutations() == 1


def test_a_key_sent_as_a_header_parameter_does_not_enable_retries_by_itself(
    downstream: Downstream,
) -> None:
    """Finding: retries of a POST are allowed only when ``idempotency_key_header`` is *bound in the config*
    (``_max_attempts``), not when the caller sends the key as the spec's own ``Idempotency-Key`` header
    parameter. Without the binding a lost response surfaces as an error after one attempt."""
    request, _ = build_tool(downstream, POST_KEY, max_attempts=3)
    downstream.set_fault("drop_response_after_commit")
    with pytest.raises(OpenApiExecutionError, match="request failed after 1 attempt"):
        request(**post_input("key-no-binding"))
    assert downstream.mutations() == 1


def test_a_derived_input_schema_refuses_the_configured_idempotency_key_input(
    downstream: Downstream,
) -> None:
    """Finding: ``idempotency_key_header`` reads ``input_data["idempotency_key"]``, but the schema derived from
    the spec has ``additionalProperties: false`` and no such property, so the bound tool rejects it. The key
    has to travel as the spec's own ``Idempotency-Key`` header parameter."""
    request, _ = build_tool(downstream, POST_KEY, idempotency_key_header=KEY_HEADER)
    with pytest.raises(OpenApiExecutionError, match="idempotency_key"):
        request(path_params={"case_id": CASE}, body=BODY, idempotency_key="key-x")
    assert downstream.mutations() == 0


def test_the_configured_idempotency_key_header_works_without_a_derived_schema(
    downstream: Downstream,
) -> None:
    """The documented path does work when no input schema is applied (a hand-written tool config)."""
    _, config = build_tool(downstream, POST_KEY, idempotency_key_header=KEY_HEADER)
    result = execute_openapi_http_tool(
        execution_config=config,
        input_schema=None,
        input_data={"path_params": {"case_id": CASE}, "body": BODY, "idempotency_key": "key-y"},
    )
    assert result["status_code"] == 201
    assert downstream.mutations() == 1


class _FlakyEndpoint(BaseHTTPRequestHandler):
    """Answers 503 once, then 201, and records what each POST carried (a stand-in for any downstream)."""

    posts: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        type(self).posts.append({"idempotency_key": self.headers.get("Idempotency-Key")})
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        status = 503 if len(type(self).posts) == 1 else 201
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: Any) -> None:
        return


def _post_to_flaky(input_data: dict[str, Any], **config: Any) -> dict[str, Any]:
    _FlakyEndpoint.posts = []
    server = HTTPServer(("127.0.0.1", 0), _FlakyEndpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return execute_openapi_http_tool(
            execution_config={
                "kind": "openapi_http",
                "method": "POST",
                "path": "/things",
                "server_url": f"http://127.0.0.1:{server.server_address[1]}",
                "auth_binding": None,
                "request_content_types": ["application/json"],
                "idempotency_key_header": KEY_HEADER,
                "max_attempts": 3,
                **config,
            },
            input_schema=None,
            input_data=input_data,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_bound_header_with_no_key_sent_does_not_retry_a_post() -> None:
    """The hazard this used to pin (D1 finding, ``_max_attempts`` decided on the *binding* of the header): with the
    header bound and no key supplied, a POST that got a 503 was sent again keyless, a duplicate create against a
    downstream that does not deduplicate. Retries now need a key that the request actually carries."""
    result = _post_to_flaky({"body": {"x": 1}})
    assert result["attempts"] == 1 and result["status_code"] == 503
    assert [post["idempotency_key"] for post in _FlakyEndpoint.posts] == [None], (
        "one POST, and it was not repeated"
    )


@pytest.mark.parametrize("via", ["idempotency_key", "header_params", "header_params_lowercase"])
def test_a_post_that_carries_its_key_is_retried_with_the_same_key(via: str) -> None:
    """Whichever way the key is supplied, the retry carries the same one (the header name is matched case-insensitively)."""
    if via == "idempotency_key":
        data: dict[str, Any] = {"body": {"x": 1}, "idempotency_key": "k-1"}
    else:
        name = KEY_HEADER if via == "header_params" else KEY_HEADER.lower()
        data = {"body": {"x": 1}, "header_params": {name: "k-1"}}
    result = _post_to_flaky(data)
    assert result["attempts"] == 2 and result["status_code"] == 201
    assert [post["idempotency_key"] for post in _FlakyEndpoint.posts] == ["k-1", "k-1"]


def test_a_blank_key_is_refused_by_the_transport_before_anything_is_sent() -> None:
    with pytest.raises(OpenApiExecutionError) as refused:
        _post_to_flaky({"body": {"x": 1}, "header_params": {KEY_HEADER: "  "}})
    assert refused.value.never_sent is True, "an illegal header value never leaves the client"
    assert _FlakyEndpoint.posts == []


# ---------------------------------------------------------------- what a dispatcher also has to handle


def test_a_timeout_after_the_commit_is_recovered_by_retrying_with_the_key(
    downstream: Downstream,
) -> None:
    """The more realistic loss: the answer is late, not dropped. Attempt 1 times out after the commit, attempt 2
    replays. (The default timeout is 30 s, so loss detection is slow unless the operation sets one.)"""
    request, _ = build_tool(
        downstream,
        POST_KEY,
        max_attempts=2,
        timeout_seconds=0.3,
        idempotency_key_header=KEY_HEADER,
    )
    downstream.set_fault("delay_response_seconds", seconds=1)
    result = request(**post_input("key-slow", version=1))
    assert result["attempts"] == 2
    assert result["status_code"] == 200
    assert downstream.mutations() == 1


def test_a_failure_before_the_commit_is_retried_safely(downstream: Downstream) -> None:
    request, _ = build_tool(downstream, POST_KEY, max_attempts=2, idempotency_key_header=KEY_HEADER)
    downstream.set_fault("fail_before_commit")
    result = request(**post_input("key-fail", version=1))
    assert result["attempts"] == 2
    assert result["status_code"] == 201
    assert downstream.mutations() == 1


def test_key_reuse_for_another_request_is_a_result_not_an_exception(downstream: Downstream) -> None:
    """A dispatcher has to classify 412 and 422 itself: the executor returns every 4xx as a normal result and
    never retries it."""
    request, _ = build_tool(downstream, POST_KEY)
    assert request(**post_input("key-reuse"))["status_code"] == 201
    other = post_input("key-reuse")
    other["body"] = {"document_type": "bank_statement"}
    reused = request(**other)
    assert reused["status_code"] == 422
    assert reused["attempts"] == 1
    assert downstream.mutations() == 1


def test_a_refused_connection_and_an_egress_block_keep_their_own_cause(
    downstream: Downstream,
) -> None:
    """The cause tells "never left" from "may have committed"; the exception class does not."""
    request, config = build_tool(downstream, POST_KEY)
    dead = {**config, "server_url": "http://127.0.0.1:9"}  # nothing listens on the discard port
    with pytest.raises(OpenApiExecutionError) as refused:
        execute_openapi_http_tool(
            execution_config=dead, input_schema=None, input_data=post_input("key-dead")
        )
    assert isinstance(refused.value.__cause__, httpx.ConnectError)

    bind_egress_policy(EgressPolicy())  # closed: blocks loopback
    with pytest.raises(OpenApiExecutionError) as blocked:
        request(**post_input("key-blocked"))
    assert not isinstance(blocked.value.__cause__, httpx.TransportError)
    assert downstream.mutations() == 0


def test_header_names_are_exact_in_the_derived_schema(downstream: Downstream) -> None:
    """Edge cases for the change that decides whether "a key was sent": the derived schema is case-sensitive and
    typed, and the standard ETag form of ``If-Match`` does not fit an integer-typed spec."""
    request, _ = build_tool(downstream, POST_KEY)
    lowercase = post_input("key-case")
    lowercase["header_params"] = {"idempotency-key": "key-case"}
    with pytest.raises(OpenApiExecutionError, match="Idempotency-Key|idempotency-key"):
        request(**lowercase)
    etag = post_input("key-etag")
    etag["header_params"]["If-Match"] = '"1"'
    with pytest.raises(OpenApiExecutionError, match="integer"):
        request(**etag)
    keyless = {"path_params": {"case_id": CASE}, "body": BODY}
    assert request(**keyless)["status_code"] == 400, (
        "header_params is optional to the schema, required by the system"
    )
    assert downstream.mutations() == 0


def test_an_imported_tool_has_neither_a_bound_key_header_nor_retries(
    downstream: Downstream,
) -> None:
    """Finding: nothing in the import path sets ``idempotency_key_header`` or ``max_attempts``; the tests above
    add them by hand."""
    _, config = build_tool(downstream, POST_KEY)
    assert "idempotency_key_header" not in config
    assert "max_attempts" not in config
