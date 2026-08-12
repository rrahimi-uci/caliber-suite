"""Egress tests for the declarative OpenAPI executor.

These tests deliberately do **not** monkeypatch ``build_client``. That is the whole
point: the route tests replace it with a plain ``httpx.Client`` over a
``MockTransport``, which swallows the real client's signature and let a call that
raised ``TypeError: build_client() missing 1 required keyword-only argument:
'policy'`` on every un-mocked invocation pass a full green suite. Anything here
that opens a socket goes through ``caliber.egress`` for real.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from caliber.egress import EgressPolicy
from caliber.integrations.openapi.executor import (
    OpenApiExecutionError,
    active_egress_policy,
    bind_egress_policy,
    execute_openapi_http_tool,
)


def _config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "kind": "openapi_http",
        "method": "GET",
        "path": "/tickets/{ticket_id}",
        "server_url": "https://tickets.example.com",
        "auth_binding": None,
        "request_content_types": [],
    }
    config.update(overrides)
    return config


class _Handler(BaseHTTPRequestHandler):
    """Records requests and replays a scripted sequence of responses."""

    statuses: list[int] = []
    received: list[dict[str, Any]] = []
    responder: Any = None

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self._respond(body=self.rfile.read(length).decode("utf-8") if length else "")

    def _respond(self, body: str = "") -> None:
        type(self).received.append({"path": self.path, "headers": dict(self.headers), "body": body})
        responder = type(self).responder
        if callable(responder):
            status, payload, headers = responder(self.path, body, dict(self.headers))
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        statuses = type(self).statuses
        index = len(type(self).received) - 1
        status = statuses[index] if index < len(statuses) else 200
        payload = json.dumps({"ticket_id": "T-1", "status": "open"}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        # Must never reach the tool result — see ``_REDACTED_RESPONSE_HEADERS``.
        self.send_header("Set-Cookie", "upstream_session=super-secret")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        """Keep the test output clean."""


@pytest.fixture
def local_server():
    """A real HTTP server on loopback, so the egress transport does real work."""

    _Handler.statuses = []
    _Handler.received = []
    _Handler.responder = None
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, _Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _reset_bound_policy():
    """Force the unbound (safe-default) policy for every test in this module.

    ``_ACTIVE_EGRESS_POLICY`` is process-global — the same design as
    ``bind_sandbox_config``/``bind_module_allowlist`` — so whichever app fixture
    last called ``create_app()`` in this test session may have left a permissive
    policy bound (e.g. a route test module that allowlists its own fake upstream).
    Resetting to ``None`` *before* each test, not only after, is what makes this
    module's "default policy" assertions hold regardless of what ran earlier in
    the same pytest session.
    """

    bind_egress_policy(None)
    yield
    bind_egress_policy(None)


def test_default_policy_blocks_loopback_instead_of_raising_typeerror() -> None:
    """The regression test for the missing ``policy=`` argument.

    Before the fix this raised ``TypeError`` from ``build_client`` — the request was
    never attempted and no policy was ever consulted. It must now be a *policy*
    refusal, which proves both that the call signature is right and that the
    default is closed rather than open.
    """

    with pytest.raises(OpenApiExecutionError) as excinfo:
        execute_openapi_http_tool(
            execution_config=_config(server_url="http://127.0.0.1:9"),
            input_schema=None,
            input_data={"path_params": {"ticket_id": "T-1"}},
        )
    message = str(excinfo.value)
    assert "TypeError" not in message
    assert "loopback" in message.lower() or "blocked" in message.lower()


def test_unbound_policy_defaults_to_closed() -> None:
    policy = active_egress_policy()
    assert policy.enabled is True
    assert policy.block_loopback is True
    assert policy.block_private is True
    assert policy.block_link_local is True


def test_bound_policy_is_used_when_no_override_is_passed(local_server) -> None:
    """A policy bound at startup reaches the executor with no threading at all."""

    server, handler = local_server
    host, port = server.server_address[0], server.server_address[1]
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))

    result = execute_openapi_http_tool(
        execution_config=_config(server_url=f"http://{host}:{port}"),
        input_schema=None,
        input_data={"path_params": {"ticket_id": "T-1"}},
    )

    assert result["status_code"] == 200
    assert result["json"] == {"ticket_id": "T-1", "status": "open"}
    assert handler.received[0]["path"] == "/tickets/T-1"


def test_explicit_policy_overrides_a_closed_bound_policy(local_server) -> None:
    server, _handler = local_server
    host, port = server.server_address[0], server.server_address[1]
    bind_egress_policy(EgressPolicy())  # closed: would block loopback

    result = execute_openapi_http_tool(
        execution_config=_config(server_url=f"http://{host}:{port}"),
        input_schema=None,
        input_data={"path_params": {"ticket_id": "T-1"}},
        egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})),
    )
    assert result["status_code"] == 200


def test_response_credential_headers_are_not_returned(local_server) -> None:
    server, _handler = local_server
    host, port = server.server_address[0], server.server_address[1]

    result = execute_openapi_http_tool(
        execution_config=_config(server_url=f"http://{host}:{port}"),
        input_schema=None,
        input_data={"path_params": {"ticket_id": "T-1"}},
        egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})),
    )

    lowered = {key.lower() for key in result["headers"]}
    assert "set-cookie" not in lowered
    assert "content-type" in lowered
    assert "super-secret" not in json.dumps(result["headers"])


def test_retry_recovers_a_retryable_status(local_server) -> None:
    server, handler = local_server
    host, port = server.server_address[0], server.server_address[1]
    handler.statuses = [503, 200]

    result = execute_openapi_http_tool(
        execution_config=_config(server_url=f"http://{host}:{port}", max_attempts=3),
        input_schema=None,
        input_data={"path_params": {"ticket_id": "T-1"}},
        egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})),
    )

    assert result["status_code"] == 200
    assert result["attempts"] == 2
    assert len(handler.received) == 2


def test_client_error_is_not_retried(local_server) -> None:
    server, handler = local_server
    host, port = server.server_address[0], server.server_address[1]
    handler.statuses = [404, 200]

    result = execute_openapi_http_tool(
        execution_config=_config(server_url=f"http://{host}:{port}", max_attempts=3),
        input_schema=None,
        input_data={"path_params": {"ticket_id": "T-1"}},
        egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})),
    )

    assert result["status_code"] == 404
    assert result["attempts"] == 1
    assert len(handler.received) == 1


def test_non_idempotent_method_is_not_retried_without_an_idempotency_key(local_server) -> None:
    """A retried POST is a duplicate create. Silence is not an acceptable default."""

    server, handler = local_server
    host, port = server.server_address[0], server.server_address[1]
    handler.statuses = [503, 200]

    result = execute_openapi_http_tool(
        execution_config=_config(
            method="POST",
            path="/tickets",
            server_url=f"http://{host}:{port}",
            max_attempts=3,
            request_content_types=["application/json"],
        ),
        input_schema=None,
        input_data={"body": {"title": "hello"}},
        egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})),
    )

    assert result["status_code"] == 503
    assert result["attempts"] == 1
    assert len(handler.received) == 1


def test_non_idempotent_method_retries_once_an_idempotency_key_is_bound(local_server) -> None:
    server, handler = local_server
    host, port = server.server_address[0], server.server_address[1]
    handler.statuses = [503, 200]

    result = execute_openapi_http_tool(
        execution_config=_config(
            method="POST",
            path="/tickets",
            server_url=f"http://{host}:{port}",
            max_attempts=3,
            idempotency_key_header="Idempotency-Key",
            request_content_types=["application/json"],
        ),
        input_schema=None,
        input_data={"body": {"title": "hello"}, "idempotency_key": "abc-123"},
        egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})),
    )

    assert result["status_code"] == 200
    assert result["attempts"] == 2
    assert handler.received[0]["headers"]["Idempotency-Key"] == "abc-123"
    assert handler.received[1]["headers"]["Idempotency-Key"] == "abc-123"


def test_private_range_is_blocked_by_default() -> None:
    with pytest.raises(OpenApiExecutionError):
        execute_openapi_http_tool(
            execution_config=_config(server_url="http://10.0.0.1"),
            input_schema=None,
            input_data={"path_params": {"ticket_id": "T-1"}},
        )


def test_link_local_metadata_endpoint_is_blocked_by_default() -> None:
    """The SSRF case the proposal's risk table names first."""

    with pytest.raises(OpenApiExecutionError):
        execute_openapi_http_tool(
            execution_config=_config(server_url="http://169.254.169.254"),
            input_schema=None,
            input_data={"path_params": {"ticket_id": "T-1"}},
        )


