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
    with pytest.raises(OpenApiExecutionError, match="request failed after 1 attempt"):
        request(**post_input("key-lost"))
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
    request, _ = build_tool(
        downstream, POST_KEY, max_attempts=3, idempotency_key_header="Idempotency-Key"
    )
    downstream.set_fault("drop_response_after_commit")
    result = request(**post_input("key-retry"))
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
    request, _ = build_tool(downstream, POST_KEY, idempotency_key_header="Idempotency-Key")
    with pytest.raises(OpenApiExecutionError, match="idempotency_key"):
        request(path_params={"case_id": CASE}, body=BODY, idempotency_key="key-x")
    assert downstream.mutations() == 0


def test_the_configured_idempotency_key_header_works_without_a_derived_schema(
    downstream: Downstream,
) -> None:
    """The documented path does work when no input schema is applied (a hand-written tool config)."""
    _, config = build_tool(downstream, POST_KEY, idempotency_key_header="Idempotency-Key")
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


def test_retries_are_allowed_when_a_header_is_bound_even_if_no_key_is_sent() -> None:
    """Finding: ``_max_attempts`` decides on the *binding* of ``idempotency_key_header``, not on a key actually
    being sent. With the header bound and no key supplied, a POST that got a 503 is retried keyless: against a
    downstream that does not deduplicate, that is a duplicate create."""
    _FlakyEndpoint.posts = []
    server = HTTPServer(("127.0.0.1", 0), _FlakyEndpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = execute_openapi_http_tool(
            execution_config={
                "kind": "openapi_http",
                "method": "POST",
                "path": "/things",
                "server_url": f"http://127.0.0.1:{server.server_address[1]}",
                "auth_binding": None,
                "request_content_types": ["application/json"],
                "idempotency_key_header": "Idempotency-Key",
                "max_attempts": 3,
            },
            input_schema=None,
            input_data={"body": {"x": 1}},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result["attempts"] == 2
    assert [post["idempotency_key"] for post in _FlakyEndpoint.posts] == [None, None], (
        "two POSTs reached the downstream and neither carried a key"
    )