def test_oauth_client_credentials_fetches_a_token_and_caches_it(local_server) -> None:
    server, handler = local_server
    host, port = server.server_address[0], server.server_address[1]

    def responder(
        path: str, body: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if path == "/oauth/token":
            assert headers["Authorization"].startswith("Basic ")
            assert "grant_type=client_credentials" in body
            return (
                200,
                {"access_token": "oauth-token", "token_type": "Bearer", "expires_in": 3600},
                {"Content-Type": "application/json"},
            )
        return 200, {"ticket_id": "T-1", "status": "open"}, {"Content-Type": "application/json"}

    handler.responder = responder
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))

    config = _config(
        server_url=f"http://{host}:{port}",
        auth_binding={
            "kind": "oauth_client_credentials",
            "token_url": f"http://{host}:{port}/oauth/token",
            "client_id": "client-id",
            "client_secret_ref": "env://OPENAPI_OAUTH_SECRET",
            "scopes": ["tickets.read"],
        },
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("OPENAPI_OAUTH_SECRET", "super-secret")
        first = execute_openapi_http_tool(
            execution_config=config,
            input_schema=None,
            input_data={"path_params": {"ticket_id": "T-1"}},
        )
        second = execute_openapi_http_tool(
            execution_config=config,
            input_schema=None,
            input_data={"path_params": {"ticket_id": "T-1"}},
        )

    assert first["status_code"] == 200
    assert second["status_code"] == 200
    auth_headers = [item["headers"].get("Authorization", "") for item in handler.received]
    assert auth_headers.count("Bearer oauth-token") == 2
    assert sum(1 for item in handler.received if item["path"] == "/oauth/token") == 1


def test_oauth_refresh_token_uses_refresh_grant(local_server) -> None:
    server, handler = local_server
    host, port = server.server_address[0], server.server_address[1]

    def responder(
        path: str, body: str, _headers: dict[str, str]
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if path == "/oauth/token":
            assert "grant_type=refresh_token" in body
            assert "refresh_token=refresh-me" in body
            assert "client_id=refresh-client" in body
            return (
                200,
                {"access_token": "refreshed-token", "token_type": "Bearer"},
                {"Content-Type": "application/json"},
            )
        return 200, {"ticket_id": "T-1", "status": "resolved"}, {"Content-Type": "application/json"}

    handler.responder = responder
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))

    config = _config(
        server_url=f"http://{host}:{port}",
        auth_binding={
            "kind": "oauth_refresh_token",
            "token_url": f"http://{host}:{port}/oauth/token",
            "client_id": "refresh-client",
            "client_auth_method": "body",
            "refresh_token_secret_ref": "env://OPENAPI_REFRESH_TOKEN",
        },
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("OPENAPI_REFRESH_TOKEN", "refresh-me")
        result = execute_openapi_http_tool(
            execution_config=config,
            input_schema=None,
            input_data={"path_params": {"ticket_id": "T-1"}},
        )

    assert result["json"]["status"] == "resolved"
    assert handler.received[-1]["headers"]["Authorization"] == "Bearer refreshed-token"
